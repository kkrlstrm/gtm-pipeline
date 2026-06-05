# Writing a provider

A provider is a directory under `providers/<name>/` with a `manifest.yaml` and, when the
call is gnarly, a thin `adapter.py`. Agents never hardcode a provider — they read your
manifest and either issue the request themselves (spec-only) or run your adapter
(script-backed). Adding a provider is the only way to change *how* a capability is
fulfilled; wiring it into a run is config (`gtm.config.yaml` waterfalls).

## 1. Pick the capabilities

Map the provider's API to the canonical capabilities (full contracts in
[capabilities.md](capabilities.md)):

`company_search` · `people_search` · `email_enrich` · `email_validate` ·
`phone_enrich` · `phone_validate` · `sequencer_push` · `linkedin_url_lookup`

A provider declares one block per capability it supports. One provider can serve several
(e.g. Prospeo does `email_enrich` + `phone_enrich`; Apollo does four).

## 2. Spec-only or script-backed?

**Spec-only** (manifest only — the agent issues the `curl`): a single sync
request/response with simple pagination and a JSON body. Examples: apollo, dropleads,
clearoutphone, prospeo, smartlead.

**Script-backed** (`implemented_by: script` + `adapter.py`): use when the call is
- async with polling (apify actor run; fullenrich bulk),
- fire-and-poll fan-out,
- needs pre-resolution (apify company-name → LinkedIn URL),
- or returns shapes that break naive parsing (control chars, deep nesting).

When unsure, start spec-only; promote to a script if the agent struggles.

## 3. manifest.yaml

```yaml
name: <name>                      # must equal the directory name
display_name: <Pretty Name>
docs_url: <link>
auth:
  type: header | bearer | basic | query | env | none
  env: <PROVIDER_API_KEY>         # the single source of truth for "do I have this key?"
  header: <Header-Name>           # for type: header
  param: <query_param>            # for type: query
base_url: https://api.example.com
defaults:
  pacing_notes: <rate limits, Cloudflare quirks, etc.>

capabilities:
  <capability>:
    cost: "<human-readable cost>"
    # --- spec-only ---
    method: POST
    path: /v1/endpoint
    request_template: |           # {{tokens}} substituted from the canonical input
      { "q": "{{titles}}", "page": {{page}} }
    response:
      list_path: data.items       # where the result array lives (dot path)
      pagination: { has_more: "len(items) == per_page" }
      field_map:                  # provider field -> canonical field (ONLY place provider names appear)
        their_field: canonical_field
    status_semantics:             # enrichers: how to interpret a result
      accept: ["VERIFIED"]
      reverify_via: { capability: email_validate, statuses: ["UNVERIFIED"] }
      miss: [null]
    match_key_priority:           # enrichers: which identifiers to match on, in order
      - { when: "has provider_ids.<name>", use: ["id"] }
      - { when: "linkedin_url is vanity",  use: ["linkedin_url"] }
      - { when: "linkedin_url is member_id OR absent", use: ["first_name","last_name","domain"] }
    gotchas:
      - "Anything weird a future maintainer must know — encode quirks as DATA here, not in the agent."
    # --- script-backed instead of method/path ---
    implemented_by: script
    script: { runner: python3, entry: providers/<name>/adapter.py, modes: [run, estimate] }
```

Key rules:
- **`auth.env` is the keyed-check.** `show-plan.py` and every agent decide availability
  from it. A `builtin: true` / `auth.type: none` provider is always available.
- **`field_map` is the only place provider field names live.** Canonical names are stable.
- **Encode quirks as data** (`gotchas`, `status_semantics`, `match_key_priority`,
  `pagination`) so the agent stays generic.

## 4. adapter.py (the thin-script CLI contract)

If script-backed, the adapter MUST follow this uniform contract so adapters are
interchangeable:

- **Invoke:** `python3 providers/<name>/adapter.py --capability <cap> --input '<JSON>'`
  (also accept `--input-file PATH` and stdin), plus `--estimate` (cost only, no spend).
- **Input:** one JSON object matching the capability's canonical input.
- **Output:** canonical JSON to **stdout**; logs/progress to **stderr**; non-zero exit on
  hard failure; `{ "error": {...}, ... }` envelope on soft failure.
- **stdlib only** — no pip installs (so it stays fetch-and-pipe friendly). Shelling out to
  `curl` is fine (e.g. when a vendor blocks Python's user-agent).
- **Secrets from the environment only** (`os.environ[...]`). Never embedded, never fetched
  over the network.

Copy an existing adapter as a template: `providers/fullenrich/adapter.py` (bulk
fire-and-poll, empty-domain guard), `providers/apify/adapter.py` (async + pre-resolution),
`providers/lemlist/adapter.py` (multi-step sequencer push).

## 5. Honor the framework-wide invariants

1. **Apify member-ID LinkedIn URLs** (`/in/ACw…`) store/dedup fine but don't match in
   most APIs — `match_key_priority` must fall back to name+domain.
2. **Empty domain** can fail an enricher batch — *exclude* (don't fail) empty-domain rows
   and report them.
3. **Dedup normalization** must match the shared spec (strip query, strip trailing slash,
   lowercase) — never re-normalize differently.

## 6. Wire it up and verify

1. Add the key to `.env.example`.
2. Put the provider in a waterfall (or `sequencer:`) in `gtm.config.yaml`.
3. `python3 scripts/show-plan.py` — confirm it resolves where you expect.
4. `python3 providers/<name>/adapter.py --capability <cap> --estimate --input '{...}'`
   (if scripted) — confirm the no-spend path.
5. `bash scripts/selftest.sh` and `bash scripts/scrub-check.sh` before committing.

No agent files should change. If you found yourself editing an `agents/*.md` to add a
provider, something belongs in the manifest instead.
