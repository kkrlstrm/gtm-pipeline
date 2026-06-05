#!/usr/bin/env python3
"""
providers/lemlist/adapter.py — lemlist sequencer push (the `activate` stage target).

    python3 providers/lemlist/adapter.py --capability sequencer_push --input '<JSON>'
    ... --estimate                                  # plan only, no writes

Input (canonical sequencer_push):
  {
    "campaign": { "name": "...", "steps": [ {type, subject?, message?, delay?}, ... ] },
    "leads":    [ export rows: {first_name,last_name,company_name,company_domain,
                                linkedin_url,title,email,phone, ...}, ... ],
    "options":  { "dedupe": true, "verify": true }
  }
Output:
  { "provider":"lemlist","capability":"sequencer_push",
    "campaign_id","sequence_id","imported","skipped_existing","failed","errors":[...] }

Uses `curl` via subprocess (lemlist blocks Python urllib with HTTP 403). stdlib only;
secret from LEMLIST_API_KEY (HTTP Basic, empty username). Logs to stderr.
"""

import argparse
import json
import os
import subprocess
import sys
import time

API = "https://api.lemlist.com/api"
RATE_SLEEP_EVERY = 10      # requests
RATE_SLEEP_SECS = 1.0

# canonical export row -> lemlist lead field
LEAD_MAP = {
    "email": "email", "first_name": "firstName", "last_name": "lastName",
    "company_name": "companyName", "title": "jobTitle", "linkedin_url": "linkedinUrl",
    "phone": "phone", "company_domain": "companyDomain",
}


def log(m):
    print(m, file=sys.stderr)


def _curl(method, url, key, body=None):
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method, url,
           "-u", f":{key}", "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stdout
    nl = out.rfind("\n")
    if nl < 0:
        return 0, out
    code = int(out[nl + 1:].strip() or 0)
    return code, out[:nl]


def to_lead(row):
    lead = {}
    for canon, lem in LEAD_MAP.items():
        v = row.get(canon)
        if v:
            lead[lem] = v
    return lead


def run(inp, estimate):
    campaign = inp.get("campaign", {}) or {}
    name = campaign.get("name") or "gtm-pipeline campaign"
    steps = campaign.get("steps", []) or []
    leads = inp.get("leads", []) or []
    options = inp.get("options", {}) or {}

    # only leads with a usable identity (email or linkedin)
    usable = [r for r in leads if r.get("email") or r.get("linkedin_url")]
    skipped_no_identity = len(leads) - len(usable)

    if estimate:
        return {"provider": "lemlist", "capability": "sequencer_push",
                "would_create_campaign": name, "steps": len(steps),
                "leads_to_import": len(usable),
                "skipped_no_identity": skipped_no_identity}

    key = os.environ.get("LEMLIST_API_KEY")
    if not key:
        return {"error": {"type": "missing_key", "message": "LEMLIST_API_KEY not set"}}

    # 1) create campaign -> _id + sequenceId
    code, body = _curl("POST", f"{API}/campaigns", key, {"name": name})
    if code not in (200, 201):
        return {"error": {"type": "create_campaign_failed", "status": code, "body": body[:300]}}
    try:
        j = json.loads(body)
        campaign_id = j.get("_id")
        sequence_id = j.get("sequenceId")
    except Exception as e:  # noqa: BLE001
        return {"error": {"type": "bad_create_response", "message": str(e), "body": body[:300]}}
    log(f"created campaign {campaign_id} (sequence {sequence_id})")

    # 2) add steps to the SEQUENCE (not the campaign) — 405 otherwise
    if sequence_id and steps:
        for i, step in enumerate(steps):
            sc, sb = _curl("POST", f"{API}/sequences/{sequence_id}/steps", key, step)
            if sc not in (200, 201):
                log(f"  step {i} failed: http {sc} {sb[:150]}")
            if (i + 1) % RATE_SLEEP_EVERY == 0:
                time.sleep(RATE_SLEEP_SECS)

    # 3) import leads (dedupe/verify as query params)
    q = []
    if options.get("dedupe", True):
        q.append("deduplicate=true")
    if options.get("verify"):
        q.append("verifyEmail=true")
    qs = ("?" + "&".join(q)) if q else ""
    leads_url = f"{API}/campaigns/{campaign_id}/leads/{qs}"

    imported, skipped_existing, failed, errors = 0, 0, 0, []
    for n, row in enumerate(usable):
        lead = to_lead(row)
        lc, lb = _curl("POST", leads_url, key, lead)
        low = lb.lower()
        if lc in (200, 201) and "already in the campaign" not in low:
            imported += 1
        elif "already in the campaign" in low:
            skipped_existing += 1            # expected — in another active campaign
        else:
            failed += 1
            if len(errors) < 20:
                errors.append({"lead": lead.get("email") or lead.get("linkedinUrl"),
                               "status": lc, "body": lb[:150]})
        if (n + 1) % RATE_SLEEP_EVERY == 0:
            time.sleep(RATE_SLEEP_SECS)

    return {"provider": "lemlist", "capability": "sequencer_push",
            "campaign_id": campaign_id, "sequence_id": sequence_id,
            "imported": imported, "skipped_existing": skipped_existing,
            "skipped_no_identity": skipped_no_identity, "failed": failed,
            "errors": errors}


def load_input(args):
    if args.input is not None:
        raw = args.input
    elif args.input_file is not None:
        with open(args.input_file, encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    return json.loads(raw.strip() or "{}")


def main():
    p = argparse.ArgumentParser(description="lemlist sequencer adapter")
    p.add_argument("--capability", required=True, choices=["sequencer_push"])
    p.add_argument("--input")
    p.add_argument("--input-file")
    p.add_argument("--estimate", action="store_true")
    args = p.parse_args()
    try:
        inp = load_input(args)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(e)}}))
        sys.exit(2)
    result = run(inp, args.estimate)
    print(json.dumps(result, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
