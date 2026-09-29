# Changelog

## 2.0 — runs you can check (2026-09)

Carries the run-integrity practices from the production system this pipeline was extracted
from into the public reference implementation. `/gtm <brief>` works as before; the additions
are deterministic checks around it, plus a guided setup.

**New**
- `/gtm-setup`: seven questions and your website become `context/*.md`, a do-not-contact
  list and a config; ends with a readiness check.
- `scripts/doctor.py`: one command that says whether a checkout is ready and what to fix.
- Run ledger (`log_event`, `run_report`) and `/gtm status` / `/gtm resume`. The approved plan
  and expansion are frozen on the list (`update_list`).
- Do-not-contact suppression (`suppress`, `check_suppression`) with a research posture that
  fails open and flags, and a send posture that fails closed. Every export writes a
  `.verdict.json` sidecar and prints `UNSCRUBBED` when no pass was applied.
- List QA (`qa`, `qa_resolve`): duplicate people under name variants, email-domain typos and
  mismatches, role mailboxes, non-E.164 phones and glued-on extensions, shared numbers
  labelled as direct dials. See [docs/run-integrity.md](docs/run-integrity.md).
- Activation preflight (`preflight_activate`): exit 6 until the list is scrubbed after its last
  change and QA-clean. `activate_gate: auto` does not skip it.
- `preview-titles` workflow: checks the inferred title set against titles used at sample
  companies before sourcing is paid for.
- `REVIEW.md` (the quality bar, binding on sub-agents), `AGENTS.md` (contract for Codex, Cursor
  and other agents), `CLAUDE.md`.
- `scripts/test_gates.py` (the gates on real defect shapes, both backends) and
  `scripts/sweep_checks.py` (tests over whole classes of drift), both in `selftest.sh`.
- Worked example is now reproducible: `examples/dach-fintech-cfos/replay.py` regenerates its
  outputs through the real CLI.

**Changed**
- `storage/cli.py` reads the backend from `gtm.config.yaml`; `--backend/--dir` are optional
  overrides. The resolved backend is printed on every call. Categorical exit codes:
  2 bad input · 3 not configured · 4 not found · 5 backend failure · 6 gate blocked.
- Workflows return `failed[]` (`score-leads`: `unscored_ids`) instead of silently filtering
  out items whose sub-agent returned nothing. `enrich-companies` now uses a narrow agent type
  (`company-intel-researcher`); all workflows accept `agentType: ''` to use the generic one.
- Export gains a `phone_ext` column. Phones are stored as E.164 with the extension separate.
- Contacts gain `skip_reason`, `email_source_url`, `phone_source_url`.

**Upgrading a Postgres database:** re-run `psql "$DATABASE_URL" -f storage/postgres/schema.sql`.
It is idempotent and adds the new table and columns in place.

## 1.x

The original reference implementation: brief → seven stages → sequencer, BYOK providers,
role and segment expansion, local and Postgres backends.
