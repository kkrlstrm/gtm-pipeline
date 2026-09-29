#!/usr/bin/env python3
"""
scripts/doctor.py — is this checkout ready for a real run? One command, plain answers.

    python3 scripts/doctor.py [--config gtm.config.yaml]

Checks, in the order a first run would hit them:
  1. gtm.config.yaml exists
  2. context/icp.md and context/personas.md exist and are filled in (no <placeholder> text left)
  3. the do-not-contact file exists (activation is blocked without it, by default)
  4. storage is reachable — and on Postgres, that the schema is current
  5. every stage resolves to at least one provider you have a key for; the sequencer is keyed

A key that is set has not been shown to work: the first call to that provider is the real
test, and a 401/403 there stops the stage with the provider's own error.

Exit 0 when nothing is FAIL (warnings are fine), 1 otherwise. stdlib only; the provider
section uses PyYAML when it is installed and says so when it is not.
"""

import argparse
import importlib.util
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = 0
WARNS = 0


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ok(msg):
    print(f"  ok    {msg}")


def warn(msg, fix=None):
    global WARNS
    WARNS += 1
    print(f"  warn  {msg}" + (f"\n        → {fix}" if fix else ""))


def fail(msg, fix=None):
    global FAILS
    FAILS += 1
    print(f"  FAIL  {msg}" + (f"\n        → {fix}" if fix else ""))


PLACEHOLDER = re.compile(r"<[^>\n]{3,}>")


def check_context(cfg, cli):
    print("\ncontext")
    cdir = cli._resolve(cli._cfg(cfg, "context", "dir", default="./context"), cfg)
    files = cli._cfg(cfg, "context", "files", default={}) or {}
    if not isinstance(files, dict):
        files = {}
    for key, required in (("icp", True), ("personas", True), ("segments", False), ("exclusions", False)):
        name = files.get(key) or f"{key}.md"
        path = os.path.join(cdir, name)
        rel = os.path.relpath(path, ROOT)
        if not os.path.exists(path):
            if required:
                fail(f"{rel} missing", "run /gtm-setup, or copy the .example and fill it in")
            else:
                warn(f"{rel} missing (optional)",
                     "without segments.md every in-persona contact is one tier" if key == "segments"
                     else "without exclusions.md nothing is hard-filtered before scoring")
            continue
        body = "\n".join(l for l in open(path, encoding="utf-8").read().splitlines()
                         if not l.lstrip().startswith("#"))
        left = PLACEHOLDER.findall(body)
        if left:
            (fail if required else warn)(f"{rel} still has template placeholders, e.g. {left[0][:60]}",
                                         "replace every <...> with your own text (or run /gtm-setup)")
        else:
            ok(f"{rel}")


def check_suppression(cfg, cli):
    print("\ndo-not-contact")
    path = cli._cfg(cfg, "suppression", "file", default="context/do-not-contact.csv")
    required = cli._cfg(cfg, "suppression", "required_for_activation", default=True) is not False
    full = cli._configured_suppression(cfg)       # resolved exactly as the CLI resolves it
    if not os.path.exists(full):
        msg = f"{path} missing"
        fix = (f"create it (a header-only file `email,domain,phone,linkedin_url,reason` means "
               f"'my list is empty'); see context/do-not-contact.csv.example")
        (warn if required else ok)(msg + (" — activation will be BLOCKED until it exists" if required else ""), fix)
        return
    entries, stats = cli._load_suppression(full, cli._cfg(cfg, "qa", "default_country", default="US"))
    probs = cli._suppression_problems(stats)
    if probs:
        fail(f"{path}: " + "; ".join(probs), "fix the file; activation refuses it until then")
        return
    if stats["unrecognised_columns"]:
        warn(f"{path}: ignored columns {stats['unrecognised_columns']}",
             "recognised: email, domain, phone, linkedin_url (and common variants) + reason")
    n = sum(len(v) for v in entries.values())
    ok(f"{path}: {n} entr{'y' if n == 1 else 'ies'} "
       f"({', '.join(f'{k} {len(v)}' for k, v in entries.items() if v) or 'empty — checked against nothing'})")


