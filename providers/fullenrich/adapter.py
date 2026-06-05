#!/usr/bin/env python3
"""
providers/fullenrich/adapter.py — FullEnrich email enrichment adapter.

Implements the uniform thin-script CLI contract (docs/writing-a-provider.md):

    python3 providers/fullenrich/adapter.py --capability email_enrich \
        --input '{"contacts": [ ... ]}'        # or --input-file PATH, or stdin
    python3 providers/fullenrich/adapter.py --capability email_enrich \
        --input '{...}' --estimate              # cost only, no spend

- Canonical JSON to STDOUT; logs/progress to STDERR.
- Secrets from the environment only (FULLENRICH_API_KEY). Never embedded, never fetched.
- stdlib only (no pip installs) so it can be fetched-and-piped.

Input  (canonical):  {"contacts": [Contact, ...], "options": {...}}
  Each Contact: {id?, first_name, last_name, company_domain, linkedin_url?, ...}
Output (canonical, run mode):
  {
    "provider": "fullenrich", "capability": "email_enrich",
    "submitted": N,
    "results":  [{contact_id?, index, first_name, last_name, domain,
                  email|null, email_status, source, raw_validation}],
    "excluded": [{contact_id?, index, reason}]      # e.g. empty_domain
  }

Encodes two framework-wide invariants:
  1. FullEnrich fails the whole batch on ANY empty domain -> we EXCLUDE empty-domain
     rows (we do not fail them) and report them.
  2. Apify member-ID LinkedIn URLs (/in/ACw...) don't resolve in matching -> we drop
     the member-ID URL and fall back to name + domain.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

SUBMIT_PATH = "/api/v2/contact/enrich/bulk"
POLL_INTERVAL = 5          # seconds
MAX_POLL_TIME = 180        # seconds
BASE_URL = "https://app.fullenrich.com"


def log(msg):
    print(msg, file=sys.stderr)


def is_member_id_url(url):
    """Apify Short-mode member-ID URLs (/in/ACw...) don't resolve in matching."""
    if not url:
        return False
    return "/in/acw" in url.lower()


def has_domain(c):
    d = (c.get("company_domain") or "").strip()
    return bool(d)


# ---------------------------------------------------------------------------
# HTTP (stdlib)
# ---------------------------------------------------------------------------

