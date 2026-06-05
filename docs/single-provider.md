# Running on a single provider

You don't need ten keys. The framework is provider-agnostic, so the pipeline runs on
whatever you have — including **one** vendor for everything. Two vendors can drive the
whole pipeline solo:

| One key | Covers | Stages |
|---|---|---|
| **Apollo** | people_search, company_search, email_enrich, phone_enrich | 0, 1, 3, 4 |
| **Prospeo** | company_search, people_search, email_enrich, phone_enrich, company_enrich | 0, 0.5, 1, 3, 4 |

`web_research` (builtin, no key) always covers stage 0 discovery and 0.5 account intel, so
even an enrichment-only key gives you discovery + intel + that vendor's stages for free.

## Prospeo only

```yaml
# gtm.config.yaml
waterfalls:
  company_search: [web_research, prospeo]
  company_enrich: [web_research, prospeo]
  people_search:  [prospeo]
  email_enrich:   [prospeo]
  email_validate: []            # Prospeo emails come back VERIFIED — no separate validator
  phone_enrich:   [prospeo]
  phone_validate: []
```
Set `PROSPEO_API_KEY`, run `python3 scripts/show-plan.py`, done. One `/enrich-person` call
even returns email **and** mobile together (set `enrich_mobile:true`), so stages 3+4 can
share a call.

## Apollo only

```yaml
waterfalls:
  company_search: [web_research, apollo]
  people_search:  [apollo]
  email_enrich:   [apollo]
  phone_enrich:   [apollo]
  phone_validate: []            # or add clearoutphone if you have it
```

## Why the "free search" providers were chosen

- **Apollo** — the People/Company **Search** API spends **no credits**; only enrichment
  does. But API access needs a **paid plan** (Basic has no API; Professional+ unlocks it)
  plus a **master API key**. So the *lowest paid plan* gets you free partial search.
- **Dropleads** — its people-search returns full names + LinkedIn URLs and likewise spends
  no enrichment credits; API needs a paid account (entry ~$29). We use only its free
  search (masked email/phone are ignored).
- **Apify** — **LinkedIn-only.** It scrapes a company's LinkedIn employees; it does not do
  email/phone or non-LinkedIn sourcing. Use it when you want LinkedIn-sourced contacts.

## How it degrades gracefully

`show-plan.py` resolves each stage to the providers whose keys are set; missing ones are
skipped, builtins stay on, and a stage with nothing available is flagged. Add a key later
and that stage lights up — no agent or prompt edits, ever.
