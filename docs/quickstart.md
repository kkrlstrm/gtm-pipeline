# Quickstart

## 1. Configure (3 files)

```bash
cp .env.example .env                        # fill in ONLY the keys you have
cp gtm.config.example.yaml gtm.config.yaml  # waterfalls / storage / autonomy
cp context/icp.md.example       context/icp.md
cp context/personas.md.example  context/personas.md
# optional but recommended:
cp context/segments.md.example   context/segments.md
cp context/exclusions.md.example context/exclusions.md
```

Fill in `context/icp.md` (what you sell, who you target, seed companies) and
`context/personas.md` (persona → title keywords). These are the brain of your targeting.

## 2. Load secrets (local env only — never transmitted)

```bash
set -a && source .env && set +a
```

## 3. See what your keys + config will do

```bash
python3 scripts/show-plan.py
```

This resolves each stage to the providers you actually have keys for. A stage with no
available provider is flagged (⚠); a builtin like `web_research` is always on. Partial keys
are fine — you get a working, thinner pipeline.

## 4. Run it from Claude Code

```
/gtm target mid-market fintech CFOs in DACH for our compliance product
```

The orchestrator interprets the brief against your context + config, shows a plan
(**Gate #1**), then runs the available stages threading one `list_id`:

```
company_search → people_search → qualify → email_enrich → phone_enrich → activate
```

You can also run a single stage: `/company-discovery`, `/contact-sourcer`,
`/contact-qualifier`, `/email-finder`, `/phone-finder`, `/activate`.

## Storage backends

Default is `local` (zero setup) — data in `./.gtm-data/` as JSON/CSV. To use Postgres:

```bash
# in gtm.config.yaml:  storage.backend: postgres
psql "$DATABASE_URL" -f storage/postgres/schema.sql
# optional cross-campaign suppression (don't re-contact bounced/unsubscribed):
psql "$DATABASE_URL" -f storage/postgres/master-optional.sql   # + enable_master_dedup: true
```

Dedup is byte-identical across backends, so you can switch without surprises.

## The four gates (autonomy)

Set in `gtm.config.yaml` under `defaults.autonomy`:

| Gate | Config key | Default | What it controls |
|---|---|---|---|
| #1 Plan | (orchestrator) | on | Approve the resolved plan before any run |
| Paid source | `paid_source_gate` | `warn` | Before spending on a paid source (`auto`/`warn`/`confirm`) |
| #3 Pre-enrich | `pre_enrich_confirm_over` | 50 contacts / 500 credits | Confirm before large paid enrichment |
| #4 Activate | `activate_gate` | `confirm` | Confirm before pushing to a live sequencer |

Power users can set gates to `auto` for hands-off runs; the defaults keep a human on the
spend and the send.

## Output

The campaign-ready CSV is written by `export` (`./.gtm-data/lists/<id>/export.csv` on
local; a file path on postgres). If `activate` ran, you also get the sequencer campaign id
and import counts.

## Verify your install (no keys needed)

```bash
bash scripts/selftest.sh     # storage round-trip, adapter estimates, plan resolution
bash scripts/scrub-check.sh  # secret / leak gate (run before publishing a fork)
```
