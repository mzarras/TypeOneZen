#!/usr/bin/env python3
"""TypeOneZen query script — compact JSON output for common BG/insulin/meal queries."""

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
SUMMARY_PATH = Path.home() / "TypeOneZen" / "summaries" / "stats_cache.json"
MONITOR_SCRIPT = Path.home() / "TypeOneZen" / "monitor.py"
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

    out({
        "glucose_mg_dl": row["glucose_mg_dl"],
        "trend": row["trend"],
        "trend_arrow": row["trend_arrow"],
        "timestamp_ny": to_ny(ts),
        "minutes_ago": round(mins_ago, 1),
    })


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


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TypeOneZen query tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("now", help="Current BG + trend")

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

    args = parser.parse_args()

    cmds = {
        "now": cmd_now,
        "range": cmd_range,
        "insulin": cmd_insulin,
        "meals": cmd_meals,
        "workouts": cmd_workouts,
        "summary": cmd_summary,
        "monitor": cmd_monitor,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
