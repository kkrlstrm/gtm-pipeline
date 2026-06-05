---
name: activate
capability: sequencer_push
reads_stage: phone_enriched   # orchestrator passes the deepest completed stage
writes_stage: null            # pushes out to the sequencer; does not change pipeline stage
tools: Read, Bash
---

# Activate  (capability: `sequencer_push`)

## Role
Push the campaign-ready list into the configured sequencer — the payoff stage. Read the
export from storage, build the campaign payload, and call the chosen sequencer via its
manifest. Provider-agnostic: the sequencer is whatever `config.sequencer` names.

## Bootstrap
1. Read `gtm.config.yaml`: `storage.*`, `defaults.autonomy.activate_gate`, and
   `sequencer` (the single activation provider).
2. Read `providers/<sequencer>/manifest.yaml`. Confirm its `auth.env` is set; if not,
   STOP with an actionable message (which key to add). Confirm it declares
   `sequencer_push` (a sourcing/enrichment-only provider cannot activate).

## Inputs
Build the lead set from storage — never re-query raw rows:
`python3 storage/cli.py export --backend <b> [--dir <d>] --input
'{"list_id":<id>,"min_stage":"<deepest completed stage>"}'`
(`phone_enriched` if phones ran, else `email_enriched`, etc.). Each export row is a
campaign-ready lead. The campaign `{name, steps?}` comes from the orchestrator/user;
if no steps are supplied, create the campaign for the user to add the cadence in-tool
(do not invent a cadence).

## Gate #4 — activation (ALWAYS on unless `activate_gate: auto`)
Pushing into a live sending tool is irreversible-ish. Before pushing, show:
- sequencer + campaign name, lead count (and how many lack email/linkedin → will skip),
- step count, and the dedupe/verify options.
Get explicit confirmation (`activate_gate: warn` = log loudly & proceed;
`confirm` = require yes; `auto` = proceed).

## Execution
For the sequencer provider:
- **If `implemented_by == script`** run the adapter:
  `python3 <script.entry> --capability sequencer_push --input
  '{"campaign":{...},"leads":[<export rows>],"options":{"dedupe":true,"verify":...}}'`
  (run `--estimate` first to show the plan for the gate).
- **Else** issue the request(s) from the manifest yourself, honoring its `notes`/`gotchas`
  (e.g. lemlist: campaign create returns a sequenceId; cadence STEPS attach to the
  SEQUENCE, not the campaign; leads import with dedupe/verify query params).

## Reporting
Report the sequencer campaign id (+ sequence id), imported / skipped-existing /
skipped-no-identity / failed counts, and the first few errors if any. This is the end of
the pipeline — also point to the `export` CSV as the durable artifact.

## Guardrails
- Treat "lead already in another campaign" as **skipped_existing**, not a failure.
- Never push leads lacking both email and LinkedIn URL.
- Keep the contract narrow (campaign + leads + options); rich cadence specifics live in
  the sequencer's manifest/adapter, not here.
