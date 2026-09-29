---
name: gtm
description: >
  Run the GTM contact pipeline from a plain-English campaign brief. Interprets the
  brief against context/ + gtm.config.yaml, plans the run, and drives the stages
  (company_search → people_search → qualify → email_enrich → phone_enrich → activate)
  threading one list_id, with approval gates, a do-not-contact pass, list QA and an
  activation preflight. Also reports on or resumes an existing list. Use when the user
  describes a campaign ("find me … at … for …"), says "/gtm …", "run the pipeline",
  "build a list", "status of list N", or "resume list N".
allowed-tools: Read, Bash, WebSearch, WebFetch, Task, Workflow
---

The user's input: **$ARGUMENTS**

Execute the orchestrator: read `agents/orchestrator/agent.md` and follow it exactly. Read
`REVIEW.md` once per session; it binds you and every sub-agent you start.

- **`status <list_id>`** → `python3 storage/cli.py run_report --input '{"list_id":<id>}'`;
  present stages logged, counts, cost (estimate vs actual), open warnings and gate verdicts.
  Change nothing.
- **`resume <list_id>`** → orchestrator §0: continue from the first stage whose latest event
  is not ok/warn/skipped, using the plan and expansion frozen on the list. Say where you are
  resuming and why.
- **Anything else is a brief:**
  1. Read `gtm.config.yaml` and the `context/` files. If `icp.md` / `personas.md` are missing or
     still templates, offer `/gtm-setup` instead of guessing an ICP.
  2. Interpret the brief into a concrete plan, expand the role and segment, and preview titles
     on a few sample companies.
  3. Present the plan (Gate #1) and get approval unless autonomy says otherwise; freeze it onto
     the list.
  4. Run only the available stages, threading the `list_id`. After each stage: `log_event`,
     then read `list_summary` back and compare it with what the stage reported.
  5. Before activation: `qa`, `suppress posture=send`, `preflight_activate` (must exit 0), then
     Gate #4.
  6. Produce the export (say **UNSCRUBBED** if `suppression_applied` is false) and finish with
     `run_report`.

If the brief is empty, ask for one (what to sell / who to target / where), or offer to
infer it from `context/icp.md`.
