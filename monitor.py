"""
TypeOneZen — Zero-token rule-based BG monitor.

Checks six rules against real CGM + meal data and sends iMessage alerts
when thresholds are crossed. No AI — pure Python + SQLite.

Features:
- Progressive alert cadence (escalating re-alerts for sustained conditions)
- IOB estimation from bolus/correction doses
- Snooze mechanism (DB-backed, CLI-controlled)
- Richer context in alerts (IOB, trend, correction suggestions)

Usage:
    python3 monitor.py            # Run all rules, send alerts
    python3 monitor.py --dry-run  # Print alerts without sending or logging

    python3 monitor.py --snooze SUSTAINED_HIGH          # Snooze for 2hr (default)
    python3 monitor.py --snooze ALL --snooze-duration 90 # Snooze all for 90 min
    python3 monitor.py --snooze-status                   # List active snoozes
    python3 monitor.py --unsnooze                        # Clear all snoozes

Future: Reply "handled" via iMessage → Apple Shortcut runs:
    python3 ~/TypeOneZen/monitor.py --snooze ALL
"""

from __future__ import annotations

import argparse
import os
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
SCHEDULE_DEFAULT = [0, 30, 60, 60, 60]    # For highs (SUSTAINED, OVERNIGHT, POST_MEAL)
SCHEDULE_URGENT  = [0, 15, 15, 15, 15]    # For lows (LOW_WARNING)

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


# ── Helpers ─────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(UTC)


def ny_now() -> datetime:
    return datetime.now(NY)


def fmt_time_ny(iso_ts: str) -> str:
    """Format an ISO timestamp as a short NY local time string."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(NY).strftime("%-I:%M%p").lower()


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
    readings = conn.execute("""
        SELECT glucose_mg_dl, timestamp, trend FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 3
    """).fetchall()

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


def build_context_line(conn, iob: float, trend: dict) -> str:
    """Build a context line: IOB + trend + last correction."""
    parts = []

    # IOB
    if iob > 0:
        est_drop = round(iob * ISF)
        parts.append(f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL)")
    else:
        parts.append("IOB: no recent doses")

    # Trend
    parts.append(f"Trend: {trend['arrow']} {trend['description']}")

    # Last correction
    cutoff = (utc_now() - timedelta(hours=6)).isoformat()
    last_corr = conn.execute("""
        SELECT timestamp, units FROM insulin_doses
        WHERE timestamp > ? AND type = 'correction'
        ORDER BY timestamp DESC LIMIT 1
    """, (cutoff,)).fetchone()

    if last_corr:
        corr_time = fmt_time_ny(last_corr["timestamp"])
        parts.append(f"Last correction: {last_corr['units']}u at {corr_time}")

    return " | ".join(parts)


def get_alert_history(conn, rule_name: str, hours: float = 6.0,
                      dedup_key: str = None) -> list[dict]:
    """Return all alerts for this rule in the last N hours, ordered by triggered_at ASC.

    If dedup_key is provided, filters to alerts with that specific dedup_key.
    """
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    if dedup_key is not None:
        rows = conn.execute("""
            SELECT triggered_at, message, dedup_key FROM alert_log
            WHERE rule_name = ? AND triggered_at > ? AND dedup_key = ?
            ORDER BY triggered_at ASC
        """, (rule_name, cutoff, dedup_key)).fetchall()
    else:
        rows = conn.execute("""
            SELECT triggered_at, message, dedup_key FROM alert_log
            WHERE rule_name = ? AND triggered_at > ?
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


