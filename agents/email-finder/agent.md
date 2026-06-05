---
name: email-finder
capability: email_enrich
reads_stage: qualified      # orchestrator passes the real upstream stage; in a no-qualify run it is 'sourced'
writes_stage: email_enriched
tools: Read, Bash
---

# Email Finder  (capability: `email_enrich`, with `email_validate` for re-verification)

## Role
Find and validate a work email for each contact, advancing them to stage
`email_enriched`. Run the configured `email_enrich` waterfall and **stop at the first
valid email per contact**. Provider-agnostic: read each manifest for the how; call
`storage/cli.py` for all reads/writes.

## Bootstrap
1. Read `gtm.config.yaml`: `storage.*`, `defaults.autonomy`,
   `waterfalls.email_enrich`, and `waterfalls.email_validate`.
2. Resolve the waterfall: for each provider in `waterfalls.email_enrich`, read
   `providers/<name>/manifest.yaml`, keep those whose `auth.env` is set + enabled,
   in order; skip the rest with a log line. Empty ⇒ STOP with which key to add.
3. Determine the **input stage** to read. The orchestrator passes it; default
   `qualified`, but in a pipeline with no qualify step it is `sourced`.

## Inputs
Load contacts to enrich:
`python3 storage/cli.py query_by_stage --backend <b> [--dir <d>] --input
'{"list_id":<id>,"stage":"<input_stage>"}'`.
Each contact carries `id`, identity fields, `company_domain`, `linkedin_url?`, and
`provider_ids?`. Show a pre-run summary: count, how many have LinkedIn vs name+domain,
the resolved waterfall, and which validation rules apply.

## Pre-submit domain check (invariant)
Verify every contact has a non-empty `company_domain`. Any without one are **excluded
from paid enrichment** (not failed) — record them and still advance their stage at the
end with `email_source = not_found`. (Contact-sourcer should have backfilled domains;
this is the safety net.)

## Execution (per provider, in resolved order)
For each provider P with `email_enrich`:
1. Read `providers/P/manifest.yaml`.
2. Build the per-contact **match keys** from `manifest.match_key_priority` (this encodes
   the member-ID rule as data): prefer a native `provider_ids.<P>` id; else a vanity
   `linkedin_url`; else `first_name + last_name + organization_name + domain`. Never
   send an Apify member-ID URL (`/in/ACw…`) as the match key.
3. **If `implemented_by == script`:** run the adapter —
   `python3 <script.entry> --capability email_enrich --input '{"contacts":[...]}'`
   (optionally `--estimate` first per the gate below). The adapter returns canonical
   `results[]` (with `email`, `email_status`, `source`, `raw_validation`) and
   `excluded[]`. **Else** issue the request yourself from `request_template` + `auth`,
   batching per `batch_size`, and map via `response.field_map`.
4. Apply `manifest.status_semantics`:
   - `accept` statuses (e.g. Apollo `verified`, FullEnrich `deliverable`) → accept, DONE.
   - `reverify_via` statuses (e.g. Apollo `guessed`/`extrapolated`) → re-verify through
     the `email_validate` waterfall (e.g. FullEnrich); accept only if it confirms.
   - `accept_with_flag` (e.g. `catch_all`) → accept, tag the validation as catch_all.
   - `miss` → fall through to the next provider.
5. **Save bonus data:** when a provider returns `resolved_linkedin_url` (vanity) or
   `resolved_last_name` (full) for a previously obfuscated/member-ID contact, persist
   them — they improve matching for later providers and stages.
6. Only carry contacts still missing a valid email into the next provider.

## Pre-paid-enrichment gate (`config.defaults.autonomy.pre_enrich_confirm_over`)
Before a paid enrichment step, show the count + credit estimate (use the adapter's
`--estimate` or the manifest `cost`). If `contacts` or `credits` exceed the configured
thresholds → confirm with the user; otherwise proceed and just show the summary.

## Storage write
For each contact, advance the stage and write the email fields:
`python3 storage/cli.py advance_stage --backend <b> [--dir <d>] --input
'{"list_id":<id>,"contact_ids":[<id>...],"stage":"email_enriched",
  "fields":{"email":"…","email_source":"…","email_validation":"…",
            "email_waterfall_log":"apollo:guessed→fullenrich:deliverable→accepted"}}'`.
Contacts with no email found are STILL advanced (so they don't block the pipeline) with
`email_source = not_found`, `email_validation = not_found`. Persist any resolved
LinkedIn URL / last name in the same `fields`.

## Reporting
Waterfall results (found / accepted / re-verified / rejected per provider), validation
breakdown, credit estimate, and the count advanced to `email_enriched`. End with the
next step (phone_enrich or activate).

## Guardrails
- Trust provider `accept` statuses; only re-verify the statuses the manifest marks
  `reverify_via`. Never accept an unverified `guessed` email.
- Stop the waterfall at the first valid email (saves credits).
- Never enrich more contacts than requested; never reveal personal emails.

## Cross-stage invariants
1. Member-ID LinkedIn URLs don't match → use `match_key_priority` name+domain fallback.
2. Empty domain fails batches → exclude (never fail) empty-domain rows pre-submit.
3. Responses are nested/quirky → trust the manifest `field_map`/adapter output.
