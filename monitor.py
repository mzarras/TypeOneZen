"""
TypeOneZen — Zero-token rule-based BG monitor.

Checks BG rules against real CGM + meal data, plus pump/loop rules against
live Nightscout state, and sends iMessage alerts when thresholds are
crossed. No AI — pure Python + SQLite.

Features:
- Progressive alert cadence (escalating re-alerts for sustained conditions)
- IOB estimation from bolus/correction doses
- Snooze mechanism (DB-backed, CLI-controlled)
- Richer context in alerts (IOB, trend, correction suggestions)
- Live "current BG" from Nightscout for rules that cite the latest reading
  (falls back to the newest SQLite row if Nightscout is unavailable/stale)
- Pump rules via Nightscout: low reservoir, pod age, loop-not-looping
  (these no-op if nightscout-client isn't installed/configured)

Usage:
    python3 monitor.py            # Run all rules, send alerts
    python3 monitor.py --dry-run  # Print alerts without sending or logging

    python3 monitor.py --snooze HIGH_STUCK              # Snooze for 2hr (default)
    python3 monitor.py --snooze ALL --snooze-duration 90 # Snooze all for 90 min
    python3 monitor.py --snooze-status                   # List active snoozes
    python3 monitor.py --unsnooze                        # Clear all snoozes

Future: Reply "handled" via iMessage → Apple Shortcut runs:
    python3 ~/TypeOneZen/monitor.py --snooze ALL
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(Path(__file__).resolve().parent / ".env"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_db

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
IMSG = "/opt/homebrew/bin/imsg"
PHONE = os.getenv("ALERT_PHONE", "")

# ── Insulin / BG constants (customize for your pump settings) ──────
AIT_MINUTES = 180       # Active Insulin Time (3 hours)
ISF = 35                # Insulin Sensitivity Factor: 1u drops BG ~35 mg/dL
TARGET_BG = 110         # Target BG for correction calculations
ICR = 4                 # Insulin-to-Carb Ratio (not used in alerting, for reference)

# ── Escalation schedules (minutes after previous alert) ────────────
SCHEDULE_DEFAULT = [0, 30, 60, 60, 60]    # For highs (HIGH_STUCK)
SCHEDULE_URGENT  = [0, 15, 15, 15, 15]    # For lows (LOW_WARNING)

# ── Nightscout pump/loop thresholds ─────────────────────────────────
RESERVOIR_LOW_UNITS = 20.0   # low-reservoir alert threshold (units)
POD_WARN_HOURS = 72          # pod age first warning
POD_URGENT_HOURS = 78        # pod age urgent warning (pod hard-stops at 80h)
POD_HARD_STOP_HOURS = 80
LOOP_STALE_MINUTES = 30      # devicestatus older than this → loop-not-looping
LIVE_BG_MAX_AGE_MINUTES = 15 # live Nightscout reading older than this is stale

# ── Low-alert tiers (loop-aware) ─────────────────────────────────────
# Backtested against 7 months of this user's CGM data (2025-12 → 2026-07):
# the old "projected < 80" trigger went on to a real low (<70) only 24% of
# the time, and rate-only rapid-drop alerts only 16% — the Trio loop
# zero-temps well before carbs are needed. So low alerts now defer to
# Trio's own predictions and only page when the loop itself can't fix it.
LOW_URGENT_BG = 70           # below this: alert unconditionally
LOW_NEAR_BG = 80             # near-low band where loop predictions decide
PRED_SEVERE_BG = 60          # Trio-predicted nadir below this alerts from any BG
LOOP_MAX_AGE_MINUTES = 15    # loop computation older than this → no loop visibility
PRED_HORIZON_STEPS = 12      # 12 × 5-min steps = 60-min prediction window
HIST_MIN_EPISODES = 10       # min similar past episodes to trust the pattern gate
HIST_LOW_RATE_GATE = 0.40    # suppress if <40% of similar drops ever reached <70
RAPID_DROP_MAX_BG = 120      # rapid-drop backstop only when already this low

# ── High-alert tiers (loop-aware) ────────────────────────────────────
# Backtested the same way: the old "avg >200 for 90 min" trigger fired
# ~27×/month with 62% of episodes resolving on their own within 2h, and a
# separate overnight >160 rule would have paged on 56% of all nights.
# What separates a stuck high from one the loop fixes is persistence +
# trend + Trio's own forecast — so the high rule pages only when the loop
# can't fix it (see assess_high_risk).
HIGH_TRIGGER_BG = 180        # episode = contiguous readings above this
HIGH_AVG_BG = 200            # loop-blind fallback also requires 90-min avg above
HIGH_PERSIST_MINUTES = 45    # backtested: cuts noise 62%→35%, misses 1/70 stuck highs
HIGH_URGENT_BG = 300         # this high for 30+ min pages regardless of loop state
HIGH_URGENT_MINUTES = 30
HIGH_EVENTUAL_GATE = 180     # Trio predicts landing above this → it isn't fixing it
HIGH_INSULIN_REQ_GATE = 0.5  # units Trio still wants before a high counts as stuck
HIGH_SITE_SUSPECT_HOURS = 3  # this long >180 with delivery maxed → suspect the pod site

# Nightscout client is optional — pump/loop rules no-op if it isn't installed
try:
    from nightscout_client import NightscoutClient
    from nightscout_client.exceptions import NightscoutError, NightscoutConnectionError
    NIGHTSCOUT_AVAILABLE = True
except ImportError:
    NIGHTSCOUT_AVAILABLE = False

# Set by main() so state-transition rules don't persist state in dry-run
DRY_RUN = False

# ── Dexcom trend descriptions → numeric rate mapping ───────────────
TREND_RATES = {
    "rising quickly":  30,
    "rising":          15,
    "rising slightly":  7,
    "steady":           0,
    "falling slightly": -7,
    "falling":         -15,
    "falling quickly": -30,
}

TREND_ARROWS = {
    "rising quickly":  "\u21c8",   # ⇈
    "rising":          "\u2191",   # ↑
    "rising slightly": "\u2197",   # ↗
    "steady":          "\u2192",   # →
    "falling slightly":"\u2198",   # ↘
    "falling":         "\u2193",   # ↓
    "falling quickly": "\u21ca",   # ⇊
}

# Nightscout direction strings → Dexcom-style trend descriptions, so a live
# Nightscout reading plugs into TREND_RATES/TREND_ARROWS above
NS_DIRECTION_TO_TREND = {
    "DoubleUp":      "rising quickly",
    "SingleUp":      "rising",
    "FortyFiveUp":   "rising slightly",
    "Flat":          "steady",
    "FortyFiveDown": "falling slightly",
    "SingleDown":    "falling",
    "DoubleDown":    "falling quickly",
}


# ── Helpers ─────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(UTC)


def ny_now() -> datetime:
    return datetime.now(NY)


def parse_ts_utc(iso_ts: str) -> datetime:
    """Parse an ISO timestamp, assuming UTC if naive."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def fmt_time_ny(iso_ts: str) -> str:
    """Format an ISO timestamp as a short NY local time string."""
    return parse_ts_utc(iso_ts).astimezone(NY).strftime("%-I:%M%p").lower()


