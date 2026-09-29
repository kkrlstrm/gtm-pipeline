# gtm-pipeline

<!-- portfolio-status -->
**Status:** Reference implementation of the list-building path GTM engineers use on an internal GTM platform (see [internal-gtm-platform](https://github.com/kkrlstrm/internal-gtm-platform)). Tenant data, provider adapters and company-specific policy stay private. · **Layer:** Workload: list building · **[Portfolio map ›](https://github.com/kkrlstrm)**

## gtm-pipeline lets anyone with Claude Code request a campaign-ready list in plain English.

Run `/gtm-setup` once: it asks seven questions, reads your website, and writes your ICP,
personas and do-not-contact list. Then describe the campaign in plain English inside Claude
Code:

```
/gtm target mid-market fintech CFOs in DACH for our compliance product
```

It turns that brief into a deduped, qualified, enriched, **sequencer-ready** contact list.
Seven explicit stages, each threading one `list_id`, with deterministic checks between them:

```
company_search → company_enrich → people_search → qualify → email_enrich → phone_enrich → activate
   (discover)      (account intel)   (source)      (score)     (find email)   (find phone)   (push to sequencer)
                                          └ do-not-contact    └ list QA                 └ QA · do-not-contact · preflight
```

Before those stages, the brief is interpreted into a plan you approve at **Gate #1** — including
a **role & segment expansion** so an ambiguous title matches every title that *means* it
(`principal` → head of school, headteacher, school director) instead of being matched literally
and silently under-sourcing ([role expansion](docs/role-expansion.md)).

It's not a Clay clone. It makes the GTM workflow itself **portable, auditable, and
agent-executable.**

---

## Why this exists

Most GTM list-building has the same failure modes:

- The **targeting logic** lives in one operator's head — and a literal title search silently
  misses the synonyms it didn't think to type.
- The **enrichment logic** lives inside one vendor's UI.
- **Provider swaps** mean rebuilding the workflow.
- **Agents** research, but lose state, duplicate work, and flood context.
- **Sending tools** get a list — but not the reasoning behind it.

`gtm-pipeline` makes the workflow portable: your judgment lives in `context/*.md` and
`gtm.config.yaml`, providers become swappable capabilities, every stage writes to one shared
`list_id`, and human gates protect spend and sending.

## Before / after

**Before** — manually: build a list in one tool → export/import to another → search for
people → apply persona judgment by hand → run enrichment waterfalls → check the CRM → clean
a CSV → push to a sequencer. It works, but it's hard to repeat, audit, or hand off.

**After** — write `/gtm target mid-market fintech CFOs in DACH for our compliance product`,
and the pipeline reads your ICP context → expands the role/segment into the titles and
industries that actually count (you approve it) → resolves providers from your keys → discovers
companies → suppresses CRM matches → enriches account intel → sources contacts → scores them
against your rubric → finds verified emails/phones → exports or activates. Same expertise —
encoded once, requested in plain English.

## See it run

A complete run is in **[examples/dach-fintech-cfos/](examples/dach-fintech-cfos/)**: the
brief, the ICP, the real provider plan, cited account intel, the campaign-ready
[export.csv](examples/dach-fintech-cfos/output/export.csv), and the
[activation log](examples/dach-fintech-cfos/activation-log.md). It shows the **shape** of a
run — the data flow, the canonical records, the SKIPs removed before paid enrichment —
generated through the actual `storage/cli.py` + `show-plan.py`.

## Runs you can check

A list-building run can exit cleanly and still be wrong: a researcher that died is counted as
"nobody there", a phone is stored in a form no dialer takes, a customer's CFO is on the list
because the do-not-contact file was never read. The pipeline makes those cases visible and
stops them before the send:

- **A run ledger.** Every stage records its provider, counts, credits estimated and spent, and
  warnings. `/gtm status <list_id>` rebuilds the story from the ledger; `/gtm resume
  <list_id>` continues an interrupted list with its frozen plan.
- **Do-not-contact, in two postures.** Checked after sourcing (a missing list is flagged and
  the run continues, because research contacts nobody) and before activation (a missing list
  blocks the push). Every export carries a verdict file saying whether it was scrubbed.
- **List QA.** It catches defects that a name+company dedupe and a human review both missed:
  the same person under two spellings, a one-character email-domain typo, dot-format phones
  and glued-on extensions, one switchboard number labelled as five people's direct dials.
  Mechanical fixes are one command.
- **An activation preflight** that no autonomy setting skips.
- **Title preview.** Before sourcing is paid for, a few sample companies show which real
  titles your title set catches and which it misses.
- **Failures are reported, not filtered.** Every fan-out returns the items whose sub-agent
  failed, and the qualifier never advances a contact nobody scored.

The rules are in [REVIEW.md](REVIEW.md) and the mechanics in
[docs/run-integrity.md](docs/run-integrity.md). Both backends run the same tested gates.

## What this unlocks

- **Provider choice is configuration.** Swap Apollo for Prospeo, LeadMagic, AI Ark, FullEnrich,
  Dropleads, Smartlead, Lemlist, or HeyReach in `gtm.config.yaml` — never a prompt or agent.
  Only keyed providers run, so a partial key set still gives a working, thinner pipeline.
- **State survives the pipeline.** Every stage writes canonical records under one `list_id`,
  so discovery → sourcing → qualify → enrichment → activation never "research it and lose it."
- **Judgment is explicit and reviewable.** Personas, segments, exclusions, and the 0–10
  rubric live in `context/*.md` — versioned, not in someone's head — and the role/segment
  expansion that widens ambiguous titles is a reviewed artifact at Gate #1, not tacit knowledge.
- **Expensive work is routed.** Research/sourcing on Sonnet subagents, high-volume scoring on
  Haiku, intermediate results kept out of the main context (below).
- **Spend and sends are gated.** Plan → qualify review → pre-paid-enrichment → activation,
  all configurable.

Storage is `local` (zero-setup) or `postgres` (shared, cross-campaign dedup) with identical
semantics; secrets are read from your local `.env` and sent only to each provider's own API.

## Why subagents matter here

A naive agentic workflow asks one model to research companies, source people, score leads,
enrich records, **and** hold every intermediate result. It loses state and floods context.

`gtm-pipeline` fans out subagents for the parallel stages via bundled workflows:

- **`discover-companies`** — one `company-researcher` (Sonnet) per search angle, deduped by domain.
- **`source-people`** — one `people-sourcer` (Sonnet) per company.
- **`enrich-companies`** — a parallel research pass per account, then synthesize + source-verify.
- **`score-leads`** — batched **Haiku** scoring against your rubric.
- **`preview-titles`** — a few sample companies, to check the title set against real titles.

The workflow script owns the loop, merge, and dedupe; the main agent receives only the final
structured result — breadth without flooding context or paying top-tier prices for cheap work.
Each workflow runs a narrow agent type (web tools only, no skill listing), so every sub-agent
starts with a small context, and each returns the items that failed next to the results.

## The operator abstraction

The point isn't speed — it's that **the skill of building a good GTM list can be encoded**:
what good accounts look like, which personas matter, which titles to include or exclude,
which providers to trust per stage, when to spend, when to suppress, when to activate. A
senior operator defines it once; anyone then requests the outcome in plain English.

The runtime is open source on purpose. The durable value isn't the plumbing — it's the
judgment you encode into it and the per-campaign context it accumulates. Open-sourcing the
pattern is a deliberate bet that differentiation lives in that judgment layer, not the pipes.

## Is this a Clay replacement?

No. Clay is where a human operates a workflow. gtm-pipeline is where an agent executes a workflow that has already been encoded.

## Architecture

One brief flows down five layers — inputs → brain → provider seam → storage → out. Agents
speak only in capabilities and read provider manifests, so you swap providers by editing
config, never code. Full write-up + ASCII fallback in [docs/architecture.md](docs/architecture.md).

```mermaid
flowchart TD
  brief["🗣️ Plain-English brief<br/>/gtm 'target DACH fintech CFOs…'"]

  subgraph IN["① Your inputs — bring your own"]
    ctx["context/*.md<br/>icp · personas · segments · exclusions"]
    cfg["gtm.config.yaml<br/>waterfalls · gates · storage"]
    env[".env<br/>provider keys — local only, never sent"]
  end

  subgraph BRAIN["② Framework brain — speaks only in capabilities"]
    orch["Orchestrator<br/>interpret → expand + preview titles → plan → gates → thread list_id"]
    agents["Capability agents (one per stage)<br/>company-discovery · company-enricher · contact-sourcer<br/>contact-qualifier · email-finder · phone-finder · activate"]
    orch --> agents
  end

  subgraph SEAM["③ Provider seam — swap by config, not code"]
    man["providers/*/manifest.yaml<br/>auth · endpoints · field_map · gotchas"]
    api["API providers<br/>apollo · prospeo · leadmagic · ai-ark · dropleads · apify(LinkedIn)<br/>fullenrich · clearoutphone · firecrawl"]
    builtin["web_research — builtin, no key"]
    wf["Claude subagent fan-out (token-efficient)<br/>discover-companies · source-people · enrich-companies · score-leads<br/>Sonnet research/sourcing · Haiku scoring"]
    man --> api
    man --> builtin --> wf
  end

  subgraph TRUTH["④ Storage — source of truth, byte-identical dedup"]
    cli["storage/cli.py<br/>lists · contacts · export · run ledger<br/>suppress · qa · preflight_activate"]
    db[("local files  or  postgres")]
    cli --> db
  end

  subgraph OUT["⑤ Out"]
    csv["📄 export.csv — campaign-ready list"]
    seq["📣 Sequencer<br/>lemlist · smartlead · heyreach"]
    crm["🗂️ HubSpot CRM — dedupe / suppression"]
  end

  brief --> orch
  IN --> orch
  agents -->|read knowledge| man
  agents <-->|ops only| cli
  agents -. optional dedupe .-> crm
  cli --> csv
  agents -->|activate| seq
```

## Quickstart

In Claude Code:

```
/gtm-setup                       # ICP, personas, do-not-contact list, config — then a readiness check
/gtm <your brief>                # run it
/gtm status <list_id>            # what ran, counts, credits, open warnings
```

Or by hand:

```bash
cp .env.example .env                       # fill in the keys you have
cp gtm.config.example.yaml gtm.config.yaml # tweak waterfalls / storage / autonomy
cp context/icp.md.example      context/icp.md        # describe what you sell & who you target
cp context/personas.md.example context/personas.md   # persona → title keywords
set -a && source .env && set +a
python3 scripts/doctor.py                  # ready? says exactly what is missing
```

The default config uses the `local` backend, so a first run needs no database. You can run
the **whole pipeline on one key** (Apollo, Prospeo, LeadMagic, or AI Ark) — see
[docs/single-provider.md](docs/single-provider.md). Full walkthrough in
[docs/quickstart.md](docs/quickstart.md).

## Providers

Providers are interchangeable **capabilities** — company search, people search, email
enrich, phone enrich, CRM dedupe, sequencer push. Swap any of them by editing
`gtm.config.yaml`, never the agents ([how](docs/swapping-providers.md)). A provider is used
only if its key is set.

They're also **additive, not just swappable**: list 1, 2, 3, 4, or all of them in a stage and
**whichever keys you bring run together** — search stages union + dedup across providers, enrich
stages waterfall and stop at the first valid result per contact. One key runs the pipeline solo;
add another and it slots into the same waterfall, no agent changes
([stacking](docs/swapping-providers.md#you-can-stack-as-many-as-you-want-additive-waterfalls)).

| Provider | Capabilities | Kind |
|---|---|---|
| `web_research` | company_search, linkedin_url_lookup, company_enrich, people_search | builtin (no key) |
| `firecrawl` | company_enrich | script |
| `apify` | people_search | script (LinkedIn) |
| `apollo` | people_search, company_search, email_enrich, phone_enrich | spec — single-provider stack |
| `dropleads` | people_search | spec |
| `fullenrich` | email_enrich, phone_enrich | script |
| `clearoutphone` | phone_validate | spec |
| `prospeo` | company_search, people_search, email_enrich, phone_enrich, company_enrich | spec — single-provider stack |
| `leadmagic` | company_search, company_enrich, people_search, email_enrich, email_validate, phone_enrich, linkedin_url_lookup | spec — single-provider stack + cheap validator |
| `ai-ark` | company_search, company_enrich, people_search, email_enrich, phone_enrich, linkedin_url_lookup | spec + script — discovery+intel in one call; async/trackId email |
| `lemlist` | sequencer_push (email) | script |
| `smartlead` | sequencer_push (email) | spec |
| `heyreach` | sequencer_push (LinkedIn) | spec |
| `hubspot` | crm_dedupe (suppress CRM dupes) | script, read-only |

Run `python3 scripts/show-plan.py` to see which ones your current keys + config resolve to.

## Layout

| Path | What |
|---|---|
| `REVIEW.md` · `AGENTS.md` · `CLAUDE.md` | The quality bar every run clears; the contract for any coding agent (Claude Code, Codex, Cursor); Claude Code specifics |
| `agents/` | The pipeline brain — one capability-agnostic agent per stage + an orchestrator |
| `.claude/` | Bundled subagent workflows + custom subagents (the parallel fan-out) |
| `providers/` | Pluggable provider registry (declarative `manifest.yaml` + optional `adapter.py`) |
| `storage/` | `cli.py` (uniform op set, run ledger, gates) + self-contained Postgres schema |
| `context/` | Your ICP / personas / segments / exclusions (shipped as `.example` skeletons) |
| `examples/` | A complete synthetic run, end to end |
| `docs/` | Architecture, capability taxonomy, single-provider, how to write a provider |

## Docs

- [examples/dach-fintech-cfos/](examples/dach-fintech-cfos/) — a complete worked run (ICP, config, provider plan, export CSV, activation log)
- [docs/architecture.md](docs/architecture.md) — the layered diagram (brief → … → sequencer/CRM)
- [docs/quickstart.md](docs/quickstart.md) — setup, the gates, storage backends
- [docs/run-integrity.md](docs/run-integrity.md) — the run ledger, do-not-contact, list QA, activation preflight, exit codes
- [docs/single-provider.md](docs/single-provider.md) — run on one key (Apollo / Prospeo); why the free-search providers
- [docs/capabilities.md](docs/capabilities.md) — capability taxonomy + canonical records + storage ops
- [docs/role-expansion.md](docs/role-expansion.md) — ambiguous titles/industries → an explicit, gate-reviewed set (the recall fix)
- [docs/swapping-providers.md](docs/swapping-providers.md) — config-only provider swaps
- [docs/writing-a-provider.md](docs/writing-a-provider.md) — add a manifest / adapter

## Compliance & acceptable use

You are responsible for using each provider within its terms — including automation/scraping
limits (e.g. LinkedIn data reached via `web_research` or `apify`) and applicable
data-protection law (GDPR, CCPA, and local rules) when you store or contact people. The
framework gives you the seams to choose compliant providers and to gate sending; it does not
grant permission to use any provider or dataset. Confirm your own legal basis before a live
run.

## Verify (no keys needed)

```bash
bash scripts/selftest.sh      # storage, gates on real defects, adapters, plan, sweep checks
bash scripts/scrub-check.sh   # secret/leak gate — run before publishing a fork
python3 examples/dach-fintech-cfos/replay.py   # regenerate the worked example through the real CLI
```

## Security & status

Secrets are read from your local `.env` only and never fetched over the network — details in
[SECURITY.md](SECURITY.md).

This is an open-source **reference implementation** — a pattern and portable execution layer,
not a finished product. The architecture and a full example are here; the example data is
synthetic (so it shows the data flow, not live-API survival), and real runs need your own
keys. Build on it; don't treat it as a drop-in "GTM OS." Contributions welcome —
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE).

---

<!-- portfolio-footer -->
## Where this fits

Part of a portfolio of **governed, AI-native GTM systems** — reference implementations and reusable patterns extracted from a private production stack. In that system this is the provider-portable pipeline that turns a plain-English brief into a sequencer-ready list.

**Full portfolio map → [github.com/kkrlstrm](https://github.com/kkrlstrm)**

Works with:
- [gtm-research](https://github.com/kkrlstrm/gtm-research) — supplies cached, source-verified enrichment
- [gtm-deliverability](https://github.com/kkrlstrm/gtm-deliverability) — receives the list for a recipient-aware rollout
