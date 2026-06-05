#!/usr/bin/env python3
"""
scripts/show-plan.py — resolve gtm.config.yaml against your manifests + env and print
which provider each stage will actually use. This mirrors EXACTLY the resolution rule the
agents follow, so it doubles as the provider-swappability proof: change a waterfall (or the
sequencer) in gtm.config.yaml and rerun — the resolved providers change with NO agent edits.

    python3 scripts/show-plan.py [--config gtm.config.yaml] [--providers providers]

Resolution rule (same as every agent's Bootstrap):
  - A provider in a waterfall is AVAILABLE if its manifest is `builtin: true`/auth.type none
    (no key needed), OR its manifest.auth.env is set and non-empty in the environment.
  - providers_enabled: auto => all keyed/builtin are candidates; an explicit list further
    restricts to those names.
  - For each capability, the resolved order = waterfall ∩ available, in waterfall order.

Requires PyYAML (a dev/utility convenience; the agents read YAML natively and need no deps).
"""

import argparse
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required for this helper: pip install pyyaml")


def load_manifest(providers_dir, name):
    path = os.path.join(providers_dir, name, "manifest.yaml")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def availability(manifest):
    """Returns (available: bool, reason: str)."""
    if manifest is None:
        return False, "no manifest"
    auth = manifest.get("auth") or {}
    if manifest.get("builtin") or auth.get("type") == "none" or not auth.get("env"):
        return True, "builtin (no key)"
    env = auth["env"]
    if os.environ.get(env):
        return True, f"{env} set"
    return False, f"{env} not set"


def resolve(waterfall, providers_dir, enabled):
    out = []
    for name in waterfall:
        if enabled != "auto" and name not in enabled:
            out.append((name, False, "disabled (not in providers_enabled)"))
            continue
        ok, reason = availability(load_manifest(providers_dir, name))
        out.append((name, ok, reason))
    return out


def main():
    ap = argparse.ArgumentParser(description="Resolve the pipeline plan from config + env")
    ap.add_argument("--config", default="gtm.config.yaml")
    ap.add_argument("--providers", default="providers")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        sys.exit(f"config not found: {args.config} (copy gtm.config.example.yaml)")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    enabled = cfg.get("providers_enabled", "auto")
    waterfalls = cfg.get("waterfalls", {})
    backend = (cfg.get("storage") or {}).get("backend", "?")

    print(f"storage backend : {backend}")
    print(f"providers_enabled: {enabled}\n")

    order = ["company_search", "company_enrich", "people_search", "qualify",
             "email_enrich", "email_validate", "phone_enrich", "phone_validate",
             "crm_dedupe"]
    caps = [c for c in order if c in waterfalls or c == "qualify"]
    for cap in caps:
        if cap == "qualify":
            print(f"{cap:16s}: context-driven (no provider)")
            continue
        rows = resolve(waterfalls.get(cap, []), args.providers, enabled)
        chosen = [n for n, ok, _ in rows if ok]
        detail = "  ".join(f"{n}{'' if ok else ' (skip)'}" for n, ok, _ in rows) or "—"
        flag = "" if chosen else "   ⚠ NO AVAILABLE PROVIDER"
        print(f"{cap:16s}: {' -> '.join(chosen) or '(none)'}{flag}")
        print(f"{'':16s}    [{detail}]")

    # Sequencer
    seq = cfg.get("sequencer")
    if seq:
        ok, reason = availability(load_manifest(args.providers, seq))
        print(f"\nsequencer       : {seq} {'(ready)' if ok else '(NOT available: ' + reason + ')'}")


if __name__ == "__main__":
    main()
