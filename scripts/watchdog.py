#!/usr/bin/env python3
"""
TypeOneZen — Watchdog: independent dead-man's switch for the monitoring pipeline.

Runs from launchd (NOT cron) every 5 minutes, entirely independent of the
cron-driven `ns_sync.py -> poller.py -> monitor.py` pipeline it watches. If
cron dies, the Mac reboots without cron re-registering, or a Python import
breaks the whole chain at startup, monitor.py never runs and nothing pages
— this script is the layer below monitor.py that notices and pages anyway.

Deliberately minimal dependencies so it keeps working when the rest of the
system is broken:
  - Resolves the repo root via $TZ_HOME (default ~/TypeOneZen), same
    convention as scripts/log_omnipod_screenshot.py.
  - Loads .env with python-dotenv if importable, else falls back to a tiny
    manual parser (a broken pip environment can't take the watchdog down
    with it).
  - Talks to SQLite read-only (never writes to the DB) and to iMessage via
    the `imsg` CLI directly with subprocess — no nightscout_client, no db.py.

Checks (in order):
  1. Pipeline heartbeat — mtime of logs/monitor.log. monitor.py prints at
     least a summary line every run, so a healthy pipeline advances this
     file's mtime every 5 minutes. Older than HEARTBEAT_MAX_MINUTES means
     cron/monitor.py hasn't run recently (cron dead? Mac slept? Python
     broke at import time before monitor.py could log anything?).
  2. Data freshness — newest glucose_readings timestamp in SQLite. Backstop
     for total blindness when monitor.py itself is dead (so rule 1 above
     didn't already catch it), or when the pipeline runs but CGM data has
     actually stopped arriving. Older than DATA_MAX_MINUTES alerts; an
     unreadable/corrupt DB is itself treated as an alert.

Alerting: /opt/homebrew/bin/imsg, 3 attempts with 10s/30s backoff (mirrors
monitor.py's send_imsg). Throttled via a JSON state file
(logs/watchdog_state.json) so a persisting outage pages at most once per 2h
per check — but the throttle resets the moment a check recovers, so a new
outage always pages immediately rather than waiting out the old cooldown.

If HEALTHCHECKS_URL is set (optional, in .env) and every check passes, this
pings it (GET, best-effort, errors ignored) — a hook for an external
dead-man's switch (e.g. healthchecks.io) that can also notice "the whole
Mac is off", which this script cannot detect on its own since launchd
doesn't run while the Mac is asleep/off either.

Always exits 0 — launchd should never see this script "crash", even if an
individual check blows up; a bug in one check must not silence the rest.

Usage:
    python3 scripts/watchdog.py
    TZ_HOME=/path/to/checkout python3 scripts/watchdog.py
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Paths (TZ_HOME convention — see scripts/log_omnipod_screenshot.py) ──────
TZ_HOME = Path(os.environ.get("TZ_HOME", Path.home() / "TypeOneZen"))
DATA_DIR = TZ_HOME / "data"
DB_PATH = DATA_DIR / "TypeOneZen.db"
LOG_DIR = TZ_HOME / "logs"
ENV_PATH = TZ_HOME / ".env"
MONITOR_LOG_PATH = LOG_DIR / "monitor.log"
STATE_PATH = LOG_DIR / "watchdog_state.json"
WATCHDOG_LOG_PATH = LOG_DIR / "watchdog.log"

IMSG_BIN = "/opt/homebrew/bin/imsg"

# ── Thresholds ────────────────────────────────────────────────────────────
HEARTBEAT_MAX_MINUTES = 20   # monitor.log mtime older than this -> cron/monitor dead
DATA_MAX_MINUTES = 60        # newest CGM reading older than this -> data has gone silent
THROTTLE_HOURS = 2.0         # max one alert per check type per this many hours
HEALTHCHECKS_TIMEOUT_SECONDS = 10
IMSG_SEND_BACKOFFS = [10, 30]  # seconds between the 3 total send attempts


# ── .env loading — dotenv if importable, else a tiny manual fallback ───────

def _manual_parse_env(path: Path) -> dict:
    """Minimal KEY=VALUE .env parser, used only if `dotenv` can't be
    imported (e.g. a broken/partial pip environment). Skips blank lines and
    '#' comments; strips a single layer of matching quotes from values.
    """
    values: dict = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def load_env(env_path: Path) -> None:
    """Populate os.environ from .env. Prefers python-dotenv (matches the
    rest of the codebase); falls back to _manual_parse_env if dotenv isn't
    importable, so a broken pip environment can't take the watchdog down
    with it. Never overrides variables already set in the environment.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(env_path))
    except ImportError:
        for key, val in _manual_parse_env(env_path).items():
            os.environ.setdefault(key, val)


