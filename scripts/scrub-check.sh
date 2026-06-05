#!/usr/bin/env bash
# scrub-check.sh — pre-publish gate. FAIL-CLOSED: any finding exits non-zero.
#
# Scans the working tree for:
#   1. source-org / baked-ICP strings that must have been templated out
#   2. the network secret-fetch pattern that must never reappear (local env only)
#   3. hardcoded local home paths to a .env
#   4. secret-shaped strings in committed files (placeholders excepted)
# Then runs gitleaks if installed (authoritative).
#
# This script excludes ITSELF from the scan (it necessarily contains the patterns
# it searches for).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

FAIL=0
red()  { printf '\033[31m%s\033[0m\n' "$1"; }
grn()  { printf '\033[32m%s\033[0m\n' "$1"; }
fail() { red   "FAIL  $1"; FAIL=1; }
pass() { grn   "ok    $1"; }
note() { printf '      %s\n' "$1"; }

# grep flags shared by every scan. Never scan .git, local data, caches, the
# lockfiles, the env file, or this script itself.
GREP=(grep -rniI
  --exclude-dir=.git
  --exclude-dir=.gtm-data
  --exclude-dir=__pycache__
  --exclude-dir=node_modules
  --exclude-dir=.venv
  --exclude='.env'
  --exclude='*.lock'
  --exclude='scrub-check.sh')

echo "== scrub-check =="

# 1. source-org / baked-ICP strings ------------------------------------------------
echo "[1/4] source-org & baked-ICP strings"
if "${GREP[@]}" \
     -e 'repath' \
     -e 'cubico' -e 'infracapital' -e 'encavis' -e 'amprion' -e 'transnetbw' \
     -e 'gtm-persona-mapping' \
     . ; then
  fail "source-org / baked-ICP strings present — template them into context/"
else
  pass "no source-org / baked-ICP strings"
fi

# 2. network secret-fetch pattern --------------------------------------------------
echo "[2/4] network secret-fetch pattern"
if "${GREP[@]}" \
     -e 'gh api .*contents/\.env' \
     -e 'eval .*gh api' \
     -e 'curl .*contents/\.env' \
     . ; then
  fail "network secret-fetch bootstrap present — delete it (local env only)"
else
  pass "no network secret-fetch pattern"
fi

# 3. hardcoded local .env paths ----------------------------------------------------
echo "[3/4] hardcoded local .env paths"
if "${GREP[@]}" -E '/Users/[A-Za-z0-9._-]+/.*\.env' . ; then
  fail "hardcoded local .env path present — read from \$ENV instead"
else
  pass "no hardcoded local .env paths"
fi

# 4. secret-shaped strings ---------------------------------------------------------
echo "[4/4] secret-shaped strings in committed files"
SECRET=0
# private keys, anywhere
if "${GREP[@]}" -e 'BEGIN [A-Z ]*PRIVATE KEY' . ; then SECRET=1; fi
# long opaque values assigned to a credential-ish var, excluding .example files
# and obvious placeholders.
if grep -rnI \
     --exclude-dir=.git --exclude-dir=.gtm-data --exclude-dir=__pycache__ \
     --exclude-dir=node_modules --exclude-dir=.venv \
     --exclude='.env' --exclude='*.example' --exclude='scrub-check.sh' \
     -E '(API_KEY|APIKEY|TOKEN|SECRET|PASSWORD|BEARER)["'"'"' ]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9_+/-]{24,}' . \
   | grep -vEi '(<[a-z_]+>|your[_-]|example|placeholder|changeme|xxxx|\bnpg_|user:pass)' ; then
  SECRET=1
fi
if [ "$SECRET" -ne 0 ]; then
  fail "possible secret-shaped string in a committed file — use a placeholder"
else
  pass "no secret-shaped strings"
fi

# Optional authoritative scanner ---------------------------------------------------
if command -v gitleaks >/dev/null 2>&1; then
  echo "[+] gitleaks detect"
  if ! gitleaks detect --no-banner --redact --source "$ROOT" >/dev/null 2>&1; then
    fail "gitleaks reported findings (run: gitleaks detect --redact -v)"
  else
    pass "gitleaks clean"
  fi
else
  note "gitleaks not installed — skipping (brew install gitleaks for an authoritative scan)"
fi

echo
if [ "$FAIL" -ne 0 ]; then
  red "✗ scrub-check FAILED — do not publish."
  exit 1
fi
grn "✓ scrub-check passed."
