#!/usr/bin/env python3
"""
providers/hubspot/adapter.py — read-only HubSpot CRM dedupe (capability crm_dedupe).

Checks whether companies (by domain) or contacts (by email) ALREADY EXIST in your HubSpot
CRM, so list-building can suppress accounts/people you already have. Custom read API — no
MCP needed. stdlib only; private-app token from HUBSPOT_TOKEN (Bearer).

    python3 providers/hubspot/adapter.py --capability crm_dedupe \
        --input '{"object":"company","values":["acme.com","globex.io"]}'
    python3 providers/hubspot/adapter.py --capability crm_dedupe \
        --input '{"object":"contact","values":["jane@acme.com"]}'
    ... --estimate     # counts only, no calls

Output:
  { "object":"company", "checked":N, "found":M,
    "matches": { "acme.com": {"exists":true,"id":"123","name":"Acme"},
                 "globex.io": {"exists":false} } }

READ-ONLY by design: it only searches. It never writes to your CRM.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://api.hubapi.com"
SEARCH = {
    "company": {"path": "/crm/v3/objects/companies/search", "prop": "domain", "name_prop": "name"},
    "contact": {"path": "/crm/v3/objects/contacts/search", "prop": "email", "name_prop": "firstname"},
}
CHUNK = 100          # HubSpot search IN filter / page size
MAX_PAGES = 50


def log(m):
    print(m, file=sys.stderr)


def _post(path, token, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") if e.fp else "")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def crm_dedupe(inp, estimate):
    obj = inp.get("object", "company")
    if obj not in SEARCH:
        return {"error": {"type": "bad_input", "message": "object must be 'company' or 'contact'"}}
    values = [str(v).strip().lower() for v in inp.get("values", []) if str(v).strip()]
    values = list(dict.fromkeys(values))           # de-dupe inputs, preserve order
    cfg = SEARCH[obj]

    if estimate:
        return {"object": obj, "checked": len(values),
                "note": f"would search HubSpot {obj}s by {cfg['prop']} (read-only, free within API limits)"}

    matches = {v: {"exists": False} for v in values}
    if not values:
        return {"object": obj, "checked": 0, "found": 0, "matches": matches}

    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        return {"error": {"type": "missing_key", "message": "HUBSPOT_TOKEN not set"}}

    found = 0
    for i in range(0, len(values), CHUNK):
        chunk = values[i:i + CHUNK]
        after = None
        for _ in range(MAX_PAGES):
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": cfg["prop"], "operator": "IN", "values": chunk}]}],
                "properties": [cfg["prop"], cfg["name_prop"]],
                "limit": CHUNK,
            }
            if after:
                body["after"] = after
            status, raw = _post(cfg["path"], token, body)
            if status != 200:
                return {"error": {"type": "search_failed", "status": status, "body": raw[:300]},
                        "object": obj, "matches": matches}
            data = json.loads(raw)
            for r in data.get("results", []):
                props = r.get("properties") or {}
                val = (props.get(cfg["prop"]) or "").strip().lower()
                if val in matches and not matches[val]["exists"]:
                    matches[val] = {"exists": True, "id": r.get("id"),
                                    "name": props.get(cfg["name_prop"])}
                    found += 1
            after = (((data.get("paging") or {}).get("next") or {}).get("after"))
            if not after:
                break

    return {"object": obj, "checked": len(values), "found": found, "matches": matches}


def main():
    p = argparse.ArgumentParser(description="HubSpot CRM dedupe adapter")
    p.add_argument("--capability", required=True, choices=["crm_dedupe"])
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

    result = crm_dedupe(inp, args.estimate)
    print(json.dumps(result, ensure_ascii=False))
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
