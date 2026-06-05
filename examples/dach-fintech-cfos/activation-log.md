# Activation log — push to lemlist (Stage 5)

> Synthetic example. No real campaign was created.

## Gate #4 — activation (always on)

```
Activation summary — list dach-fintech-cfos (list_id 1)
  sequencer:        lemlist  (LEMLIST_API_KEY set)
  campaign name:    "RegLedger — DACH fintech CFOs (Q2 2026)"
  leads to import:  4   (from export at min_stage = phone_enriched)
  lacking email/li: 0
  steps:            1 (email)
  options:          dedupe=true, verify=true

Proceed? [y/N]  y
```

## Pre-push CRM suppression (hubspot crm_dedupe, between stages 3 → 4)

```
$ python3 providers/hubspot/adapter.py --capability crm_dedupe \
    --input '{"object":"contact","values":["lena.hoffmann@nimbuspay.example", ... ]}'
{ "object":"contact", "checked":4, "found":0,
  "matches": { "lena.hoffmann@nimbuspay.example": {"exists":false}, ... } }
→ 0 already in CRM. (Rheinmetrik was already dropped at the company-level dedupe + exclusions.)
```

## sequencer_push result (providers/lemlist/adapter.py)

```json
{
  "provider": "lemlist",
  "capability": "sequencer_push",
  "campaign_id": "cam_EXAMPLE9aQ2",
  "sequence_id": "seq_EXAMPLE7hPj",
  "imported": 4,
  "skipped_existing": 0,
  "skipped_no_identity": 0,
  "failed": 0,
  "errors": []
}
```

## Result

```
✓ Campaign created: "RegLedger — DACH fintech CFOs (Q2 2026)"  (cam_EXAMPLE9aQ2)
  Imported 4 leads · 0 already in another campaign · 0 failed
  Durable artifact: output/export.csv

Next: connect a sending mailbox + set the schedule in lemlist to start sending.
```
