#!/usr/bin/env python3
"""TypeOneZen query script — compact JSON output for common BG/insulin/meal queries."""

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_HOME = Path(os.environ.get("TZ_HOME", Path.home() / "TypeOneZen"))
DB_PATH = TZ_HOME / "data" / "TypeOneZen.db"
SUMMARY_PATH = TZ_HOME / "summaries" / "stats_cache.json"
MONITOR_SCRIPT = TZ_HOME / "monitor.py"
ENV_PATH = TZ_HOME / ".env"
UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def utc_now():
    return datetime.now(UTC)


def to_ny(iso_ts):
    """Convert ISO timestamp string to NY time string."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(NY).strftime("%Y-%m-%d %-I:%M%p").lower()


def to_ny_short(iso_ts):
    """Convert ISO timestamp string to short NY time string."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(NY).strftime("%-I:%M%p").lower()


def out(data):
    print(json.dumps(data, indent=2))


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def ny_day_bounds(date_str=None):
    """Return (start_utc_iso, end_utc_iso, date_str) for an NY calendar day
    (midnight-to-midnight NY, converted to UTC). Defaults to today in NY."""
    if date_str:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        d = datetime.now(NY).date()
    start_ny = datetime(d.year, d.month, d.day, tzinfo=NY)
    end_ny = start_ny + timedelta(days=1)
    return start_ny.astimezone(UTC).isoformat(), end_ny.astimezone(UTC).isoformat(), d.isoformat()


def _parse_clock(text):
    """Parse a bare clock time like '3am', '3:15am', '15:30' into a time()."""
    text = text.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time: {text!r}")


def parse_time_arg(text):
    """Parse a user-supplied time expression into an NY-local datetime.
    Supports 'HH:MM', 'H:MMam/pm', '3am' (today NY, or yesterday if that
    would be in the future), 'yesterday 3am', and ISO 'YYYY-MM-DD HH:MM'."""
    raw = text.strip()
    lowered = raw.lower()
    now_ny = datetime.now(NY)

    if lowered.startswith("yesterday"):
        rest = lowered[len("yesterday"):].strip()
        base_date = (now_ny - timedelta(days=1)).date()
        t = _parse_clock(rest)
        return datetime(base_date.year, base_date.month, base_date.day,
                         t.hour, t.minute, t.second, tzinfo=NY)

    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=NY)
    except ValueError:
        pass

    t = _parse_clock(lowered)
    dt = datetime(now_ny.year, now_ny.month, now_ny.day,
                   t.hour, t.minute, t.second, tzinfo=NY)
    if dt > now_ny:
        dt -= timedelta(days=1)
    return dt


# ── Nightscout (live pump/loop data) ─────────────────────────────────

def _load_nightscout_env():
    """Load NIGHTSCOUT_URL/NIGHTSCOUT_TOKEN from ~/TypeOneZen/.env if not
    already set in the environment. Stdlib-only (no dotenv dependency)."""
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in ("NIGHTSCOUT_URL", "NIGHTSCOUT_TOKEN") and key not in os.environ:
                os.environ[key] = value.strip().strip("'").strip('"')
    except OSError:
        pass


def _nightscout_client():
    """Return (client, error_dict_or_None). Client is None if unavailable."""
    try:
        from nightscout_client import NightscoutClient
    except ImportError:
        return None, {"error": "nightscout-client not installed"}
    _load_nightscout_env()
    if not os.environ.get("NIGHTSCOUT_URL"):
        return None, {"error": "NIGHTSCOUT_URL not configured"}
    return NightscoutClient.from_env(), None


