# Running on a single provider

You don't need ten keys. The framework is provider-agnostic, so the pipeline runs on
whatever you have — including **one** vendor for everything. Four vendors can drive the
whole pipeline solo:

| One key | Covers | Stages |
|---|---|---|
| **Apollo** | people_search, company_search, email_enrich, phone_enrich | 0, 1, 3, 4 |
| **Prospeo** | company_search, people_search, email_enrich, phone_enrich, company_enrich | 0, 0.5, 1, 3, 4 |
| **LeadMagic** | company_search*, company_enrich, people_search, email_enrich, **email_validate**, phone_enrich | 0*, 0.5, 1, 3, 4 |
| **AI Ark** | company_search, company_enrich, people_search, email_enrich, phone_enrich, linkedin_url_lookup | 0, 0.5, 1, 3, 4 |

\* LeadMagic's company_search is **look-alike** (seed-based), not cold keyword org search —
keep `web_research` ahead of it for zero-seed discovery, then let LeadMagic expand from the
first exemplars. It's the only single-provider stack that also brings a dedicated cheap
`email_validate` (0.25 cr / check).

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

## LeadMagic only

```yaml
# gtm.config.yaml
waterfalls:
  company_search: [web_research, leadmagic]   # web_research seeds; leadmagic look-alikes expand
  company_enrich: [leadmagic]                 # company-search (1cr) + company-funding (4cr)
  people_search:  [leadmagic]                 # identity only (1cr/person)
  email_enrich:   [leadmagic]                 # email-finder, self-validating, free-on-miss
  email_validate: [leadmagic]                 # dedicated 0.25cr validator
  phone_enrich:   [leadmagic]                 # mobile-finder — needs a linkedin_url/email key
  phone_validate: []
```
Set `LEADMAGIC_API_KEY`, run `python3 scripts/show-plan.py`. Two sequencing notes: its mobile
finder is keyed on a **LinkedIn URL or email** (not name+domain), so phone enrich runs after
sourcing/email; and emails come back self-validated, so the `email_validate` slot is really
there to re-check emails from *other* enrichers in a mixed stack.

## AI Ark only

```yaml
# gtm.config.yaml
waterfalls:
  company_search: [ai-ark]          # cold filter + look-alike in one call
  company_enrich: [ai-ark]          # intel ships INLINE with the search — no extra call/credit
  people_search:  [ai-ark]          # returns the trackId the email finder needs
  email_enrich:   [ai-ark]          # async; consumes the search trackId (single-use, 6h TTL)
  email_validate: []                # AI Ark self-validates its finds; no standalone validator
  phone_enrich:   [ai-ark]          # synchronous; LinkedIn URL or domain+full_name
  phone_validate: []
```
Set `AI_ARK_API_KEY` (sent as the `X-TOKEN` header), run `python3 scripts/show-plan.py`. AI Ark
is the strongest **discovery** stack here — one `POST /companies` does cold filter + semantic +
look-alike *and* returns funding/tech/firmographics inline, so stages 0 and 0.5 collapse into a
single call. The catch: email enrichment is **async and bound to the search's trackId** (run it
promptly, once), and there's **no standalone validator** — so in a mixed stack, pair it with
LeadMagic for the `email_validate` gate.

## Why the "free search" providers were chosen

- **Apollo** — the People/Company **Search** API spends **no credits**; only enrichment
  does. But API access needs a **paid plan** (Basic has no API; Professional+ unlocks it)
  plus a **master API key**. So the *lowest paid plan* gets you free partial search.
- **Dropleads** — its people-search returns full names + LinkedIn URLs and likewise spends
  no enrichment credits; API needs a paid account (entry ~$29). We use only its free
  search (masked email/phone are ignored).
- **Apify** — **LinkedIn-only.** It scrapes a company's LinkedIn employees; it does not do
  email/phone or non-LinkedIn sourcing. Use it when you want LinkedIn-sourced contacts.
- **LeadMagic** — no plan hack needed: it's **pay-on-success** (a not-found result is free)
  with a **100-credit free tier** to test. That makes it cheap to stack as a *late* waterfall
  tier — you pay nothing for the rows it can't find. Its dedicated `email_validate` (0.25 cr)
  is the best-value validation step in the stack.
- **AI Ark** — also **pay-per-result** with a 100-credit free tier, and the best **discovery**
  engine here: cold filter + semantic + look-alike in one `/companies` call that returns funding
  + technographics + firmographics inline (stages 0 and 0.5 in one). Trade-offs to wire around:
  its email finder is **async + trackId-coupled** (single-use, 6h TTL — so it lives behind an
  `adapter.py`), and it has **no standalone email validator**.

## How it degrades gracefully

`show-plan.py` resolves each stage to the providers whose keys are set; missing ones are
skipped, builtins stay on, and a stage with nothing available is flagged. Add a key later
and that stage lights up — no agent or prompt edits, ever.
