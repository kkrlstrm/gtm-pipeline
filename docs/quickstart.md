# Quickstart

## 0. The short way

Open the repo in Claude Code and run:

```
/gtm-setup
```

It asks seven questions (your site, what you sell, who buys, where, your best customers, who
never to contact, who you never sell to), reads your website, writes the `context/` files and
the do-not-contact list, copies the config, and runs `python3 scripts/doctor.py`. Then go to
step 4. The manual route is below.

## 1. Configure (3 files)

```bash
cp .env.example .env                        # fill in ONLY the keys you have
cp gtm.config.example.yaml gtm.config.yaml  # waterfalls / storage / autonomy
cp context/icp.md.example       context/icp.md
cp context/personas.md.example  context/personas.md
# optional but recommended:
cp context/segments.md.example   context/segments.md
cp context/exclusions.md.example context/exclusions.md
# who must never be contacted (a header-only file means "none"):
printf 'email,domain,phone,linkedin_url,reason\n' > context/do-not-contact.csv
```

Fill in `context/icp.md` (what you sell, who you target, seed companies) and
`context/personas.md` (persona → title keywords). These are the brain of your targeting.

## 2. Load secrets (local env only — never transmitted)

```bash
set -a && source .env && set +a
```

## 3. Check the setup

```bash
python3 scripts/doctor.py
```

It checks the config, that your context files are filled in (no `<placeholder>` left), the
do-not-contact file, storage (and on Postgres, that the schema is current), and which provider
each stage resolves to from the keys you have. Exit 0 means ready. A stage with no available
provider is a warning, and a builtin like `web_research` is always on. Partial keys are fine:
you get a working, thinner pipeline. `python3 scripts/show-plan.py` prints only the provider
plan.

## 4. Run it from Claude Code

```
/gtm target mid-market fintech CFOs in DACH for our compliance product
```

The orchestrator interprets the brief against your context + config, shows a plan
(**Gate #1**), then runs the available stages threading one `list_id`:

```
company_search → people_search → do-not-contact → qualify → QA → email_enrich → phone_enrich
  → QA → do-not-contact (send) → preflight → activate
```

`/gtm status <list_id>` shows what ran, counts, credits estimated vs spent, and open warnings.
`/gtm resume <list_id>` continues an interrupted list with its frozen plan.

You can also run a single stage: `/company-discovery`, `/contact-sourcer`,
`/contact-qualifier`, `/email-finder`, `/phone-finder`, `/activate`.

## Storage backends

Default is `local` (zero setup) — data in `./.gtm-data/` as JSON/CSV. To use Postgres:

```bash
# in gtm.config.yaml:  storage.backend: postgres
psql "$DATABASE_URL" -f storage/postgres/schema.sql      # idempotent; re-run it to upgrade
# optional cross-campaign suppression (don't re-contact bounced/unsubscribed):
psql "$DATABASE_URL" -f storage/postgres/master-optional.sql   # + enable_master_dedup: true
```

Dedup is byte-identical across backends, so you can switch without surprises.

## The gates

Human gates, set in `gtm.config.yaml` under `defaults.autonomy`:

| Gate | Config key | Default | What it controls |
|---|---|---|---|
| #1 Plan | (orchestrator) | on | Approve the plan, the title set and its preview before any run |
| Paid source | `paid_source_gate` | `warn` | Before spending on a paid source (`auto`/`warn`/`confirm`) |
| #2 Qualify | (always on) | on | Review QUALIFY / MAYBE / SKIP |
| #3 Pre-enrich | `pre_enrich_confirm_over` | 50 contacts / 500 credits | Confirm before large paid enrichment |
| #4 Activate | `activate_gate` | `confirm` | Confirm before pushing to a live sequencer |

Deterministic gates, which no autonomy setting skips (details in
[run-integrity.md](run-integrity.md)):

| Gate | When | On failure |
|---|---|---|
| Do-not-contact, research posture | after sourcing | flags the list UNSCRUBBED, continues |
| List QA | after qualify, after enrichment | lists findings to drop / fix / keep |
| Do-not-contact, send posture | before activation | blocks |
| Activation preflight | before `sequencer_push` | blocks until scrubbed and QA-clean |

Power users can set the human gates to `auto` for hands-off runs; the defaults keep a human on
the spend and the send.

## Output

The campaign-ready CSV is written by `export` (`./.gtm-data/lists/<id>/export.csv` on
local; a file path on postgres), with `<csv>.verdict.json` beside it recording whether it was
scrubbed and its QA state. If `activate` ran, you also get the sequencer campaign id and
import counts.

## Verify your install (no keys needed)

```bash
bash scripts/selftest.sh     # storage, gates, adapters, plan resolution, sweep checks
bash scripts/scrub-check.sh  # secret / leak gate (run before publishing a fork)
```
