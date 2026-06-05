#!/usr/bin/env python3
"""
providers/ai-ark/adapter.py — AI Ark enrichment adapter (email_enrich + phone_enrich).

Why a script (not a spec-only manifest like prospeo/leadmagic): AI Ark's email finder is
ASYNC and TRACK-ID COUPLED — you submit by the trackId returned from a prior people-search
(single-use, expires 6h), it runs in the background, and results arrive via webhook OR by
polling. That submit->poll shape (plus result re-mapping) is exactly what an adapter is for.
The mobile-phone finder, by contrast, is SYNCHRONOUS and per-contact, so it's a simple loop.

Implements the uniform thin-script CLI contract (docs/writing-a-provider.md):

    python3 providers/ai-ark/adapter.py --capability email_enrich \
        --input '{"track_id":"<uuid from people-search>","contacts":[ ... ]}'
    python3 providers/ai-ark/adapter.py --capability phone_enrich \
        --input '{"contacts":[ ... ]}'
    ... --estimate            # cost only, no spend, no network

- Canonical JSON to STDOUT; logs/progress to STDERR.
- Secret from the environment only (AI_ARK_API_KEY). Never embedded, never fetched.
- stdlib only (no pip installs) so it can be fetched-and-piped.

Encodes the framework-wide member-ID invariant: Apify Short-mode LinkedIn URLs (/in/ACw...)
don't resolve in matching, so we drop them and fall back to domain + full_name.

VERIFY-AT-INTEGRATION (no live key was available at authoring time): the three email-finder
paths below (submit / poll-results / poll-stats) and the per-result credit costs are encoded
from the published reference index but not yet confirmed against a live response. The
--estimate path is network-free and exact; the run path is structurally correct but treat the
path constants as the first thing to confirm with a real key.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://api.ai-ark.com/api/developer-portal/v1"

# --- email-finder (async, by trackId) — CONFIRM these against the live OpenAPI -------------
EMAIL_SUBMIT_PATH   = "/people/email-finder"                       # POST {trackId, webhook?}
EMAIL_RESULTS_PATH  = "/people/email-finder/{track_id}/results"    # GET  -> found emails
EMAIL_STATS_PATH    = "/people/email-finder/{track_id}/statistics" # GET  -> {total, found, state}

# --- mobile-phone-finder (synchronous, per contact) ---------------------------------------
PHONE_PATH = "/people/mobile-phone-finder"                         # POST {linkedin} or {domain, fullName}

POLL_INTERVAL = 5          # seconds
MAX_POLL_TIME = 180        # seconds

# Pay-per-result ceilings (AI Ark bills per verified email / per phone FOUND, so these are
# upper bounds for budgeting, not guaranteed spend). Confirm exact per-op credits with a key.
CREDIT = {"email_enrich": 1, "phone_enrich": 1}


def log(msg):
    print(msg, file=sys.stderr)


def is_member_id_url(url):
    """Apify Short-mode member-ID URLs (/in/ACw...) don't resolve in matching."""
    if not url:
        return False
    return "/in/acw" in url.lower()


def full_name(c):
    fn = (c.get("full_name") or "").strip()
    if fn:
        return fn
    parts = [c.get("first_name"), c.get("last_name")]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


# ---------------------------------------------------------------------------
# HTTP (stdlib) — X-TOKEN auth
# ---------------------------------------------------------------------------

