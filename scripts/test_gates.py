#!/usr/bin/env python3
"""
scripts/test_gates.py — the run-integrity gates, exercised end to end through the real CLI.

    python3 scripts/test_gates.py --backend local
    GTM_TEST_DATABASE_URL=postgresql://... python3 scripts/test_gates.py --backend postgres

Each scenario is a defect that got past a name+org dedupe and a human approval on a real
list (see docs/run-integrity.md): a person listed twice under two spellings of their name, a
dot-format phone with a glued-on extension, a one-character email-domain typo, a shared line
labelled as someone's direct dial. The test asserts the gates find each one, that resolving
it does what it says (a duplicate drop removes the extra row, not the person), and that
activation refuses until the list is scrubbed and clean. Same assertions on both backends.
stdlib only; exits non-zero on the first failure.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "storage", "cli.py")

PASS = 0
CONFIG = "/nonexistent"


def run(op, inp, flags, expect_code=0):
    proc = subprocess.run(
        [sys.executable, CLI, op, "--config", CONFIG, *flags, "--input", json.dumps(inp)],
        capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != expect_code:
        sys.exit(f"FAIL {op}: exit {proc.returncode}, want {expect_code}\nstdout: {proc.stdout}\n"
                 f"stderr: {proc.stderr}")
    return json.loads(proc.stdout), proc.stderr


def check(desc, got, want):
    global PASS
    if got != want:
        sys.exit(f"FAIL {desc}: got {got!r}, want {want!r}")
    PASS += 1
    print(f"ok   {desc} ({got})")


def unit_checks():
    """Pure-function cases, each one a wrong answer the gates once gave."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gtm_cli", CLI)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    ph = lambda s, cc="US": (cli.normalize_phone(s, cc) or {}).get("e164")
    check("(0) trunk prefix dropped after the country code",
          (ph("+49 (0)30 1234567"), ph("+44 (0)20 7946 0000")), ("+49301234567", "+442079460000"))
    check("'box 12' is not an extension", cli.normalize_phone("+1 415 555 0142 box 12")["ext"], None)
    check("glued extension still split", cli.normalize_phone("+16175550188ext.204")["ext"], "204")
    check("non-NANP national number without + is refused", ph("030 1234567", "DE"), None)
    pk = lambda f, l: cli.person_key({"first_name": f, "last_name": l})
    check("middle initial ignored", pk("Tom R.", "Walsh") == pk("Tom", "Walsh"), True)
    check("hyphenated / middle names are different people",
          (pk("Ann", "Lee-Smith") == pk("Ann", "Smith"), pk("Mary Ann", "Smith") == pk("Mary", "Smith"),
           pk("Jean-Pierre", "Dupont") == pk("Jean", "Dupont")), (False, False, False))
    check("typo rule ignores TLD and short labels",
          (cli._domain_typo("bank.be", "bank.de"), cli._domain_typo("a.com", "b.com"),
           cli._domain_typo("cityoriverton.org", "cityofriverton.org")), (False, False, True))
    lk = cli.linkedin_key
    check("LinkedIn variants share one suppression key",
          len({lk("https://www.linkedin.com/in/Jane/"), lk("http://linkedin.com/in/jane?x=1"),
               lk("de.linkedin.com/in/jane")}), 1)
    y = cli._mini_yaml
    check("config reader: BOM, block list, quoted #",
          (y("\ufeffstorage:\n  backend: postgres\n")["storage"]["backend"],
           y("qa:\n  other_org_patterns:\n    - isd\n    - '.k12.'\n")["qa"]["other_org_patterns"],
           y("suppression:\n  file: \"ctx/dnc #2.csv\"  # note\n")["suppression"]["file"]),
          ("postgres", ["isd", ".k12."], "ctx/dnc #2.csv"))
    bad = 0
    for text in ("a:\n  <<: *x\n", "a: [x,\n  y]\n", "a:\n  - k: v\n", "a: |\n  x\n"):
        try:
            y(text)
        except cli.ConfigError:
            bad += 1
    check("config reader refuses what it cannot read (merge keys, multi-line flow, maps in lists, block scalars)", bad, 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["local", "postgres"], default="local")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp()
    if a.backend == "local":
        flags = ["--backend", "local", "--dir", os.path.join(tmp, ".gtm-data")]
    else:
        if not os.environ.get("GTM_TEST_DATABASE_URL"):
            sys.exit("set GTM_TEST_DATABASE_URL to a throwaway database with schema.sql applied")
        flags = ["--backend", "postgres", "--db-url-env", "GTM_TEST_DATABASE_URL"]

    dnc = os.path.join(tmp, "do-not-contact.csv")
    with open(dnc, "w") as f:
        f.write("email,domain,phone,linkedin_url,reason\n"
                "blocked.person@acme.com,,,,asked to be removed\n"
                ",campus.stateu.edu,,,customer\n")
    global CONFIG
    CONFIG = os.path.join(tmp, "gtm.config.yaml")
    with open(CONFIG, "w") as f:
        f.write("suppression:\n  file: do-not-contact.csv\n  required_for_activation: true\n"
                "qa:\n  default_country: US\n")
    if a.backend == "local":
        unit_checks()

    lid = run("create_list", {"name": "gates", "search_criteria": {"plan": {"brief": "t"}}}, flags)[0]["list_id"]
    run("update_list", {"list_id": lid, "search_criteria": {"expansion": {"roles": []}}}, flags)
    meta = run("get_list", {"list_id": lid}, flags)[0]["list"]
    check("update_list merges into search_criteria", sorted(meta["search_criteria"]), ["expansion", "plan"])

    people = [
        {"first_name": "Tom", "last_name": "Walsh", "company_name": "Acme", "company_domain": "acme.com",
         "linkedin_url": "https://linkedin.com/in/tomwalsh"},
        {"first_name": "Tom R.", "last_name": "Walsh", "company_name": "Acme", "company_domain": "acme.com"},
        {"first_name": "Dana", "last_name": "Reyes", "company_name": "City of Riverton",
         "company_domain": "cityofriverton.org"},
        {"first_name": "Blocked", "last_name": "Person", "company_name": "Acme", "company_domain": "acme.com"},
        {"first_name": "Campus", "last_name": "Lead", "company_name": "State U", "company_domain": "campus.stateu.edu"},
        {"first_name": "Other", "last_name": "Campus", "company_name": "State U", "company_domain": "north.stateu.edu"},
        {"first_name": "Ann", "last_name": "Lee", "company_name": "Globex", "company_domain": "globex.com"},
        {"first_name": "Bo", "last_name": "Chen", "company_name": "Globex", "company_domain": "globex.com"},
    ]
    up = run("upsert_contacts", {"list_id": lid, "contacts": people}, flags)[0]
    check("inserted", up["inserted"], 8)
    ids = [c["id"] for c in run("query_list", {"list_id": lid}, flags)[0]["contacts"]]
    tom, tom2, dana, blocked, campus, other, ann, bo = ids

    run("advance_stage", {"list_id": lid, "contact_ids": ids, "stage": "email_enriched"}, flags)
    fields = {
        tom: {"email": "tom.walsh@acme.com", "phone": "+1415.555.0142"},
        tom2: {"email": "twalsh@acme.com"},
        dana: {"email": "dreyes@cityoriverton.org", "phone": "+16175550188ext.204"},
        blocked: {"email": "blocked.person@acme.com"},
        campus: {"email": "lead@campus.stateu.edu"},
        other: {"email": "other@north.stateu.edu"},
        ann: {"email": "ann@globex.com", "phone": "+1 (312) 555-0100", "phone_type": "direct_dial"},
        bo: {"email": "bo@globex.com", "phone": "312.555.0100", "phone_type": "direct_dial"},
    }
    for cid, fl in fields.items():
        run("update_contacts", {"list_id": lid, "contact_ids": [cid], "fields": fl}, flags)

    # --- activation refuses an unscrubbed list --------------------------------
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("preflight blocks without a do-not-contact pass",
          any("do-not-contact" in b for b in pf["blockers"]), True)

    # --- send posture fails closed on a missing file; research posture fails open ----
    v, err = run("suppress", {"list_id": lid, "posture": "send", "file": os.path.join(tmp, "nope.csv")},
                 flags, expect_code=6)
    check("send posture + missing file exits 6, applied=false", v["verdict"]["applied"], False)
    v, err = run("suppress", {"list_id": lid, "posture": "research", "file": os.path.join(tmp, "nope.csv")},
                 flags)
    check("research posture + missing file exits 0 but says so", ("NOT APPLIED" in err, v["verdict"]["applied"]),
          (True, False))

    # --- suppression applies email + subdomain rules -------------------------
    v, _ = run("suppress", {"list_id": lid, "posture": "research"}, flags)
    check("suppressed exactly the blocked email + blocked campus", sorted(s["id"] for s in v["suppressed"]),
          sorted([blocked, campus]))
    check("a blocked subdomain does not suppress its sibling campus",
          other in [s["id"] for s in v["suppressed"]], False)

    # --- QA finds the four real defects ---------------------------------------
    qa, _ = run("qa", {"list_id": lid}, flags)
    codes = sorted({f["code"] for f in qa["findings"] if f["severity"] == "error"})
    check("qa error codes", codes,
          ["duplicate_person", "email_domain_typo", "phone_not_e164", "shared_phone_labelled_direct"])
    by = {f["code"]: f for f in qa["findings"]}
    check("duplicate keeps the richer row", (by["duplicate_person"]["keep_id"], by["duplicate_person"]["drop_ids"]),
          (tom, [tom2]))
    check("typo suggestion uses the company domain", by["email_domain_typo"]["suggestion"],
          {"email": "dreyes@cityofriverton.org"})
    phone_fixes = sorted((f["contact_ids"][0], f["suggestion"]["phone"], f["suggestion"].get("phone_ext"))
                         for f in qa["findings"] if f["code"] == "phone_not_e164")
    check("dot-format + glued extension normalize to E.164 + ext", phone_fixes,
          sorted([(tom, "+14155550142", None), (dana, "+16175550188", "204"),
                  (ann, "+13125550100", None), (bo, "+13125550100", None)]))

    # --- preflight still blocks: scrubbed now, but QA errors open --------------
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("preflight blocks on open QA errors", any("unresolved QA errors" in b for b in pf["blockers"]), True)
    check("a research-posture pass does not satisfy the send boundary",
          any("research posture" in b for b in pf["blockers"]), True)

    # --- resolve: drop the duplicate row, fix the rest ------------------------
    run("qa_resolve", {"list_id": lid, "keys": [by["duplicate_person"]["key"]], "action": "drop"}, flags)
    rows = {c["id"]: c for c in run("query_list", {"list_id": lid}, flags)[0]["contacts"]}
    check("duplicate drop removes only the redundant row", (rows[tom]["stage"], rows[tom2]["stage"]),
          ("email_enriched", "skipped"))
    qa, _ = run("qa", {"list_id": lid}, flags)
    fix_keys = [f["key"] for f in qa["findings"] if f["severity"] == "error" and f.get("suggestion")]
    res, _ = run("qa_resolve", {"list_id": lid, "keys": fix_keys, "action": "fix"}, flags)
    rows = {c["id"]: c for c in run("query_list", {"list_id": lid}, flags)[0]["contacts"]}
    check("fixed phone + extension persisted", (rows[dana]["phone"], rows[dana]["phone_ext"]),
          ("+16175550188", "204"))
    check("fixed email persisted", rows[dana]["email"], "dreyes@cityofriverton.org")
    check("shared line relabelled", (rows[ann]["phone_type"], rows[bo]["phone_type"]), ("switchboard", "switchboard"))
    check("no open QA errors after resolve", res["qa"]["open_errors"], 0)

    # --- a contact added after the scrub makes the verdict stale ---------------
    run("upsert_contacts", {"list_id": lid, "contacts": [
        {"first_name": "Late", "last_name": "Add", "company_name": "Initech", "company_domain": "initech.com",
         "stage": "email_enriched", "email": "late@initech.com"}]}, flags)
    late = max(c["id"] for c in run("query_list", {"list_id": lid}, flags)[0]["contacts"])
    run("advance_stage", {"list_id": lid, "contact_ids": [late], "stage": "email_enriched",
                          "fields": {"email": "late@initech.com"}}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("rows added after the scrub block activation", any("after the last" in b for b in pf["blockers"]), True)

    run("suppress", {"list_id": lid, "posture": "send"}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid}, flags)
    check("preflight passes once scrubbed + clean", pf["ok"], True)

    # --- the send gate re-checks: nothing edited after the scrub gets through ----
    run("advance_stage", {"list_id": lid, "contact_ids": [blocked], "stage": "email_enriched"}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("a suppressed row moved back is caught", any("match the do-not-contact" in b for b in pf["blockers"]), True)
    run("suppress", {"list_id": lid, "posture": "send"}, flags)
    run("update_contacts", {"list_id": lid, "contact_ids": [other], "fields": {"email": "x@sub.campus.stateu.edu"}}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("an email edited onto a blocked domain is caught", any("match the do-not-contact" in b for b in pf["blockers"]), True)
    run("suppress", {"list_id": lid, "posture": "send"}, flags)
    with open(dnc, "a") as f:
        f.write("nobody@example.org,,,,added later\n")
    pf, _ = run("preflight_activate", {"list_id": lid}, flags, expect_code=6)
    check("a do-not-contact file changed after the scrub is caught", any("changed after" in b for b in pf["blockers"]), True)
    run("suppress", {"list_id": lid, "posture": "send", "file": dnc}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid}, flags)
    check("preflight passes again after a fresh send-posture pass", pf["ok"], True)

    # --- export carries the verdict --------------------------------------------
    out = os.path.join(tmp, "export.csv")
    ex, err = run("export", {"list_id": lid, "min_stage": "email_enriched", "out_path": out}, flags)
    check("export says the list was scrubbed", ex["suppression_applied"], True)
    check("export excludes suppressed + dropped rows", ex["count"], 5)
    check("verdict sidecar written", os.path.exists(out + ".verdict.json"), True)

    # --- the ledger recorded every gate ----------------------------------------
    run("log_event", {"list_id": lid, "stage": "email_enrich", "status": "ok", "provider": "x",
                      "counts": {"found": 6}, "cost": {"estimate": 6, "actual": 7, "unit": "credits"}}, flags)
    rep, _ = run("run_report", {"list_id": lid}, flags)
    check("run_report lists logged stages",
          all(s in rep["stages_logged"] for s in ("suppress", "qa", "qa_resolve", "preflight_activate", "email_enrich")),
          True)
    check("run_report sums cost", rep["cost"]["credits"], {"estimate": 6, "actual": 7})
    check("run_report keeps the plan", rep["plan"], {"brief": "t"})
    check("resolved QA and a passed preflight leave no open warnings", rep["open_warnings"], [])

    # --- read-only suppression check (company-level, before enrichment) ---------
    cs, _ = run("check_suppression", {"domains": ["campus.stateu.edu", "north.stateu.edu"]}, flags)
    check("check_suppression matches only the blocked campus", sorted(cs["matches"]), ["campus.stateu.edu"])

    # --- do-not-contact files in the shapes people actually export -------------
    alias = os.path.join(tmp, "alias.csv")
    with open(alias, "w") as f:
        f.write("Email Address,Company Domain,Phone Number,LinkedIn\n"
                "a@x.com,,,\n,y.org,,\n,,+49 (0)30 1234567,\n,,,https://www.linkedin.com/in/Jane/\n")
    cs, _ = run("check_suppression", {"file": alias, "emails": ["A@x.com"], "domains": ["mail.y.org"],
                                      "phones": ["+49 30 1234567"], "linkedin_urls": ["de.linkedin.com/in/jane"]}, flags)
    check("loose headers, (0) phones and LinkedIn variants all match", (cs["applied"], len(cs["matches"])), (True, 4))
    unknown = os.path.join(tmp, "unknown.csv")
    with open(unknown, "w") as f:
        f.write("Mail,Firma\na@x.com,X\n")
    cs, _ = run("check_suppression", {"file": unknown, "emails": ["a@x.com"]}, flags)
    check("a file with rows but no recognised columns is not 'applied'", cs["applied"], False)
    v, _ = run("suppress", {"list_id": lid, "posture": "send", "file": unknown}, flags, expect_code=6)
    check("...and the send posture refuses it", v["verdict"]["applied"], False)
    badphone = os.path.join(tmp, "badphone.csv")
    with open(badphone, "w") as f:
        f.write("phone\n020 7946 0000\n")
    run("suppress", {"list_id": lid, "posture": "send", "file": badphone}, flags, expect_code=6)
    check("an unreadable phone entry fails the send posture closed", True, True)

    # --- both backends store the same thing ----------------------------------------
    lid2 = run("create_list", {"name": "parity"}, flags)[0]["list_id"]
    up, _ = run("upsert_contacts", {"list_id": lid2, "contacts": [
        {"first_name": "Zed", "last_name": "Z", "company_domain": "z.com", "linkedin_url": "https://linkedin.com/in/zzz",
         "email": "zed@z.com", "phone": "+14155550142", "phone_type": "mobile", "qualification_score": "7.6",
         "email_waterfall_log": ["a", "b"], "enrich_recommended": True, "not_a_column": 1},
        {"first_name": "Amy", "last_name": "A", "company_domain": "a.com", "linkedin_url": "https://linkedin.com/in/aaa"}]}, flags)
    check("unknown fields are reported, not silently kept or lost", up.get("ignored_fields"), ["not_a_column"])
    rows = run("query_list", {"list_id": lid2}, flags)[0]["contacts"]
    zed, amy = rows[0], rows[1]
    check("ids follow input order", (zed["first_name"], zed["id"] < amy["id"]), ("Zed", True))
    check("upsert keeps enrichment fields and coerces types the same way",
          (zed["email"], zed["phone"], zed["qualification_score"], zed["email_waterfall_log"], zed["enrich_recommended"]),
          ("zed@z.com", "+14155550142", 8, '["a", "b"]', "true"))
    r, _ = run("advance_stage", {"list_id": lid2, "contact_ids": [str(zed["id"])], "stage": "qualified"}, flags)
    check("string contact ids work", r["updated"], 1)
    r, _ = run("update_contacts", {"list_id": lid2, "contact_ids": [zed["id"]], "fields": {"stage": "phone_enriched"}}, flags)
    rows = {c["id"]: c for c in run("query_list", {"list_id": lid2}, flags)[0]["contacts"]}
    check("update_contacts never changes the stage", rows[zed["id"]]["stage"], "qualified")
    run("update_contacts", {"list_id": lid2, "contact_ids": [zed["id"]], "fields": {"title": "CFO $GTMTXT"}}, flags, expect_code=2)
    run("update_contacts", {"list_id": lid2, "contact_ids": [zed["id"]], "fields": {"qualification_score": "nan"}}, flags, expect_code=2)
    run("export", {"list_id": lid2, "min_stage": "phone"}, flags, expect_code=2)
    run("advance_stage", {"list_id": 999999, "contact_ids": [1], "stage": "qualified"}, flags, expect_code=4)

    # --- a 'keep' stops applying when the value it accepted changes ----------------
    run("update_contacts", {"list_id": lid2, "contact_ids": [amy["id"]], "fields": {"email": "amy@a.com", "phone": "+1 415 555 0199"}}, flags)
    run("advance_stage", {"list_id": lid2, "contact_ids": [zed["id"], amy["id"]], "stage": "email_enriched"}, flags)
    qa, _ = run("qa", {"list_id": lid2}, flags)
    k = [f["key"] for f in qa["findings"] if f["code"] == "phone_not_e164"][0]
    run("qa_resolve", {"list_id": lid2, "keys": [k], "action": "keep"}, flags)
    run("suppress", {"list_id": lid2, "posture": "send"}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid2}, flags)
    check("an acknowledged finding does not block", pf["ok"], True)
    run("update_contacts", {"list_id": lid2, "contact_ids": [amy["id"]], "fields": {"phone": "+1 (212) 555.0199"}}, flags)
    pf, _ = run("preflight_activate", {"list_id": lid2}, flags, expect_code=6)
    check("...until the value it accepted changes", any("unresolved QA errors" in b for b in pf["blockers"]), True)
    qa, _ = run("qa", {"list_id": lid2}, flags)
    keys = [f["key"] for f in qa["findings"] if f["severity"] == "error"] + [k]
    run("qa_resolve", {"list_id": lid2, "keys": keys[:1] * 2, "action": "fix", "fields": {"phone": "+12125550199"}}, flags,
        expect_code=2)
    check("caller fields are refused for more than one key", True, True)

    # --- errors are categorical ------------------------------------------------
    run("get_list", {"list_id": 999999}, flags, expect_code=4)
    run("log_event", {"list_id": lid, "stage": "x", "status": "fine"}, flags, expect_code=2)

    print(f"\ntest_gates ({a.backend}): {PASS} passed")


if __name__ == "__main__":
    main()
