# Capabilities & canonical records

The framework's agents speak only in **capabilities** (abstract verbs). Each provider
manifest declares which capabilities it supports and maps its native request/response
shapes to the canonical contracts below. Swapping a provider = changing a waterfall in
`gtm.config.yaml`; no agent edits.

## Capability taxonomy

| Capability | Stage | Input (canonical) | Output (canonical) |
|---|---|---|---|
| `company_search` | 0 discovery | `{keywords[], industries[], geographies[], employee_range, seed_companies[], exclude_domains[], page, page_size}` | `Company[]` + `{total, page, has_more, cost}` |
| `company_enrich` | 0.5 account intel | `{companies[]{name,domain}, custom_fields[]}` | per-company `{intel{...}, sources[], verified}` (account-level) |
| `people_search` | 1 sourcing | `{companies[]{name,domain}, titles[], seniorities[], geographies[], page, page_size}` | `Contact[]` (identity only) + `{total, has_more, cost, provider_ids{}}` |
| `email_enrich` | 3 | `Contact[]` (linkedin_url?, first/last, company_domain, provider_ids?) | per-contact `{email, email_status, source, raw_validation, resolved_linkedin_url?, resolved_last_name?}` |
| `email_validate` | 3 (gate) | `{email, domain}` | `{status: deliverable\|catch_all\|invalid\|unknown}` |
| `phone_enrich` | 4 | `Contact[]` | per-contact `{phone (E.164), phone_type, source, raw_validation}` |
| `phone_validate` | 4 | `{number, country_code}` | `{status: valid\|invalid, line_type?}` |
| `crm_dedupe` | opt. (0→0.5, 3→4) | `{object: company\|contact, values[] (domains or emails)}` | `{matches{value: {exists, id, name?}}}` (read-only CRM suppression) |
| `sequencer_push` | 5 activate | `{campaign{name, steps[]?}, leads[], options{dedupe, verify}}` | `{campaign_id, sequence_id?, imported, skipped_existing, failed}` |
| `linkedin_url_lookup` | 2 (opt) | `{first_name, last_name?, company, title?}` | `{linkedin_url?, confidence}` |

Notes:
- `company_search` and `people_search` are distinct verbs; a provider with no native
  org search can synthesize `company_search` by aggregating unique companies from
  `people_search` (the discovery agent does this).
- `email_validate` / `phone_validate` are separate so validate-only providers are
  first-class; an all-in-one enricher can declare both `*_enrich` and `*_validate` so
  the framework skips the extra call.
- `email_enrich` output carries `resolved_linkedin_url` + `resolved_last_name` because
  a match step often returns these as bonus data that downstream stages depend on.

## Canonical records

The pipeline stores one canonical shape so the storage backend needs no remapping.

```jsonc
// Canonical Company  (identity from discovery; `intel` added by company_enrich)
{ "company_name", "company_domain", "linkedin_url", "country",
  "estimated_employees", "industry", "source",            // provider name
  "discovery_reason", "db_status",                          // new | contacted | engaged
  "intel": {                                               // account intel (company_enrich)
    "founded_year", "hq_location", "description", "sub_industry",
    "funding_stage", "total_raised", "last_round", "investors": [],
    "tech_stack": [], "leadership": [{ "name", "title" }],
    "signals": [],                                          // hiring / funding / "why now"
    "custom": {} },                                         // user-defined fields
  "sources": [],                                            // evidence URLs
  "verified": false, "enriched": false }

// Canonical Contact (superset; stage determines which fields are populated)
{ "first_name", "last_name", "full_name", "title", "seniority", "department",
  "company_name", "company_domain", "linkedin_url", "country", "location",
  "source",                                                 // provider that found it
  "provider_ids": { "apollo": "...", "dropleads": 123 },    // generalizes a single apollo_id
  "lead_quality_score",
  "email", "email_source", "email_validation", "email_waterfall_log",
  "phone", "phone_type", "phone_source", "phone_validation", "phone_waterfall_log" }
```

Storage adds: `id` (per-list), `stage`
(`sourced → qualified → email_enriched → phone_enriched`, plus `skipped`),
`linkedin_url_normalized`, and per-stage timestamps. Qualification fields
(`qualification_status/score`, `matched_persona`, `company_segment`,
`qualification_notes`) are populated at the `qualified` stage.

## Stage handoff (storage ops)

Agents call `storage/cli.py` — never raw SQL/file IO:

| Op | Input | Output |
|---|---|---|
| `create_list` | `{name, description?, search_criteria?}` | `{list_id}` |
| `upsert_contacts` | `{list_id, contacts[]}` | `{inserted, skipped_duplicates, total}` |
| `advance_stage` | `{list_id, contact_ids[], stage, fields?}` | `{updated, not_found[]}` |
| `query_by_stage` | `{list_id, stage}` | `{contacts[]}` |
| `list_summary` | `{list_id?}` | `{lists[]}` (per-stage counts) |
| `export` | `{list_id, min_stage?, out_path?}` | `{rows[], path, count}` |
| `crossref_master` | `{linkedin_urls[]}` | `{statuses{url: new\|…}}` |
| `upsert_companies` | `{list_id, companies[]}` | `{inserted, updated, total}` (insert-or-merge on domain) |
| `query_companies` | `{list_id}` | `{companies[]}` |

Companies are deduped/merged on `normalize_domain` (lowercase, drop protocol/`www.`/path/
trailing dot) — byte-identical between `storage/cli.py` and `storage/postgres/schema.sql`.
`upsert_companies` merges new `intel` into an existing company rather than duplicating it,
so discovery (identity) and `company_enrich` (intel) write to the same record.

Dedup is on `linkedin_url_normalized` — and that normalization is **byte-identical**
between the local backend (`storage/cli.py: normalize_linkedin_url`) and Postgres
(`storage/postgres/schema.sql: normalize_linkedin_url()`): strip the query string,
strip one trailing slash, lowercase. Rows with a null normalized URL are never deduped
(matching the Postgres partial unique index), so agents dedup null-URL providers
(e.g. Apollo search) in-memory on name+company before upsert.