def check_storage(cfg, cli):
    print("\nstorage")
    backend = os.environ.get("GTM_BACKEND") or cli._cfg(cfg, "storage", "backend", default="local")
    if backend == "local":
        d = os.environ.get("GTM_DATA_DIR") or cli._cfg(cfg, "storage", "local", "dir", default="./.gtm-data")
        full = os.path.abspath(d) if os.environ.get("GTM_DATA_DIR") else cli._resolve(d, cfg)
        parent = full if os.path.exists(full) else os.path.dirname(full.rstrip("/"))
        if os.access(parent, os.W_OK):
            ok(f"local backend, writable ({d})")
        else:
            fail(f"local backend dir not writable: {d}")
        return
    url_env = cli._cfg(cfg, "storage", "postgres", "url_env", default="DATABASE_URL")
    if not os.environ.get(url_env):
        fail(f"postgres backend but ${url_env} is not set", "set -a && source .env && set +a")
        return
    b = cli.PostgresBackend(url_env)
    try:
        b._psql("SELECT 1;")
    except Exception as e:  # noqa: BLE001
        fail(f"cannot reach postgres via ${url_env}: {str(e)[:200]}")
        return
    have = b._json("SELECT COALESCE(json_agg(table_name), '[]'::json)::text FROM information_schema.tables"
                   " WHERE table_name IN ('pipeline_lists','pipeline_contacts','pipeline_companies',"
                   "'pipeline_run_events');") or []
    cols = b._json("SELECT COALESCE(json_agg(column_name), '[]'::json)::text FROM information_schema.columns"
                   " WHERE table_name = 'pipeline_contacts' AND column_name IN ('phone_ext','skip_reason');") or []
    if len(have) < 4 or len(cols) < 2:
        fail("postgres schema is missing tables/columns this version needs",
             "psql \"$DATABASE_URL\" -f storage/postgres/schema.sql   (idempotent; upgrades in place)")
    else:
        ok(f"postgres reachable via ${url_env}, schema current")


def check_providers(cfg_path):
    print("\nproviders")
    try:
        import yaml  # noqa: F401
    except ImportError:
        warn("PyYAML not installed — skipping provider resolution", "pip install -r requirements.txt")
        return
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "show-plan.py"),
                           "--config", cfg_path, "--providers", os.path.join(ROOT, "providers")],
                          capture_output=True, text=True, cwd=ROOT)
    for line in proc.stdout.splitlines():
        if "NO AVAILABLE PROVIDER" in line:
            cap = line.split(":")[0].strip()
            warn(f"{cap}: no provider you have a key for — this stage will be skipped",
                 "add a key from .env.example for one of the providers listed under it")
        elif line.startswith("sequencer") and "NOT available" in line:
            warn(line.strip(), "activation needs the sequencer's key; export still works without it")
        elif line.startswith(("storage backend", "providers_enabled")):
            continue          # storage is reported above, from the same overrides the CLI uses
        elif line.strip() and not line.startswith(" "):
            print(f"        {line}")


def main():
    ap = argparse.ArgumentParser(description="Check this checkout is ready for a run")
    ap.add_argument("--config", default=os.path.join(ROOT, "gtm.config.yaml"))
    a = ap.parse_args()
    cli = _load("gtm_cli", "storage/cli.py")

    print("config")
    cfg_path = a.config
    if not os.path.exists(a.config):
        fail(f"{os.path.relpath(a.config, ROOT)} missing",
             "cp gtm.config.example.yaml gtm.config.yaml   (or run /gtm-setup)")
        cfg_path = os.path.join(ROOT, "gtm.config.example.yaml")
    try:
        cfg = cli.load_config(cfg_path)
    except cli.ConfigError as e:
        fail(f"cannot read config: {e}", "fix the file, or pip install pyyaml for full YAML")
        print(f"\nNOT READY: {FAILS} fail, {WARNS} warn")
        sys.exit(1)
    if cfg_path == a.config:
        ok(os.path.relpath(a.config, ROOT))

    check_context(cfg, cli)
    check_suppression(cfg, cli)
    check_storage(cfg, cli)
    check_providers(cfg_path)

    print(f"\n{'READY' if not FAILS else 'NOT READY'}: {FAILS} fail, {WARNS} warn")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
