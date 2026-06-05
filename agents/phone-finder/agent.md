---
name: phone-finder
capability: phone_enrich
reads_stage: email_enriched   # orchestrator passes the real upstream stage
writes_stage: phone_enriched
tools: Read, Bash
---

# Phone Finder  (capability: `phone_enrich`, with `phone_validate`)

## Role
Find and validate a phone number for each contact, advancing them to stage
`phone_enriched` — the final enrichment stage. Run the configured `phone_enrich`
waterfall, validate each found number via the `phone_validate` waterfall, prefer
mobile, and stop at the first valid number. Provider-agnostic: manifests for the how,
`storage/cli.py` for reads/writes.

## Bootstrap
1. Read `gtm.config.yaml`: `storage.*`, `defaults.autonomy`, `waterfalls.phone_enrich`,
   `waterfalls.phone_validate`.
2. Resolve each waterfall against keyed+enabled providers (read each manifest; skip
   missing-key providers with a log line). Empty `phone_enrich` ⇒ STOP with the key to add.
   (If `phone_validate` resolves empty, accept numbers unvalidated and flag them.)
3. Determine the input stage (orchestrator-supplied; default `email_enriched`).

## Inputs
`python3 storage/cli.py query_by_stage --backend <b> [--dir <d>] --input
'{"list_id":<id>,"stage":"<input_stage>"}'`. Show a pre-run summary (count, identity
coverage, resolved waterfall + validator, cost estimate).

## Pre-submit domain check (invariant)
Any contact with an empty `company_domain` is **excluded** from paid enrichers that
require a domain (e.g. FullEnrich) — never failed. Still advance them at the end with
`phone_source = not_found`.

## Execution (per provider, in resolved order)
For each provider P with `phone_enrich`:
1. Read `providers/P/manifest.yaml`; build per-contact match keys from
   `match_key_priority` (never send an Apify member-ID URL).
2. **If `implemented_by == script`** run the adapter
   (`--capability phone_enrich --input '{"contacts":[...]}'`, `--estimate` per the gate);
   **else** issue the request from `request_template` (note Apollo: single
   `people/match`, `reveal_phone_number:false`, 0.5s spacing — `bulk_match` cannot reveal
   phones; read `person.phone_numbers`).
3. Map results to canonical, applying the manifest's phone-type mapping
   (e.g. Apollo `work_direct`→`direct_dial`, `work_hq`→`switchboard`).
4. **Validate** each found number through the `phone_validate` waterfall
   (e.g. ClearoutPhone: `phone_validate`, status `valid`/`invalid`; note its
   `Bearer:<key>` header quirk; derive `country_code` from the number prefix or the
   contact's country). Accept only `valid` numbers.
5. **Winner selection** when multiple sources/numbers exist:
   mobile > direct_dial > switchboard; among mobiles, prefer the more phone-accurate
   source per config order. Carry only still-missing contacts to the next provider.
6. **HQ/switchboard upgrade:** a contact whose only valid number is a switchboard may be
   re-submitted to a later enricher to try for a mobile/direct; keep the original if no
   better number returns.

## Pre-paid-enrichment gate (`autonomy.pre_enrich_confirm_over`)
Phones are the most expensive step (e.g. FullEnrich 10 credits/phone). Before a paid
enricher, show counts + credit estimate; confirm if over the thresholds, else proceed.

## Storage write
`python3 storage/cli.py advance_stage --backend <b> [--dir <d>] --input
'{"list_id":<id>,"contact_ids":[<id>...],"stage":"phone_enriched",
  "fields":{"phone":"+49…","phone_type":"mobile","phone_source":"…",
            "phone_validation":"valid","phone_waterfall_log":"…"}}'`.
Advance not-found contacts too (`phone_source = not_found`). This is the final stage —
after it, the list is campaign-ready for `export` / `activate`.

## Reporting
Per-step found/validated counts, phone-type breakdown (mobile/direct/switchboard),
source distribution, credit estimate, and the count advanced to `phone_enriched`.

## Guardrails
- Always validate before accepting (unless no validator is keyed — then flag).
- Prefer mobile; never accept a switchboard silently — flag it.
- Use the single match endpoint for Apollo phones; never set `reveal_phone_number:true`
  without a webhook.

## Cross-stage invariants
1. Member-ID LinkedIn URLs don't match → `match_key_priority` name+domain fallback.
2. Empty domain fails enricher batches → exclude (never fail) empty-domain rows.
3. Responses are nested/quirky → trust the manifest `field_map`/adapter output
   (FullEnrich phone is a `{number, region}` dict).
