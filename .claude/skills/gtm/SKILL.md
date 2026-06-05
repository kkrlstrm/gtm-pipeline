---
name: gtm
description: >
  Run the GTM contact pipeline from a plain-English campaign brief. Interprets the
  brief against context/ + gtm.config.yaml, plans the run, and drives the stages
  (company_search → people_search → qualify → email_enrich → phone_enrich → activate)
  threading one list_id, with approval gates. Use when the user describes a campaign
  ("find me … at … for …"), says "/gtm …", "run the pipeline", or "build a list".
allowed-tools: Read, Bash, WebSearch, WebFetch, Task
---

The user's campaign brief: **$ARGUMENTS**

Execute the orchestrator: read `agents/orchestrator/agent.md` and follow it exactly.

1. Read `gtm.config.yaml` and the `context/` files.
2. Interpret the brief above into a concrete plan (segments, personas→titles,
   geography, per-stage resolved waterfalls showing keyed vs skipped providers,
   sequencer, cost ceilings).
3. Present the plan (Gate #1) and get approval unless autonomy says otherwise.
4. Run only the available stages (agent exists AND ≥1 keyed provider for
   provider-backed stages), threading the `list_id`, honoring Gates #2–#4.
5. Produce the export CSV (or activation result) and a stage-by-stage summary.

If the brief is empty, ask for one (what to sell / who to target / where), or offer to
infer it from `context/icp.md`.
