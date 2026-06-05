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
| [output/companies.jsonl](output/companies.jsonl) | account intel from `company_enrich` (cited) |
| [output/export.csv](output/export.csv) | the campaign-ready contact list |
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
5. **qualify** → scored against the rubric. *Gate #2:* 3 QUALIFY (the CFOs, scores 8–9),
   1 MAYBE (a Compliance champion, kept to multithread Tirol), **2 SKIP** — a
   *Werkstudent* (title exclusion) and the current-customer CFO (domain exclusion). The two
   SKIPs never reach paid enrichment.
6. **email_enrich** → *Gate #3:* found verified emails (Apollo verified, FullEnrich for the
   rest).
7. **phone_enrich** → mobiles/direct dials where available; Jonas came back phone-not-found
   (still advanced).
8. **activate** → *Gate #4:* exported the 4 enriched contacts and pushed them to lemlist —
   see [activation-log.md](activation-log.md).

## The payoff

[output/export.csv](output/export.csv) — 4 campaign-ready rows (the 2 SKIPs are correctly
absent), sorted by qualification score:

| name | title | company | email | phone | persona | score |
|---|---|---|---|---|---|---|
| Lena Hoffmann | CFO | Nimbus Pay | lena.hoffmann@nimbuspay.example | +49 30 5550 1234 (mobile) | Economic Buyer | 9 |
| Marco Brunner | CFO | Helvetia Ledger | marco.brunner@helvetialedger.example | +41 44 555 0199 (direct) | Economic Buyer | 8 |
| Sophie Maier | CFO | Tirol Treasury | sophie.maier@tiroltreasury.example | +43 1 5550 0177 (mobile) | Economic Buyer | 8 |
| Jonas Gruber | Head of Compliance | Tirol Treasury | jonas.gruber@tiroltreasury.example | — | Compliance Champion | 6 |

## Reproduce the shape

The outputs were generated through the real `storage/cli.py` and `scripts/show-plan.py`
(so the CSV columns and the plan are exactly what a live run yields). To try your own run,
copy this folder's `context/` + `gtm.config.yaml` into a working dir, set your keys, and
`/gtm <your brief>`.
