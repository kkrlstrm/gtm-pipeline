# Run integrity: the ledger, the gates, and what they catch

A list-building run can look finished and be wrong: a sub-agent died and its company was
counted as "no one there"; a scored batch never came back; a phone number is stored in a form
no dialer accepts; a customer's CFO is on the list because the do-not-contact file was never
read. Each of those exits 0. This page covers the parts of the pipeline that make those cases
visible and stop them before the send. [`REVIEW.md`](../REVIEW.md) states the rules; this page
covers the mechanics.

Everything here is deterministic code in [`storage/cli.py`](../storage/cli.py), identical on
the local and Postgres backends, and tested by [`scripts/test_gates.py`](../scripts/test_gates.py)
on both.

```
people_search ─► suppress (research) ─► qualify ─► qa ─► email/phone enrich ─► qa ─► suppress (send) ─► preflight_activate ─► activate
                 fail open, flagged                 resolve before paying      resolve         fail closed         fail closed
```

## 1. The run ledger

Every stage appends one event:

```bash
python3 storage/cli.py log_event --input '{"list_id":7,"stage":"email_enrich","status":"ok",
  "provider":"apollo+fullenrich","counts":{"found":212,"accepted":198},
  "cost":{"estimate":240,"actual":231,"unit":"credits"},"warnings":[]}'
```

`status` is `ok`, `warn`, `error` or `skipped`. The gates (`suppress`, `qa`, `qa_resolve`,
`preflight_activate`) log themselves. `run_report` assembles the story from the ledger rather
than from the model's memory of the run:

```bash
python3 storage/cli.py run_report --input '{"list_id":7}'    # or: /gtm status 7
```

It returns the frozen plan, per-stage counts, every event, cost totals (estimate vs actual per
unit), the gate verdicts, and `open_warnings`. "Open" means the latest event of each stage, so
re-running a stage or resolving QA clears the warnings it superseded. A missing do-not-contact
pass and unresolved QA errors are always listed.

**Read-back check.** After each stage, the orchestrator compares `list_summary` with the count
the stage reported. If a stage says it advanced 40 contacts and the summary shows 38, that is
an error. It is not rounded away.

**Resume.** The approved plan and expansion are frozen onto the list (`update_list` →
`search_criteria.plan` / `.expansion`). `/gtm resume <list_id>` reads them back and continues
from the first stage whose latest event is not `ok`, `warn` or `skipped`, without
re-interpreting the brief.

**Storage parity.** Both backends store exactly the schema's contact columns, coerced by one
function: lists and objects in text columns are JSON-encoded, numbers must be finite,
`qualification_score` is an integer, unknown keys are dropped and listed in `ignored_fields`.
Ids follow input order on both. Local-backend calls take a file lock, so stage agents running
in parallel cannot overwrite each other's writes.

## 2. Do-not-contact (suppression)

`context/do-not-contact.csv` (path set by `suppression.file`, relative to the config file)
lists anyone who must never be contacted. Use any of the columns `email`, `domain`, `phone`,
`linkedin_url`, plus an optional `reason`. Common export headers work too (`Email Address`,
`Company Domain`, `Website`, `Phone Number`, `LinkedIn`):

```csv
email,domain,phone,linkedin_url,reason
jane.doe@example.com,,,,asked to be removed 2026-01-12
,customer-one.example,,,current customer
,campus-a.shared-system.example,,,customer (one campus of a shared system)
```

- A **domain** suppresses itself and its subdomains, **never its parent**. List a customer
  campus as `campus-a.system.edu` and its sibling campuses stay contactable. Listing
  `system.edu` would suppress the whole system.
- **Phones** are compared in E.164 (a `(0)` trunk prefix after the country code is dropped),
  and **LinkedIn URLs** by their `/in/<slug>` path, so scheme, `www.`, a country subdomain or a
  query string do not hide a match.
- **Nothing is dropped quietly.** A file with rows but no recognised column, or with an entry
  that cannot be read (an email without `@`, a phone with no country code outside US/CA), is
  reported. Such a file is not `applied`, and the send posture refuses it until it is fixed:
  an entry the loader drops is a person the list stops protecting.