def _request(method, url, token, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace") if e.fp else "")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _parse(body):
    # FullEnrich responses can contain raw control chars -> strict=False.
    return json.loads(body, strict=False)


# ---------------------------------------------------------------------------
# Capability: email_enrich
# ---------------------------------------------------------------------------

ENRICH_FIELD = {"email_enrich": "contact.emails", "phone_enrich": "contact.phones"}
CREDIT = {"email_enrich": 1, "phone_enrich": 10}


def normalize_status(raw):
    s = (raw or "").upper()
    if s in ("DELIVERABLE", "HIGH_PROBABILITY"):
        return "deliverable"
    if s == "CATCH_ALL":
        return "catch_all"
    return "invalid"


def build_submission(contacts, enrich_field):
    """Returns (data_payload, submitted_meta, excluded)."""
    data, meta, excluded = [], [], []
    for idx, c in enumerate(contacts):
        entry_meta = {
            "contact_id": c.get("id"),
            "index": idx,
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "domain": (c.get("company_domain") or "").strip(),
        }
        if not has_domain(c):
            excluded.append({**{k: entry_meta[k] for k in ("contact_id", "index")},
                             "reason": "empty_domain"})
            continue
        entry = {
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "domain": entry_meta["domain"],
            "enrich_fields": [enrich_field],
        }
        lu = c.get("linkedin_url")
        if lu and not is_member_id_url(lu):
            entry["linkedin_url"] = lu
        data.append(entry)
        meta.append(entry_meta)
    return data, meta, excluded


def _email_result(m, ci):
    mpwe = (ci or {}).get("most_probable_work_email") or {}
    email = mpwe.get("email")
    raw_status = mpwe.get("status")
    return {
        **{k: m[k] for k in ("contact_id", "index", "first_name", "last_name", "domain")},
        "email": email,
        "email_status": normalize_status(raw_status) if email else "invalid",
        "source": "fullenrich", "raw_validation": raw_status,
    }


def _phone_result(m, ci):
    ci = ci or {}
    mpp = ci.get("most_probable_phone")
    phone, region = None, None
    if isinstance(mpp, dict):
        phone = (mpp.get("number") or "").replace(" ", "") or None
        region = mpp.get("region")
    elif ci.get("phones"):
        first = ci["phones"][0] or {}
        phone = (first.get("number") or "").replace(" ", "") or None
        region = first.get("region")
    return {
        **{k: m[k] for k in ("contact_id", "index", "first_name", "last_name", "domain")},
        "phone": phone,
        "phone_type": None,            # FullEnrich does not classify type; validate downstream
        "source": "fullenrich", "raw_validation": region,
    }


def run_enrich(capability, contacts, estimate):
    data, meta, excluded = build_submission(contacts, ENRICH_FIELD[capability])

    if estimate:
        return {
            "provider": "fullenrich",
            "capability": capability,
            "cost_estimate_credits": len(data) * CREDIT[capability],
            "submittable": len(data),
            "excluded_empty_domain": len(excluded),
        }

    if not data:
        return {"provider": "fullenrich", "capability": capability,
                "submitted": 0, "results": [], "excluded": excluded}

    token = os.environ.get("FULLENRICH_API_KEY")
    if not token:
        return {"error": {"type": "missing_key",
                          "message": "FULLENRICH_API_KEY not set in environment"},
                "excluded": excluded}

    # Submit
    payload = {"name": f"gtm-pipeline-{capability}", "data": data}
    status, body = _request("POST", BASE_URL + SUBMIT_PATH, token, body=payload)
    if status not in (200, 201, 202):
        return {"error": {"type": "submit_failed", "status": status, "body": body[:500]},
                "excluded": excluded, "partial": []}
    try:
        enr_id = _parse(body).get("enrichment_id") or _parse(body).get("id")
    except Exception as e:  # noqa: BLE001
        return {"error": {"type": "bad_submit_response", "message": str(e), "body": body[:500]},
                "excluded": excluded}
    if not enr_id:
        return {"error": {"type": "no_enrichment_id", "body": body[:500]}, "excluded": excluded}

    log(f"submitted {len(data)} contacts -> enrichment {enr_id}; polling...")

    # Poll
    poll_url = f"{BASE_URL}{SUBMIT_PATH}/{enr_id}"
    elapsed = 0
    parsed = None
    while elapsed < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        st, pbody = _request("GET", poll_url, token)
        if st != 200:
            log(f"  poll http {st} ({elapsed}s)")
            continue
        try:
            parsed = _parse(pbody)
        except Exception as e:  # noqa: BLE001
            log(f"  poll parse error: {e}")
            continue
        state = (parsed.get("status") or parsed.get("state") or "").upper()
        log(f"  status={state or '?'} ({elapsed}s)")
        if state == "FINISHED":
            break
    else:
        return {"error": {"type": "timeout", "message": f"not FINISHED after {MAX_POLL_TIME}s",
                          "enrichment_id": enr_id}, "excluded": excluded}

    # Parse nested results, mapped back by position to submitted meta.
    out_rows = parsed.get("data", []) if isinstance(parsed, dict) else []
    builder = _email_result if capability == "email_enrich" else _phone_result
    results = []
    for i, m in enumerate(meta):
        ci = (out_rows[i] or {}).get("contact_info") if i < len(out_rows) else {}
        results.append(builder(m, ci))

    return {"provider": "fullenrich", "capability": capability,
            "submitted": len(data), "results": results, "excluded": excluded}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_input(args):
    if args.input is not None:
        raw = args.input
    elif args.input_file is not None:
        with open(args.input_file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    return json.loads(raw.strip() or "{}")


def main():
    p = argparse.ArgumentParser(description="FullEnrich adapter")
    p.add_argument("--capability", required=True, choices=["email_enrich", "phone_enrich"])
    p.add_argument("--input")
    p.add_argument("--input-file")
    p.add_argument("--estimate", action="store_true")
    args = p.parse_args()

    try:
        inp = load_input(args)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": {"type": "bad_input_json", "message": str(e)}}))
        sys.exit(2)

    result = run_enrich(args.capability, inp.get("contacts", []), estimate=args.estimate)
    print(json.dumps(result, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
