#!/usr/bin/env python3
"""
providers/firecrawl/adapter.py — Firecrawl as the company_enrich "eyes" (and an optional
turnkey extractor). Firecrawl v2 (https://docs.firecrawl.dev). stdlib only; Bearer key
from FIRECRAWL_API_KEY. Canonical JSON to stdout, logs to stderr.

Modes (the enrich-companies workflow's subagents call `scrape`/`search`; `company_enrich`
is a turnkey alternative that lets Firecrawl's own extractor build the intel):

    --capability scrape         --input '{"url":"https://acme.com"}'
    --capability search         --input '{"query":"acme.com funding","limit":5}'
    --capability company_enrich --input '{"companies":[{"name":"Acme","domain":"acme.com"}],"custom_fields":[]}'
    ... --estimate

Claude remains the enrichment brain by default — Firecrawl is better page content, not a
second LLM. The `company_enrich` mode is offered for users who want Firecrawl's extractor
to do the structuring instead.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.firecrawl.dev/v2"
EXTRACT_POLL_INTERVAL = 5
EXTRACT_MAX_POLL = 120


def log(m):
    print(m, file=sys.stderr)


def _req(method, path, key, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") if e.fp else "")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _key():
    return os.environ.get("FIRECRAWL_API_KEY")


# --------------------------------------------------------------------------- modes
def scrape(inp):
    url = inp.get("url")
    if not url:
        return {"error": {"type": "bad_input", "message": "url required"}}
    formats = inp.get("formats") or ["markdown"]
    code, body = _req("POST", "/scrape", _key(), {"url": url, "formats": formats})
    if code != 200:
        return {"error": {"type": "scrape_failed", "status": code, "body": body[:300]}}
    data = (json.loads(body).get("data") or {})
    return {"provider": "firecrawl", "capability": "scrape", "url": url,
            "markdown": data.get("markdown", ""), "metadata": data.get("metadata", {})}


def search(inp):
    query = inp.get("query")
    if not query:
        return {"error": {"type": "bad_input", "message": "query required"}}
    code, body = _req("POST", "/search", _key(),
                      {"query": query, "limit": inp.get("limit", 5)})
    if code != 200:
        return {"error": {"type": "search_failed", "status": code, "body": body[:300]}}
    data = json.loads(body).get("data") or []
    results = [{"url": r.get("url"), "title": r.get("title"), "markdown": r.get("markdown")}
               for r in (data if isinstance(data, list) else [])]
    return {"provider": "firecrawl", "capability": "search", "query": query, "results": results}


INTEL_SCHEMA = {
    "type": "object",
    "properties": {
        "founded_year": {"type": "string"}, "hq_location": {"type": "string"},
        "description": {"type": "string"}, "estimated_employees": {"type": "string"},
        "industry": {"type": "string"}, "sub_industry": {"type": "string"},
        "funding_stage": {"type": "string"}, "total_raised": {"type": "string"},
        "investors": {"type": "array", "items": {"type": "string"}},
        "tech_stack": {"type": "array", "items": {"type": "string"}},
        "signals": {"type": "array", "items": {"type": "string"}},
    },
}


def company_enrich(inp, estimate):
    companies = inp.get("companies", [])
    targets = [(c, (c.get("domain") or c.get("company_domain"))) for c in companies if isinstance(c, dict)]
    targets = [(c, d) for c, d in targets if d]
    if estimate:
        return {"provider": "firecrawl", "capability": "company_enrich",
                "companies": len(targets), "note": "Firecrawl /extract credits per company"}
    if not targets:
        return {"provider": "firecrawl", "capability": "company_enrich", "rows": []}

    key = _key()
    custom = inp.get("custom_fields") or []
    prompt = ("Extract factual company intel: what they do, HQ, founded year, employee "
              "count, industry, funding stage + total raised + investors, tech stack, and "
              "recent buying/expansion signals. " + (f"Also: {'; '.join(custom)}. " if custom else "")
              + "Leave a field empty if not found; do not guess.")

    rows = []
    for c, dom in targets:
        url = dom if dom.startswith("http") else f"https://{dom}"
        code, body = _req("POST", "/extract", key,
                          {"urls": [url], "prompt": prompt, "schema": INTEL_SCHEMA})
        if code != 200:
            log(f"  extract submit failed for {dom}: {code}")
            continue
        try:
            job = json.loads(body)
        except Exception:  # noqa: BLE001
            continue
        intel = job.get("data")
        job_id = job.get("id")
        # poll if async
        elapsed = 0
        while intel is None and job_id and elapsed < EXTRACT_MAX_POLL:
            time.sleep(EXTRACT_POLL_INTERVAL)
            elapsed += EXTRACT_POLL_INTERVAL
            sc, sb = _req("GET", f"/extract/{job_id}", key)
            if sc == 200:
                j = json.loads(sb)
                if (j.get("status") or "").lower() in ("completed", "success") or j.get("data"):
                    intel = j.get("data")
                    break
        rows.append({
            "company_name": c.get("name") or c.get("company_name") or dom,
            "company_domain": dom,
            "intel": intel or {},
            "sources": [url],
            "verified": False,      # Firecrawl extract is not source-verified like the Claude pass
            "enriched": True,
        })
    return {"provider": "firecrawl", "capability": "company_enrich", "rows": rows}


def main():
    p = argparse.ArgumentParser(description="Firecrawl adapter")
    p.add_argument("--capability", required=True, choices=["scrape", "search", "company_enrich"])
    p.add_argument("--input")
    p.add_argument("--input-file")
    p.add_argument("--estimate", action="store_true")
    args = p.parse_args()

    raw = args.input if args.input is not None else (
        open(args.input_file, encoding="utf-8").read() if args.input_file else sys.stdin.read())
    try:
        inp = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(e)}})); sys.exit(2)

    if args.capability != "company_enrich" or not args.estimate:
        if not _key():
            print(json.dumps({"error": {"type": "missing_key", "message": "FIRECRAWL_API_KEY not set"}}))
            sys.exit(1)

    if args.capability == "scrape":
        result = scrape(inp)
    elif args.capability == "search":
        result = search(inp)
    else:
        result = company_enrich(inp, args.estimate)

    print(json.dumps(result, ensure_ascii=False))
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