- The file is gitignored at the repo root because it names real people.

The same check runs in two postures, because the cost of a wrong answer differs:

| | `posture: research` (after sourcing) | `posture: send` (before activation) |
|---|---|---|
| File missing | keeps every row, prints `DO-NOT-CONTACT NOT APPLIED`, exits **0** with `applied: false` | exits **6**, `applied: false` |
| Why | looking people up contacts nobody; blocking research on a missing file costs more than it saves | a wrong send cannot be taken back |

Matched rows move to `skipped` with `skip_reason` naming what matched. The verdict is stored on
the list: `applied`, `posture`, `file`, `at`, counts, and `max_checked_id`. **Read `applied`,
not the exit code**, because a research-posture run that checked nothing still exits 0.

A header-only file is a valid list: it records that you decided your list is empty. That is a
different state from never having checked, and the verdict records the difference.

`check_suppression` is the read-only form, used on company domains **before** paying to enrich
accounts: `{"domains":[...]}` returns the matches and writes nothing.

`export` writes `<csv>.verdict.json` next to every CSV, recording whether the list was
scrubbed, against which file, when, and the QA state. When the list has no applied pass,
export prints `UNSCRUBBED` to stderr and returns `suppression_applied: false`.

## 3. List QA

`qa` checks every live row and returns findings with a `key`, a `severity`, the
`contact_ids` involved, and a `suggestion` when the fix is mechanical. The key is
`<code>:<ids>:<fingerprint>`, where the fingerprint covers the values the check judged, so a
`keep` stops applying as soon as those values change. Each check below exists
because the defect got past a name+company dedupe **and** a human approval on a production
list.

| Code | Severity | What it catches |
|---|---|---|
| `duplicate_person` | error | One person listed twice under two spellings (`Tom Walsh` / `Tom R. Walsh`, same company). A name+company dedupe misses it; in a CRM the second row overwrites the first, so which title survives depends on send order. Only initials and honorifics are ignored: `Ann Lee-Smith` / `Ann Smith` are not matched. Names the richer row to keep. |
| `duplicate_email` | error | The same email on two rows |
| `email_domain_typo` | error | One domain label (never the TLD, at least 5 characters) one character off the company's (`cityoriverton.org` vs `cityofriverton.org`; not `bank.be` vs `bank.de`). Suggests the corrected address. |
| `email_other_org` | error | Email at a different kind of organisation, by `qa.other_org_patterns` (e.g. a city employee's address at the school district that shares the city's name) |
| `email_domain_mismatch` | warn | Email not on the company domain. Organisations run several domains, so this is a question to answer, not a fault. |
| `email_invalid` | error | Not an email |
| `role_mailbox` | warn | `info@`, `finance@`, `records@`: a shared inbox, not a person |
| `freemail` | warn | A personal address |
| `phone_not_e164` | error | Dots, spaces or brackets (`+1415.555.0142`), or an extension glued on (`+16175550188ext.204`). Suggests E.164 plus a separate `phone_ext`. |
| `phone_invalid` | error | Cannot be placed in a country. Without a `+`, only `qa.default_country: US/CA` is assumed; guessing any other country code would be inventing digits. |
| `shared_phone_labelled_direct` | error | One number on several people, labelled as each one's `mobile`/`direct_dial`. A shared number is a switchboard. |
| `missing_domain` | warn | No company domain; enrichers will skip the row |
| `no_reachable_identity` | warn | Enriched but no email and no LinkedIn URL; a sequencer cannot reach it |

Resolve with `qa_resolve`:

```bash
# drop — for a duplicate, drops ONLY the redundant rows (drop_ids), after copying any field
# the kept row lacks (a phone, an email) from them; never drops the person
python3 storage/cli.py qa_resolve --input '{"list_id":7,"keys":["duplicate_person:12,15:3f9a1c2e"],"action":"drop"}'
# fix — applies each finding's suggestion (or pass "fields", with a single key)
python3 storage/cli.py qa_resolve --input '{"list_id":7,"keys":["phone_not_e164:3:8b2d0e41"],"action":"fix"}'
# keep — records a human decision to keep the row as is; the finding stops blocking
python3 storage/cli.py qa_resolve --input '{"list_id":7,"keys":["email_domain_typo:9:c01d77aa"],"action":"keep","note":"confirmed alias domain"}'
```

