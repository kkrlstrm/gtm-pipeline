---
name: company-enricher
description: >
  Build deep, source-cited account intel for an accepted company list (capability
  company_enrich) — funding, tech stack, leadership, hiring/"why now" signals, description
  — using Fire-Enrich-style Claude subagent fan-out, BEFORE people sourcing. Persists to
  the companies store. Use when the user wants to "enrich the companies", "get account
  intel", "research these accounts", or run only the company-enrichment stage.
allowed-tools: Read, Bash, WebSearch, WebFetch, Task, Workflow
---

Input (list_id and/or the accepted companies, optional custom_fields):
**$ARGUMENTS**

Execute the company enricher: read `agents/company-enricher/agent.md` and follow it
exactly — bootstrap (config + resolve the company_enrich waterfall; skip if disabled),
load the accepted companies (or `storage/cli.py query_companies`), honor the cost gate,
run the bundled `enrich-companies` workflow (Claude subagent fan-out: parallel dimension
researchers per company → synthesize + verify; `useFirecrawl:true` if firecrawl is keyed),
then persist the cited intel via `storage/cli.py upsert_companies`. Report enriched/verified
counts and the standout "why now" signals.