def trend_arrow(start: float, end: float) -> str:
    diff = end - start
    if diff > 10:
        return "\u2197"   # ↗
    elif diff < -10:
        return "\u2198"   # ↘
    return "\u2192"       # →


def estimate_iob(conn) -> float:
    """Estimate current insulin-on-board from bolus/correction doses in the last 3 hours.

    Uses linear decay over AIT_MINUTES. Returns 0.0 if no recent doses.
    """
    cutoff = (utc_now() - timedelta(minutes=AIT_MINUTES)).isoformat()
    doses = conn.execute("""
        SELECT timestamp, units, type FROM insulin_doses
        WHERE timestamp > ? AND type IN ('bolus', 'correction')
        ORDER BY timestamp ASC
    """, (cutoff,)).fetchall()

    now = utc_now()
    total_iob = 0.0

    for dose in doses:
        dose_time = datetime.fromisoformat(dose["timestamp"])
        if dose_time.tzinfo is None:
            dose_time = dose_time.replace(tzinfo=UTC)
        elapsed_min = (now - dose_time).total_seconds() / 60
        remaining_fraction = max(0.0, 1.0 - elapsed_min / AIT_MINUTES)
        total_iob += dose["units"] * remaining_fraction

    return round(total_iob, 2)


# ── Live Trio loop state (Nightscout devicestatus) ──────────────────
#
# Trio uploads its full oref computation every loop cycle: net IOB
# (including basal suspensions the bolus-only estimate can't see), COB,
# the temp-basal decision, prediction arrays, and — when its own zero-temp
# isn't enough to prevent a low — a carbs-required amount in the reason
# string. Low alerting defers to this when it's fresh.

_loop_state = None  # per-run cache: {"loop": dict|None}

_CARBS_REQ_PATTERNS = [
    re.compile(r"add\s+(\d+)\s*g\s+carbs", re.I),
    re.compile(r"(\d+)\s*(?:g\s+)?add'?l\s+carbs\s+req", re.I),
    re.compile(r"carbsReq[:\s]+(\d+)", re.I),
]


def fetch_loop_state() -> dict | None:
    """Fetch Trio's latest loop computation once per run (cached).

    Returns the nightscout-client loop() dict (iob/cob/eventual_bg/pred_bgs/
    temp_rate/reason/...) or None when Nightscout isn't configured, errors,
    or the computation is older than LOOP_MAX_AGE_MINUTES. Callers treat
    None as "no loop visibility" and fall back to CGM-only logic.
    """
    global _loop_state
    if _loop_state is not None:
        return _loop_state["loop"]

    loop = None
    if NIGHTSCOUT_AVAILABLE and os.getenv("NIGHTSCOUT_URL"):
        try:
            client = NightscoutClient.from_env()
            data = client.loop()
            age = data.get("data_age_minutes") if data else None
            if data and age is not None and age <= LOOP_MAX_AGE_MINUTES:
                loop = data
                print(f"  [LOOP] Trio loop state: IOB {data.get('iob')}u, "
                      f"eventual {data.get('eventual_bg')}, temp {data.get('temp_rate')} U/hr "
                      f"({age:.0f} min old)")
            else:
                print(f"  [LOOP] loop data stale/missing ({age} min old) — CGM-only fallback")
        except Exception as e:
            print(f"  [LOOP] loop state unavailable ({e}) — CGM-only fallback")

    _loop_state = {"loop": loop}
    return loop


def parse_carbs_req(reason: str | None) -> int | None:
    """Extract oref's carbs-required grams from a loop reason string."""
    if not reason:
        return None
    for pat in _CARBS_REQ_PATTERNS:
        m = pat.search(reason)
        if m:
            return int(m.group(1))
    return None


def loop_predicted_min(loop: dict | None) -> float | None:
    """Trio's predicted BG minimum over the next PRED_HORIZON_STEPS×5 min.

    Takes the min across all prediction scenarios (IOB/ZT/COB/UAM); falls
    back to eventual_bg if the arrays are absent.
    """
    if loop is None:
        return None
    preds = loop.get("pred_bgs") or {}
    mins = [min(arr[:PRED_HORIZON_STEPS]) for arr in preds.values() if arr]
    if mins:
        return float(min(mins))
    return loop.get("eventual_bg")


def describe_loop_action(loop: dict | None) -> str:
    """Human-readable summary of what Trio is currently doing about it."""
    if loop is None:
        return "no loop data"
    rate = loop.get("temp_rate")
    if rate is None:
        return "loop active"
    if rate == 0:
        return "basal suspended"
    return f"temp basal {rate:g} U/hr"


def current_iob(conn) -> tuple[float, str]:
    """(IOB units, source): Trio's net IOB when fresh, else the bolus-decay
    estimate. Trio's number accounts for suspended/temp basal — the estimate
    can read several units high while the loop is zero-temping.
    """
    loop = fetch_loop_state()
    if loop is not None and loop.get("iob") is not None:
        return round(float(loop["iob"]), 2), "trio"
    return estimate_iob(conn), "est"


def effective_isf() -> float:
    """Trio's autosens-adjusted ISF when available, else the static constant."""
    loop = fetch_loop_state()
    if loop is not None and loop.get("isf"):
        return float(loop["isf"])
    return ISF


# ── Historical drop patterns (7 months of CGM) ──────────────────────

_history_cache = None  # per-run cache: list[(datetime, int)] ascending


