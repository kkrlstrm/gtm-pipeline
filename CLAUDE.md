# CLAUDE.md

Read [`AGENTS.md`](AGENTS.md) (how the repo works, for any agent) and
[`REVIEW.md`](REVIEW.md) (the quality bar). This file adds only what is specific to Claude Code.

## Commands

| Command | What it does |
|---|---|
| `/gtm-setup` | First run: writes your ICP, personas, segments, exclusions and do-not-contact file from your website and a few answers, then checks your setup |
| `/gtm <brief>` | Runs the pipeline from a plain-English brief |
| `/gtm status <list_id>` | Shows what ran, what was skipped, cost, and open warnings (from the run ledger) |
| `/gtm resume <list_id>` | Continues a list from its last completed stage with its frozen plan |
| `/company-discovery`, `/contact-sourcer`, `/contact-qualifier`, `/email-finder`, `/phone-finder`, `/activate` | One stage on its own |

## Workflows and agent types

The fan-out stages run as workflows in `.claude/workflows/`. Each passes a narrow agent type
from `.claude/agents/` (web tools only, no skill listing), which keeps each sub-agent's
starting context small. Pass `agentType: ''` in a workflow's args to fall back to the generic
sub-agent. Every workflow returns `failed[]` (or `unscored_ids`); report those.

| Workflow | Agent type | Model |
|---|---|---|
| `discover-companies` | `company-researcher` | Sonnet |
| `enrich-companies` | `company-intel-researcher` | Haiku per dimension, Sonnet to synthesize |
| `preview-titles` | `people-sourcer` | Sonnet |
| `source-people` | `people-sourcer` | Sonnet |
| `score-leads` | `lead-scorer` | Haiku |