def get_similar_meal_outcomes(conn, carbs_g, tolerance=15) -> dict | None:
    """Find average spike and correction for meals with similar carbs.

    Returns dict with avg_spike, avg_correction_units, sample_count, or None.
    """
    if carbs_g is None or carbs_g <= 0:
        return None

    low = carbs_g - tolerance
    high = carbs_g + tolerance

    # Find meals with similar carbs (older than 6 hours to exclude current episode)
    cutoff = (utc_now() - timedelta(hours=6)).isoformat()
    meals = conn.execute("""
        SELECT id, timestamp, carbs_g FROM meals
        WHERE carbs_g BETWEEN ? AND ?
          AND timestamp < ?
        ORDER BY timestamp DESC
        LIMIT 20
    """, (low, high, cutoff)).fetchall()

    if not meals:
        return None

    spikes = []
    corrections = []

    for meal in meals:
        meal_ts = meal["timestamp"]

        # Get baseline BG (avg 30 min before meal)
        baseline_row = conn.execute("""
            SELECT AVG(glucose_mg_dl) as avg_bg FROM glucose_readings
            WHERE timestamp BETWEEN datetime(?, '-30 minutes') AND ?
        """, (meal_ts, meal_ts)).fetchone()

        if not baseline_row or baseline_row["avg_bg"] is None:
            continue

        baseline = baseline_row["avg_bg"]

        # Get peak BG 30-120 min after meal
        peak_row = conn.execute("""
            SELECT MAX(glucose_mg_dl) as peak_bg FROM glucose_readings
            WHERE timestamp BETWEEN datetime(?, '+30 minutes')
                                AND datetime(?, '+120 minutes')
        """, (meal_ts, meal_ts)).fetchone()

        if peak_row and peak_row["peak_bg"] is not None:
            spikes.append(peak_row["peak_bg"] - baseline)

        # Find correction bolus within 2 hours of meal
        corr_row = conn.execute("""
            SELECT SUM(units) as total_units FROM insulin_doses
            WHERE timestamp BETWEEN ? AND datetime(?, '+120 minutes')
              AND type = 'correction'
        """, (meal_ts, meal_ts)).fetchone()

        if corr_row and corr_row["total_units"] is not None:
            corrections.append(corr_row["total_units"])

    if not spikes:
        return None

    return {
        "avg_spike": round(sum(spikes) / len(spikes), 0),
        "avg_correction_units": round(sum(corrections) / len(corrections), 1) if corrections else None,
        "sample_count": len(spikes),
    }


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
    conn.commit()


