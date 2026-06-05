---
name: contact-sourcer
description: >
  Source people at target companies (capability people_search) and write them to
  storage at stage 'sourced'. Runs the configured people_search waterfall, backfills
  domains, dedups, and cross-references the master DB. Use when the user wants to
  "source contacts", "find people at these companies", or run only the sourcing stage.
allowed-tools: Read, Bash, WebSearch, WebFetch
---

Input (companies + persona/titles + geography, and optionally an existing list_id):
**$ARGUMENTS**

Execute the contact sourcer: read `agents/contact-sourcer/agent.md` and follow it
exactly — bootstrap (config + context + resolve the people_search waterfall from keyed
providers), source per provider via their manifests, backfill domains, dedup +
crossref_master, then write via `storage/cli.py` (create_list if needed, upsert_contacts).
Report the waterfall table and the resulting `list_id`.
