# REVIEW.md — the quality bar

The standard every run of this pipeline clears, whichever agent, stage or provider produced
the work. [`AGENTS.md`](AGENTS.md) says how the system works; this file says what a good run
looks like and what must never happen.

**Who this binds.** The orchestrator and every sub-agent it starts. A fan-out worker boots
with nothing but its task, so the rules that matter are repeated in each
`.claude/agents/*.md` and in each workflow prompt. If you are a sub-agent reading this, it
applies to you.

**Where it is enforced.** The parts a program can check are enforced by
[`storage/cli.py`](storage/cli.py): `suppress`, `qa` and `preflight_activate` refuse to let a
list reach a sequencer while a rule below is broken. The rest is judgment, and lives here.

---

## 1. Silent success is the main failure

A run that exits 0 having done less than it says is worse than a crash, because no one looks.
In list building it takes these forms: a sub-agent that died is counted as "0 people found";
a batch the scorer never returned is advanced as if it had been judged; a stage that wrote to
local files while you believed it was on Postgres; a CSV that reads as scrubbed and wasn't.

1. **Judge a stage by what it wrote, not by what it said.** After every stage, read
   `list_summary` back and compare it with the count the stage reported. A mismatch is an
   error to report, not a rounding difference.
2. **A failed item is a failure, not an empty result.** Every workflow returns `failed[]` (or
   `unscored_ids`). Report them, and record them with `log_event status=warn`. Never advance
   an unscored contact.
3. **Record every stage in the run ledger** (`log_event`): provider, counts, cost estimate and
   actual, warnings. The final report is built from the ledger (`run_report`), not from
   memory, so a skipped stage cannot disappear from the story.
4. **A fail-open check that returns 0 may not have run.** `suppress` with `posture=research`
   keeps every row and exits 0 when the do-not-contact file is missing, so a research run is
   never blocked. Read its `applied` field, never the exit code.

## 2. Never invent a fact, and never un-invent one

- **Found on a real source, or blank.** No guessed emails (`first.last@` is a pattern, not a
  finding), no pattern-assembled phone numbers, no inferred LinkedIn URLs, no "probably" funding
  figures. A blank field with a note saying what was searched is a valid, expected result.
- **A direct dial or mobile is the named person's own line.** A switchboard, department line,
  or any number shared by several people is `switchboard`, never `direct_dial` or `mobile`.
  `qa` flags a shared number labelled as someone's own line.
- **Answer for the entity you were asked about.** Two organisations can share a name (a city
  and its school district; a company and its foundation). Resolve by domain, and say which one
  you resolved. An email domain that differs from the company domain is a question to answer,
  not noise: `qa` flags it, and flags a one-character near-miss as a typo.
- **Scope leaves only by an explicit decision.** A title, segment or region comes out of the
  plan only when the user said so. When you remove something, name it on its own line in the
  plan ("Removing: Head of Finance") so the approver can see it went.

## 3. Spend and contact are gated

- **Suppress and dedupe before you enrich.** Never pay to enrich a row you are about to drop.
  Order: source → do-not-contact + CRM + master dedup → qualify → QA → paid enrichment.
- **Research is not contact.** Looking up a published staff page contacts nobody, so the
  research posture fails open and flags. **The send boundary fails closed**:
  `preflight_activate` re-matches every row about to be sent against the configured
  do-not-contact file, and blocks until a `posture=send` pass has run against the current file
  after the last change, and every QA error is resolved.
- **Every gate that spends or sends shows its numbers first**: rows, credits estimated, and
  what will be skipped. Record the estimate and the actual in the ledger; an estimate that
  undercounts is a bug to report, not a surprise to absorb.
- **Human approval is for decisions, not for proofreading.** The plan gate decides who we
  target and which titles count; the qualify gate decides MAYBEs. Do not ask a person to
  re-check what a deterministic check already covers.

## 4. Which way to fail

| Boundary | Direction | Why |
|---|---|---|
| Research reads (web lookups, do-not-contact during sourcing, cache/CRM lookups) | Fail open, flag `unverified` | A false block costs more than a miss here |
| Sending (`preflight_activate`, `suppress posture=send`, `sequencer_push`) | Fail closed | A wrong send cannot be taken back |
| Model output | Synthesize, never relay verbatim | The value is the judgment |
| Tool or provider errors | Surface verbatim, never paraphrase away | The exact error is the fix |

## 5. Deterministic code owns consequences

- **Use the CLI, not inline code.** Storage, suppression, QA and export go through
  `storage/cli.py`. If you find yourself writing a `python3 -c` to reshape rows, the op is
  missing: add it to the CLI, with a test, instead.
- **Providers are configuration.** Changing how a capability is fulfilled is a manifest edit,
  never an edit to an agent prompt.
- **Branch on exit codes.** 0 ok · 2 bad input · 3 not configured · 4 not found ·
  5 backend failure · 6 gate blocked.

## 6. One list is one audience

Split a brief into two lists only when the **audience** differs, meaning you would build a
separate lead list for it. A different angle, channel or geography slice for the same audience
is a field on one list, not a second list.

---

When a rule here goes stale because models or providers improved, remove it. The bar is
expected to get shorter as well as longer.
