---
name: contact-qualifier
description: >
  Score sourced contacts against the ICP and tier them QUALIFY / MAYBE / SKIP so paid
  enrichment is spent only on good-fit contacts. Context-driven (segments.md rubric +
  personas.md + exclusions.md); no provider, no credits. Always includes a human review
  gate. Use when the user wants to "qualify the list", "score these contacts", or run only
  the qualify stage.
allowed-tools: Read, Bash
---

Input (list_id, optional thresholds/overrides):
**$ARGUMENTS**

Execute the qualifier: read `agents/contact-qualifier/agent.md` and follow it exactly —
bootstrap (config + personas.md + segments.md + exclusions.md), load `sourced` contacts
via `storage/cli.py query_by_stage`, apply exclusions, classify company segment, match
persona, score 0–10 per the context rubric, present the QUALIFY/MAYBE/SKIP review (Gate
#2) for human sign-off, then `advance_stage` to `qualified` (or `skipped`). Report the
tier/segment/persona breakdown.
