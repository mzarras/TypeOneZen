#!/usr/bin/env bash
# TypeOneZen — install OpenClaw skills.
#
# Copies:
#   1. examples/openclaw-skill  -> ~/.openclaw/workspace/skills/typeonezen
#   2. nightscout-client's skills/nightscout -> ~/.openclaw/workspace/skills/nightscout
#      (searched for; not guaranteed to be found — see report at the end)
#
# Idempotent: safe to re-run any time (uses rsync --delete so the target
# always exactly mirrors the source; nothing is appended/duplicated).
#
# Usage:
#   cd ~/TypeOneZen && bash setup/install_skills.sh
#   NSCLIENT_DIR=/path/to/nightscout-client bash setup/install_skills.sh   # override search

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_SKILLS="$HOME/.openclaw/workspace/skills"

echo "=== TypeOneZen OpenClaw skill install ==="
mkdir -p "$WORKSPACE_SKILLS"

# ── 1. typeonezen skill (always available — lives in this repo) ────────────
echo
echo "[1/2] Installing typeonezen skill..."
SRC="$REPO_DIR/examples/openclaw-skill"
DST="$WORKSPACE_SKILLS/typeonezen"

if [ ! -d "$SRC" ]; then
  echo "ERROR: $SRC not found. Is this script running from a TypeOneZen checkout?"
  exit 1
fi

mkdir -p "$DST"
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC/" "$DST/"
echo "OK: $SRC -> $DST"
echo "    ($(find "$DST" -type f | wc -l | tr -d ' ') files)"

# ── 2. nightscout skill (from the sibling nightscout-client repo) ──────────
echo
echo "[2/2] Looking for the nightscout-client skill (skills/nightscout)..."

NS_SKILL_SRC=""

# 0. Explicit override
if [ -n "${NSCLIENT_DIR:-}" ] && [ -d "$NSCLIENT_DIR/skills/nightscout" ]; then
  NS_SKILL_SRC="$NSCLIENT_DIR/skills/nightscout"
  echo "  Found via \$NSCLIENT_DIR override: $NS_SKILL_SRC"
fi

# 1. Common sibling-checkout layouts (TypeOneZen and nightscout-client cloned
#    as siblings, per the PumpStuff repo layout)
if [ -z "$NS_SKILL_SRC" ]; then
  for candidate in \
    "$REPO_DIR/../nightscout-client/skills/nightscout" \
    "$HOME/nightscout-client/skills/nightscout" \
    "$HOME/GitRepos/PumpStuff/nightscout-client/skills/nightscout" \
    "$HOME/GitRepos/nightscout-client/skills/nightscout"
  do
    if [ -d "$candidate" ]; then
      NS_SKILL_SRC="$(cd "$candidate" && pwd)"
      echo "  Found via sibling checkout: $NS_SKILL_SRC"
      break
    fi
  done
fi

# 2. Editable pip install (pip install -e ../nightscout-client) — modern pip
#    (PEP 660) points __file__ at the real source tree, so we can walk up
#    from the installed package to the repo root and look for skills/.
if [ -z "$NS_SKILL_SRC" ]; then
  PKG_DIR="$(python3 -c "
try:
    import nightscout_client, os
    print(os.path.dirname(os.path.abspath(nightscout_client.__file__)))
except Exception:
    pass
" 2>/dev/null || true)"
  if [ -n "$PKG_DIR" ]; then
    # Walk up a few levels looking for a skills/nightscout dir (editable
    # installs land at <repo>/nightscout_client/__init__.py or similar;
    # a regular site-packages install won't have skills/ alongside it).
    CANDIDATE_ROOT="$PKG_DIR"
    for _ in 1 2 3; do
      CANDIDATE_ROOT="$(dirname "$CANDIDATE_ROOT")"
      if [ -d "$CANDIDATE_ROOT/skills/nightscout" ]; then
        NS_SKILL_SRC="$CANDIDATE_ROOT/skills/nightscout"
        echo "  Found via installed package location: $NS_SKILL_SRC"
        break
      fi
    done
  fi
fi

if [ -n "$NS_SKILL_SRC" ]; then
  DST2="$WORKSPACE_SKILLS/nightscout"
  mkdir -p "$DST2"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    "$NS_SKILL_SRC/" "$DST2/"
  echo "OK: $NS_SKILL_SRC -> $DST2"
else
  echo "NOT FOUND: could not locate nightscout-client's skills/nightscout directory."
  echo "  Searched: \$NSCLIENT_DIR, sibling checkouts next to this repo, and the"
  echo "  installed nightscout-client package location."
  echo "  Fix by either:"
  echo "    - cloning nightscout-client as a sibling of this repo, e.g.:"
  echo "        git clone https://github.com/mzarras/nightscout-client.git ~/nightscout-client"
  echo "      then re-run this script, or"
  echo "    - pointing at it directly:"
  echo "        NSCLIENT_DIR=/path/to/nightscout-client bash setup/install_skills.sh"
  echo "  This skill provides nscli usage guidance (loop reason, profile,"
  echo "  status, live stats) — see examples/openclaw-skill/SKILL.md's"
  echo "  'nscli Section' for what it's for. Zenbot works without it, with"
  echo "  reduced guidance on when to reach for nscli vs tz_query.py."
fi

echo
echo "=== Done ==="
echo "Installed skills in $WORKSPACE_SKILLS:"
ls -1 "$WORKSPACE_SKILLS" 2>/dev/null || echo "  (none)"
echo
echo "OpenClaw auto-discovers any <workspace>/skills/<name>/SKILL.md — no"
echo "config registration needed. Restart/reconnect the OpenClaw session"
echo "(or send it a message) to pick up changes."
