#!/usr/bin/env bash
#
# One command to set this project up and verify it against your broker.
#
#   bash scripts/setup.sh
#
# It installs dependencies, creates .env if it is missing (prompting for your
# credentials locally — they are never transmitted anywhere), builds the
# dashboard, runs the test suite, and finally runs the read-only broker
# verification and saves a shareable report.
#
# Safe to re-run. It never overwrites an existing .env.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
REPORT="doctor-report.txt"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
step() { printf '\n'; bold "$1"; }

# --------------------------------------------------------------------------
step "1/6  Checking prerequisites"

command -v python3 >/dev/null 2>&1 || die "python3 is not installed."
command -v node    >/dev/null 2>&1 || die "node is not installed (Node 20+ required)."
command -v npm     >/dev/null 2>&1 || die "npm is not installed."

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
ok "python3 ${PY_VERSION}"
ok "node $(node -v)"
[ "${NODE_MAJOR}" -ge 20 ] || warn "Node 20+ is recommended; you have $(node -v)."

# --------------------------------------------------------------------------
step "2/6  Installing dependencies"

if [ -d node_modules ] && [ -f node_modules/.package-lock.json ]; then
  ok "node modules already installed (delete node_modules to reinstall)"
else
  npm install --no-audit --no-fund >/dev/null 2>&1 || die "npm install failed. Run 'npm install' to see why."
  ok "node modules installed"
fi

# pytest is the only Python dependency, and only for the test suite.
if python3 -c 'import pytest' >/dev/null 2>&1; then
  ok "pytest available"
else
  python3 -m pip install --quiet pytest >/dev/null 2>&1 \
    && ok "pytest installed" \
    || warn "could not install pytest; step 5 will be skipped"
fi

# --------------------------------------------------------------------------
step "3/6  Credentials"

# .env must be ignored before we ever write a secret into it.
if ! git check-ignore -q .env 2>/dev/null; then
  die ".env is not gitignored. Refusing to write credentials into a tracked file."
fi

if [ -f .env ]; then
  ok ".env already exists — leaving it untouched"
else
  echo "  Your TradeLocker DEMO credentials are written to .env on THIS machine only."
  echo "  They are never sent anywhere, never logged, and .env is gitignored."
  echo
  printf '  TradeLocker email:      '; read -r TL_EMAIL
  printf '  TradeLocker password:   '; read -rs TL_PASSWORD; echo
  printf '  Server (e.g. GATESFX):  '; read -r TL_SERVER
  printf '  Account ID (the number after #): '; read -r TL_ACC

  umask 077
  {
    echo "# Written by scripts/setup.sh. Never commit this file."
    echo "TRADELOCKER_EMAIL=${TL_EMAIL}"
    echo "TRADELOCKER_PASSWORD=${TL_PASSWORD}"
    echo "TRADELOCKER_SERVER=${TL_SERVER}"
    echo "TRADELOCKER_ACC_ID=${TL_ACC}"
    echo "TRADELOCKER_URL=https://demo.tradelocker.com/backend-api"
    echo
    echo "# paper = simulate fills against live prices, nothing sent to the broker."
    echo "TRADING_MODE=paper"
    echo
    echo "# Protects the dashboard's pause / kill-switch / scan controls."
    echo "DASHBOARD_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  } > .env
  chmod 600 .env
  unset TL_PASSWORD
  ok ".env created with permissions 600"
  warn "Set DATABASE_URL in .env before deploying, or risk state resets on every redeploy."
fi

# Load it for the verification step without exporting the password into logs.
set -a; . ./.env; set +a

# --------------------------------------------------------------------------
step "4/6  Building the dashboard"

npm run build >/dev/null 2>&1 || die "the frontend build failed. Run 'npm run build' to see why."
ok "dashboard built into dist/"

# --------------------------------------------------------------------------
step "5/6  Running the test suite"

if python3 -c 'import pytest' >/dev/null 2>&1; then
  if python3 -m pytest -q >/tmp/pytest-setup.log 2>&1; then
    ok "$(tail -n 1 /tmp/pytest-setup.log)"
  else
    tail -n 20 /tmp/pytest-setup.log
    die "tests failed. Do not deploy until this is resolved."
  fi
else
  warn "pytest unavailable — skipped"
fi

# --------------------------------------------------------------------------
step "6/6  Verifying your broker account (read-only)"

echo "  This connects to TradeLocker and reads only. It never places an order."
echo

# --safe masks balances and account identifiers; the integration details that
# actually matter for diagnosis are preserved.
set +e
python3 -m bot.doctor --safe | tee "${REPORT}"
DOCTOR_STATUS=$?
set -e

echo
bold "Report saved to ${ROOT}/${REPORT}"
echo

if [ "${DOCTOR_STATUS}" -eq 0 ]; then
  bold "Everything passed."
  echo "  Start it with:   npm start"
  echo "  Then open:       http://localhost:5000"
  echo
  echo "  It runs in PAPER mode: orders are simulated against live prices and"
  echo "  nothing is sent to your account. Switch to real demo orders later by"
  echo "  setting TRADING_MODE=demo_live in .env."
else
  bold "Some checks failed."
  echo "  Send the contents of ${REPORT} to Claude — it has account figures"
  echo "  masked and contains exactly what is needed to fix the integration."
fi