def _request(method, url, token, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-TOKEN": token}
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


# ---------------------------------------------------------------------------
# Capability: phone_enrich (synchronous, per contact)
# ---------------------------------------------------------------------------

def phone_key(c):
    """Returns (request_body, used_key) or (None, reason) if the contact can't be queried.
    Priority: vanity LinkedIn URL -> domain + full_name. Member-ID URLs are dropped."""
    lu = c.get("linkedin_url")
    if lu and not is_member_id_url(lu):
        return {"linkedin": lu}, "linkedin_url"
    dom = (c.get("company_domain") or "").strip()
    fn = full_name(c)
    if dom and fn:
        return {"domain": dom, "fullName": fn}, "domain+full_name"
    return None, "no_phone_key"


def extract_phone(parsed):
    """AI Ark mobile finder returns {"data": [["+1XXX..."]]} — a nested array. Flatten to E.164."""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if isinstance(data, list):
        for row in data:
            if isinstance(row, list) and row:
                num = str(row[0]).replace(" ", "").strip()
                if num:
                    return num
            elif isinstance(row, str) and row.strip():
                return row.replace(" ", "").strip()
    return None


def run_phone(contacts, estimate):
    addressable, excluded = [], []
    for idx, c in enumerate(contacts):
        body, key = phone_key(c)
        meta = {"contact_id": c.get("id"), "index": idx}
        if body is None:
            excluded.append({**meta, "reason": key})
        else:
            addressable.append((meta, body, key))

    if estimate:
        return {
            "provider": "ai-ark", "capability": "phone_enrich",
            "cost_estimate_credits": len(addressable) * CREDIT["phone_enrich"],
            "addressable": len(addressable),
            "excluded_no_key": len(excluded),
            "note": "AI Ark bills per phone FOUND — estimate is an upper bound.",
        }

    if not addressable:
        return {"provider": "ai-ark", "capability": "phone_enrich",
                "submitted": 0, "results": [], "excluded": excluded}

    token = os.environ.get("AI_ARK_API_KEY")
    if not token:
        return {"error": {"type": "missing_key", "message": "AI_ARK_API_KEY not set in environment"},
                "excluded": excluded}

    results = []
    for meta, body, key in addressable:
        st, raw = _request("POST", BASE_URL + PHONE_PATH, token, body=body)
        if st == 404:                       # documented: data not found for criteria
            results.append({**meta, "phone": None, "phone_type": None,
                            "source": "ai-ark", "match_key": key, "raw_validation": "not_found"})
            continue
        if st != 200:
            results.append({**meta, "phone": None, "phone_type": None, "source": "ai-ark",
                            "match_key": key, "raw_validation": f"http_{st}"})
            continue
        try:
            phone = extract_phone(json.loads(raw))
        except Exception as e:              # noqa: BLE001
            phone = None
            log(f"  phone parse error idx={meta['index']}: {e}")
        results.append({**meta, "phone": phone,
                        "phone_type": None,   # AI Ark does not classify line type; validate downstream
                        "source": "ai-ark", "match_key": key,
                        "raw_validation": "found" if phone else "empty"})
        time.sleep(0.2)                      # stay under 5 rps

    return {"provider": "ai-ark", "capability": "phone_enrich",
            "submitted": len(addressable), "results": results, "excluded": excluded}


# ---------------------------------------------------------------------------
# Capability: email_enrich (async, by trackId)
# ---------------------------------------------------------------------------

def run_email(inp, estimate):
    contacts = inp.get("contacts", [])

    if estimate:
        return {
            "provider": "ai-ark", "capability": "email_enrich",
            "cost_estimate_credits": len(contacts) * CREDIT["email_enrich"],
            "submittable": len(contacts),
            "note": "Requires a track_id from a people-search (single-use, 6h TTL). "
                    "AI Ark bills per verified email FOUND — estimate is an upper bound.",
        }

    track_id = (inp.get("track_id") or inp.get("trackId") or "").strip()
    if not track_id:
        return {"error": {"type": "missing_track_id",
                          "message": "AI Ark email_enrich requires `track_id` from the people-search "
                                     "response (single-use, expires 6h after that search). It cannot "
                                     "enrich a stored contact without a fresh search trackId."}}

    token = os.environ.get("AI_ARK_API_KEY")
    if not token:
        return {"error": {"type": "missing_key", "message": "AI_ARK_API_KEY not set in environment"}}

    # Submit (optionally with a webhook for async delivery).
    submit_body = {"trackId": track_id}
    if inp.get("webhook"):
        submit_body["webhook"] = inp["webhook"]
    st, raw = _request("POST", BASE_URL + EMAIL_SUBMIT_PATH, token, body=submit_body)
    if st == 404:
        return {"error": {"type": "track_id_invalid",
                          "message": "track id not found, expired (>6h), or already used"}}
    if st not in (200, 201, 202):
        return {"error": {"type": "submit_failed", "status": st, "body": raw[:500]}}

    # If a webhook was supplied, results are delivered there — return the accepted job.
    if inp.get("webhook"):
        try:
            acc = json.loads(raw)
        except Exception:                   # noqa: BLE001
            acc = {"raw": raw[:500]}
        return {"provider": "ai-ark", "capability": "email_enrich",
                "mode": "webhook", "track_id": track_id, "accepted": acc,
                "note": "Results will POST to your webhook when finished (auto-retries up to 3x)."}

    log(f"submitted email-finder for trackId {track_id}; polling...")

    # Poll statistics until finished, then fetch results.
    stats_url = BASE_URL + EMAIL_STATS_PATH.format(track_id=track_id)
    results_url = BASE_URL + EMAIL_RESULTS_PATH.format(track_id=track_id)
    elapsed = 0
    while elapsed < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        sst, sbody = _request("GET", stats_url, token)
        if sst != 200:
            log(f"  stats http {sst} ({elapsed}s)")
            continue
        try:
            stats = json.loads(sbody)
        except Exception as e:              # noqa: BLE001
            log(f"  stats parse error: {e}")
            continue
        state = (stats.get("state") or "").upper()
        st_obj = stats.get("statistics") or stats
        log(f"  state={state or '?'} found={st_obj.get('found')}/{st_obj.get('total')} ({elapsed}s)")
        if state in ("FINISHED", "DONE", "COMPLETED"):
            break
    else:
        return {"error": {"type": "timeout", "message": f"not finished after {MAX_POLL_TIME}s",
                          "track_id": track_id,
                          "hint": "confirm EMAIL_STATS_PATH/EMAIL_RESULTS_PATH against the live API"}}

    rst, rbody = _request("GET", results_url, token)
    if rst != 200:
        return {"error": {"type": "results_fetch_failed", "status": rst, "body": rbody[:500],
                          "hint": "confirm EMAIL_RESULTS_PATH against the live API, or use the webhook"}}
    try:
        payload = json.loads(rbody)
    except Exception as e:                  # noqa: BLE001
        return {"error": {"type": "bad_results_json", "message": str(e), "body": rbody[:500]}}

    rows = payload.get("content") or payload.get("data") or payload.get("results") or []
    results = []
    for r in rows:
        r = r or {}
        email = r.get("email")
        results.append({
            "identifier": r.get("id") or r.get("identifier"),
            "first_name": r.get("first_name"), "last_name": r.get("last_name"),
            "email": email,
            # All returned emails (SMTP & CATCH_ALL) are BounceBan-verified in real time —
            # AI Ark is self-validating, so its status IS the validation.
            "email_status": "catch_all" if str(r.get("type") or "").upper() == "CATCH_ALL"
                            else ("deliverable" if email else "invalid"),
            "source": "ai-ark", "raw_validation": r.get("type") or r.get("status"),
        })

    return {"provider": "ai-ark", "capability": "email_enrich", "mode": "poll",
            "track_id": track_id, "found": len(results), "results": results}


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
    p = argparse.ArgumentParser(description="AI Ark adapter")
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

    if args.capability == "phone_enrich":
        result = run_phone(inp.get("contacts", []), estimate=args.estimate)
    else:
        result = run_email(inp, estimate=args.estimate)

    print(json.dumps(result, ensure_ascii=False))
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