def fetch_nightscout_live():
    """Live Nightscout context for `now`. Returns:
      dict of live values on success,
      {"error": ...} if Nightscout is unreachable/errors,
      None if nightscout isn't installed/configured (SQLite-only mode).
    """
    client, err = _nightscout_client()
    if client is None:
        return None
    try:
        from nightscout_client.exceptions import NightscoutError
        now = client.now()
        pump = client.pump()
        return {
            "iob": now.get("iob"),
            "cob": now.get("cob"),
            "loop_status": pump.get("loop_status"),
            "last_loop_minutes_ago": pump.get("last_loop_minutes_ago"),
            "reservoir": pump.get("reservoir_display"),
            "pod_age_hours": pump.get("pod_age_hours"),
            "data_age_minutes": now.get("data_age_minutes"),
        }
    except NightscoutError as e:
        return {"error": f"nightscout: {e}"}
    except Exception as e:
        return {"error": f"nightscout: {e}"}


# ── Subcommands ──────────────────────────────────────────────────────

def cmd_now(args):
    """Current BG + trend + time since last reading."""
    conn = get_db()
    row = conn.execute("""
        SELECT glucose_mg_dl, trend, trend_arrow, timestamp
        FROM glucose_readings ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    conn.close()

    if not row:
        out({"error": "No glucose readings found"})
        return

    ts = row["timestamp"]
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins_ago = (utc_now() - dt).total_seconds() / 60

    # Live pump/loop context from Nightscout (degrades gracefully:
    # null if not configured, {"error": ...} if unreachable)
    out({
        "glucose_mg_dl": row["glucose_mg_dl"],
        "trend": row["trend"],
        "trend_arrow": row["trend_arrow"],
        "timestamp_ny": to_ny(ts),
        "minutes_ago": round(mins_ago, 1),
        "nightscout": fetch_nightscout_live(),
    })


def cmd_pump(args):
    """Live pump status from Nightscout (reservoir, pod age, loop status)."""
    client, err = _nightscout_client()
    if client is None:
        out(err)
        return
    try:
        from nightscout_client.exceptions import NightscoutError
        pump = client.pump()
    except NightscoutError as e:
        out({"error": f"nightscout: {e}"})
        return
    except Exception as e:
        out({"error": f"nightscout: {e}"})
        return

    result = dict(pump)
    if result.get("site_changed_at"):
        try:
            result["site_changed_ny"] = to_ny(result["site_changed_at"])
        except (ValueError, TypeError):
            pass
    out(result)


def cmd_range(args):
    """BG stats over last N hours."""
    hours = args.hours
    conn = get_db()
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    rows = conn.execute("""
        SELECT glucose_mg_dl FROM glucose_readings
        WHERE timestamp > ? ORDER BY timestamp
    """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        out({"error": f"No readings in last {hours}h"})
        return

    values = [r["glucose_mg_dl"] for r in rows]
    in_range = sum(1 for v in values if 70 <= v <= 180)

    out({
        "hours": hours,
        "count": len(values),
        "avg": round(sum(values) / len(values), 1),
        "min": min(values),
        "max": max(values),
        "tir_pct": round(in_range / len(values) * 100, 1),
        "below_70": sum(1 for v in values if v < 70),
        "above_180": sum(1 for v in values if v > 180),
    })


def cmd_insulin(args):
    """Insulin doses over last N hours."""
    hours = args.hours
    conn = get_db()
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    rows = conn.execute("""
        SELECT timestamp, units, type, notes
        FROM insulin_doses WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,)).fetchall()
    conn.close()

    doses = []
    totals = {}
    for r in rows:
        dose_type = r["type"] or "unknown"
        doses.append({
            "time_ny": to_ny_short(r["timestamp"]),
            "units": r["units"],
            "type": dose_type,
            "notes": r["notes"],
        })
        totals[dose_type] = totals.get(dose_type, 0) + r["units"]

    out({
        "hours": hours,
        "count": len(doses),
        "total_units": round(sum(totals.values()), 1),
        "by_type": {k: round(v, 1) for k, v in totals.items()},
        "doses": doses,
    })


