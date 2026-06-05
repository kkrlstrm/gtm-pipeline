---
name: phone-finder
description: >
  Find and validate phone numbers for a list's contacts (capability phone_enrich),
  advancing them to stage 'phone_enriched'. Runs the configured phone_enrich waterfall,
  validates each number (phone_validate), prefers mobile, and honors the
  pre-paid-enrichment gate. Use when the user wants to "find phone numbers", "enrich
  phones for list N", or run only the phone stage.
allowed-tools: Read, Bash
---

Input (list_id, optional input stage to read, optional max/skip):
**$ARGUMENTS**

Execute the phone finder: read `agents/phone-finder/agent.md` and follow it exactly —
bootstrap (config + resolve the phone_enrich and phone_validate waterfalls from keyed
providers), load contacts via `storage/cli.py query_by_stage`, run the waterfall (script
adapters or manifest-driven requests) applying each manifest's `match_key_priority`,
validate found numbers, pick the best (mobile > direct > switchboard), honor the
pre-paid gate, then `advance_stage` to `phone_enriched` (advancing not-found contacts
too). Report the waterfall results and phone-type breakdown.