def similar_drop_history(conn, bg: float, rate_per_15: float,
                         bg_tol: float = 8, rate_tol: float = 8) -> dict | None:
    """How drops like the current one resolved in this user's own history.

    Scans all stored CGM readings for past moments with similar BG and
    15-min rate of change, then checks the following 60 min for a real low.
    Episodes closer than 45 min apart count once; the last 2 hours are
    excluded (that's the episode being evaluated). Returns None when there
    are fewer than HIST_MIN_EPISODES matches to learn from.
    """
    global _history_cache
    if _history_cache is None:
        rows = conn.execute("""
            SELECT timestamp, glucose_mg_dl FROM glucose_readings
            ORDER BY timestamp ASC
        """).fetchall()
        _history_cache = [(parse_ts_utc(r["timestamp"]), r["glucose_mg_dl"]) for r in rows]

    readings = _history_cache
    now = utc_now()
    episodes = 0
    went_low = 0
    nadirs = []
    last_match = None

    for i in range(2, len(readings) - 3):
        ts, g = readings[i]
        if (now - ts).total_seconds() < 2 * 3600:
            continue
        t_old, g_old = readings[i - 2]
        span_min = (ts - t_old).total_seconds() / 60
        if not (0 < span_min <= 20):
            continue
        rate = (g - g_old) / span_min * 15
        if abs(g - bg) > bg_tol or abs(rate - rate_per_15) > rate_tol:
            continue
        if last_match is not None and (ts - last_match).total_seconds() < 45 * 60:
            continue
        last_match = ts
        future = [b for (t, b) in readings[i + 1:i + 40]
                  if (t - ts).total_seconds() <= 3600]
        if not future:
            continue
        episodes += 1
        nadir = min(future)
        nadirs.append(nadir)
        if nadir < LOW_URGENT_BG:
            went_low += 1

    if episodes < HIST_MIN_EPISODES:
        return None

    nadirs.sort()
    return {
        "episodes": episodes,
        "went_low": went_low,
        "low_rate": went_low / episodes,
        "median_nadir": nadirs[len(nadirs) // 2],
    }


# ── Live current BG (Nightscout) ────────────────────────────────────
#
# Rules that cite the "current" BG try a live Nightscout reading first so
# alerts never lag behind the 5-minute sync cadence (or race ns_sync's DB
# writes). Falls back to the newest SQLite row if Nightscout isn't
# configured/reachable or the reading is stale. Read-only: the live reading
# is never inserted into the DB — ns_sync owns writes.

_live_bg_state = None  # per-run cache: {"reading": dict|None}


def fetch_live_bg() -> dict | None:
    """Fetch the newest CGM reading live from Nightscout once per run (cached).

    Returns {"glucose_mg_dl": int, "timestamp": ISO-UTC str, "trend": str|None}
    shaped like a glucose_readings row, or None if Nightscout is not
    configured, unreachable, errors, or returns an empty/stale result.
    """
    global _live_bg_state
    if _live_bg_state is not None:
        return _live_bg_state["reading"]

    reading = None
    if NIGHTSCOUT_AVAILABLE and os.getenv("NIGHTSCOUT_URL"):
        try:
            client = NightscoutClient.from_env()
            entries = client.entries(count=1)  # newest first
            entry = entries[0] if entries else {}
            sgv = entry.get("sgv")
            raw_time = entry.get("time")
            if sgv is not None and raw_time:
                ts = parse_ts_utc(raw_time.replace("Z", "+00:00"))
                age_min = (utc_now() - ts).total_seconds() / 60
                if age_min <= LIVE_BG_MAX_AGE_MINUTES:
                    reading = {
                        "glucose_mg_dl": int(sgv),
                        "timestamp": ts.astimezone(UTC).isoformat(),
                        "trend": NS_DIRECTION_TO_TREND.get(entry.get("direction")),
                    }
                    print(f"  [BG SOURCE] live Nightscout: {reading['glucose_mg_dl']} "
                          f"at {fmt_time_ny(reading['timestamp'])} ({age_min:.0f} min old)")
                else:
                    print(f"  [BG SOURCE] Nightscout reading stale ({age_min:.0f} min old) "
                          f"— falling back to SQLite")
            else:
                print("  [BG SOURCE] Nightscout returned no usable entry — falling back to SQLite")
        except Exception as e:
            print(f"  [BG SOURCE] live Nightscout unavailable ({e}) — falling back to SQLite")
    else:
        print("  [BG SOURCE] Nightscout not configured — using SQLite")

    _live_bg_state = {"reading": reading}
    return reading


def get_current_reading(conn) -> dict | None:
    """Newest BG reading for "current BG" checks.

    Uses the live Nightscout reading when it's fresher than SQLite, else the
    newest SQLite row. Returns a dict with glucose_mg_dl/timestamp/trend keys
    (same shape either way), or None if there's no data at all.
    """
    row = conn.execute("""
        SELECT glucose_mg_dl, timestamp, trend FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

    live = fetch_live_bg()
    if live is not None and (row is None or
                             parse_ts_utc(live["timestamp"]) > parse_ts_utc(row["timestamp"])):
        return live
    return dict(row) if row is not None else None


def get_bg_trend(conn) -> dict:
    """Calculate BG trend from recent readings.

    Returns dict with:
        rate_per_15: float — mg/dL change per 15 min (negative = falling)
        arrow: str — trend arrow character
        description: str — e.g. "falling", "rising", "stable"
        current_bg: int or None
        current_ts: str or None
        dexcom_trend: str or None — raw Dexcom trend string
    """
    readings = [dict(r) for r in conn.execute("""
        SELECT glucose_mg_dl, timestamp, trend FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 3
    """).fetchall()]

    # Prefer a fresher live Nightscout reading as the "current" one
    live = fetch_live_bg()
    if live is not None and (not readings or
                             parse_ts_utc(live["timestamp"]) > parse_ts_utc(readings[0]["timestamp"])):
        readings.insert(0, live)
        readings = readings[:3]

    result = {
        "rate_per_15": 0.0,
        "arrow": "\u2192",
        "description": "stable",
        "current_bg": None,
        "current_ts": None,
        "dexcom_trend": None,
    }

    if not readings:
        return result

    result["current_bg"] = readings[0]["glucose_mg_dl"]
    result["current_ts"] = readings[0]["timestamp"]
    result["dexcom_trend"] = readings[0]["trend"]

    # Primary: use Dexcom trend if available
    dexcom_trend = readings[0]["trend"]
    if dexcom_trend and dexcom_trend in TREND_RATES:
        rate = TREND_RATES[dexcom_trend]
        result["rate_per_15"] = rate
        result["arrow"] = TREND_ARROWS.get(dexcom_trend, "\u2192")
        if rate > 5:
            result["description"] = "rising"
        elif rate < -5:
            result["description"] = "falling"
        else:
            result["description"] = "stable"
        return result

    # Fallback: calculate from last 3 readings
    if len(readings) >= 2:
        newest = readings[0]
        oldest = readings[-1]
        try:
            t_new = datetime.fromisoformat(newest["timestamp"])
            t_old = datetime.fromisoformat(oldest["timestamp"])
            span_min = (t_new - t_old).total_seconds() / 60
            if span_min > 0:
                rate = (newest["glucose_mg_dl"] - oldest["glucose_mg_dl"]) / span_min * 15
                result["rate_per_15"] = round(rate, 1)
                if rate > 5:
                    result["arrow"] = "\u2197"
                    result["description"] = "rising"
                elif rate < -5:
                    result["arrow"] = "\u2198"
                    result["description"] = "falling"
        except (ValueError, TypeError):
            pass

    return result


def suggested_correction(current_bg: float, target: float = TARGET_BG,
                         isf: float = ISF, iob: float = 0.0) -> float:
    """Calculate suggested correction dose (informational only)."""
    needed = (current_bg - target) / isf
    adjusted = max(0.0, needed - iob)
    return round(adjusted, 1)


def get_alert_history(conn, rule_name: str, hours: float = 6.0,
                      dedup_key: str = None) -> list[dict]:
    """Return all alerts for this rule in the last N hours, ordered by triggered_at ASC.

    If dedup_key is provided, filters to alerts with that specific dedup_key.
    """
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    # sent = 1 only: an alert that failed to deliver must not consume an
    # escalation slot — excluding it here makes the next 5-min run retry.
    if dedup_key is not None:
        rows = conn.execute("""
            SELECT triggered_at, message, dedup_key FROM alert_log
            WHERE rule_name = ? AND triggered_at > ? AND dedup_key = ? AND sent = 1
            ORDER BY triggered_at ASC
        """, (rule_name, cutoff, dedup_key)).fetchall()
    else:
        rows = conn.execute("""
            SELECT triggered_at, message, dedup_key FROM alert_log
            WHERE rule_name = ? AND triggered_at > ? AND sent = 1
            ORDER BY triggered_at ASC
        """, (rule_name, cutoff)).fetchall()

    return [{"triggered_at": r["triggered_at"], "message": r["message"],
             "dedup_key": r["dedup_key"]} for r in rows]


def should_escalate(alert_history: list[dict], schedule: list[int]) -> tuple:
    """Determine if enough time has passed to fire the next escalation level.

    Returns (should_fire: bool, level: int).
    """
    n = len(alert_history)

    # No previous alerts — fire at level 0
    if n == 0:
        return (True, 0)

    # Get required wait for the next level
    wait_minutes = schedule[min(n, len(schedule) - 1)]
    last_alert_time = datetime.fromisoformat(alert_history[-1]["triggered_at"])
    if last_alert_time.tzinfo is None:
        last_alert_time = last_alert_time.replace(tzinfo=UTC)

    elapsed = (utc_now() - last_alert_time).total_seconds() / 60

    if elapsed >= wait_minutes:
        return (True, n)
    return (False, n)


def is_snoozed(conn, rule_name: str) -> bool:
    """Check if a rule is currently snoozed."""
    now_iso = utc_now().isoformat()
    row = conn.execute("""
        SELECT id FROM alert_snoozes
        WHERE (rule_name = ? OR rule_name = 'ALL')
          AND expires_at > ?
        LIMIT 1
    """, (rule_name, now_iso)).fetchone()
    return row is not None


# ── Table management ───────────────────────────────────────────────

def ensure_tables(conn):
    """Create alert_log and alert_snoozes tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name   TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            message     TEXT NOT NULL,
            sent        INTEGER DEFAULT 0,
            dedup_key   TEXT
        )
    """)
    # Add dedup_key column if table already existed without it
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(alert_log)").fetchall()]
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE alert_log ADD COLUMN dedup_key TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_snoozes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_name   TEXT NOT NULL,
            snoozed_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            reason      TEXT
        )
    """)
    # Key/value state store (shared with ns_sync.py) — used by the
    # state-transition pump rules (e.g. low reservoir crossing).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def was_recently_alerted(conn, rule_name: str, hours: float = 2.0) -> bool:
    """Check if this rule fired within the last `hours` hours."""
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    row = conn.execute("""
        SELECT id FROM alert_log
        WHERE rule_name = ? AND triggered_at > ? AND sent = 1
        LIMIT 1
    """, (rule_name, cutoff)).fetchone()
    return row is not None


