---
name: email-finder
description: >
  Find and validate work emails for a list's contacts (capability email_enrich),
  advancing them to stage 'email_enriched'. Runs the configured email_enrich waterfall,
  stops at the first valid email, re-verifies "guessed" results, and honors the
  pre-paid-enrichment gate. Use when the user wants to "find emails", "enrich emails
  for list N", or run only the email stage.
allowed-tools: Read, Bash
---

Input (list_id, optional input stage to read, optional max/skip):
**$ARGUMENTS**

Execute the email finder: read `agents/email-finder/agent.md` and follow it exactly —
bootstrap (config + resolve the email_enrich waterfall from keyed providers), load
contacts via `storage/cli.py query_by_stage`, run the waterfall (script adapters or
manifest-driven requests) applying each manifest's `match_key_priority` and
`status_semantics`, honor the pre-paid gate, then `advance_stage` to `email_enriched`
(advancing not-found contacts too). Report the waterfall results and validation breakdown.
