#!/usr/bin/env python3
"""
examples/dach-fintech-cfos/replay.py — regenerate this example's outputs through the real CLI.

    python3 examples/dach-fintech-cfos/replay.py

Takes what each provider returned (seed/run.json, synthetic) and drives it through
storage/cli.py in pipeline order: create the list and freeze the plan, source, do-not-contact
pass, qualify, QA, enrich, QA again, send-posture scrub, activation preflight, export, report.
Nothing calls a provider. Writes output/: export.csv (+ .verdict.json), qa-findings.json,
list-summary.json and run-report.json. Uses a throwaway local data dir.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "output")
DATA = tempfile.mkdtemp()


def cli(op, inp, expect=0):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "storage", "cli.py"), op,
                        "--config", os.path.join(HERE, "gtm.config.yaml"),
                        "--backend", "local", "--dir", DATA, "--input", json.dumps(inp)],
                       capture_output=True, text=True, cwd=HERE)
    if p.returncode != expect:
        sys.exit(f"{op} exited {p.returncode} (want {expect}):\n{p.stdout}\n{p.stderr}")
    return json.loads(p.stdout)


def dump(name, obj):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    seed = json.load(open(os.path.join(HERE, "seed", "run.json"), encoding="utf-8"))
    L = seed["list"]
    lid = cli("create_list", {"name": L["name"], "description": L["description"]})["list_id"]
    cli("update_list", {"list_id": lid, "search_criteria": {"plan": L["plan"], "expansion": L["expansion"]}})
    for ev in seed["events"]:
        cli("log_event", {"list_id": lid, **ev})

    ident = [{k: v for k, v in c.items() if k not in ("qualify", "email", "phone")} for c in seed["contacts"]]
    cli("upsert_contacts", {"list_id": lid, "contacts": ident})
    rows = cli("query_list", {"list_id": lid})["contacts"]
    by_name = {r["full_name"]: r["id"] for r in rows}

    # do-not-contact, research posture: the current customer's CFO leaves before scoring
    cli("suppress", {"list_id": lid, "posture": "research"})

    # qualify (Gate #2: the MAYBE was kept to multithread Tirol)
    tally = {}
    live = {r["id"] for r in cli("query_by_stage", {"list_id": lid, "stage": "sourced"})["contacts"]}
    for c in seed["contacts"]:
        q = c.get("qualify")
        cid = by_name[c["full_name"]]
        if not q or cid not in live:
            continue
        keep = q["qualification_status"] == "QUALIFY" or q.get("keep")
        fields = {k: v for k, v in q.items() if k != "keep"}
        if not keep:
            fields["skip_reason"] = f"qualify: {q.get('qualification_notes', '')}"
        cli("advance_stage", {"list_id": lid, "contact_ids": [cid],
                              "stage": "qualified" if keep else "skipped", "fields": fields})
        tally[q["qualification_status"]] = tally.get(q["qualification_status"], 0) + 1
    cli("log_event", {"list_id": lid, "stage": "qualify", "status": "ok", "counts": tally})
    cli("qa", {"list_id": lid})

    # enrichment, as the providers returned it
    for c in seed["contacts"]:
        cid = by_name[c["full_name"]]
        if "email" in c and cid in live:
            cli("advance_stage", {"list_id": lid, "contact_ids": [cid], "stage": "email_enriched", "fields": c["email"]})
    cli("log_event", {"list_id": lid, "stage": "email_enrich", "status": "ok", "provider": "apollo+fullenrich",
                      "counts": {"found": 4, "accepted": 4}, "cost": {"estimate": 6, "actual": 5, "unit": "credits"}})
    for c in seed["contacts"]:
        cid = by_name[c["full_name"]]
        if "phone" in c and cid in live:
            cli("advance_stage", {"list_id": lid, "contact_ids": [cid], "stage": "phone_enriched", "fields": c["phone"]})
    cli("log_event", {"list_id": lid, "stage": "phone_enrich", "status": "warn", "provider": "apollo+fullenrich",
                      "counts": {"found": 3, "not_found": 1}, "cost": {"estimate": 40, "actual": 30, "unit": "credits"},
                      "warnings": ["no phone_validate provider keyed; numbers accepted unvalidated"]})

    # QA before the send: the providers returned phones with spaces, which QA flags
    findings = cli("qa", {"list_id": lid})
    dump("qa-findings.json", findings)
    fixable = [f["key"] for f in findings["findings"] if f["severity"] == "error" and f.get("suggestion")]
    if fixable:
        cli("qa_resolve", {"list_id": lid, "keys": fixable, "action": "fix",
                           "note": "normalize provider phone format to E.164"})

    # the send boundary
    cli("suppress", {"list_id": lid, "posture": "send"})
    cli("preflight_activate", {"list_id": lid, "min_stage": "email_enriched"})

    cli("export", {"list_id": lid, "min_stage": "email_enriched",
                   "out_path": os.path.join(OUT, "export.csv")})
    # the sidecar records a temp path; point it at the committed file instead
    vpath = os.path.join(OUT, "export.csv.verdict.json")
    v = json.load(open(vpath, encoding="utf-8"))
    v["csv"] = "output/export.csv"
    if v.get("suppression"):
        v["suppression"]["file"] = "context/do-not-contact.csv"
    dump("export.csv.verdict.json", v)
    dump("list-summary.json", cli("list_summary", {"list_id": lid}))
    rep = cli("run_report", {"list_id": lid})
    if (rep.get("gates") or {}).get("suppression"):
        rep["gates"]["suppression"]["file"] = "context/do-not-contact.csv"
    dump("run-report.json", rep)
    print(f"regenerated {OUT}: {len(fixable)} QA fix(es) applied, export + verdict + report written")


if __name__ == "__main__":
    main()
