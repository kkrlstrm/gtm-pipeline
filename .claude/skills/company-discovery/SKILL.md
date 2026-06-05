---
name: company-discovery
description: >
  Find target companies that fit the ICP (capability company_search), create the pipeline
  list, and hand the company set to people_search. Supports look-alike / segment /
  expansion modes and synthesizes company_search from people_search when a provider lacks
  native org search. Use when the user wants to "find companies", "build a target account
  list", or run only the discovery stage.
allowed-tools: Read, Bash, WebSearch, WebFetch
---

Input (a brief / segment / seed companies, optional geography):
**$ARGUMENTS**

Execute company discovery: read `agents/company-discovery/agent.md` and follow it exactly
— bootstrap (config + icp.md + resolve the company_search waterfall, where builtin
providers like web_research are always available), discover companies (builtin
WebSearch/WebFetch, script, or manifest-driven requests; synthesize from people_search if
needed), dedupe + apply exclusions, `create_list`, and return the company list + list_id
for the sourcing stage.
