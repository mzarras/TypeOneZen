#!/usr/bin/env bash
# TypeOneZen — installs the watchdog LaunchAgent (independent pipeline
# dead-man's switch).
#
# This is deliberately NOT part of setup/crontab.txt's cron pipeline —
# scripts/watchdog.py runs under launchd, a scheduler independent of cron,
# so it keeps watching ns_sync/poller/monitor even if cron itself dies, the
# Mac reboots without cron re-registering, or a Python import breaks the
# pipeline before monitor.py can log anything.
#
# Idempotent: safe to re-run. Unloads any existing instance first so a
# re-run after editing the plist actually picks up the change.
#
# Usage:
#   cd ~/TypeOneZen && bash setup/install_watchdog.sh
#
# Uninstall:
#   launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.typeonezen.watchdog.plist
#   rm ~/Library/LaunchAgents/com.typeonezen.watchdog.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.typeonezen.watchdog"
PLIST_SRC="$SCRIPT_DIR/$LABEL.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
USERNAME="$(id -un)"
UID_NUM="$(id -u)"

echo "=== TypeOneZen watchdog install ==="
echo "Repo dir: $REPO_DIR"

if [ ! -f "$PLIST_SRC" ]; then
  echo "ERROR: $PLIST_SRC not found."
  exit 1
fi

# launchd plists take literal absolute paths (no ~ expansion), and the
# rest of this codebase assumes the repo lives at literally $HOME/TypeOneZen
# (see setup/SETUP.md "Known Issues" #3) — warn, don't block, same as
# setup/install.sh does for the same constraint.
if [ "$REPO_DIR" != "$HOME/TypeOneZen" ]; then
  echo
  echo "WARNING: this checkout is at $REPO_DIR, not \$HOME/TypeOneZen."
  echo "         The plist points launchd at"
  echo "         /Users/$USERNAME/TypeOneZen/scripts/watchdog.py regardless of"
  echo "         where this script runs from. Move/symlink the repo to"
  echo "         \$HOME/TypeOneZen first, or the watchdog will run the wrong"
  echo "         copy (or none, if nothing exists there)."
  echo
fi

# ── Fill in the YOUR_MAC_USERNAME placeholder into a temp copy ─────────────
# (the tracked plist keeps the placeholder so it stays generic across
# machines — see the comment block at the top of the plist itself)
TMP_PLIST="$(mktemp -t "${LABEL}.plist")"
trap 'rm -f "$TMP_PLIST"' EXIT
sed "s/YOUR_MAC_USERNAME/$USERNAME/g" "$PLIST_SRC" > "$TMP_PLIST"

echo
echo "[1/3] Installing plist to $PLIST_DEST ..."
mkdir -p "$LAUNCH_AGENTS_DIR"
cp "$TMP_PLIST" "$PLIST_DEST"
echo "OK"

echo
echo "[2/3] Loading via launchctl ..."
# Unload any existing instance first (both forms are safe no-ops if it
# isn't currently loaded) so a re-run after editing the plist takes effect.
launchctl bootout "gui/$UID_NUM" "$PLIST_DEST" >/dev/null 2>&1 || true
launchctl unload -w "$PLIST_DEST" >/dev/null 2>&1 || true

if launchctl bootstrap "gui/$UID_NUM" "$PLIST_DEST" 2>/dev/null; then
  echo "OK: loaded via 'launchctl bootstrap' (modern macOS)"
else
  echo "'launchctl bootstrap' failed or unavailable — falling back to legacy 'launchctl load -w' (older macOS)..."
  launchctl load -w "$PLIST_DEST"
  echo "OK: loaded via 'launchctl load -w'"
fi

echo
echo "[3/3] Verify:"
echo "  \$ launchctl list | grep typeonezen"
launchctl list | grep typeonezen || echo "  (not listed yet — give launchd a few seconds, then re-run that command)"

echo
echo "Runs immediately (RunAtLoad) and then every 5 minutes (StartInterval 300)."
echo "Logs:"
echo "  tail -f $REPO_DIR/logs/watchdog.log            # structured watchdog log"
echo "  tail -f $REPO_DIR/logs/watchdog_launchd.log     # launchd's stdout/stderr capture"
echo
echo "Uninstall:"
echo "  launchctl bootout \"gui/\$(id -u)\" $PLIST_DEST"
echo "  rm $PLIST_DEST"
