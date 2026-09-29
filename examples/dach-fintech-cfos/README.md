# Example run — DACH fintech CFOs

A complete, end-to-end campaign run, start to finish. **All data here is synthetic** —
fictional companies on `.example` domains, fake names/emails/phones. It shows what each
stage produces, not real prospects.

## The brief

```
/gtm target mid-market fintech CFOs in DACH for our compliance product
```

That one line, interpreted against the [context files](context/) and
[gtm.config.yaml](gtm.config.yaml), produced everything below.

## What's in this folder

| File | What it is |
|---|---|
| [context/icp.md](context/icp.md) · [personas.md](context/personas.md) · [segments.md](context/segments.md) · [exclusions.md](context/exclusions.md) | the ICP — what we sell, who we target, how we score, who to skip |
| [gtm.config.yaml](gtm.config.yaml) | the wiring: providers per stage, gates, storage |
| [provider-plan.txt](provider-plan.txt) | real `scripts/show-plan.py` output for this config + keys |
| [context/do-not-contact.csv](context/do-not-contact.csv) | who must never be contacted (here: the current customer's domain) |
| [output/companies.jsonl](output/companies.jsonl) | account intel from `company_enrich` (cited) |
| [output/qa-findings.json](output/qa-findings.json) | what list QA found before the send (three phones not dialable as stored) |
| [output/export.csv](output/export.csv) | the campaign-ready contact list |
| [output/export.csv.verdict.json](output/export.csv.verdict.json) | the verdict shipped with the CSV: scrubbed against which file, when, QA state |
| [output/run-report.json](output/run-report.json) | the run ledger: every stage, counts, credits estimated vs spent, open warnings |
| [seed/run.json](seed/run.json) · [replay.py](replay.py) | what the providers returned, and the script that drives it through the real CLI |
| [activation-log.md](activation-log.md) | the push to the sequencer (Stage 5) |

## How the run went, stage by stage

**Keys for this run:** `APOLLO_API_KEY`, `FULLENRICH_API_KEY`, `LEMLIST_API_KEY`,
`HUBSPOT_TOKEN`. The [provider plan](provider-plan.txt) resolves each stage accordingly
(and honestly flags `phone_validate` as having no provider — no ClearoutPhone key).

1. **company_search** → found the DACH fintechs (web_research fan-out + Apollo), created
   the list. *Gate #1: plan approved.*
2. **crm_dedupe (0 → 0.5)** → checked domains against HubSpot; the current customer
   (`rheinmetrik.example`) was flagged (and also excluded in `exclusions.md`).
3. **company_enrich** → built cited account intel per company —
   [output/companies.jsonl](output/companies.jsonl): funding stage, employees, tech stack,
   and "why now" signals (Nimbus Pay raised Series C and is hiring finance roles; Tirol
   Treasury posted a Head of Compliance role).
4. **people_search** → sourced 6 contacts (Apollo + web_research).
5. **do-not-contact (research posture)** → the current customer's CFO matched
   `rheinmetrik.example` in [do-not-contact.csv](context/do-not-contact.csv) and left the list
   with `skip_reason` recorded, before anyone scored or paid for her.
6. **qualify** → scored against the rubric. *Gate #2:* 3 QUALIFY (the CFOs, scores 8–9),
   1 MAYBE (a Compliance champion, kept to multithread Tirol), 1 SKIP — a *Werkstudent*
   (title exclusion). Neither removed row reaches paid enrichment.
7. **email_enrich** → *Gate #3:* found verified emails (Apollo verified, FullEnrich for the
   rest). Ledger: 6 credits estimated, 5 spent.
8. **phone_enrich** → mobiles/direct dials where available; Jonas came back phone-not-found
   (still advanced). Logged as `warn`: no `phone_validate` provider was keyed, so the numbers
   are unvalidated, and the run report keeps saying so.
9. **QA before the send** → [three findings](output/qa-findings.json): the providers returned
   `+49 30 5550 1234`-style numbers with spaces, which some dialers reject. `qa_resolve fix`
   applied the E.164 form each finding suggested.
10. **do-not-contact (send posture) + preflight** → scrubbed again against the same file (no
    rows had been added since, but the send gate always re-checks), then
    `preflight_activate` returned `ok`.
11. **activate** → *Gate #4:* exported the 4 enriched contacts and pushed them to lemlist —
    see [activation-log.md](activation-log.md).

## The payoff

[output/export.csv](output/export.csv) — 4 campaign-ready rows (the 2 SKIPs are correctly
absent), sorted by qualification score:

| name | title | company | email | phone | persona | score |
|---|---|---|---|---|---|---|
| Lena Hoffmann | CFO | Nimbus Pay | lena.hoffmann@nimbuspay.example | +493055501234 (mobile) | Economic Buyer | 9 |
| Marco Brunner | CFO | Helvetia Ledger | marco.brunner@helvetialedger.example | +41445550199 (direct) | Economic Buyer | 8 |
| Sophie Maier | CFO | Tirol Treasury | sophie.maier@tiroltreasury.example | +43155500177 (mobile) | Economic Buyer | 8 |
| Jonas Gruber | Head of Compliance | Tirol Treasury | jonas.gruber@tiroltreasury.example | — | Compliance Champion | 6 |

## Reproduce it

```bash
python3 examples/dach-fintech-cfos/replay.py
```

This drives [seed/run.json](seed/run.json) (what each provider returned) through the real
`storage/cli.py` in pipeline order and rewrites `output/`. No provider is called. The plan in
[provider-plan.txt](provider-plan.txt) is real `scripts/show-plan.py` output. To try your own
run, copy this folder's `context/` + `gtm.config.yaml` into a working dir, set your keys, and
`/gtm <your brief>`.
