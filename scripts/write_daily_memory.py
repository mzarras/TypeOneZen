#!/usr/bin/env python3
"""
Writes a daily memory file to ~/.openclaw/workspace/memory/YYYY-MM-DD.md
Summarizes the day's T1D data + any notable events from alert_log and notes.
Runs nightly at 10:30pm via cron.
"""

import warnings
warnings.filterwarnings("ignore")

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

TIR_LOW = 70
TIR_HIGH = 180


def now_ny():
    return datetime.now(NY)


def to_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def from_utc(s):
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        if len(s) == 19:
            s += "+00:00"
        return datetime.fromisoformat(s).astimezone(NY)
    except Exception:
        return None


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def main():
    now = now_ny()
    today = now.date()
    today_start = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=NY)
    today_end = today_start + timedelta(days=1)

    conn = get_db()

    # BG stats
    bg_rows = conn.execute(
        "SELECT glucose_mg_dl FROM glucose_readings WHERE timestamp >= ? AND timestamp < ?",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()

    bg_vals = [r["glucose_mg_dl"] for r in bg_rows]
    if bg_vals:
        tir = round(sum(1 for v in bg_vals if TIR_LOW <= v <= TIR_HIGH) / len(bg_vals) * 100)
        avg_bg = round(sum(bg_vals) / len(bg_vals))
        min_bg = round(min(bg_vals))
        max_bg = round(max(bg_vals))
        low_count = sum(1 for v in bg_vals if v < TIR_LOW)
        high_count = sum(1 for v in bg_vals if v > TIR_HIGH)
    else:
        tir = avg_bg = min_bg = max_bg = low_count = high_count = None

    # Insulin
    insulin_rows = conn.execute(
        "SELECT units, type, notes FROM insulin_doses WHERE timestamp >= ? AND timestamp < ?",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()
    total_insulin = round(sum(r["units"] for r in insulin_rows), 1)
    meal_insulin = round(sum(r["units"] for r in insulin_rows if r["type"] == "meal"), 1)
    correction_insulin = round(sum(r["units"] for r in insulin_rows if r["type"] == "correction"), 1)
    correction_count = sum(1 for r in insulin_rows if r["type"] == "correction")

    # Meals
    meal_rows = conn.execute(
        "SELECT description, carbs_g FROM meals WHERE timestamp >= ? AND timestamp < ?",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()
    total_carbs = round(sum(r["carbs_g"] or 0 for r in meal_rows))

    # Workouts
    workout_rows = conn.execute(
        "SELECT activity_type, started_at, ended_at, notes FROM workouts WHERE started_at >= ? AND started_at < ?",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()

    # Alerts fired today
    alert_rows = conn.execute(
        "SELECT rule_name, triggered_at, message FROM alert_log WHERE triggered_at >= ? AND triggered_at < ? ORDER BY triggered_at",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()

    # Notes written today
    note_rows = conn.execute(
        "SELECT body, tags FROM notes WHERE timestamp >= ? AND timestamp < ?",
        (to_utc(today_start), to_utc(today_end))
    ).fetchall()

    # 7-day and 30-day TIR for context
    def historical_tir(days):
        rows = conn.execute(
            "SELECT glucose_mg_dl FROM glucose_readings WHERE timestamp >= datetime('now', ?)",
            (f"-{days} days",)
        ).fetchall()
        if not rows:
            return None
        vals = [r["glucose_mg_dl"] for r in rows]
        return round(sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / len(vals) * 100)

    tir_7d = historical_tir(7)
    tir_30d = historical_tir(30)

    conn.close()

    # Build markdown
    lines = [f"# Daily Memory — {today.strftime('%A, %B %-d, %Y')}", ""]

    # BG Summary
    lines.append("## Blood Glucose")
    if bg_vals:
        lines.append(f"- TIR: {tir}% (7d avg: {tir_7d}%, 30d avg: {tir_30d}%)")
        lines.append(f"- Avg: {avg_bg} mg/dL | Low: {min_bg} | High: {max_bg}")
        if low_count:
            lines.append(f"- ⚠️ {low_count} low episode(s) today (BG < 70)")
        if high_count > 5:
            lines.append(f"- {high_count} readings above 180 mg/dL")
    else:
        lines.append("- No BG data recorded today")
    lines.append("")

    # Insulin
    lines.append("## Insulin")
    lines.append(f"- Total: {total_insulin}u | Meal: {meal_insulin}u | Correction: {correction_insulin}u ({correction_count}x)")
    lines.append("")

    # Meals
    lines.append("## Meals")
    if meal_rows:
        lines.append(f"- {len(meal_rows)} meals logged, {total_carbs}g carbs total")
        for r in meal_rows:
            lines.append(f"  - {r['description']} ({r['carbs_g'] or 0}g carbs)")
    else:
        lines.append("- No meals logged")
    lines.append("")

    # Workouts
    lines.append("## Workouts")
    if workout_rows:
        for w in workout_rows:
            notes = {}
            try:
                notes = json.loads(w["notes"] or "{}")
            except Exception:
                pass
            s = from_utc(w["started_at"])
            e = from_utc(w["ended_at"])
            dur = round((e - s).total_seconds() / 60) if s and e else "?"
            dist = round(notes.get("total_distance", 0) / 1000, 1) if notes.get("total_distance") else None
            hr = notes.get("avg_heart_rate")
            line = f"- {w['activity_type']}, {dur} min"
            if dist:
                line += f", {dist} km"
            if hr:
                line += f", avg HR {hr}"
            lines.append(line)
    else:
        lines.append("- No workouts today")
    lines.append("")

    # Alerts
    if alert_rows:
        lines.append("## Alerts Fired")
        for a in alert_rows:
            ts = from_utc(a["triggered_at"])
            ts_str = ts.strftime("%-I:%M%p").lower() if ts else "?"
            lines.append(f"- {ts_str} — {a['rule_name']}")
        lines.append("")

    # Notes
    if note_rows:
        lines.append("## Notes")
        for n in note_rows:
            lines.append(f"- {n['body']}")
            if n["tags"]:
                lines.append(f"  tags: {n['tags']}")
        lines.append("")

    # Write file
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MEMORY_DIR / f"{today}.md"
    out_path.write_text("\n".join(lines))
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