def send_imsg(message: str):
    """Send an iMessage via the imsg CLI tool."""
    if not PHONE:
        raise RuntimeError("ALERT_PHONE not set in .env")
    subprocess.run(
        [IMSG, "send", "--to", PHONE, "--text", message],
        check=True,
        capture_output=True,
    )


# ── Rule implementations ───────────────────────────────────────────

_MAX_BASAL_RE = re.compile(r"maxSafeBasal:?\s*([\d.]+)", re.I)


def loop_delivery_maxed(loop: dict | None) -> bool:
    """True when Trio's temp basal is pinned at its safety cap — the reason
    string cites maxSafeBasal when the requested rate was clamped."""
    if loop is None:
        return False
    m = _MAX_BASAL_RE.search(loop.get("reason") or "")
    if not m:
        return False
    temp = loop.get("temp_rate")
    return temp is not None and temp >= float(m.group(1)) - 0.05


def current_high_episode(conn) -> dict | None:
    """The in-progress high episode: contiguous readings above HIGH_TRIGGER_BG.

    Walks recent readings newest-first; the episode is broken only by two
    consecutive readings at/below the threshold, so a single sensor dip
    doesn't reset the escalation clock. The episode's start timestamp is the
    escalation dedup_key — a new high never inherits a resolved episode's
    escalation level. Returns {"start_ts", "duration_min", "avg_bg"} or None
    when the current BG isn't above the threshold.
    """
    current = get_current_reading(conn)
    if current is None or current["glucose_mg_dl"] <= HIGH_TRIGGER_BG:
        return None

    cutoff = (utc_now() - timedelta(hours=8)).isoformat()
    readings = [dict(r) for r in conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,)).fetchall()]

    if not readings or parse_ts_utc(current["timestamp"]) > parse_ts_utc(readings[0]["timestamp"]):
        readings.insert(0, {"glucose_mg_dl": current["glucose_mg_dl"],
                            "timestamp": current["timestamp"]})

    episode = []
    low_streak = 0
    for r in readings:
        if r["glucose_mg_dl"] > HIGH_TRIGGER_BG:
            low_streak = 0
            episode.append(r)
        else:
            low_streak += 1
            if low_streak >= 2:
                break

    if not episode:
        return None

    start_ts = episode[-1]["timestamp"]
    duration_min = (parse_ts_utc(episode[0]["timestamp"])
                    - parse_ts_utc(start_ts)).total_seconds() / 60
    avg_bg = sum(r["glucose_mg_dl"] for r in episode) / len(episode)
    return {"start_ts": start_ts, "duration_min": duration_min,
            "avg_bg": avg_bg}