All keys are validated before anything is written. Use the keys from the latest `qa` output;
a key for data that has since changed returns exit 4.

The orchestrator runs `qa` after qualify, so duplicates are dropped before you pay to enrich
them twice, and again after enrichment, which is when typos and phone formats appear.

## 4. Activation preflight

`preflight_activate` is the send boundary. It does not trust the stored verdict alone: it
re-reads the **configured** do-not-contact file and re-matches every row about to be sent. It
exits **6** with `blockers[]` when:

- any row about to be sent matches the do-not-contact list now (always a blocker, whatever
  the config says), which catches a row moved back from `skipped`, an email edited onto a
  blocked domain, or a QA fix that produced a blocked address;
- no `send`-posture pass has been applied, the last pass used a different file than
  `suppression.file`, the file has changed since that pass (SHA-256), rows were added after
  it, or the file has unreadable entries (these become warnings only with
  `suppression.required_for_activation: false`; there is no per-call override);
- any QA error on a row about to be sent is unresolved;
- there are no rows at the requested stage.

After any change to the list or the file, run `suppress posture=send` again, then preflight.
The `activate` stage refuses to build a payload until preflight exits 0.
`activate_gate: auto` skips only the human confirmation. It never skips preflight.

## 5. Workflows report what failed

Each fan-out workflow returns its failures next to its results: `failed[]` from
`discover-companies`, `enrich-companies`, `source-people` and `preview-titles`,
`unscored_ids` from `score-leads`, and `partial[]` from `enrich-companies` for companies with
dimensions no researcher covered (those records are marked unverified). Before this, a sub-agent that returned nothing was filtered
out, so its company looked the same as a company with nobody to find. The stage agents report
the failures, log them as warnings, and offer a re-run of just those items. The qualifier
leaves unscored contacts at `sourced` instead of advancing them unjudged.

Each workflow also passes a narrow agent type (`.claude/agents/*.md`: web tools only, no skill
listing). In the production system this pipeline was extracted from, a generic sub-agent
started at about 60k tokens of context before doing any work, and narrow agent types on the
same job measured roughly 19k–32k. The saving grows with list length, because every item
starts its own sub-agent. Pass `agentType: ''` in a workflow's args to use the generic
sub-agent instead.

## 6. Title preview

An inferred title set is a hypothesis about how real companies name a job. Before sourcing,
`preview-titles` samples a few companies (`defaults.autonomy.title_preview`, default 3), lists
the titles actually in use in that function, and marks which ones the set would match
literally. The reviewer sees titles to add (seen, not matched) and titles to exclude (seen, and
the wrong sense) with the page each was seen on, and corrects the set at Gate #1, before any
sourcing is paid for.

## 7. Exit codes

| Code | Meaning |
|---|---|
| 0 | ok (for `suppress posture=research`, also read `applied`) |
| 2 | bad input (JSON, a missing field, an invalid value) |
| 3 | not configured (e.g. the database env var is unset) |
| 4 | not found (list id, QA key) |
| 5 | backend failure (psql error) |
| 6 | gate blocked (`suppress posture=send` could not apply the list; `preflight_activate` found a blocker) |

## 8. How these stay true: sweep checks

[`scripts/sweep_checks.py`](../scripts/sweep_checks.py) runs in `selftest.sh`. Each check
covers a whole class of drift rather than one case: export columns vs the SQL function,
every CLI-writable field vs the schema (with an upgrade `ALTER` for existing databases), both
backends implementing every op, every workflow using a narrow agent type and reporting
failures, every provider in a waterfall having a manifest, every op named in a doc existing in
the CLI, and the stdlib config reader agreeing with PyYAML on every key it reads.
