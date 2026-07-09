#!/usr/bin/env bash
# TypeOneZen — idempotent local install script.
#
# Safe to re-run any time. Never overwrites an existing .env.
#
# Usage:
#   cd ~/TypeOneZen && bash setup/install.sh
#
# Known constraint (see setup/SETUP.md "Known Issues"): every TypeOneZen
# script hardcodes Path.home()/"TypeOneZen" for the DB, .env, logs, and
# data dirs — there is no TZ_HOME override actually honored at runtime.
# This script therefore warns (but does not fail) if it isn't running
# from a checkout literally at $HOME/TypeOneZen, because that's the one
# location the rest of the codebase will actually look at.

set -euo pipefail

# ── Resolve repo root (directory containing this script's parent) ──────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXPECTED_DIR="$HOME/TypeOneZen"

cd "$REPO_DIR"

echo "=== TypeOneZen install ==="
echo "Repo dir: $REPO_DIR"

if [ "$REPO_DIR" != "$EXPECTED_DIR" ]; then
  echo
  echo "WARNING: This checkout is not at $EXPECTED_DIR."
  echo "         db.py, poller.py, ns_sync.py, monitor.py, and the parsers"
  echo "         all hardcode \$HOME/TypeOneZen for the DB/.env/logs paths"
  echo "         regardless of where this script runs from. If this isn't"
  echo "         a symlink or the real location, move/clone the repo to"
  echo "         $EXPECTED_DIR before continuing."
  echo
fi

# ── 1. python3 version check (>= 3.9) ───────────────────────────────────────
echo
echo "[1/8] Checking python3 version..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install it first (see setup/SETUP.md section 0)."
  exit 1
fi
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')"
if [ "$PY_OK" != "1" ]; then
  echo "ERROR: python3 is $PY_VERSION, need 3.9+."
  exit 1
fi
echo "OK: python3 $PY_VERSION"

# ── 2. pip3 install -r requirements.txt ─────────────────────────────────────
echo
echo "[2/8] Installing Python dependencies from requirements.txt..."
if python3 -m pip install --user -r requirements.txt; then
  echo "OK: requirements installed (--user)"
else
  echo "First attempt failed (likely PEP 668 externally-managed-environment)."
  echo "Retrying with --break-system-packages..."
  python3 -m pip install --user --break-system-packages -r requirements.txt
  echo "OK: requirements installed (--user --break-system-packages)"
fi

# ── 3. nightscout-client from GitHub if missing ─────────────────────────────
echo
echo "[3/8] Checking for nightscout-client package..."
if python3 -c "import nightscout_client" >/dev/null 2>&1; then
  echo "OK: nightscout_client already importable"
else
  echo "nightscout_client not found — installing from GitHub..."
  echo "  (prefer an editable sibling checkout if you have one: see setup/SETUP.md section 1)"
  if python3 -m pip install --user "git+https://github.com/mzarras/nightscout-client.git"; then
    echo "OK: installed nightscout-client from GitHub"
  else
    echo "First attempt failed (likely PEP 668). Retrying with --break-system-packages..."
    python3 -m pip install --user --break-system-packages "git+https://github.com/mzarras/nightscout-client.git"
    echo "OK: installed nightscout-client from GitHub (--break-system-packages)"
  fi
fi

# ── 4. Create data/logs/summaries dirs ──────────────────────────────────────
echo
echo "[4/8] Creating data directories..."
mkdir -p data/imports/glooko data/imports/fit data/imports/correlatewell logs summaries
echo "OK: data/imports/{glooko,fit,correlatewell}, logs/, summaries/"

# ── 5. .env.example -> .env (never overwrite) ───────────────────────────────
echo
echo "[5/8] Setting up .env..."
if [ -f .env ]; then
  echo "OK: .env already exists — leaving it untouched"
else
  cp .env.example .env
  echo "OK: copied .env.example -> .env (edit it now — see setup/SETUP.md section 3)"
fi

# ── 6. Check for imsg binary ─────────────────────────────────────────────────
echo
echo "[6/8] Checking for imsg CLI..."
IMSG_BIN=""
for candidate in /opt/homebrew/bin/imsg /usr/local/bin/imsg; do
  if [ -x "$candidate" ]; then
    IMSG_BIN="$candidate"
    break
  fi
done
if [ -n "$IMSG_BIN" ]; then
  echo "OK: found imsg at $IMSG_BIN"
else
  echo "MISSING: imsg CLI not found."
  echo "  Install it with:"
  echo "    brew install steipete/tap/imsg"
  echo "  TypeOneZen's monitor.py and scripts/daily_summary.py call it at"
  echo "  the hardcoded path /opt/homebrew/bin/imsg — on Apple Silicon that's"
  echo "  where Homebrew installs it by default. See setup/SETUP.md section 0"
  echo "  for the macOS permissions (Full Disk Access, Automation) it needs."
fi

# ── 7. Check for sqlite3 ─────────────────────────────────────────────────────
echo
echo "[7/8] Checking for sqlite3 CLI..."
if command -v sqlite3 >/dev/null 2>&1; then
  echo "OK: sqlite3 found ($(command -v sqlite3))"
else
  echo "MISSING: sqlite3 not found (unexpected — it ships with macOS)."
  echo "  Install with: brew install sqlite3"
fi

# ── 8. Summary ───────────────────────────────────────────────────────────────
echo
echo "[8/8] Done."
echo
echo "Next steps:"
echo "  1. Edit .env (see setup/SETUP.md section 3)"
echo "  2. python3 db.py   (see setup/SETUP.md section 4 — known first-run quirk documented there)"
echo "  3. Continue with setup/SETUP.md section 5 (data backfill)"