def assess_high_risk(conn, episode: dict, current_bg: float,
                     rate_per_15: float) -> dict | None:
    """Decide whether the in-progress high warrants an alert.

    Tiers (first match wins):
      URGENT       — BG ≥ HIGH_URGENT_BG for HIGH_URGENT_MINUTES. A high this
                     severe can mean a failed pod site, where even Trio's
                     predictions can't be trusted; pages regardless of loop.
      SITE_SUSPECT — hours above HIGH_TRIGGER_BG with delivery pinned at
                     Trio's safety cap and Trio still predicting a high
                     landing: insulin isn't winning — bad site or missed bolus.
      STUCK        — persisted ≥ HIGH_PERSIST_MINUTES, not falling, and
                     Trio's own forecast lands above HIGH_EVENTUAL_GATE while
                     it still wants ≥ HIGH_INSULIN_REQ_GATE units.
      LOOP_BLIND   — no fresh loop data: CGM-only fallback (persisted, not
                     falling, 90-min avg > HIGH_AVG_BG).

    Returns None when no alert should fire — notably whenever Trio's
    eventual_bg says the correction it's already running lands in range
    (backtested: 62% of "avg >200" episodes resolve within 2h on their own).
    """
    # URGENT: severe high sustained for HIGH_URGENT_MINUTES
    cutoff = (utc_now() - timedelta(minutes=HIGH_URGENT_MINUTES + 5)).isoformat()
    row = conn.execute("""
        SELECT MIN(glucose_mg_dl) AS min_bg, COUNT(*) AS cnt
        FROM glucose_readings WHERE timestamp > ?
    """, (cutoff,)).fetchone()
    if (current_bg >= HIGH_URGENT_BG and row is not None and row["cnt"] >= 4
            and row["min_bg"] is not None and row["min_bg"] >= HIGH_URGENT_BG):
        return {"tier": "URGENT", "loop": fetch_loop_state()}

    falling = rate_per_15 < -5
    loop = fetch_loop_state()

    if loop is not None:
        eventual = loop.get("eventual_bg")
        insulin_req = loop.get("insulin_req") or 0.0
        if (episode["duration_min"] >= HIGH_SITE_SUSPECT_HOURS * 60
                and loop_delivery_maxed(loop)
                and (eventual is None or eventual > HIGH_EVENTUAL_GATE)):
            return {"tier": "SITE_SUSPECT", "loop": loop}
        if (episode["duration_min"] >= HIGH_PERSIST_MINUTES and not falling
                and eventual is not None and eventual > HIGH_EVENTUAL_GATE
                and insulin_req >= HIGH_INSULIN_REQ_GATE):
            return {"tier": "STUCK", "loop": loop}
        return None  # Trio's correction is on track to land in range

    if episode["duration_min"] >= HIGH_PERSIST_MINUTES and not falling:
        cutoff90 = (utc_now() - timedelta(minutes=90)).isoformat()
        avg_row = conn.execute("""
            SELECT AVG(glucose_mg_dl) AS avg_bg, COUNT(*) AS cnt
            FROM glucose_readings WHERE timestamp > ?
        """, (cutoff90,)).fetchone()
        if (avg_row is not None and avg_row["cnt"] >= 6
                and avg_row["avg_bg"] is not None
                and avg_row["avg_bg"] > HIGH_AVG_BG):
            return {"tier": "LOOP_BLIND", "loop": None}

    return None


def rule_high_stuck(conn) -> list[dict]:
    """Rule 1: Stuck high — loop-aware replacement for the old sustained-high,
    overnight-high, and post-meal-spike rules.

    One episode = contiguous time above HIGH_TRIGGER_BG (current_high_episode);
    the episode start is the escalation dedup_key. Follow-ups re-run the full
    assessment, so escalations stop the moment Trio regains the upper hand.
    Overnight gets no special lower threshold: being woken is only worth it
    when the loop can't fix the high, which is exactly what the tiers encode.
    """
    trend = get_bg_trend(conn)
    if trend["current_bg"] is None:
        return []
    current_bg = trend["current_bg"]

    episode = current_high_episode(conn)
    if episode is None:
        return []

    assessment = assess_high_risk(conn, episode, current_bg, trend["rate_per_15"])
    if assessment is None:
        return []

    history = get_alert_history(conn, "HIGH_STUCK", hours=12.0,
                                dedup_key=episode["start_ts"])
    should_fire, level = should_escalate(history, SCHEDULE_DEFAULT)
    if not should_fire:
        return []

    current_time = fmt_time_ny(trend["current_ts"])
    arrow = trend["arrow"]
    dur = episode["duration_min"]
    dur_note = f"{dur / 60:.1f}h" if dur >= 90 else f"{dur:.0f} min"
    since = ""
    if level > 0 and history:
        since = f" — first alert {fmt_time_ny(history[0]['triggered_at'])}"

    tier = assessment["tier"]
    loop = assessment.get("loop")

    trio_bits = []
    if loop is not None:
        if loop.get("temp_rate") is not None:
            maxed = " (maxed)" if loop_delivery_maxed(loop) else ""
            trio_bits.append(f"temp {loop['temp_rate']:g} U/hr{maxed}")
        if loop.get("iob") is not None:
            trio_bits.append(f"IOB {loop['iob']:.1f}u net")
        if loop.get("cob"):
            trio_bits.append(f"COB {loop['cob']:.0f}g")
        if loop.get("eventual_bg") is not None:
            trio_bits.append(f"predicts ~{loop['eventual_bg']:.0f}")
    trio_note = ("Trio: " + ", ".join(trio_bits) + ".") if trio_bits else "No loop data."

    if tier == "URGENT":
        msg = (
            f"\U0001f6a8 BG {current_bg:.0f}{arrow} — at/above {HIGH_URGENT_BG} for "
            f"{HIGH_URGENT_MINUTES}+ min ({current_time}){since}. {trio_note}\n"
            f"Check the pod site — a manual correction may be needed."
        )
    elif tier == "SITE_SUSPECT":
        msg = (
            f"\U0001f6a8 High for {dur_note} and Trio is maxed out: BG {current_bg:.0f}{arrow} "
            f"(avg {episode['avg_bg']:.0f}) ({current_time}){since}. {trio_note}\n"
            f"Suspect pod site or missed meal bolus — consider a pod change or manual injection."
        )
    elif tier == "STUCK":
        insulin_req = (loop or {}).get("insulin_req") or 0.0
        cob = (loop or {}).get("cob") or 0.0
        extras = []
        if insulin_req >= HIGH_INSULIN_REQ_GATE:
            extras.append(f"Manual bolus of ~{insulin_req:.1f}u would cover what Trio still wants.")
        if cob > 0:
            extras.append(f"{cob:.0f}g COB still absorbing — possibly an under-bolused meal.")
        msg = (
            f"\u26a0\ufe0f High and stuck: {current_bg:.0f}{arrow} for {dur_note} "
            f"(avg {episode['avg_bg']:.0f}) ({current_time}){since}. {trio_note}\n"
            f"{' '.join(extras)}".rstrip()
        )
    else:  # LOOP_BLIND
        iob, _ = current_iob(conn)
        msg = (
            f"\u26a0\ufe0f Sustained high with no loop visibility: BG {current_bg:.0f}{arrow} "
            f"for {dur_note} (avg {episode['avg_bg']:.0f}) ({current_time}){since}. "
            f"IOB {iob}u (est.).\n"
            f"Can't confirm Trio is correcting — check the loop and consider a manual correction."
        )

    return [{"rule": "HIGH_STUCK", "message": msg, "dedup_key": episode["start_ts"]}]


