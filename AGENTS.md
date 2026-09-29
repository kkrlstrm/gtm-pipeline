# AGENTS.md — the contract for any coding agent

This file is tool-neutral: it applies to Claude Code, Codex, Cursor, or any agent that can
read files and run shell commands. [`CLAUDE.md`](CLAUDE.md) adds the Claude Code specifics.
[`REVIEW.md`](REVIEW.md) is the quality bar every run clears; read it before a live run.

## What this repo does

A plain-English campaign brief becomes a deduped, qualified, enriched, sequencer-ready contact
list. One `list_id` threads through these stages:

```
company_search → company_enrich → people_search → [suppress] → qualify → [qa] →
email_enrich → phone_enrich → [qa · suppress(send) · preflight] → activate
```

## How to run it

- **Entry point:** follow [`agents/orchestrator/agent.md`](agents/orchestrator/agent.md). Each
  stage is a markdown procedure in `agents/<stage>/agent.md`. They are plain instructions, so
  any agent can follow them.
- **Storage:** every read and write goes through `python3 storage/cli.py <op> --input '<json>'`.
  It reads the backend from `gtm.config.yaml`. Never write raw SQL or edit files under
  `.gtm-data/` by hand. Ops, inputs and exit codes are listed at the top of `storage/cli.py`.
- **Fan-out:** Claude Code runs the parallel stages as workflows in `.claude/workflows/`. An
  agent without a workflow runtime runs the same prompt once per item, sequentially, and must
  still report the items that failed.
- **Providers:** chosen by `gtm.config.yaml` waterfalls and described by
  `providers/<name>/manifest.yaml`. Keys come from the local environment only.

## Rules that are easy to break

1. Read the verdict, not the exit code: `suppress` (research posture) exits 0 with
   `applied: false` when it could not check anything.
2. After each stage, compare `list_summary` with what the stage reported, and `log_event` it.
3. Never push to a sequencer unless `preflight_activate` returns `ok: true` (exit 0).
4. Never invent an email, phone, title, URL or company fact. Blank is a valid answer.
5. Never edit an agent prompt to support a provider; edit the manifest.

## Checks before you commit

```bash
bash scripts/selftest.sh      # storage, gates, adapters, workflow syntax, sweep tests
bash scripts/scrub-check.sh   # no secrets, no private strings
```
