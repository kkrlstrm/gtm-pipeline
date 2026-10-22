#!/usr/bin/env python3
"""
providers/alphaosint/adapter.py — people_search via johnvc/linkedin-company-employees-api.

Uniform CLI contract (mirrors providers/apify/adapter.py):

    python3 providers/alphaosint/adapter.py --capability people_search --input '<JSON>'
    ... --estimate          # batch counts only, no run, no invented dollar amount
    ... --resolve-only      # echo company names/URLs (Actor accepts both)

Canonical input (people_search):
  { "companies": [ {"name": "...", "domain": "..."} | "Acme GmbH" | "<linkedin url>" ],
    "titles": [...], "seniorities": ["Director","VP","CxO","Manager","Senior","Owner"],
    "geographies": ["Germany", ...],
    "max": 100 }

Canonical output:
  { "provider":"alphaosint","capability":"people_search",
    "contacts":[ {first_name,last_name,full_name,title,company_name,linkedin_url,location,source,meta?} ],
    "meta": { "resolved": [...], "failed": [...], "count": N, "batches": N } }

stdlib only. Secret from APIFY_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ACTOR_ID = "johnvc~linkedin-company-employees-api"
BASE_URL = "https://api.apify.com/v2"
COMPANY_BATCH_SIZE = 5
SYNC_MAX_ITEMS = 100
POLL_INTERVAL = 5
MAX_POLL_TIME = 900
MAX_TITLE_KEYWORDS = 20
MAX_LOCATIONS = 10

# Canonical seniority -> title words the Actor can filter on (no seniority IDs).
SENIORITY_TITLE_WORDS: dict[str, list[str]] = {
    "director": ["director"],
    "vp": ["vp", "vice president"],
    "cxo": ["chief", "ceo", "cfo", "cto", "coo", "cmo", "cro"],
    "c-level": ["chief", "ceo", "cfo", "cto", "coo"],
    "c_suite": ["chief", "ceo", "cfo", "cto", "coo"],
    "manager": ["manager"],
    "senior": ["senior"],
    "owner": ["owner"],
    "partner": ["partner"],
    "head": ["head of"],
}


def log(message: str) -> None:
    """Write progress to stderr so stdout stays JSON-only."""
    print(message, file=sys.stderr)


def api_request(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> Any:
    """Call the Apify REST API and return parsed JSON."""
    sep = "&" if "?" in path else "?"
    url = f"{BASE_URL}{path}{sep}token={token}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=360) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode() if exc.fp else ""
        log(f"Apify API error {exc.code}: {err}")
        raise SystemExit(1) from exc


def chunked(items: list[str], size: int) -> list[list[str]]:
    """Split a list into consecutive chunks of at most `size`."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def company_strings(inp: dict[str, Any]) -> list[str]:
    """Normalize companies[] entries to plain strings (name, domain, or URL)."""
    out: list[str] = []
    for company in inp.get("companies", []):
        if isinstance(company, dict):
            value = company.get("domain") or company.get("name") or company.get("linkedin_url") or ""
        else:
            value = str(company)
        value = value.strip()
        if value:
            out.append(value)
    return out