def was_recently_alerted(conn, rule_name: str, hours: float = 2.0) -> bool:
    """Check if this rule fired within the last `hours` hours."""
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    row = conn.execute("""
        SELECT id FROM alert_log
        WHERE rule_name = ? AND triggered_at > ?
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

def rule_post_meal_spike(conn) -> list[dict]:
    """Rule 1: Detect post-meal BG spikes with progressive escalation."""
    alerts = []
    cutoff = (utc_now() - timedelta(hours=6)).isoformat()

    meals = conn.execute("""
        SELECT id, timestamp, description, carbs_g
        FROM meals WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,)).fetchall()

    iob = estimate_iob(conn)
    trend = get_bg_trend(conn)

    for meal in meals:
        meal_ts = meal["timestamp"]
        carbs = meal["carbs_g"] or 0

        # Stop escalating after 4 hours post-meal
        meal_dt = datetime.fromisoformat(meal_ts)
        if meal_dt.tzinfo is None:
            meal_dt = meal_dt.replace(tzinfo=UTC)
        hours_since_meal = (utc_now() - meal_dt).total_seconds() / 3600
        if hours_since_meal > 4:
            continue

        # Get escalation history for this specific meal
        history = get_alert_history(conn, "POST_MEAL_SPIKE", hours=4.0, dedup_key=meal_ts)
        should_fire, level = should_escalate(history, SCHEDULE_DEFAULT)

        if not should_fire:
            continue

        # Baseline: avg BG in 30 min before meal
        baseline_rows = conn.execute("""
            SELECT AVG(glucose_mg_dl) as avg_bg
            FROM glucose_readings
            WHERE timestamp BETWEEN datetime(?, '-30 minutes') AND ?
        """, (meal_ts, meal_ts)).fetchone()

        baseline = baseline_rows["avg_bg"] if baseline_rows and baseline_rows["avg_bg"] else None
        if baseline is None:
            continue

        # Peak: max BG 30–120 min after meal
        peak_row = conn.execute("""
            SELECT MAX(glucose_mg_dl) as peak_bg,
                   timestamp as peak_ts
            FROM glucose_readings
            WHERE timestamp BETWEEN datetime(?, '+30 minutes')
                                AND datetime(?, '+120 minutes')
        """, (meal_ts, meal_ts)).fetchone()

        if peak_row is None or peak_row["peak_bg"] is None:
            continue

        peak_bg = peak_row["peak_bg"]
        peak_ts = peak_row["peak_ts"]
        rise = peak_bg - baseline

        if rise <= 60:
            continue

        # Get current BG
        current_row = conn.execute("""
            SELECT glucose_mg_dl, timestamp FROM glucose_readings
            ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        if current_row is None:
            continue

        current_bg = current_row["glucose_mg_dl"]
        current_time = fmt_time_ny(current_row["timestamp"])

        # Guard: only escalate if BG still >160
        if level > 0 and current_bg <= 160:
            continue

        desc = (meal["description"] or "meal")[:20]
        est_drop = round(iob * ISF)

        if level == 0:
            arrow = trend["arrow"]
            peak_time = fmt_time_ny(peak_ts) if peak_ts else "?"
            iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL) — may come down on its own."
                        if iob > 0 else "IOB: no recent doses.")
            msg = (
                f"\U0001f4c8 Post-meal spike: +{rise:.0f} mg/dL after {desc} "
                f"({carbs:.0f}g carbs). Peak {peak_bg} at {peak_time}. "
                f"Current: {current_bg}{arrow} ({current_time})\n"
                f"{iob_note}"
            )
        elif level == 1:
            arrow = trend["arrow"]
            iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL). Watching — may still need correction."
                        if iob > 0 else "IOB: no recent doses. May need correction.")
            msg = (
                f"\U0001f4c8 Still elevated after {desc} ({carbs:.0f}g carbs, +{rise:.0f} spike). "
                f"Current: {current_bg}{arrow} ({current_time}).\n"
                f"{iob_note}"
            )
        else:
            # Level 2+: include historical context
            mins_post = int(hours_since_meal * 60)
            iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL)."
                        if iob > 0 else "IOB: no recent doses.")
            msg = (
                f"\U0001f4c8 Persistent high after {desc} ({carbs:.0f}g carbs). "
                f"Current: {current_bg}{trend['arrow']} ({current_time}), {mins_post} min post-meal.\n"
                f"{iob_note}"
            )
            # Add historical meal pattern if available
            similar = get_similar_meal_outcomes(conn, carbs)
            if similar and similar["sample_count"] >= 2:
                msg += f"\nSimilar meals averaged +{similar['avg_spike']:.0f} mg/dL spike"
                if similar["avg_correction_units"]:
                    msg += f"; correction of {similar['avg_correction_units']}u typically helped"
                msg += f" ({similar['sample_count']} meals)."

        alerts.append({
            "rule": "POST_MEAL_SPIKE",
            "message": msg,
            "dedup_key": meal_ts,
        })

    return alerts


def rule_sustained_high(conn) -> list[dict]:
    """Rule 2: Sustained high — avg BG > 200 for 90 min and current > 180.

    Uses progressive escalation with SCHEDULE_DEFAULT.
    """
    cutoff = (utc_now() - timedelta(minutes=90)).isoformat()
    avg_row = conn.execute("""
        SELECT AVG(glucose_mg_dl) as avg_bg, COUNT(*) as cnt
        FROM glucose_readings WHERE timestamp > ?
    """, (cutoff,)).fetchone()

    if avg_row is None or avg_row["cnt"] < 6:
        return []

    avg_bg = avg_row["avg_bg"]

    current_row = conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

    if current_row is None:
        return []

    current_bg = current_row["glucose_mg_dl"]
    current_time = fmt_time_ny(current_row["timestamp"])

    if avg_bg <= 200 or current_bg <= 180:
        return []

    # Progressive escalation
    history = get_alert_history(conn, "SUSTAINED_HIGH", hours=6.0)
    should_fire, level = should_escalate(history, SCHEDULE_DEFAULT)

    if not should_fire:
        return []

    iob = estimate_iob(conn)
    trend = get_bg_trend(conn)
    est_drop = round(iob * ISF)

    if level == 0:
        iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL)."
                    if iob > 0 else "IOB: no recent doses.")
        msg = (
            f"\u26a0\ufe0f Sustained high: avg {avg_bg:.0f} over 90 min, "
            f"currently {current_bg} ({current_time}). Consider correction.\n"
            f"{iob_note}"
        )
    elif level == 1:
        iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL)."
                    if iob > 0 else "IOB: no recent doses.")
        first_alert = history[0]["triggered_at"]
        mins_since = int((utc_now() - datetime.fromisoformat(
            first_alert if "+" in first_alert or first_alert.endswith("Z")
            else first_alert + "+00:00"
        ).replace(tzinfo=None).replace(tzinfo=UTC)).total_seconds() / 60)
        msg = (
            f"\u26a0\ufe0f Still high: avg {avg_bg:.0f}, currently {current_bg}{trend['arrow']} "
            f"({current_time}), {mins_since} min and counting.\n"
            f"{iob_note}"
        )
    else:
        # Level 2+: include correction suggestion
        corr = suggested_correction(current_bg, iob=iob)
        if iob > 0:
            iob_note = f"IOB ~{iob}u."
        else:
            iob_note = "IOB: no recent doses."
        msg = (
            f"\u26a0\ufe0f Still high: avg {avg_bg:.0f}, currently {current_bg}{trend['arrow']} "
            f"({current_time}).\n"
            f"{iob_note} Suggested correction: ~{corr}u (BG {current_bg}, target {TARGET_BG}, ISF {ISF})."
        )

    return [{"rule": "SUSTAINED_HIGH", "message": msg}]


def rule_rapid_drop(conn) -> list[dict]:
    """Rule 3: Rapid drop — BG dropped > 30 in last 30 min.

    Keeps simple 2hr cooldown (no progressive escalation). Adds IOB context.
    """
    if was_recently_alerted(conn, "RAPID_DROP"):
        return []

    readings = conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 6
    """).fetchall()

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
    if drop > 30:
        rate = drop / span_min * 60  # per hour
        bg_time = fmt_time_ny(current_ts)

        iob = estimate_iob(conn)
        est_drop = round(iob * ISF)

        if iob > 0:
            iob_note = f"IOB ~{iob}u (est. further drop ~{est_drop} mg/dL). Consider fast carbs now."
        else:
            iob_note = "IOB: no recent doses."

        msg = (
            f"\u26a0\ufe0f Rapid drop: {oldest_bg}\u2192{current_bg} "
            f"in {span_min:.0f} min ({rate:.0f}/hr) as of {bg_time}.\n"
            f"{iob_note}"
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

    current_row = conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

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


def rule_overnight_high(conn) -> list[dict]:
    """Rule 5: Overnight high — BG > 160 for > 60 min between 23:00-07:00 NY.

    Uses progressive escalation with SCHEDULE_DEFAULT.
    """
    now_ny = ny_now()
    hour = now_ny.hour

    # Only run between 23:00 and 07:00 NY time
    if 7 <= hour < 23:
        return []

    # Look at the last 90 min of readings
    cutoff = (utc_now() - timedelta(minutes=90)).isoformat()
    readings = conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        WHERE timestamp > ?
        ORDER BY timestamp ASC
    """, (cutoff,)).fetchall()

    if len(readings) < 6:
        return []

    # Count consecutive minutes above 160
    high_start = None
    max_high_duration = 0

    for r in readings:
        bg = r["glucose_mg_dl"]
        ts = r["timestamp"]
        if bg > 160:
            if high_start is None:
                high_start = ts
        else:
            if high_start is not None:
                try:
                    dur = (datetime.fromisoformat(ts) - datetime.fromisoformat(high_start)).total_seconds() / 60
                    max_high_duration = max(max_high_duration, dur)
                except (ValueError, TypeError):
                    pass
                high_start = None

    if high_start is not None:
        try:
            dur = (datetime.fromisoformat(readings[-1]["timestamp"]) - datetime.fromisoformat(high_start)).total_seconds() / 60
            max_high_duration = max(max_high_duration, dur)
        except (ValueError, TypeError):
            pass

    if max_high_duration <= 60:
        return []

    # Get current BG
    current_row = conn.execute("""
        SELECT glucose_mg_dl, timestamp FROM glucose_readings
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

    if current_row is None:
        return []

    current_bg = current_row["glucose_mg_dl"]
    current_time = fmt_time_ny(current_row["timestamp"])

    # Guard: only alert/escalate if still >160
    if current_bg <= 160:
        return []

    # Progressive escalation
    history = get_alert_history(conn, "OVERNIGHT_HIGH", hours=6.0)
    should_fire, level = should_escalate(history, SCHEDULE_DEFAULT)

    if not should_fire:
        return []

    high_readings = [r["glucose_mg_dl"] for r in readings if r["glucose_mg_dl"] > 160]
    avg_high = sum(high_readings) / len(high_readings) if high_readings else 0

    iob = estimate_iob(conn)
    est_drop = round(iob * ISF)

    if level == 0:
        iob_note = (f"IOB ~{iob}u (est. drop ~{est_drop} mg/dL)."
                    if iob > 0 else "IOB: no recent doses.")
        msg = (
            f"\U0001f319 Overnight high: avg {avg_high:.0f} for {max_high_duration:.0f} min. "
            f"Currently {current_bg}{get_bg_trend(conn)['arrow']} ({current_time}).\n"
            f"{iob_note}"
        )
    else:
        # Level 1+: include correction suggestion
        corr = suggested_correction(current_bg, iob=iob)
        iob_note = (f"IOB ~{iob}u." if iob > 0 else "IOB: no recent doses.")
        msg = (
            f"\U0001f319 Overnight high: avg {avg_high:.0f} for {max_high_duration:.0f} min. "
            f"Currently {current_bg}{get_bg_trend(conn)['arrow']} ({current_time}).\n"
            f"{iob_note} Small correction of ~{corr}u would target {TARGET_BG}."
        )

    return [{"rule": "OVERNIGHT_HIGH", "message": msg}]


def rule_low_warning(conn) -> list[dict]:
    """Rule 6: Low warning — catches gradual lows that RAPID_DROP misses.

    Fires when:
      (a) Projected BG in 15 min < 80 (i.e. current_bg + rate < 80), OR
      (b) BG < 90 AND IOB > 0.5u AND actively falling (rate < -5/15min), OR
      (c) BG < 80 (already below threshold, regardless of trend)

    Does NOT fire when:
      - BG >= 80 AND trend is stable or rising — covers normal overnight
        fluctuation patterns like 80→88→90→82→90→95.

    Follow-up alerts (level > 0) re-verify conditions before firing.
    If BG has stabilized (>= 85 and stable/rising), follow-ups are suppressed.

    Uses SCHEDULE_URGENT (15-min cadence) since lows are time-sensitive.
    """
    trend = get_bg_trend(conn)

    if trend["current_bg"] is None:
        return []

    current_bg = trend["current_bg"]
    current_ts = trend["current_ts"]
    rate = trend["rate_per_15"]
    description = trend["description"]  # "stable", "rising", or "falling"

    iob = estimate_iob(conn)
    est_drop = round(iob * ISF)

    # Suppression: BG >= 80 and stable/rising → normal fluctuation, don't alert
    if current_bg >= 80 and description in ("stable", "rising"):
        return []

    # Check trigger conditions
    projected_bg = current_bg + rate   # estimated BG in ~15 min
    condition_a = projected_bg < 80    # projected to cross below 80 in 15 min
    condition_b = current_bg < 90 and iob > 0.5 and rate < -5  # near-low + IOB + falling
    condition_c = current_bg < 80      # already below 80

    if not (condition_a or condition_b or condition_c):
        return []

    # Progressive escalation (urgent schedule, 2hr window)
    history = get_alert_history(conn, "LOW_WARNING", hours=2.0)
    should_fire, level = should_escalate(history, SCHEDULE_URGENT)

    if not should_fire:
        return []

    # Follow-up re-check: if BG has stabilized since initial alert, suppress
    if level > 0 and current_bg >= 80 and description in ("stable", "rising"):
        return []

    current_time = fmt_time_ny(current_ts)

    if level == 0:
        if iob > 0:
            iob_note = f"IOB ~{iob}u (est. further drop ~{est_drop} mg/dL)."
        else:
            iob_note = ""

        if condition_a or condition_c:
            msg = (
                f"\u26a0\ufe0f Low warning: BG {current_bg}{trend['arrow']} and falling "
                f"({rate:+.0f}/15min, projected {round(projected_bg)} in 15min). {iob_note}\n"
                f"~15-20g fast carbs recommended."
            )
        else:
            # condition_b: IOB-based trigger
            msg = (
                f"\u26a0\ufe0f Low warning: BG {current_bg}{trend['arrow']} with active insulin. "
                f"{iob_note}\n"
                f"Consider fast carbs — IOB may push BG lower."
            )
    else:
        # Level 1+: show previous BG if available
        prev_note = ""
        if history:
            first_time = fmt_time_ny(history[0]["triggered_at"])
            prev_note = f" (first alert at {first_time})"

        if iob > 0:
            iob_note = f"IOB ~{iob}u."
        else:
            iob_note = ""

        msg = (
            f"\u26a0\ufe0f Still trending low: BG {current_bg}{trend['arrow']} "
            f"({current_time}){prev_note}. {iob_note}\n"
            f"~15g fast carbs suggested."
        )

    return [{"rule": "LOW_WARNING", "message": msg}]


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
                        help="Snooze a rule (e.g., SUSTAINED_HIGH or ALL)")
    parser.add_argument("--snooze-duration", type=int, default=120,
                        help="Snooze duration in minutes (default: 120)")
    parser.add_argument("--snooze-status", action="store_true",
                        help="Show active snoozes")
    parser.add_argument("--unsnooze", action="store_true",
                        help="Clear all active snoozes")
    args = parser.parse_args()

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
        ("POST_MEAL_SPIKE", rule_post_meal_spike),
        ("SUSTAINED_HIGH", rule_sustained_high),
        ("RAPID_DROP", rule_rapid_drop),
        # PRE_WORKOUT_LOW_RISK: disabled from auto-monitor.
        # Only triggered when user explicitly says they're about to work out.
        # ("PRE_WORKOUT_LOW_RISK", rule_pre_workout_low_risk),
        ("OVERNIGHT_HIGH", rule_overnight_high),
        ("LOW_WARNING", rule_low_warning),
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
