---
name: activate
description: >
  Push the campaign-ready list into the configured sequencer (capability sequencer_push)
  — the final stage. Reads the storage export, builds the campaign payload, and pushes via
  the chosen sequencer's manifest/adapter, behind an always-on activation gate. Use when
  the user wants to "push to the sequencer", "create the campaign", "activate list N", or
  "send to lemlist".
allowed-tools: Read, Bash
---

Input (list_id, optional campaign name + steps):
**$ARGUMENTS**

Execute activation: read `agents/activate/agent.md` and follow it exactly — bootstrap
(config + the `sequencer` provider's manifest; confirm its key is set), build leads via
`storage/cli.py export` at the deepest completed stage, present the activation summary
(Gate #4) for confirmation, then push via the sequencer adapter
(`--capability sequencer_push`). Report the campaign id + imported/skipped/failed counts.