def cmd_meals(args):
    """Recent meals with macros."""
    hours = args.hours
    conn = get_db()
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    rows = conn.execute("""
        SELECT timestamp, description, carbs_g, protein_g, fat_g,
               fiber_g, calories, source
        FROM meals WHERE timestamp > ?
        ORDER BY timestamp DESC
    """, (cutoff,)).fetchall()
    conn.close()

    meals = []
    for r in rows:
        meal = {
            "time_ny": to_ny_short(r["timestamp"]),
            "description": r["description"],
            "carbs_g": r["carbs_g"],
        }
        if r["protein_g"]:
            meal["protein_g"] = r["protein_g"]
        if r["fat_g"]:
            meal["fat_g"] = r["fat_g"]
        if r["fiber_g"]:
            meal["fiber_g"] = r["fiber_g"]
        if r["calories"]:
            meal["calories"] = r["calories"]
        if r["source"] and r["source"] != "manual":
            meal["source"] = r["source"]
        meals.append(meal)

    out({
        "hours": hours,
        "count": len(meals),
        "total_carbs": round(sum(m.get("carbs_g", 0) or 0 for m in meals), 1),
        "meals": meals,
    })


def cmd_workouts(args):
    """Recent workouts with BG correlation."""
    days = args.days
    conn = get_db()
    cutoff = (utc_now() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT started_at, ended_at, activity_type, intensity, notes
        FROM workouts WHERE started_at > ?
        ORDER BY started_at DESC
    """, (cutoff,)).fetchall()

    workouts = []
    for r in rows:
        started = r["started_at"]
        ended = r["ended_at"]

        # BG correlation: avg 30min before, during, 60min after
        pre_bg = conn.execute("""
            SELECT AVG(glucose_mg_dl) as avg
            FROM glucose_readings
            WHERE timestamp BETWEEN datetime(?, '-30 minutes') AND ?
        """, (started, started)).fetchone()

        during_bg = None
        if ended:
            during_bg = conn.execute("""
                SELECT AVG(glucose_mg_dl) as avg
                FROM glucose_readings
                WHERE timestamp BETWEEN ? AND ?
            """, (started, ended)).fetchone()

            post_bg = conn.execute("""
                SELECT AVG(glucose_mg_dl) as avg
                FROM glucose_readings
                WHERE timestamp BETWEEN ? AND datetime(?, '+60 minutes')
            """, (ended, ended)).fetchone()
        else:
            post_bg = None

        workout = {
            "date_ny": to_ny(started),
            "activity": r["activity_type"],
            "intensity": r["intensity"],
        }
        if ended:
            start_dt = datetime.fromisoformat(started)
            end_dt = datetime.fromisoformat(ended)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            workout["duration_min"] = round((end_dt - start_dt).total_seconds() / 60)
        if r["notes"]:
            workout["notes"] = r["notes"]

        bg = {}
        if pre_bg and pre_bg["avg"]:
            bg["pre"] = round(pre_bg["avg"])
        if during_bg and during_bg["avg"]:
            bg["during"] = round(during_bg["avg"])
        if post_bg and post_bg["avg"]:
            bg["post"] = round(post_bg["avg"])
        if bg:
            workout["bg_avg"] = bg

        workouts.append(workout)

    conn.close()

    out({
        "days": days,
        "count": len(workouts),
        "workouts": workouts,
    })


def cmd_summary(args):
    """Read cached health summary (no recomputation)."""
    if not SUMMARY_PATH.exists():
        out({"error": "No stats_cache.json found. Run generate_summary.py first."})
        return

    with open(SUMMARY_PATH) as f:
        data = json.load(f)

    out(data)


def cmd_monitor(args):
    """Run monitor.py --dry-run and return results."""
    if not MONITOR_SCRIPT.exists():
        out({"error": "monitor.py not found"})
        return

    result = subprocess.run(
        [sys.executable, str(MONITOR_SCRIPT), "--dry-run"],
        capture_output=True, text=True, timeout=30,
    )

    lines = (result.stdout + result.stderr).strip().split("\n")
    out({
        "dry_run": True,
        "output": lines,
        "exit_code": result.returncode,
    })


def cmd_day(args):
    """Full-day recap (BG, insulin, carbs, meals, workouts, alerts) for an NY calendar day."""
    try:
        start_utc, end_utc, date_str = ny_day_bounds(args.date)
    except ValueError:
        out({"error": f"Invalid date: {args.date!r}, expected YYYY-MM-DD"})
        return

    conn = get_db()

    bg_rows = conn.execute("""
        SELECT glucose_mg_dl FROM glucose_readings
        WHERE timestamp >= ? AND timestamp < ?
    """, (start_utc, end_utc)).fetchall()
    values = [r["glucose_mg_dl"] for r in bg_rows]
    if values:
        in_range = sum(1 for v in values if 70 <= v <= 180)
        bg = {
            "avg": round(sum(values) / len(values), 1),
            "min": min(values),
            "max": max(values),
            "tir_pct": round(in_range / len(values) * 100, 1),
            "count": len(values),
            "below_70": sum(1 for v in values if v < 70),
            "above_180": sum(1 for v in values if v > 180),
        }
    else:
        bg = {"avg": None, "min": None, "max": None, "tir_pct": None,
              "count": 0, "below_70": 0, "above_180": 0}

    dose_rows = conn.execute("""
        SELECT units, type FROM insulin_doses
        WHERE timestamp >= ? AND timestamp < ?
    """, (start_utc, end_utc)).fetchall()
    by_type = {}
    for r in dose_rows:
        dose_type = r["type"] or "unknown"
        by_type[dose_type] = by_type.get(dose_type, 0) + r["units"]
    insulin = {
        "total_units": round(sum(by_type.values()), 1),
        "by_type": {k: round(v, 1) for k, v in by_type.items()},
    }

    meal_rows = conn.execute("""
        SELECT timestamp, description, carbs_g FROM meals
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """, (start_utc, end_utc)).fetchall()
    meals = [{
        "time": to_ny_short(r["timestamp"]),
        "description": r["description"],
        "carbs": r["carbs_g"],
    } for r in meal_rows]
    carbs_total = round(sum(m["carbs"] or 0 for m in meals), 1)

    workout_rows = conn.execute("""
        SELECT started_at, ended_at, activity_type FROM workouts
        WHERE started_at >= ? AND started_at < ?
        ORDER BY started_at
    """, (start_utc, end_utc)).fetchall()
    workouts = []
    for r in workout_rows:
        w = {"time": to_ny_short(r["started_at"]), "activity": r["activity_type"]}
        if r["ended_at"]:
            start_dt = datetime.fromisoformat(r["started_at"])
            end_dt = datetime.fromisoformat(r["ended_at"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            w["duration_min"] = round((end_dt - start_dt).total_seconds() / 60)
        workouts.append(w)

    alerts_fired = []
    if _table_exists(conn, "alert_log"):
        alert_rows = conn.execute("""
            SELECT rule_name, triggered_at FROM alert_log
            WHERE triggered_at >= ? AND triggered_at < ?
            ORDER BY triggered_at
        """, (start_utc, end_utc)).fetchall()
        alerts_fired = [{
            "rule_name": r["rule_name"],
            "time": to_ny_short(r["triggered_at"]),
        } for r in alert_rows]

    conn.close()

    out({
        "date": date_str,
        "bg": bg,
        "insulin": insulin,
        "carbs_total": carbs_total,
        "meals": meals,
        "workouts": workouts,
        "alerts_fired": alerts_fired,
    })


def cmd_overnight(args):
    """Most recent completed-or-current overnight window (11pm-7am NY)."""
    now_ny = datetime.now(NY)
    if now_ny.hour >= 23:
        start_date = now_ny.date()
    else:
        start_date = now_ny.date() - timedelta(days=1)
    window_start_ny = datetime(start_date.year, start_date.month, start_date.day, 23, 0, tzinfo=NY)
    window_end_ny = window_start_ny + timedelta(hours=8)
    window_start_utc = window_start_ny.astimezone(UTC).isoformat()
    window_end_utc = window_end_ny.astimezone(UTC).isoformat()

    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, glucose_mg_dl FROM glucose_readings
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """, (window_start_utc, window_end_utc)).fetchall()

    values = [r["glucose_mg_dl"] for r in rows]
    lows = [{"time_ny": to_ny_short(r["timestamp"]), "glucose_mg_dl": r["glucose_mg_dl"]}
            for r in rows if r["glucose_mg_dl"] < 70]

    any_alerts = False
    if _table_exists(conn, "alert_log"):
        alert_row = conn.execute("""
            SELECT COUNT(*) AS n FROM alert_log
            WHERE triggered_at >= ? AND triggered_at < ?
        """, (window_start_utc, window_end_utc)).fetchone()
        any_alerts = bool(alert_row["n"])

    conn.close()

    result = {
        "window_start_ny": window_start_ny.strftime("%Y-%m-%d %-I:%M%p").lower(),
        "window_end_ny": window_end_ny.strftime("%Y-%m-%d %-I:%M%p").lower(),
        "count": len(values),
        "lows": lows,
        "any_alerts": any_alerts,
    }
    if values:
        in_range = sum(1 for v in values if 70 <= v <= 180)
        result.update({
            "avg": round(sum(values) / len(values), 1),
            "min": min(values),
            "max": max(values),
            "tir_pct": round(in_range / len(values) * 100, 1),
        })
    else:
        result.update({"avg": None, "min": None, "max": None, "tir_pct": None})

    out(result)


