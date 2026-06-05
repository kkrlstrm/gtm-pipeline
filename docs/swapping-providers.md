# Swapping & stacking providers (config-only)

The whole point of the framework: **you change providers by editing `gtm.config.yaml`,
never the agents.** Agents speak in capabilities and read each provider's
`manifest.yaml`; they contain no provider-specific request logic. So swapping a provider
is: (1) set the new key in `.env`, (2) put the provider in the relevant waterfall (or set
`sequencer:`). That's it.

## You can stack as many as you want (additive waterfalls)

Swapping is one move; **stacking is the other — and the default.** You are never limited to
one provider per stage. List as many as you like in a waterfall — 1, 2, 3, 4, or all of them —
and **whichever keys you've set all work together, in the order you wrote them.** Providers
without a key are simply skipped; nothing else changes.

```yaml
# one key — runs the stage solo:
email_enrich: [leadmagic]

# four keys — all run, in order, additively (no agent or prompt changes):
email_enrich: [apollo, fullenrich, leadmagic, ai-ark]
```

There are two additive shapes, by stage:

- **Search stages (`company_search`, `people_search`) — UNION + dedup.** Every keyed provider
  runs and the results are pooled, then deduped on normalized LinkedIn URL / domain. More
  providers = more coverage.
- **Enrich stages (`email_enrich`, `phone_enrich`) — waterfall, stop-at-first.** Providers run
  in order; each contact stops at the first valid result, and only the *misses* carry to the
  next provider. More providers = higher fill rate, while early stops save credits.
  (`email_validate` re-verifies only the statuses a manifest flags, inside this waterfall.)

`show-plan.py` prints the live chain — the `->` **is** the waterfall, not a single pick:

```
people_search : apify -> apollo -> ai-ark -> web_research    # union + dedup
email_enrich  : apollo -> fullenrich -> leadmagic -> ai-ark  # stop at first valid
```

So one key runs the pipeline solo; add a second and it slots into the same waterfall. Bring
the keys you have — they cooperate.

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
| `web_research` | company_search, linkedin_url_lookup, company_enrich, people_search | builtin (no key) — always-on floor; Claude subagent fan-out for discovery/intel/sourcing |
| `firecrawl` | company_enrich | script; cleaner "eyes" for the fan-out (scrape/search) or turnkey /extract |
| `apify` | people_search | script; member-ID URLs, region→country expansion |
| `apollo` | people_search, company_search, email_enrich, phone_enrich | spec; phone needs the single match endpoint |
| `dropleads` | people_search | spec; nested `data.leads[]` |
| `fullenrich` | email_enrich, phone_enrich | script; fire-and-poll, empty-domain guard |
| `clearoutphone` | phone_validate | spec; `Bearer:<key>` header quirk |
| `prospeo` | company_search, people_search, email_enrich, phone_enrich, company_enrich | spec; **single-provider stack** — `/enrich-person` returns email + mobile in one call |
| `leadmagic` | company_search, company_enrich, people_search, email_enrich, email_validate, phone_enrich, linkedin_url_lookup | spec; **single-provider stack** + dedicated 0.25cr `email_validate`; pay-on-success |
| `ai-ark` | company_search, company_enrich, people_search, email_enrich, phone_enrich, linkedin_url_lookup | spec + script; discovery + account intel in one call; async/trackId email (`X-TOKEN`) |
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
