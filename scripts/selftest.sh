#!/usr/bin/env bash
# selftest.sh — no-network smoke test of the framework's deterministic parts:
# storage round-trip + dedup parity, adapter --estimate paths, and plan resolution.
# Exits non-zero on the first failed assertion. Requires python3 (and pyyaml for show-plan).

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

PASS=0; FAIL=0
ok()   { printf '\033[32mok\033[0m   %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '\033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
# assert <description> <actual> <expected>
eq()   { if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (got '$2', want '$3')"; fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
D="$TMP/.gtm-data"
CLI() { python3 storage/cli.py "$1" --backend local --dir "$D" --input "$2"; }
jq1() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

echo "== storage: local round-trip + dedup parity =="
CLI create_list '{"name":"selftest"}' >/dev/null
UP=$(CLI upsert_contacts '{"list_id":1,"contacts":[
  {"first_name":"Max","company_domain":"acme.de","linkedin_url":"https://www.linkedin.com/in/max/"},
  {"first_name":"Max2","company_domain":"acme.de","linkedin_url":"https://www.linkedin.com/in/max?x=1"},
  {"first_name":"Jane","company_domain":"globex.com","linkedin_url":"https://linkedin.com/in/jane"},
  {"first_name":"N1"},
  {"first_name":"N2"}
]}')
eq "upsert inserted"  "$(echo "$UP" | jq1 "['inserted']")" "4"
eq "upsert skipped"   "$(echo "$UP" | jq1 "['skipped_duplicates']")" "1"
CLI advance_stage '{"list_id":1,"contact_ids":[1,3],"stage":"email_enriched","fields":{"email":"a@b.com","email_source":"fullenrich"}}' >/dev/null
SUM=$(CLI list_summary '{"list_id":1}')
eq "summary email_enriched" "$(echo "$SUM" | jq1 "['lists'][0]['email_enriched']")" "2"
EXP=$(CLI export '{"list_id":1,"min_stage":"email_enriched"}')
eq "export rows at email_enriched" "$(echo "$EXP" | jq1 "['count']")" "2"
eq "crossref local all-new" "$(CLI crossref_master '{"linkedin_urls":["https://x/y"]}' | jq1 "['statuses']['https://x/y']")" "new"

echo; echo "== storage: companies insert + merge-on-domain (company_enrich) =="
INS=$(CLI upsert_companies '{"list_id":1,"companies":[{"company_name":"Acme","company_domain":"https://www.acme.com/x","source":"web_research"},{"company_name":"Globex","company_domain":"globex.io"}]}')
eq "companies inserted" "$(echo "$INS" | jq1 "['inserted']")" "2"
MRG=$(CLI upsert_companies '{"list_id":1,"companies":[{"company_domain":"acme.com","intel":{"funding_stage":"Series B"},"verified":true,"enriched":true}]}')
eq "companies merged (updated)" "$(echo "$MRG" | jq1 "['updated']")" "1"
eq "companies total after merge" "$(echo "$MRG" | jq1 "['total']")" "2"
eq "merged intel readable" "$(CLI query_companies '{"list_id":1}' | python3 -c "import sys,json;rows=json.load(sys.stdin)['companies'];a=[r for r in rows if r['company_domain_normalized']=='acme.com'][0];print(a['intel'].get('funding_stage'))")" "Series B"

echo; echo "== adapters: --estimate (no spend, no network) =="
ENR='{"contacts":[{"id":1,"first_name":"A","last_name":"B","company_domain":"acme.de","linkedin_url":"https://linkedin.com/in/a"},{"id":2,"first_name":"C","last_name":"D","company_domain":""}]}'
eq "fullenrich email estimate credits" \
  "$(python3 providers/fullenrich/adapter.py --capability email_enrich --estimate --input "$ENR" | jq1 "['cost_estimate_credits']")" "1"
eq "fullenrich phone estimate credits" \
  "$(python3 providers/fullenrich/adapter.py --capability phone_enrich --estimate --input "$ENR" | jq1 "['cost_estimate_credits']")" "10"
eq "fullenrich excludes empty-domain" \
  "$(python3 providers/fullenrich/adapter.py --capability email_enrich --estimate --input "$ENR" | jq1 "['excluded_empty_domain']")" "1"
eq "ai-ark email estimate credits" \
  "$(python3 providers/ai-ark/adapter.py --capability email_enrich --estimate --input "$ENR" | jq1 "['cost_estimate_credits']")" "2"
eq "ai-ark phone addressable (vanity URL ok; empty key excluded)" \
  "$(python3 providers/ai-ark/adapter.py --capability phone_enrich --estimate --input "$ENR" | jq1 "['addressable']")" "1"
eq "ai-ark email requires track_id (run mode errors clean)" \
  "$(python3 providers/ai-ark/adapter.py --capability email_enrich --input '{"contacts":[{"id":1}]}' | jq1 "['error']['type']")" "missing_track_id"
eq "lemlist sequencer estimate leads" \
  "$(python3 providers/lemlist/adapter.py --capability sequencer_push --estimate --input '{"campaign":{"name":"t"},"leads":[{"email":"a@b.com"},{"first_name":"x"}]}' | jq1 "['leads_to_import']")" "1"
eq "apify estimate (skip-resolve)" \
  "$(python3 providers/apify/adapter.py --capability people_search --skip-resolve --estimate --input '{"companies":["x.com"],"titles":["CFO"],"max":50}' | jq1 "['companies']")" "1"
eq "firecrawl company_enrich estimate" \
  "$(python3 providers/firecrawl/adapter.py --capability company_enrich --estimate --input '{"companies":[{"name":"Acme","domain":"acme.com"},{"name":"Globex","domain":"globex.io"}]}' | jq1 "['companies']")" "2"

echo; echo "== providers: csv loader + single-provider manifest =="
printf 'company,website,segment\nAcme,acme.com,A\nGlobex,https://globex.io/,B\n,,skip\n' > "$TMP/co.csv"
eq "csv-companies count" "$(python3 scripts/csv-companies.py --file "$TMP/co.csv" | jq1 "['count']")" "2"
if python3 -c "import yaml" 2>/dev/null; then
  eq "prospeo single-provider caps" \
    "$(python3 -c "import yaml;print(','.join(sorted(yaml.safe_load(open('providers/prospeo/manifest.yaml'))['capabilities'])))")" \
    "company_enrich,company_search,email_enrich,people_search,phone_enrich"
  eq "leadmagic single-provider caps" \
    "$(python3 -c "import yaml;print(','.join(sorted(yaml.safe_load(open('providers/leadmagic/manifest.yaml'))['capabilities'])))")" \
    "company_enrich,company_search,email_enrich,email_validate,linkedin_url_lookup,people_search,phone_enrich"
  eq "ai-ark single-provider caps" \
    "$(python3 -c "import yaml;print(','.join(sorted(yaml.safe_load(open('providers/ai-ark/manifest.yaml'))['capabilities'])))")" \
    "company_enrich,company_search,email_enrich,linkedin_url_lookup,people_search,phone_enrich"
fi
eq "hubspot crm_dedupe estimate (company)" \
  "$(python3 providers/hubspot/adapter.py --capability crm_dedupe --estimate --input '{"object":"company","values":["acme.com","globex.io"]}' | jq1 "['checked']")" "2"
eq "hubspot crm_dedupe estimate (contact)" \
  "$(python3 providers/hubspot/adapter.py --capability crm_dedupe --estimate --input '{"object":"contact","values":["a@b.com"]}' | jq1 "['object']")" "contact"

echo; echo "== compile all python =="
if python3 -m py_compile storage/cli.py providers/*/adapter.py scripts/show-plan.py 2>/dev/null; then
  ok "py_compile"; else bad "py_compile"; fi

echo; echo "== bundled workflow syntax (if node present) =="
if command -v node >/dev/null 2>&1; then
  for w in .claude/workflows/*.js; do
    if node --check "$w" 2>/dev/null; then ok "$(basename "$w") syntax"; else bad "$(basename "$w") syntax"; fi
  done
else
  echo "  (node not installed — skipping workflow syntax check)"
fi

echo; echo "== show-plan resolves (builtin always-on; missing key skipped) =="
if python3 -c "import yaml" 2>/dev/null; then
  PLAN=$(APOLLO_API_KEY=x FULLENRICH_API_KEY=x python3 scripts/show-plan.py --config gtm.config.example.yaml 2>/dev/null)
  echo "$PLAN" | grep -q "company_search  : web_research" && ok "builtin web_research resolved" || bad "web_research not resolved"
  echo "$PLAN" | grep -q "email_enrich    : apollo -> fullenrich" && ok "email waterfall resolved" || bad "email waterfall wrong"
else
  echo "  (pyyaml not installed — skipping show-plan check)"
fi

echo
echo "----------------------------------------"
printf 'selftest: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