def cmd_week(args):
    """Last 7 days vs prior 7 days: BG, variability, insulin, workouts."""
    conn = get_db()
    now = utc_now()

    def period_stats(start, end):
        bg_rows = conn.execute("""
            SELECT glucose_mg_dl FROM glucose_readings
            WHERE timestamp >= ? AND timestamp < ?
        """, (start.isoformat(), end.isoformat())).fetchall()
        values = [r["glucose_mg_dl"] for r in bg_rows]

        dose_rows = conn.execute("""
            SELECT units FROM insulin_doses
            WHERE timestamp >= ? AND timestamp < ?
        """, (start.isoformat(), end.isoformat())).fetchall()
        total_insulin = sum(r["units"] for r in dose_rows)

        workout_count = conn.execute("""
            SELECT COUNT(*) AS n FROM workouts
            WHERE started_at >= ? AND started_at < ?
        """, (start.isoformat(), end.isoformat())).fetchone()["n"]

        stats = {
            "total_insulin_units": round(total_insulin, 1),
            "avg_insulin_per_day": round(total_insulin / 7, 1),
            "workout_count": workout_count,
        }
        if values:
            mean = sum(values) / len(values)
            in_range = sum(1 for v in values if 70 <= v <= 180)
            stats["avg"] = round(mean, 1)
            stats["tir_pct"] = round(in_range / len(values) * 100, 1)
            stats["cv"] = round(statistics.pstdev(values) / mean * 100, 1) if len(values) >= 2 else None
        else:
            stats["avg"] = None
            stats["tir_pct"] = None
            stats["cv"] = None
        return stats

    last_7d = period_stats(now - timedelta(days=7), now)
    prior_7d = period_stats(now - timedelta(days=14), now - timedelta(days=7))
    conn.close()

    tir_change = None
    if last_7d["tir_pct"] is not None and prior_7d["tir_pct"] is not None:
        tir_change = round(last_7d["tir_pct"] - prior_7d["tir_pct"], 1)
    avg_change = None
    if last_7d["avg"] is not None and prior_7d["avg"] is not None:
        avg_change = round(last_7d["avg"] - prior_7d["avg"], 1)

    out({
        "last_7d": last_7d,
        "prior_7d": prior_7d,
        "tir_change": tir_change,
        "avg_change": avg_change,
    })


