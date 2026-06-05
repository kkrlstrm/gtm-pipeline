# Swapping providers (config-only)

The whole point of the framework: **you change providers by editing `gtm.config.yaml`,
never the agents.** Agents speak in capabilities and read each provider's
`manifest.yaml`; they contain no provider-specific request logic. So swapping a provider
is: (1) set the new key in `.env`, (2) put the provider in the relevant waterfall (or set
`sequencer:`). That's it.

## See what your config will actually do

```bash
python3 scripts/show-plan.py            # resolves config + manifests + env -> per-stage plan
```

It applies the exact rule every agent uses: a provider is **available** if its manifest is
`builtin` (no key) or its `auth.env` is set; each stage's resolved order is
`waterfall ∩ available`, in order. It flags any stage with no available provider (⚠) and
whether the sequencer is ready — run it before a campaign to catch a missing key.

## Example: swap the enrichment + sequencer stack

Incumbent stack → premium stack, **the only edits**:

```diff
-  email_enrich:   [apollo, fullenrich]
+  email_enrich:   [prospeo]
-  phone_enrich:   [apollo, fullenrich]
-  phone_validate: [clearoutphone]
+  phone_enrich:   [prospeo]
+  phone_validate: [prospeo]
-sequencer: lemlist
+sequencer: smartlead
```

Set `PROSPEO_API_KEY` and `SMARTLEAD_API_KEY` in `.env`, rerun `show-plan.py`, and the
plan now resolves `email_enrich -> prospeo`, `phone_enrich -> prospeo`,
`sequencer: smartlead` — with **zero changes to any `agents/*.md`**.

> Tip: if you swap to a self-validating enricher (Prospeo emails come back `VERIFIED`), you
> may not need an `email_validate` step at all — drop it from the waterfall, or
> `show-plan.py` will flag it as having no available provider.

## Providers that ship today

| Provider | Capabilities | Notes |
|---|---|---|
| `web_research` | company_search, linkedin_url_lookup, company_enrich | builtin (no key) — always-on floor; company_enrich = Claude subagent fan-out |
| `firecrawl` | company_enrich | script; cleaner "eyes" for the fan-out (scrape/search) or turnkey /extract |
| `apify` | people_search | script; member-ID URLs, region→country expansion |
| `apollo` | people_search, company_search, email_enrich, phone_enrich | spec; phone needs the single match endpoint |
| `dropleads` | people_search | spec; nested `data.leads[]` |
| `fullenrich` | email_enrich, phone_enrich | script; fire-and-poll, empty-domain guard |
| `clearoutphone` | phone_validate | spec; `Bearer:<key>` header quirk |
| `prospeo` | company_search, people_search, email_enrich, phone_enrich, company_enrich | spec; **single-provider stack** — `/enrich-person` returns email + mobile in one call |
| `lemlist` | sequencer_push | script; email sender (sequence-on-steps quirk) |
| `smartlead` | sequencer_push | spec; email sender, `api_key` query param, 400 leads/req |
| `heyreach` | sequencer_push | spec; **LinkedIn** sender — leads need a `profileUrl`, DRAFT→Start |
| `hubspot` | crm_dedupe | script, read-only; suppress companies/contacts already in your CRM |

## Adding a brand-new provider

1. Create `providers/<name>/manifest.yaml` (see any existing one; `docs/capabilities.md`
   has the canonical contracts). Declare `auth.env`, `base_url`, and a block per capability
   with `request_template` + `response.field_map` (+ `status_semantics` / `match_key_priority`
   / `gotchas` as needed).
2. If the call is async/polling, fire-and-poll, needs pre-resolution, or has parsing
   quirks, add a thin `adapter.py` (`--capability <cap> --input '<JSON>'`, canonical JSON
   to stdout, `--estimate`, stdlib-only, secret from env). Otherwise spec-only is enough.
3. Add the key to `.env.example`, drop the provider into a waterfall, and run
   `show-plan.py`. No agent edits.