def title_keywords(inp: dict[str, Any]) -> list[str]:
    """Build titleKeywords from titles plus seniority-mapped title words (max 20)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(word: str) -> None:
        key = word.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(word.strip())

    for title in inp.get("titles", []) or []:
        if isinstance(title, str):
            add(title)
    for seniority in inp.get("seniorities", []) or []:
        if not isinstance(seniority, str):
            continue
        for word in SENIORITY_TITLE_WORDS.get(seniority.lower(), [seniority]):
            add(word)
    return out[:MAX_TITLE_KEYWORDS]


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split fullName on the first space (lossy but matches the canonical shape)."""
    if not full_name or not isinstance(full_name, str):
        return None, None
    parts = full_name.strip().split(None, 1)
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def to_contacts(items: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map Actor rows to canonical contacts; collect result_type=error rows."""
    contacts: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        result_type = row.get("result_type") or row.get("resultType")
        if result_type == "error":
            failed.append(row)
            continue
        full_name = row.get("fullName") or row.get("name")
        first_name, last_name = split_name(full_name if isinstance(full_name, str) else None)
        contact: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "title": row.get("currentTitle") or row.get("headline"),
            "company_name": row.get("currentCompany") or row.get("companyName"),
            "linkedin_url": row.get("profileUrl") or row.get("linkedin_url"),
            "location": row.get("location"),
            "source": "alphaosint",
        }
        meta: dict[str, Any] = {}
        if "verified" in row:
            meta["verified"] = row["verified"]
        if row.get("matchReason") is not None:
            meta["matchReason"] = row["matchReason"]
        if meta:
            contact["meta"] = meta
        contacts.append(contact)
    return contacts, failed


def build_actor_input(
    companies: list[str],
    keywords: list[str],
    locations: list[str],
    max_results_per_company: int,
) -> dict[str, Any]:
    """Build one Actor run payload for a company batch."""
    payload: dict[str, Any] = {
        "companies": companies,
        "maxResultsPerCompany": max_results_per_company,
        "verifyEmployment": True,
    }
    if keywords:
        payload["titleKeywords"] = keywords
    if locations:
        payload["locations"] = locations[:MAX_LOCATIONS]
    return payload


def run_sync(payload: dict[str, Any], token: str) -> list[Any]:
    """Run the Actor synchronously and return dataset items."""
    result = api_request(
        "POST",
        f"/acts/{ACTOR_ID}/run-sync-get-dataset-items?format=json",
        body=payload,
        token=token,
    )
    return result if isinstance(result, list) else []


def run_async(payload: dict[str, Any], token: str) -> list[Any]:
    """Start an Actor run, poll until SUCCEEDED, return dataset items."""
    started = api_request("POST", f"/acts/{ACTOR_ID}/runs", body=payload, token=token)
    run_id = started["data"]["id"]
    dataset_id = started["data"]["defaultDatasetId"]
    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        status = api_request("GET", f"/actor-runs/{run_id}", token=token)["data"]["status"]
        log(f"  status: {status} ({elapsed}s)")
        if status == "SUCCEEDED":
            items = api_request("GET", f"/datasets/{dataset_id}/items?format=json", token=token)
            return items if isinstance(items, list) else []
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise SystemExit(1)
    raise SystemExit(1)


def main() -> None:
    """CLI entry: people_search with estimate / resolve-only / run modes."""
    parser = argparse.ArgumentParser(description="Alpha OSINT LinkedIn company-employees adapter")
    parser.add_argument("--capability", required=True, choices=["people_search"])
    parser.add_argument("--input")
    parser.add_argument("--input-file")
    parser.add_argument("--estimate", action="store_true")
    parser.add_argument("--resolve-only", action="store_true")
    args = parser.parse_args()

    raw = (
        args.input
        if args.input is not None
        else (open(args.input_file, encoding="utf-8").read() if args.input_file else sys.stdin.read())
    )
    try:
        inp = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(exc)}}))
        sys.exit(2)

    companies = company_strings(inp)
    if not companies:
        print(json.dumps({"error": {"type": "no_companies", "message": "companies[] required"}}))
        sys.exit(2)

    # Actor accepts names and URLs; resolve-only echoes them for plan inspection.
    if args.resolve_only:
        print(json.dumps({"provider": "alphaosint", "resolved": companies, "failed": []}))
        return

    keywords = title_keywords(inp)
    locations = [g for g in (inp.get("geographies") or []) if isinstance(g, str) and g.strip()]
    total_max = int(inp.get("max") or inp.get("page_size") or 100)
    per_company = max(1, math.ceil(total_max / len(companies)))
    batches = chunked(companies, COMPANY_BATCH_SIZE)

    if args.estimate:
        print(
            json.dumps(
                {
                    "provider": "alphaosint",
                    "capability": "people_search",
                    "companies": len(companies),
                    "batches": len(batches),
                    "max_profiles": total_max,
                    "max_results_per_company": per_company,
                    "title_keywords": keywords,
                    "note": "pay per person returned; see the Store pricing tab",
                }
            )
        )
        return

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print(json.dumps({"error": {"type": "missing_key", "message": "APIFY_TOKEN not set"}}))
        sys.exit(1)

    all_items: list[Any] = []
    for batch in batches:
        payload = build_actor_input(batch, keywords, locations, per_company)
        log(f"running actor for {len(batch)} companies (maxResultsPerCompany={per_company})...")
        if per_company <= SYNC_MAX_ITEMS:
            all_items.extend(run_sync(payload, token))
        else:
            all_items.extend(run_async(payload, token))

    contacts, row_failed = to_contacts(all_items)
    if len(contacts) > total_max:
        contacts = contacts[:total_max]

    print(
        json.dumps(
            {
                "provider": "alphaosint",
                "capability": "people_search",
                "contacts": contacts,
                "meta": {
                    "resolved": companies,
                    "failed": row_failed,
                    "count": len(contacts),
                    "batches": len(batches),
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