def cmd_last_bolus(args):
    """Most recent bolus dose (SMBs included) plus today's NY-day bolus total."""
    conn = get_db()
    row = conn.execute("""
        SELECT timestamp, units, notes FROM insulin_doses
        WHERE type = 'bolus' ORDER BY timestamp DESC LIMIT 1
    """).fetchone()

    if not row:
        conn.close()
        out({"error": "No bolus doses found"})
        return

    dt = datetime.fromisoformat(row["timestamp"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    mins_ago = (utc_now() - dt).total_seconds() / 60

    start_utc, end_utc, _ = ny_day_bounds()
    today_row = conn.execute("""
        SELECT SUM(units) AS total FROM insulin_doses
        WHERE type = 'bolus' AND timestamp >= ? AND timestamp < ?
    """, (start_utc, end_utc)).fetchone()
    conn.close()

    out({
        "units": row["units"],
        "time_ny": to_ny(row["timestamp"]),
        "minutes_ago": round(mins_ago, 1),
        "notes": row["notes"],
        "today_bolus_total": round(today_row["total"] or 0, 1),
    })


def cmd_last_meal(args):
    """Most recent meal entry."""
    conn = get_db()
    row = conn.execute("""
        SELECT timestamp, description, carbs_g FROM meals
        ORDER BY timestamp DESC LIMIT 1
    """).fetchone()
    conn.close()

    if not row:
        out({"error": "No meals found"})
        return

    dt = datetime.fromisoformat(row["timestamp"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    hours_ago = (utc_now() - dt).total_seconds() / 3600

    out({
        "description": row["description"],
        "carbs_g": row["carbs_g"],
        "time_ny": to_ny(row["timestamp"]),
        "hours_ago": round(hours_ago, 1),
    })


def cmd_alerts(args):
    """Alert log entries over the last N hours."""
    hours = args.hours
    conn = get_db()

    if not _table_exists(conn, "alert_log"):
        conn.close()
        out({"error": "alert_log table not found"})
        return

    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT rule_name, triggered_at, message, sent FROM alert_log
        WHERE triggered_at > ? ORDER BY triggered_at DESC
    """, (cutoff,)).fetchall()
    conn.close()

    out({
        "hours": hours,
        "count": len(rows),
        "alerts": [{
            "rule_name": r["rule_name"],
            "time_ny": to_ny(r["triggered_at"]),
            "message": r["message"],
            "sent": bool(r["sent"]),
        } for r in rows],
    })


def cmd_bg_at(args):
    """Glucose reading nearest a user-supplied time expression."""
    try:
        target_ny = parse_time_arg(args.time)
    except ValueError as e:
        out({"error": str(e)})
        return

    target_utc = target_ny.astimezone(UTC)
    conn = get_db()
    window_start = (target_utc - timedelta(hours=12)).isoformat()
    window_end = (target_utc + timedelta(hours=12)).isoformat()
    rows = conn.execute("""
        SELECT timestamp, glucose_mg_dl, trend_arrow FROM glucose_readings
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp
    """, (window_start, window_end)).fetchall()
    conn.close()

    if not rows:
        out({"error": f"No glucose readings near {args.time!r}"})
        return

    best, best_diff = None, None
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        diff = abs((dt - target_utc).total_seconds() / 60)
        if best_diff is None or diff < best_diff:
            best, best_diff = r, diff

    if best_diff > 30:
        out({"error": f"Nearest reading is {round(best_diff)} min from {args.time!r} (>30 min threshold)"})
        return

    out({
        "requested_time": args.time,
        "glucose_mg_dl": best["glucose_mg_dl"],
        "trend_arrow": best["trend_arrow"],
        "time_ny": to_ny(best["timestamp"]),
        "minutes_off": round(best_diff, 1),
    })


def cmd_a1c(args):
    """Estimated GMI (a1c-like) from mean BG over N days."""
    days = args.days
    conn = get_db()
    cutoff = (utc_now() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT timestamp, glucose_mg_dl FROM glucose_readings
        WHERE timestamp > ? ORDER BY timestamp
    """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        out({"error": f"No readings in last {days}d"})
        return

    values = [r["glucose_mg_dl"] for r in rows]
    mean = sum(values) / len(values)
    gmi = round(3.31 + 0.02392 * mean, 2)

    ny_dates = set()
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ny_dates.add(dt.astimezone(NY).date().isoformat())

    out({
        "mean": round(mean, 1),
        "gmi_estimate": gmi,
        "count": len(values),
        "days_with_data": len(ny_dates),
        "days_requested": days,
    })


def cmd_carbs(args):
    """Total carbs from meals over the last N hours."""
    hours = args.hours
    conn = get_db()
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()

    rows = conn.execute("""
        SELECT carbs_g FROM meals WHERE timestamp > ?
    """, (cutoff,)).fetchall()
    conn.close()

    out({
        "hours": hours,
        "count": len(rows),
        "total_carbs_g": round(sum(r["carbs_g"] or 0 for r in rows), 1),
    })


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TypeOneZen query tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("now", help="Current BG + trend + live pump/loop context")

    sub.add_parser("pump", help="Live pump status from Nightscout")

    p_range = sub.add_parser("range", help="BG stats over N hours")
    p_range.add_argument("hours", type=float)

    p_insulin = sub.add_parser("insulin", help="Insulin doses over N hours")
    p_insulin.add_argument("hours", type=float)

    p_meals = sub.add_parser("meals", help="Recent meals")
    p_meals.add_argument("hours", type=float)

    p_workouts = sub.add_parser("workouts", help="Recent workouts")
    p_workouts.add_argument("days", type=int)

    sub.add_parser("summary", help="Cached health summary")
    sub.add_parser("monitor", help="Run BG monitor dry-run")

    p_day = sub.add_parser("day", help="Full-day recap for an NY calendar day")
    p_day.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD (default: today NY)")

    sub.add_parser("overnight", help="Most recent overnight window (11pm-7am NY) BG stats")

    sub.add_parser("week", help="Last 7 days vs prior 7 days comparison")

    sub.add_parser("last-bolus", help="Most recent bolus + today's bolus total")

    sub.add_parser("last-meal", help="Most recent meal")

    p_alerts = sub.add_parser("alerts", help="Alert log entries over N hours")
    p_alerts.add_argument("hours", type=float, nargs="?", default=24)

    p_bgat = sub.add_parser("bg-at", help="Glucose reading nearest a given time")
    p_bgat.add_argument("time", help='e.g. "3am", "yesterday 3am", "2026-07-08 14:30"')

    p_a1c = sub.add_parser("a1c", help="Estimated GMI from mean BG over N days")
    p_a1c.add_argument("days", type=int, nargs="?", default=90)

    p_carbs = sub.add_parser("carbs", help="Total carbs over N hours")
    p_carbs.add_argument("hours", type=float, nargs="?", default=24)

    args = parser.parse_args()

    cmds = {
        "now": cmd_now,
        "pump": cmd_pump,
        "range": cmd_range,
        "insulin": cmd_insulin,
        "meals": cmd_meals,
        "workouts": cmd_workouts,
        "summary": cmd_summary,
        "monitor": cmd_monitor,
        "day": cmd_day,
        "overnight": cmd_overnight,
        "week": cmd_week,
        "last-bolus": cmd_last_bolus,
        "last-meal": cmd_last_meal,
        "alerts": cmd_alerts,
        "bg-at": cmd_bg_at,
        "a1c": cmd_a1c,
        "carbs": cmd_carbs,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
