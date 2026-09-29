#!/usr/bin/env python3
"""
scripts/sweep_checks.py — tests over whole classes of drift, not single cases.

The failure this repo is most prone to is a capability present in one path and missing from
the path actually taken: a column the CLI writes that the schema lacks, an op a doc tells
agents to call that the CLI never shipped, a workflow that forgot its agent type, a provider
in a waterfall with no manifest. Each check below sweeps every member of one such class.

    python3 scripts/sweep_checks.py        # exit 1 on the first class with a violation

stdlib only (the YAML-parity check runs when PyYAML is installed).
"""

import glob
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def load_cli():
    spec = importlib.util.spec_from_file_location("gtm_cli", os.path.join(ROOT, "storage", "cli.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


def check(name, problems):
    if problems:
        FAILED.append(name)
        print(f"FAIL {name}")
        for p in problems[:20]:
            print(f"       - {p}")
    else:
        print(f"ok   {name}")


def main():
    cli = load_cli()
    schema = read("storage/postgres/schema.sql")

    # 1. export columns: storage/cli.py vs pipeline_export() signature and SELECT order
    m = re.search(r"FUNCTION pipeline_export\(.*?RETURNS TABLE \((.*?)\) AS", schema, re.S)
    sig = [ln.strip().split()[0] for ln in m.group(1).strip().splitlines() if ln.strip()]
    sel = re.search(r"RETURN QUERY\s+SELECT(.*?)FROM pipeline_contacts", schema, re.S).group(1)
    sel_cols = [c.strip().replace("pc.", "") for c in sel.replace("\n", " ").split(",") if c.strip()]
    check("export columns match pipeline_export() signature and select list",
          [] if cli.EXPORT_COLUMNS == sig == sel_cols else
          [f"cli={cli.EXPORT_COLUMNS}", f"sig={sig}", f"select={sel_cols}"])

    # 2. every contact field the CLI will write exists as a schema column
    table = re.search(r"CREATE TABLE IF NOT EXISTS pipeline_contacts \((.*?)\n\);", schema, re.S).group(1)
    cols = {ln.strip().split()[0] for ln in table.splitlines()
            if ln.strip() and not ln.strip().startswith("--")}
    writable = cli.ADVANCE_TEXT_COLS | cli.ADVANCE_NUM_COLS | cli.ADVANCE_JSON_COLS
    missing = sorted(writable - cols)
    upgraded = set(re.findall(r"ALTER TABLE pipeline_contacts ADD COLUMN IF NOT EXISTS (\w+)", schema))
    added_after_v1 = {"phone_ext", "email_source_url", "phone_source_url", "skip_reason"}
    check("every CLI-writable field is a pipeline_contacts column", missing)
    check("columns added after v1 have an upgrade ALTER for existing databases",
          sorted(added_after_v1 - upgraded))

    # 3. both backends implement every op and primitive the shared ops rely on
    need = cli.BACKEND_OPS | {"set_run_state", "log_event", "query_events", "export_rows",
                              "default_export_path"}
    probs = []
    for cls in (cli.LocalBackend, cli.PostgresBackend):
        for op in sorted(need):
            if not callable(getattr(cls, op, None)):
                probs.append(f"{cls.__name__}.{op} missing")
    check("local and postgres backends implement the same ops", probs)

    # 4. workflows: every agent() call spreads agentOpt(...), each default agent type exists,
    #    and every workflow reports its failures
    agents = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(ROOT, ".claude/agents/*.md"))}
    probs = []
    for wf in sorted(glob.glob(os.path.join(ROOT, ".claude/workflows/*.js"))):
        src = open(wf, encoding="utf-8").read()
        name = os.path.basename(wf)
        calls = len(re.findall(r"\bagent\(", src))
        opts = len(re.findall(r"\.\.\.agentOpt\(AGENT\)", src))
        if calls != opts:
            probs.append(f"{name}: {calls} agent() call(s), {opts} with ...agentOpt(AGENT)")
        for default in re.findall(r"A\.agentType === undefined \? '([\w-]+)'", src):
            if default not in agents:
                probs.append(f"{name}: default agent type {default!r} has no .claude/agents/{default}.md")
        if "failed" not in src and "unscored_ids" not in src:
            probs.append(f"{name}: returns no failed[] / unscored_ids")
    check("workflows use narrow agent types and report failures", probs)

    # 5. providers: every waterfall entry and the sequencer have a manifest; every auth env var
    #    is documented in .env.example
    cfg = cli.load_config(os.path.join(ROOT, "gtm.config.example.yaml"))
    names = set()
    for v in (cfg.get("waterfalls") or {}).values():
        names.update(v if isinstance(v, list) else [])
    if cfg.get("sequencer"):
        names.add(cfg["sequencer"])
    probs = [f"{n}: no providers/{n}/manifest.yaml" for n in sorted(names)
             if not os.path.exists(os.path.join(ROOT, "providers", n, "manifest.yaml"))]
    env_example = read(".env.example")
    for mf in sorted(glob.glob(os.path.join(ROOT, "providers/*/manifest.yaml"))):
        text = open(mf, encoding="utf-8").read()
        for var in re.findall(r"^\s*env:\s*([A-Z][A-Z0-9_]+)", text, re.M):
            if var not in env_example:
                probs.append(f"{os.path.basename(os.path.dirname(mf))}: {var} not in .env.example")
    check("waterfall providers have manifests; manifest keys are in .env.example", probs)

    # 6. every storage op an agent/skill/doc tells you to call exists
    probs = []
    files = (glob.glob(os.path.join(ROOT, "agents/*/agent.md")) + glob.glob(os.path.join(ROOT, ".claude/skills/*/SKILL.md"))
             + glob.glob(os.path.join(ROOT, "docs/*.md")) + [os.path.join(ROOT, f) for f in ("README.md", "AGENTS.md", "CLAUDE.md", "REVIEW.md")])
    for f in files:
        text = open(f, encoding="utf-8").read()
        ops = set(re.findall(r"storage/cli\.py (\w+)", text)) | set(re.findall(r"`(\w+) --input", text))
        for op in sorted(ops - cli.OPS):
            probs.append(f"{os.path.relpath(f, ROOT)}: calls unknown op {op!r}")
    check("every op referenced in agents/skills/docs exists in storage/cli.py", probs)

    # 7. skills: directory name == frontmatter name
    probs = []
    for sk in sorted(glob.glob(os.path.join(ROOT, ".claude/skills/*/SKILL.md"))):
        d = os.path.basename(os.path.dirname(sk))
        nm = re.search(r"^name:\s*(\S+)", open(sk, encoding="utf-8").read(), re.M)
        if not nm or nm.group(1) != d:
            probs.append(f"{d}: frontmatter name {nm.group(1) if nm else None!r}")
    check("skill names match their directories", probs)

    # 8. the stdlib config reader agrees with PyYAML on everything the CLI reads
    try:
        import yaml
        probs = []
        sample = (read("gtm.config.example.yaml") + '\nqa2:\n  pats: [isd, ".k12.", \'.edu\']  # comment\n'
                  + '  files: { icp: icp.md, personas: personas.md }\n')
        a, b = cli._mini_yaml(sample), yaml.safe_load(sample)
        for ks in [("storage", "backend"), ("storage", "local", "dir"), ("storage", "postgres", "url_env"),
                   ("storage", "postgres", "enable_master_dedup"), ("suppression", "file"),
                   ("suppression", "required_for_activation"), ("qa", "default_country"),
                   ("qa", "other_org_patterns"), ("defaults", "autonomy", "title_preview"), ("qa2", "pats"),
                   ("qa2", "files")]:
            if cli._cfg(a, *ks) != cli._cfg(b, *ks):
                probs.append(f"{'.'.join(ks)}: stdlib={cli._cfg(a, *ks)!r} yaml={cli._cfg(b, *ks)!r}")
        check("stdlib config reader matches PyYAML on every key the CLI reads", probs)
    except ImportError:
        print("skip stdlib-vs-PyYAML parity (PyYAML not installed)")

    # 9. personal data is never committed by default
    gi = read(".gitignore")
    check("the do-not-contact list is gitignored",
          [] if "context/do-not-contact.csv" in gi else ["add context/do-not-contact.csv to .gitignore"])

    print(f"\nsweep_checks: {'FAILED ' + ', '.join(FAILED) if FAILED else 'all classes clean'}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