load_env(ENV_PATH)

ALERT_PHONE = os.getenv("ALERT_PHONE", "")
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL", "")


# ── Logging (RotatingFileHandler, 5MB/3 backups — matches poller.py/ns_sync.py) ──

LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("watchdog")
logger.setLevel(logging.DEBUG)
# Re-running load in the same process (e.g. re-imported by tests with a
# different TZ_HOME) should not stack up handlers pointing at stale paths.
logger.handlers.clear()

file_handler = RotatingFileHandler(
    str(WATCHDOG_LOG_PATH),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(file_handler)


# ── Helpers ──────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts_utc(iso_ts: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC if naive (matches monitor.py)."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Checks ───────────────────────────────────────────────────────────────
# Each check returns (ok: bool, message: str | None, age_minutes: float | None).
# message is only meaningful when ok is False.

def check_heartbeat(monitor_log_path: Path, now: datetime,
                     max_minutes: float = HEARTBEAT_MAX_MINUTES) -> tuple:
    """Rule 1: pipeline heartbeat via logs/monitor.log's mtime."""
    if not monitor_log_path.exists():
        msg = (
            "\U0001f415 Watchdog: monitoring pipeline hasn't run — "
            f"{monitor_log_path} doesn't exist (cron dead? Mac slept? "
            "never set up?). BG alerts are DOWN — check the Mac."
        )
        return False, msg, None

    mtime = monitor_log_path.stat().st_mtime
    age_minutes = (now.timestamp() - mtime) / 60

    if age_minutes <= max_minutes:
        return True, None, age_minutes

    msg = (
        f"\U0001f415 Watchdog: monitoring pipeline hasn't run in "
        f"{age_minutes:.0f} min (cron dead? Mac slept?). "
        "BG alerts are DOWN — check the Mac."
    )
    return False, msg, age_minutes


def check_data_freshness(db_path: Path, now: datetime,
                          max_minutes: float = DATA_MAX_MINUTES) -> tuple:
    """Rule 2: newest glucose_readings timestamp, read-only.

    Backstop for total blindness when monitor.py itself is dead. An
    unreadable/corrupt DB is itself an alert condition.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT MAX(timestamp) FROM glucose_readings"
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        msg = (
            f"\U0001f415 Watchdog: can't read the database ({db_path}): {e}. "
            "BG monitoring may be blind — check the Mac."
        )
        return False, msg, None

    newest = row[0] if row else None
    if newest is None:
        msg = (
            "\U0001f415 Watchdog: no CGM data in the database at all. "
            "BG monitoring is blind — check sensor/phone/Mac."
        )
        return False, msg, None

    try:
        newest_ts = parse_ts_utc(newest)
    except (ValueError, TypeError) as e:
        msg = (
            f"\U0001f415 Watchdog: can't parse the newest glucose_readings "
            f"timestamp ({newest!r}): {e}. BG monitoring may be blind — "
            "check the Mac."
        )
        return False, msg, None

    age_minutes = (now - newest_ts).total_seconds() / 60
    if age_minutes <= max_minutes:
        return True, None, age_minutes

    msg = (
        f"\U0001f415 Watchdog: no CGM data for {age_minutes:.0f} min and "
        "counting. BG monitoring is blind — check sensor/phone/Mac."
    )
    return False, msg, age_minutes


# ── Throttle state (JSON file, one entry per check type) ───────────────────

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def should_alert(state: dict, check_key: str, now: datetime,
                  cooldown_hours: float = THROTTLE_HOURS) -> bool:
    """Decide whether a failing check should alert right now, and record
    the decision into `state` in place.

    First failure (no entry yet) always alerts. A persisting failure alerts
    again only after `cooldown_hours` have passed since the last alert.
    Callers must call clear_throttle() on recovery so the NEXT failure — a
    fresh outage — alerts immediately rather than inheriting an old cooldown.
    """
    entry = state.get(check_key)
    last_alert = None
    if entry is not None:
        try:
            last_alert = datetime.fromisoformat(entry["last_alert"])
            if last_alert.tzinfo is None:
                last_alert = last_alert.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            last_alert = None

    if last_alert is not None and (now - last_alert) < timedelta(hours=cooldown_hours):
        return False

    state[check_key] = {"last_alert": now.isoformat()}
    return True


def clear_throttle(state: dict, check_key: str) -> None:
    state.pop(check_key, None)


# ── Alert delivery ───────────────────────────────────────────────────────

def send_alert(message: str) -> bool:
    """Send an iMessage alert via the imsg CLI. 3 attempts total (10s/30s
    backoff between), mirroring monitor.py's send_imsg. Never raises —
    the watchdog must exit 0 regardless of delivery outcome.
    """
    if not ALERT_PHONE:
        logger.error("ALERT_PHONE not set — cannot send alert: %s", message)
        return False

    attempts = len(IMSG_SEND_BACKOFFS) + 1
    for attempt in range(attempts):
        try:
            subprocess.run(
                [IMSG_BIN, "send", "--to", ALERT_PHONE, "--text", message],
                check=True,
                capture_output=True,
            )
            logger.info("Alert sent: %s", message)
            return True
        except Exception as e:
            logger.warning(
                "imsg send attempt %d/%d failed: %s", attempt + 1, attempts, e
            )
            if attempt < attempts - 1:
                time.sleep(IMSG_SEND_BACKOFFS[attempt])

    logger.error("Alert failed after %d attempts: %s", attempts, message)
    return False


def ping_healthchecks(url: str) -> None:
    """Best-effort GET to an external dead-man's-switch URL. Errors are
    logged, never raised — this is a nice-to-have, not a check.
    """
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=HEALTHCHECKS_TIMEOUT_SECONDS)
        logger.info("Healthchecks ping sent to %s", url)
    except Exception as e:
        logger.warning("Healthchecks ping failed (ignored): %s", e)


# ── Main ─────────────────────────────────────────────────────────────────

def run(now: datetime | None = None) -> int:
    """Run all checks once. Returns 0 always (callers should not treat a
    non-zero return as meaningful; kept as an int for testability)."""
    now = now or utc_now()
    state = load_state(STATE_PATH)

    hb_ok, hb_msg, hb_age = check_heartbeat(MONITOR_LOG_PATH, now)
    data_ok, data_msg, data_age = check_data_freshness(DB_PATH, now)

    for key, ok, msg, age in (
        ("heartbeat", hb_ok, hb_msg, hb_age),
        ("data", data_ok, data_msg, data_age),
    ):
        if ok:
            if key in state:
                logger.info("%s check recovered — clearing throttle", key)
            clear_throttle(state, key)
            age_note = f"{age:.1f} min old" if age is not None else "n/a"
            logger.info("%s check OK (%s)", key, age_note)
        else:
            logger.warning("%s check FAILED: %s", key, msg)
            if should_alert(state, key, now):
                if not send_alert(msg):
                    # Delivery failed — don't burn the 2h throttle on an
                    # alert that never reached the phone; the next 5-min
                    # run retries (same semantics as monitor.py's sent=1
                    # escalation filter).
                    clear_throttle(state, key)
            else:
                logger.info(
                    "%s check still failing but throttled "
                    "(alerted within the last %.0fh)", key, THROTTLE_HOURS
                )

    save_state(STATE_PATH, state)

    if hb_ok and data_ok and HEALTHCHECKS_URL:
        ping_healthchecks(HEALTHCHECKS_URL)

    logger.info(
        "Watchdog run complete: heartbeat=%s data=%s",
        "OK" if hb_ok else "FAIL", "OK" if data_ok else "FAIL",
    )
    return 0


def main() -> int:
    try:
        return run()
    except Exception:
        # A bug in the watchdog itself must not look like a crash to
        # launchd, and must not prevent future runs from trying again.
        logger.exception("watchdog crashed unexpectedly")
        return 0


if __name__ == "__main__":
    sys.exit(main())
