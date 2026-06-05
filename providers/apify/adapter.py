#!/usr/bin/env python3
"""
providers/apify/adapter.py — LinkedIn company-employees people_search via Apify.

Ported from the source pipeline's actor wrapper into the uniform CLI contract:

    python3 providers/apify/adapter.py --capability people_search --input '<JSON>'
    ... --estimate          # cost only, no run
    ... --resolve-only      # resolve company names -> LinkedIn company URLs, then stop

Canonical input (people_search):
  { "companies": [ {"name": "...", "domain": "..."} | "Acme GmbH" | "acme.de" | "<linkedin url>" ],
    "titles": [...], "seniorities": ["Director","VP","CxO","Manager","Senior","Owner"],
    "geographies": ["Germany", ...],   # already country-expanded by the agent
    "max": 100 }
Canonical output:
  { "provider":"apify","capability":"people_search",
    "contacts":[ {first_name,last_name,full_name,title,company_name,linkedin_url,location,source} ],
    "meta": { "resolved": [...], "failed": [...], "count": N } }

Runs the harvestapi/linkedin-company-employees actor. stdlib only. Secret from APIFY_TOKEN.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ACTOR_ID = "harvestapi~linkedin-company-employees"
BASE_URL = "https://api.apify.com/v2"
SYNC_MAX_ITEMS = 100
POLL_INTERVAL = 5
MAX_POLL_TIME = 900

# canonical seniority name -> Apify seniorityLevelId
SENIORITY_IDS = {
    "director": "220", "vp": "300", "cxo": "310", "c-level": "310", "c_suite": "310",
    "manager": "210", "senior": "120", "owner": "320", "partner": "320",
}

LINKEDIN_COMPANY_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/company/([a-zA-Z0-9_-]+)")


def log(m):
    print(m, file=sys.stderr)


# --------------------------------------------------------------------------- HTTP
def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") if e.fp else "")
    except Exception:  # noqa: BLE001
        return 0, ""


def api_request(method, path, body=None, token=None):
    sep = "&" if "?" in path else "?"
    url = f"{BASE_URL}{path}{sep}token={token}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=360) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode() if e.fp else ""
        log(f"Apify API error {e.code}: {err}")
        raise SystemExit(1)


# ------------------------------------------------------------ company URL resolution
def is_linkedin_company_url(s):
    return bool(LINKEDIN_COMPANY_RE.match(s.strip()))


def extract_slug(url):
    m = LINKEDIN_COMPANY_RE.match(url.strip().rstrip("/"))
    return m.group(1) if m else None


def verify_linkedin_url(url):
    slug = extract_slug(url)
    if not slug:
        return "invalid"
    status, _ = http_get(f"https://www.linkedin.com/company/{slug}/",
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    return "valid" if status == 200 else ("invalid" if status == 404 else "uncertain")


def _accept(v):
    return v in ("valid", "uncertain")


def _google_lucky(query):
    encoded = urllib.parse.quote_plus(f"{query} linkedin company")
    url = f"https://www.google.com/search?q={encoded}&btnI=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.url
    except urllib.error.HTTPError as e:
        final_url = getattr(e, "url", "") or (e.headers.get("Location", "") if hasattr(e, "headers") else "")
    except Exception:  # noqa: BLE001
        return None
    if "google.com/url" in final_url:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
        if "q" in params:
            final_url = params["q"][0]
    m = re.search(r"linkedin\.com/company/([a-zA-Z0-9_-]+)", final_url)
    return (m.group(1), f"https://www.linkedin.com/company/{m.group(1)}") if m else None


def _search_ddg(query_str):
    encoded = urllib.parse.quote_plus(query_str)
    status, body = http_get(f"https://html.duckduckgo.com/html/?q={encoded}",
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}, timeout=10)
    if status not in (200, 202):
        return []
    seen, out, skip = set(), [], {"company", "companies", "login", "signup", "about", "jobs", "pulse"}
    for m in re.finditer(r"linkedin\.com/company/([a-zA-Z0-9_-]+)", body):
        slug = m.group(1).rstrip("/")
        if slug in skip or slug in seen:
            continue
        seen.add(slug)
        out.append((slug, f"https://www.linkedin.com/company/{slug}"))
    return out[:5]


def search_linkedin_company(query):
    r = _google_lucky(query)
    if r:
        return [r]
    time.sleep(0.3)
    for q in (f"{query} site:linkedin.com/company", f'"{query}" linkedin company'):
        c = _search_ddg(q)
        if c:
            return c
        time.sleep(0.3)
    return []


def resolve_company(s):
    s = s.strip()
    if is_linkedin_company_url(s):
        slug = extract_slug(s)
        v = verify_linkedin_url(s)
        if _accept(v):
            return f"https://www.linkedin.com/company/{slug}", "url"
        for cs, cu in search_linkedin_company(slug):
            if _accept(verify_linkedin_url(cu)):
                return cu, f"corrected:{slug}->{cs}"
        return None, f"url slug '{slug}' not found"
    if "." in s and " " not in s:                       # looks like a domain
        domain = re.sub(r"^https?://", "", s).split("/")[0]
        name = domain.split(".")[0]
        direct = f"https://www.linkedin.com/company/{re.sub(r'[^a-z0-9-]', '', name.lower())}"
        if _accept(verify_linkedin_url(direct)):
            return direct, f"domain-direct:{domain}"
        for cs, cu in (search_linkedin_company(f"{name} {domain}") or search_linkedin_company(name)):
            if _accept(verify_linkedin_url(cu)):
                return cu, f"domain-search:{domain}->{cs}"
        return None, f"no LinkedIn page for domain {domain}"
    direct = re.sub(r"[^a-z0-9-]", "", s.lower().replace(" ", "-")).strip("-")
    variations = [direct]
    for suf in ("-gmbh", "-ag", "-ltd", "-inc", "-llc", "-sa", "-se", "-plc", "-group"):
        st = direct.removesuffix(suf)
        if st != direct and st:
            variations.append(st)
    if not any(direct.endswith(x) for x in ("-gmbh", "-ag", "-ltd", "-inc")):
        variations.append(f"{direct}-gmbh")
    for slug in variations:
        v = verify_linkedin_url(f"https://www.linkedin.com/company/{slug}")
        if _accept(v):
            return f"https://www.linkedin.com/company/{slug}", f"slug:{slug}"
    cands = search_linkedin_company(s)
    for cs, cu in cands:
        if _accept(verify_linkedin_url(cu)):
            return cu, f"search:{s}->{cs}"
    if cands:
        return cands[0][1], f"search-unverified:{s}->{cands[0][0]}"
    return None, f"could not resolve {s}"


def resolve_all(companies):
    resolved, failed = [], []
    for i, c in enumerate(companies):
        c = c.strip()
        if not c:
            continue
        if i > 0:
            time.sleep(1.5)
        url, method = resolve_company(c)
        if url:
            resolved.append(url)
            log(f"  {c} -> {url} [{method}]")
        else:
            failed.append({"input": c, "reason": method})
            log(f"  {c} -> FAILED ({method})")
    seen, dedup = set(), []
    for u in resolved:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup, failed


# ------------------------------------------------------------------- actor execution
def run_sync(cfg, token):
    return api_request("POST", f"/acts/{ACTOR_ID}/run-sync-get-dataset-items?format=json", body=cfg, token=token)


def run_async(cfg, token):
    result = api_request("POST", f"/acts/{ACTOR_ID}/runs", body=cfg, token=token)
    run_id = result["data"]["id"]
    dataset_id = result["data"]["defaultDatasetId"]
    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        status = api_request("GET", f"/actor-runs/{run_id}", token=token)["data"]["status"]
        log(f"  status: {status} ({elapsed}s)")
        if status == "SUCCEEDED":
            return api_request("GET", f"/datasets/{dataset_id}/items?format=json", token=token)
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise SystemExit(1)
    raise SystemExit(1)


def estimate_cost(cfg):
    mode = cfg.get("profileScraperMode", "Short ($4 per 1k)")
    max_items = cfg.get("maxItems", 25) or 2500
    per = {"Short ($4 per 1k)": 0.004, "Full ($8 per 1k)": 0.008,
           "Full + email search ($12 per 1k)": 0.012}.get(mode, 0.008)
    start = 0.02 * (len(cfg.get("companies", [])) if cfg.get("companyBatchMode") == "one_by_one" else 1)
    return round(start + max_items * per, 2)


# --------------------------------------------------------------- canonical mapping
def to_contacts(items):
    out = []
    for p in items:
        if not isinstance(p, dict):
            continue
        positions = p.get("currentPositions") or []
        pos0 = positions[0] if positions else {}
        fn, ln = p.get("firstName"), p.get("lastName")
        out.append({
            "first_name": fn,
            "last_name": ln,
            "full_name": " ".join(x for x in (fn, ln) if x) or p.get("name"),
            "title": pos0.get("title") or p.get("headline"),
            "company_name": pos0.get("companyName"),
            "linkedin_url": p.get("linkedinUrl"),
            "location": (p.get("location") or {}).get("linkedinText"),
            "source": "apify",
        })
    return out


def build_config(inp):
    companies = []
    for c in inp.get("companies", []):
        if isinstance(c, dict):
            companies.append(c.get("domain") or c.get("name") or "")
        else:
            companies.append(str(c))
    companies = [c for c in companies if c]
    seniorities = [SENIORITY_IDS[s.lower()] for s in inp.get("seniorities", []) if s.lower() in SENIORITY_IDS]
    cfg = {
        "companies": companies,
        "jobTitles": inp.get("titles", []),
        "maxItems": inp.get("max") or inp.get("page_size") or 100,
        "profileScraperMode": "Short ($4 per 1k)",
    }
    if seniorities:
        cfg["seniorityLevelIds"] = seniorities
    if inp.get("geographies"):
        cfg["locations"] = inp["geographies"]
    return cfg


def main():
    p = argparse.ArgumentParser(description="Apify LinkedIn company-employees adapter")
    p.add_argument("--capability", required=True, choices=["people_search"])
    p.add_argument("--input")
    p.add_argument("--input-file")
    p.add_argument("--estimate", action="store_true")
    p.add_argument("--resolve-only", action="store_true")
    p.add_argument("--skip-resolve", action="store_true")
    args = p.parse_args()

    raw = args.input if args.input is not None else (
        open(args.input_file, encoding="utf-8").read() if args.input_file else sys.stdin.read())
    try:
        inp = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(e)}}))
        sys.exit(2)

    cfg = build_config(inp)
    if not cfg["companies"]:
        print(json.dumps({"error": {"type": "no_companies", "message": "companies[] required"}}))
        sys.exit(2)

    # Resolve company names/domains -> verified LinkedIn company URLs
    if args.skip_resolve:
        resolved, failed = cfg["companies"], []
    else:
        log(f"resolving {len(cfg['companies'])} companies...")
        resolved, failed = resolve_all(cfg["companies"])

    if args.resolve_only:
        print(json.dumps({"provider": "apify", "resolved": resolved, "failed": failed}))
        return
    if not resolved:
        print(json.dumps({"error": {"type": "no_resolved_companies"}, "failed": failed}))
        sys.exit(1)
    cfg["companies"] = resolved

    cost = estimate_cost(cfg)
    if args.estimate:
        print(json.dumps({"provider": "apify", "capability": "people_search",
                          "cost_estimate_usd": cost, "companies": len(resolved),
                          "max_profiles": cfg["maxItems"], "failed": failed}))
        return

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print(json.dumps({"error": {"type": "missing_key", "message": "APIFY_TOKEN not set"}}))
        sys.exit(1)

    log(f"estimated cost ${cost}; running actor...")
    items = run_sync(cfg, token) if cfg["maxItems"] <= SYNC_MAX_ITEMS else run_async(cfg, token)
    contacts = to_contacts(items)
    print(json.dumps({"provider": "apify", "capability": "people_search",
                      "contacts": contacts,
                      "meta": {"resolved": resolved, "failed": failed, "count": len(contacts)}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