def rule_rapid_drop(conn) -> list[dict]:
    """Rule 3: Rapid drop — CGM-only backstop for when the loop is blind.

    When Trio is looping with fresh data, its predictions own low alerting
    (rule_low_warning defers to them) and it reacts to a fast drop faster
    than a human can — backtesting 7 months of CGM showed rate-only drop
    alerts reached a real low just 16% of the time. So this fires ONLY when
    there is no fresh loop computation (Nightscout down / loop stale), BG
    dropped > 30 in ≤ 30 min, and BG is already below RAPID_DROP_MAX_BG
    (a fast drop from 200 isn't a low emergency).

    Keeps simple 2hr cooldown (no progressive escalation). Skipped if
    LOW_WARNING alerted in the last 30 min — one alert per event.
    """
    if fetch_loop_state() is not None:
        return []

    if was_recently_alerted(conn, "RAPID_DROP"):
        return []

    if was_recently_alerted(conn, "LOW_WARNING", hours=0.5):
        return []

    readings = [dict(r) for r in conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 6
    """).fetchall()]

    # Prefer a fresher live Nightscout reading as the "current" one
    live = fetch_live_bg()
    if live is not None and (not readings or
                             parse_ts_utc(live["timestamp"]) > parse_ts_utc(readings[0]["timestamp"])):
        readings.insert(0, live)
        readings = readings[:6]

    if len(readings) < 2:
        return []

    current_bg = readings[0]["glucose_mg_dl"]
    current_ts = readings[0]["timestamp"]
    oldest_bg = readings[-1]["glucose_mg_dl"]
    oldest_ts = readings[-1]["timestamp"]

    try:
        t_current = datetime.fromisoformat(current_ts)
        t_oldest = datetime.fromisoformat(oldest_ts)
        span_min = (t_current - t_oldest).total_seconds() / 60
    except (ValueError, TypeError):
        return []

    if span_min < 10:
        return []

    drop = oldest_bg - current_bg
    if drop > 30 and current_bg < RAPID_DROP_MAX_BG:
        rate = drop / span_min * 60  # per hour
        bg_time = fmt_time_ny(current_ts)

        iob = estimate_iob(conn)
        est_drop = round(iob * effective_isf())

        if iob > 0:
            iob_note = f"IOB ~{iob}u est. (no loop data \u2014 est. further drop ~{est_drop} mg/dL)."
        else:
            iob_note = "IOB: no recent doses on record (no loop data)."

        msg = (
            f"\u26a0\ufe0f Rapid drop with no loop visibility: {oldest_bg}\u2192{current_bg} "
            f"in {span_min:.0f} min ({rate:.0f}/hr) as of {bg_time}.\n"
            f"{iob_note} Watch closely \u2014 fast carbs if it keeps falling."
        )
        return [{"rule": "RAPID_DROP", "message": msg}]
    return []


def rule_pre_workout_low_risk(conn) -> list[dict]:
    """Rule 4: BG < 120 near typical workout start time."""
    if was_recently_alerted(conn, "PRE_WORKOUT_LOW_RISK"):
        return []

    now_ny = ny_now()
    now_hour_min = now_ny.hour * 60 + now_ny.minute

    workouts = conn.execute("""
        SELECT started_at FROM workouts WHERE started_at IS NOT NULL
    """).fetchall()

    if not workouts:
        return []

    workout_minutes = []
    for w in workouts:
        try:
            dt = datetime.fromisoformat(w["started_at"]).replace(tzinfo=UTC).astimezone(NY)
            workout_minutes.append(dt.hour * 60 + dt.minute)
        except (ValueError, TypeError):
            continue

    if not workout_minutes:
        return []

    near_workout = any(abs(now_hour_min - wm) <= 30 for wm in workout_minutes)

    if not near_workout:
        return []

    current_row = get_current_reading(conn)

    if current_row is None:
        return []

    current_bg = current_row["glucose_mg_dl"]
    current_time = fmt_time_ny(current_row["timestamp"])
    if current_bg < 120:
        msg = (
            f"\U0001f3c3 Heading into typical workout time with BG at {current_bg} "
            f"({current_time}) — consider a small snack if running soon."
        )
        return [{"rule": "PRE_WORKOUT_LOW_RISK", "message": msg}]
    return []


def assess_low_risk(conn, current_bg: float, rate_per_15: float,
                    description: str) -> dict | None:
    """Decide whether the current situation warrants a low alert.

    Tiers (first match wins):
      URGENT    — BG already < LOW_URGENT_BG: always alert.
      CARBS_REQ — Trio's own algorithm says carbs are required (its
                  zero-temp isn't enough). The canonical "you must act" signal.
      PREDICTED — near-low (BG < LOW_NEAR_BG) with Trio predicting < 70
                  within the hour, or a predicted nadir < PRED_SEVERE_BG
                  from any BG.
      FALLBACK  — no fresh loop data: CGM-only projection, gated by how
                  similar drops resolved in this user's own history.

    Returns None when no alert should fire — notably whenever Trio has
    fresh predictions showing the dip resolves on its own: the loop cuts
    basal well before carbs are needed, and backtesting 7 months of this
    user's CGM shows those moments self-resolve ~3 out of 4 times.
    """
    if current_bg < LOW_URGENT_BG:
        return {"tier": "URGENT", "loop": fetch_loop_state()}

    loop = fetch_loop_state()
    if loop is not None:
        carbs_req = parse_carbs_req(loop.get("reason"))
        pred_min = loop_predicted_min(loop)
        if carbs_req:
            return {"tier": "CARBS_REQ", "carbs_req": carbs_req,
                    "pred_min": pred_min, "loop": loop}
        if pred_min is not None:
            if (current_bg < LOW_NEAR_BG and pred_min < LOW_URGENT_BG) \
                    or pred_min < PRED_SEVERE_BG:
                return {"tier": "PREDICTED", "pred_min": pred_min, "loop": loop}
            return None  # Trio sees the drop and predicts self-resolution
        # loop fresh but no predictions → fall through to CGM-only logic

    falling = description == "falling"
    projected = current_bg + rate_per_15
    near_low_falling = current_bg < LOW_NEAR_BG and falling
    projected_low = falling and projected < LOW_URGENT_BG

    if not (near_low_falling or projected_low):
        return None

    hist = similar_drop_history(conn, current_bg, rate_per_15)
    # Pattern gate applies only above the near-low band: a projected low
    # from BG >= 80 is suppressed when this user's own history says drops
    # like this almost never reach 70.
    if (hist is not None and current_bg >= LOW_NEAR_BG
            and hist["low_rate"] < HIST_LOW_RATE_GATE):
        return None

    return {"tier": "FALLBACK", "hist": hist, "loop": None}


def rule_low_warning(conn) -> list[dict]:
    """Rule 6: Low warning — loop-aware and pattern-gated.

    Defers to Trio's live predictions (assess_low_risk); when the loop is
    healthy and predicts the dip resolves, no alert is sent. Follow-up
    alerts re-run the full assessment, so escalations stop the moment the
    situation no longer qualifies.

    Uses SCHEDULE_URGENT (15-min cadence) since lows are time-sensitive.
    """
    trend = get_bg_trend(conn)

    if trend["current_bg"] is None:
        return []

    current_bg = trend["current_bg"]
    rate = trend["rate_per_15"]

    assessment = assess_low_risk(conn, current_bg, rate, trend["description"])
    if assessment is None:
        return []

    # Progressive escalation (urgent schedule, 2hr window)
    history = get_alert_history(conn, "LOW_WARNING", hours=2.0)
    should_fire, level = should_escalate(history, SCHEDULE_URGENT)
    if not should_fire:
        return []

    current_time = fmt_time_ny(trend["current_ts"])
    arrow = trend["arrow"]
    iob, iob_src = current_iob(conn)
    iob_note = f"IOB {iob}u" + (" (Trio net)" if iob_src == "trio" else " (est.)")
    since = ""
    if level > 0 and history:
        since = f" — first alert {fmt_time_ny(history[0]['triggered_at'])}"

    tier = assessment["tier"]
    loop_note = describe_loop_action(assessment.get("loop"))

    if tier == "URGENT":
        msg = (
            f"\U0001f6a8 LOW: BG {current_bg}{arrow} ({current_time}){since}. "
            f"{iob_note}, {loop_note}.\n"
            f"15g fast carbs now."
        )
    elif tier == "CARBS_REQ":
        pred = assessment.get("pred_min")
        pred_note = f" (predicted nadir ~{pred:.0f})" if pred is not None else ""
        msg = (
            f"\u26a0\ufe0f Trio says ~{assessment['carbs_req']}g carbs needed to correct.\n"
            f"BG {current_bg}{arrow} ({current_time}){since} — "
            f"{loop_note}, still predicts a low{pred_note}. {iob_note}."
        )
    elif tier == "PREDICTED":
        msg = (
            f"\u26a0\ufe0f Low likely: BG {current_bg}{arrow} ({current_time}){since}. "
            f"Trio predicts ~{assessment['pred_min']:.0f} within the hour despite {loop_note}. "
            f"{iob_note}.\n"
            f"~10-15g carbs recommended."
        )
    else:  # FALLBACK — no loop visibility
        hist = assessment.get("hist")
        hist_note = ""
        if hist is not None:
            hist_note = (f" Similar drops: {hist['went_low']} of {hist['episodes']} "
                         f"reached <70 (median nadir {hist['median_nadir']}).")
        msg = (
            f"\u26a0\ufe0f Possible low: BG {current_bg}{arrow} ({current_time}){since}, "
            f"falling {rate:+.0f}/15min — no loop data to confirm. {iob_note}.{hist_note}\n"
            f"~15g fast carbs if it keeps dropping."
        )

    return [{"rule": "LOW_WARNING", "message": msg}]


# ── Nightscout pump/loop rules ──────────────────────────────────────
#
# These rules read live pump state (Omnipod 5 via Trio → Nightscout) with
# the nightscout-client package. They no-op silently if the package isn't
# installed or NIGHTSCOUT_URL isn't configured, so the BG rules above keep
# working standalone.

_ns_state = None  # per-run cache: {"pump": dict|None, "unreachable": bool, "error": str|None}


def fetch_pump_state() -> dict:
    """Fetch pump()/loop state from Nightscout once per run (cached).

    Returns {"pump": dict or None, "unreachable": bool, "error": str or None}.
    "unreachable" is True only for connection errors (site down / no network),
    which is deliberately distinct from stale loop data (see rule_loop_stale).
    """
    global _ns_state
    if _ns_state is not None:
        return _ns_state

    result = {"pump": None, "unreachable": False, "error": None}
    if not NIGHTSCOUT_AVAILABLE or not os.getenv("NIGHTSCOUT_URL"):
        result["error"] = "nightscout not configured"
        _ns_state = result
        return result

    try:
        client = NightscoutClient.from_env()
        result["pump"] = client.pump()
    except NightscoutConnectionError as e:
        result["unreachable"] = True
        result["error"] = str(e)
    except NightscoutError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)

    _ns_state = result
    return result


def get_monitor_state(conn, key: str, default: str = None) -> str:
    """Read a value from the sync_state key/value store."""
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_monitor_state(conn, key: str, value: str):
    """Write a value to the sync_state store (skipped in --dry-run)."""
    if DRY_RUN:
        return
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, utc_now().isoformat()),
    )
    conn.commit()


def rule_low_reservoir(conn) -> list[dict]:
    """Rule 7: Low reservoir — fires once on the downward crossing below
    RESERVOIR_LOW_UNITS, then stays quiet until the reservoir goes back
    above the threshold (pod change), rather than re-firing every poll.
    """
    pump = fetch_pump_state()["pump"]
    if pump is None:
        return []

    reservoir = pump.get("reservoir")
    exact = pump.get("reservoir_exact", True)
    prev = get_monitor_state(conn, "monitor_reservoir_state", "above")

    # Omnipod reports "50+" above 50u (reservoir_exact False) — clearly above
    if reservoir is None or not exact or reservoir >= RESERVOIR_LOW_UNITS:
        if prev == "below":
            set_monitor_state(conn, "monitor_reservoir_state", "above")
        return []

    if prev == "below":
        return []  # already alerted on this crossing

    set_monitor_state(conn, "monitor_reservoir_state", "below")

    msg = (
        f"\U0001fab7 Low reservoir: {pump.get('reservoir_display', reservoir)}u left "
        f"(threshold {RESERVOIR_LOW_UNITS:.0f}u). Plan a pod change."
    )
    return [{"rule": "LOW_RESERVOIR", "message": msg}]


def _pod_age_alert(conn, rule_name: str, threshold_hours: float) -> list[dict]:
    """Shared pod-age check. Fires once per pod (dedup on site_changed_at)."""
    pump = fetch_pump_state()["pump"]
    if pump is None or pump.get("pod_age_hours") is None:
        return []

    age = pump["pod_age_hours"]
    if age < threshold_hours:
        return []

    # One alert per pod: dedup on the pod's site-change timestamp
    dedup_key = pump.get("site_changed_at") or "unknown-pod"
    history = get_alert_history(conn, rule_name, hours=96.0, dedup_key=dedup_key)
    if history:
        return []

    hours_left = max(0.0, POD_HARD_STOP_HOURS - age)
    if rule_name == "POD_AGE_URGENT":
        msg = (
            f"\U0001f6a8 Pod age {age:.0f}h — hard stop at {POD_HARD_STOP_HOURS}h "
            f"(~{hours_left:.0f}h left). Change the pod now."
        )
    else:
        msg = (
            f"⏳ Pod age {age:.0f}h (≥{POD_WARN_HOURS}h). "
            f"Hard stop at {POD_HARD_STOP_HOURS}h — plan a pod change today."
        )
    return [{"rule": rule_name, "message": msg, "dedup_key": dedup_key}]


def rule_pod_age_warn(conn) -> list[dict]:
    """Rule 8a: Pod age ≥ 72h — first warning, once per pod."""
    return _pod_age_alert(conn, "POD_AGE_WARN", POD_WARN_HOURS)


def rule_pod_age_urgent(conn) -> list[dict]:
    """Rule 8b: Pod age ≥ 78h — urgent warning, once per pod.

    Separate alert key from POD_AGE_WARN so both fire once each.
    """
    return _pod_age_alert(conn, "POD_AGE_URGENT", POD_URGENT_HOURS)


def rule_loop_stale(conn) -> list[dict]:
    """Rule 9: Loop-not-looping — Nightscout is reachable but devicestatus is
    stale (loop hasn't run in > LOOP_STALE_MINUTES). Distinct from
    NIGHTSCOUT_UNREACHABLE, which means the site itself can't be reached.
    """
    state = fetch_pump_state()
    pump = state["pump"]
    if pump is None:
        return []

    stale_min = pump.get("last_loop_minutes_ago")
    if stale_min is None:
        stale_min = pump.get("data_age_minutes")
    if stale_min is None or stale_min <= LOOP_STALE_MINUTES:
        return []

    if was_recently_alerted(conn, "LOOP_STALE"):
        return []

    msg = (
        f"\U0001f504 Loop hasn't run in {stale_min:.0f} min "
        f"(status: {pump.get('loop_status', 'unknown')}). "
        f"Check pump/phone connection — no automated basal adjustments meanwhile."
    )
    return [{"rule": "LOOP_STALE", "message": msg}]


def rule_nightscout_unreachable(conn) -> list[dict]:
    """Rule 10: Nightscout site unreachable (network/timeout) — different
    failure mode (and alert key/message) from stale loop data.
    """
    state = fetch_pump_state()
    if not state["unreachable"]:
        return []

    if was_recently_alerted(conn, "NIGHTSCOUT_UNREACHABLE"):
        return []

    msg = (
        "\U0001f4e1 Nightscout unreachable — no pump/loop visibility right now. "
        "BG monitoring continues from local readings."
    )
    return [{"rule": "NIGHTSCOUT_UNREACHABLE", "message": msg}]


# ── Snooze CLI handlers ───────────────────────────────────────────

def handle_snooze(conn, rule_name: str, duration_minutes: int):
    """Snooze a rule for the given duration."""
    now = utc_now()
    expires = now + timedelta(minutes=duration_minutes)
    conn.execute("""
        INSERT INTO alert_snoozes (rule_name, snoozed_at, expires_at, reason)
        VALUES (?, ?, ?, 'manual')
    """, (rule_name, now.isoformat(), expires.isoformat()))
    conn.commit()
    exp_local = expires.astimezone(NY).strftime("%-I:%M%p").lower()
    print(f"Snoozed {rule_name} until {exp_local} ({duration_minutes} min)")


def handle_snooze_status(conn):
    """Print active snoozes."""
    now_iso = utc_now().isoformat()
    rows = conn.execute("""
        SELECT rule_name, snoozed_at, expires_at, reason FROM alert_snoozes
        WHERE expires_at > ?
        ORDER BY expires_at ASC
    """, (now_iso,)).fetchall()

    if not rows:
        print("No active snoozes.")
        return

    print("Active snoozes:")
    for row in rows:
        exp_local = fmt_time_ny(row["expires_at"])
        remaining = (datetime.fromisoformat(row["expires_at"]).replace(tzinfo=UTC) - utc_now()).total_seconds() / 60
        print(f"  {row['rule_name']}: until {exp_local} ({remaining:.0f} min remaining)")


def handle_unsnooze(conn):
    """Clear all active snoozes."""
    now_iso = utc_now().isoformat()
    result = conn.execute("""
        DELETE FROM alert_snoozes WHERE expires_at > ?
    """, (now_iso,))
    conn.commit()
    print(f"Cleared {result.rowcount} active snooze(s).")


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TypeOneZen BG Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts without sending or logging")
    parser.add_argument("--snooze", type=str, metavar="RULE",
                        help="Snooze a rule (e.g., HIGH_STUCK or ALL)")
    parser.add_argument("--snooze-duration", type=int, default=120,
                        help="Snooze duration in minutes (default: 120)")
    parser.add_argument("--snooze-status", action="store_true",
                        help="Show active snoozes")
    parser.add_argument("--unsnooze", action="store_true",
                        help="Clear all active snoozes")
    args = parser.parse_args()

    global DRY_RUN
    DRY_RUN = args.dry_run

    conn = get_db()
    ensure_tables(conn)

    # Handle snooze commands
    if args.snooze:
        handle_snooze(conn, args.snooze.upper(), args.snooze_duration)
        conn.close()
        return

    if args.snooze_status:
        handle_snooze_status(conn)
        conn.close()
        return

    if args.unsnooze:
        handle_unsnooze(conn)
        conn.close()
        return

    # Run all rules and collect alerts
    all_alerts = []
    rules = [
        ("HIGH_STUCK", rule_high_stuck),
        ("RAPID_DROP", rule_rapid_drop),
        # PRE_WORKOUT_LOW_RISK: disabled from auto-monitor.
        # Only triggered when user explicitly says they're about to work out.
        # ("PRE_WORKOUT_LOW_RISK", rule_pre_workout_low_risk),
        ("LOW_WARNING", rule_low_warning),
        # Nightscout pump/loop rules (no-op if nightscout isn't configured)
        ("LOW_RESERVOIR", rule_low_reservoir),
        ("POD_AGE_WARN", rule_pod_age_warn),
        ("POD_AGE_URGENT", rule_pod_age_urgent),
        ("LOOP_STALE", rule_loop_stale),
        ("NIGHTSCOUT_UNREACHABLE", rule_nightscout_unreachable),
    ]

    for rule_name, rule_fn in rules:
        # Check snooze before evaluating
        try:
            if is_snoozed(conn, rule_name):
                exp_row = conn.execute("""
                    SELECT expires_at FROM alert_snoozes
                    WHERE (rule_name = ? OR rule_name = 'ALL')
                      AND expires_at > ?
                    ORDER BY expires_at DESC LIMIT 1
                """, (rule_name, utc_now().isoformat())).fetchone()
                exp_time = fmt_time_ny(exp_row["expires_at"]) if exp_row else "?"
                print(f"  [SNOOZED] {rule_name}: snoozed until {exp_time}")
                continue
        except Exception:
            pass  # If snooze check fails, run the rule anyway

        try:
            alerts = rule_fn(conn)
            all_alerts.extend(alerts)
        except Exception as e:
            print(f"  [ERROR] Rule {rule_name} failed: {e}")

    # Process alerts
    sent_count = 0
    now_iso = utc_now().isoformat()

    for alert in all_alerts:
        rule = alert["rule"]
        message = alert["message"]
        dedup_key = alert.get("dedup_key")

        if args.dry_run:
            print(f"  [DRY-RUN] {rule}: {message}")
            sent_count += 1
            continue

        try:
            send_imsg(message)
            conn.execute("""
                INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key)
                VALUES (?, ?, ?, 1, ?)
            """, (rule, now_iso, message, dedup_key))
            conn.commit()
            sent_count += 1
            print(f"  [SENT] {rule}: {message}")
        except Exception as e:
            print(f"  [ERROR] Failed to send {rule}: {e}")
            conn.execute("""
                INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key)
                VALUES (?, ?, ?, 0, ?)
            """, (rule, now_iso, message, dedup_key))
            conn.commit()

    # Summary
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"\n[{mode}] Checked {len(rules)} rules. {sent_count} alert(s) triggered.")

    conn.close()


if __name__ == "__main__":
    main()
