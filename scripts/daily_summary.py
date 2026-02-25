#!/usr/bin/env python3
"""
TypeOneZen daily health summary.
Sends personalized iMessage summaries with data-backed insights.

Usage:
    python3 daily_summary.py --period morning
    python3 daily_summary.py --period evening
    python3 daily_summary.py --period morning --dry-run
"""

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(Path.home() / "TypeOneZen" / ".env"))

DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
IMSG = "/opt/homebrew/bin/imsg"
PHONE = os.getenv("ALERT_PHONE", "")
USER_NAME = os.getenv("USER_NAME", "there")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# Pump settings
TARGET_BG = 110
ISF = 35
AIT_HOURS = 3
TIR_LOW = 70
TIR_HIGH = 180


# ── Helpers ──────────────────────────────────────────────────────────────────

def now_ny():
    return datetime.now(NY)


def to_utc_str(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def from_utc_str(s):
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


def trend_arrow(trend):
    arrows = {
        "rising quickly": "↑↑", "rising": "↑", "rising slightly": "↗",
        "steady": "→",
        "falling slightly": "↘", "falling": "↓", "falling quickly": "↓↓",
    }
    return arrows.get((trend or "").lower(), "→")


def fmt_activity(activity):
    labels = {
        "running": "🏃 run", "alpine_skiing": "⛷️ ski",
        "cardio_training": "💪 cardio", "walking": "🚶 walk",
        "cycling": "🚴 ride", "other": "🏋️ workout",
    }
    return labels.get(activity or "", f"🏋️ {activity or 'workout'}")


# ── Data queries ──────────────────────────────────────────────────────────────

def get_current_bg():
    conn = get_db()
    row = conn.execute(
        "SELECT glucose_mg_dl, trend, timestamp FROM glucose_readings ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    ts = from_utc_str(row["timestamp"])
    age = (now_ny() - ts).total_seconds() / 60 if ts else 99
    return {
        "bg": int(row["glucose_mg_dl"]),
        "trend": row["trend"],
        "arrow": trend_arrow(row["trend"]),
        "age_min": round(age, 1),
        "stale": age > 15,
    }


def get_bg_stats(start_dt, end_dt):
    conn = get_db()
    rows = conn.execute(
        "SELECT glucose_mg_dl FROM glucose_readings WHERE timestamp >= ? AND timestamp <= ?",
        (to_utc_str(start_dt), to_utc_str(end_dt))
    ).fetchall()
    conn.close()
    if not rows:
        return None
    vals = [r["glucose_mg_dl"] for r in rows]
    lows = [v for v in vals if v < TIR_LOW]
    highs = [v for v in vals if v > TIR_HIGH]
    return {
        "count": len(vals),
        "avg": round(sum(vals) / len(vals)),
        "min": round(min(vals)),
        "max": round(max(vals)),
        "tir": round(sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / len(vals) * 100),
        "low_count": len(lows),
        "high_count": len(highs),
        "lowest": round(min(lows)) if lows else None,
        "highest": round(max(highs)) if highs else None,
    }


def get_insulin_stats(start_dt, end_dt):
    conn = get_db()
    rows = conn.execute(
        "SELECT units, type FROM insulin_doses WHERE timestamp >= ? AND timestamp <= ?",
        (to_utc_str(start_dt), to_utc_str(end_dt))
    ).fetchall()
    conn.close()
    if not rows:
        return {"total": 0, "meal": 0, "correction": 0, "correction_count": 0, "count": 0}
    return {
        "total": round(sum(r["units"] for r in rows), 1),
        "meal": round(sum(r["units"] for r in rows if r["type"] == "meal"), 1),
        "correction": round(sum(r["units"] for r in rows if r["type"] == "correction"), 1),
        "correction_count": sum(1 for r in rows if r["type"] == "correction"),
        "count": len(rows),
    }


def get_meals(start_dt, end_dt):
    conn = get_db()
    rows = conn.execute(
        "SELECT description, carbs_g, timestamp FROM meals WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        (to_utc_str(start_dt), to_utc_str(end_dt))
    ).fetchall()
    conn.close()
    if not rows:
        return {"total_carbs": 0, "count": 0, "meals": []}
    meals = [{"desc": r["description"], "carbs": r["carbs_g"] or 0, "ts": from_utc_str(r["timestamp"])} for r in rows]
    return {
        "total_carbs": round(sum(m["carbs"] for m in meals)),
        "count": len(meals),
        "meals": meals,
    }


def get_workout(start_dt, end_dt):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workouts WHERE started_at >= ? AND started_at <= ? ORDER BY started_at",
        (to_utc_str(start_dt), to_utc_str(end_dt))
    ).fetchall()
    conn.close()
    if not rows:
        return None
    results = []
    for row in rows:
        notes = {}
        try:
            notes = json.loads(row["notes"] or "{}")
        except Exception:
            pass
        s = from_utc_str(row["started_at"])
        e = from_utc_str(row["ended_at"])
        dur = round((e - s).total_seconds() / 60) if s and e else None
        results.append({
            "activity": row["activity_type"],
            "intensity": row["intensity"],
            "started": s,
            "ended": e,
            "duration_min": dur,
            "distance_km": round(notes.get("total_distance", 0) / 1000, 1) if notes.get("total_distance") else None,
            "avg_hr": notes.get("avg_heart_rate"),
        })
    return results[0] if len(results) == 1 else results


def get_workout_bg(workout):
    if not workout or not isinstance(workout, dict):
        return None
    s, e = workout.get("started"), workout.get("ended")
    if not s or not e:
        return None
    pre = get_bg_stats(s - timedelta(minutes=30), s)
    during = get_bg_stats(s, e)
    post = get_bg_stats(e, e + timedelta(hours=2))
    return {
        "pre": pre["avg"] if pre else None,
        "during": during["avg"] if during else None,
        "post": post["avg"] if post else None,
    }


def get_iob():
    cutoff = now_ny() - timedelta(hours=AIT_HOURS)
    conn = get_db()
    rows = conn.execute(
        "SELECT timestamp, units FROM insulin_doses WHERE timestamp >= ? AND type IN ('meal','correction')",
        (to_utc_str(cutoff),)
    ).fetchall()
    conn.close()
    iob = 0.0
    for r in rows:
        ts = from_utc_str(r["timestamp"])
        if ts:
            hrs = (now_ny() - ts).total_seconds() / 3600
            iob += r["units"] * max(0, 1 - hrs / AIT_HOURS)
    return round(iob, 2)


def get_tir_historical(days):
    conn = get_db()
    rows = conn.execute(
        "SELECT glucose_mg_dl FROM glucose_readings WHERE timestamp >= datetime('now', ?)",
        (f"-{days} days",)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    vals = [r["glucose_mg_dl"] for r in rows]
    return round(sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / len(vals) * 100)


def get_30d_overnight_avg():
    """30-day average overnight BG (10pm–8am NY = UTC 03:00–13:00 at EST)."""
    conn = get_db()
    row = conn.execute(
        "SELECT AVG(glucose_mg_dl) as avg FROM glucose_readings "
        "WHERE (strftime('%H', timestamp) >= '03' AND strftime('%H', timestamp) < '13') "
        "AND timestamp >= datetime('now', '-30 days')"
    ).fetchone()
    conn.close()
    return round(row["avg"]) if row and row["avg"] else None


# ── Smart insights ─────────────────────────────────────────────────────────────

def insight_low_patterns():
    """Are lows correction-related?"""
    conn = get_db()
    readings = conn.execute(
        "SELECT timestamp, glucose_mg_dl FROM glucose_readings "
        "WHERE timestamp >= datetime('now', '-30 days') ORDER BY timestamp"
    ).fetchall()
    conn.close()

    low_events = []
    in_low = False
    low_start = None
    low_min = 999

    for r in readings:
        if r["glucose_mg_dl"] < TIR_LOW:
            if not in_low:
                in_low = True
                low_start = r["timestamp"]
                low_min = r["glucose_mg_dl"]
            else:
                low_min = min(low_min, r["glucose_mg_dl"])
        else:
            if in_low:
                low_events.append({"ts": low_start, "min": low_min})
                in_low = False
                low_min = 999

    if len(low_events) < 5:
        return None

    conn = get_db()
    correction_related = 0
    for low in low_events:
        ts = from_utc_str(low["ts"])
        if not ts:
            continue
        cutoff = ts - timedelta(hours=3)
        cnt = conn.execute(
            "SELECT COUNT(*) as c FROM insulin_doses WHERE timestamp >= ? AND timestamp <= ? AND type='correction'",
            (to_utc_str(cutoff), to_utc_str(ts))
        ).fetchone()["c"]
        if cnt > 0:
            correction_related += 1
    conn.close()

    pct = round(correction_related / len(low_events) * 100)
    return {"count": len(low_events), "correction_related": correction_related, "pct": pct}


def insight_workout_overnight_pattern():
    """Do workout days lead to higher overnight BG?"""
    conn = get_db()
    workout_days = {r["day"] for r in conn.execute(
        "SELECT DISTINCT date(datetime(started_at, '-5 hours')) as day FROM workouts "
        "WHERE started_at >= datetime('now', '-60 days')"
    ).fetchall()}

    if len(workout_days) < 3:
        conn.close()
        return None

    workout_nights, rest_nights = [], []
    for row in conn.execute(
        "SELECT date(datetime(timestamp, '-5 hours')) as ny_date, AVG(glucose_mg_dl) as avg "
        "FROM glucose_readings "
        "WHERE strftime('%H', timestamp) >= '03' AND strftime('%H', timestamp) < '13' "
        "AND timestamp >= datetime('now', '-60 days') GROUP BY ny_date"
    ).fetchall():
        if row["avg"]:
            (workout_nights if row["ny_date"] in workout_days else rest_nights).append(row["avg"])
    conn.close()

    if not workout_nights or not rest_nights:
        return None

    wo_avg = round(sum(workout_nights) / len(workout_nights))
    rest_avg = round(sum(rest_nights) / len(rest_nights))
    return {"workout_avg": wo_avg, "rest_avg": rest_avg, "diff": wo_avg - rest_avg, "days": len(workout_nights)}


def insight_correction_day_pattern():
    """Do correction-heavy days lead to worse overnight BG?"""
    conn = get_db()
    corr_by_day = {}
    for r in conn.execute(
        "SELECT date(datetime(timestamp, '-5 hours')) as d, COUNT(*) as cnt "
        "FROM insulin_doses WHERE type='correction' AND timestamp >= datetime('now', '-30 days') GROUP BY d"
    ).fetchall():
        corr_by_day[r["d"]] = r["cnt"]

    heavy_days = {d for d, c in corr_by_day.items() if c >= 2}
    if len(heavy_days) < 3:
        conn.close()
        return None

    heavy_nights, clean_nights = [], []
    for row in conn.execute(
        "SELECT date(datetime(timestamp, '-5 hours')) as d, AVG(glucose_mg_dl) as avg "
        "FROM glucose_readings "
        "WHERE strftime('%H', timestamp) >= '03' AND strftime('%H', timestamp) < '13' "
        "AND timestamp >= datetime('now', '-30 days') GROUP BY d"
    ).fetchall():
        if row["avg"]:
            if row["d"] in heavy_days:
                heavy_nights.append(row["avg"])
            elif corr_by_day.get(row["d"], 0) <= 1:
                clean_nights.append(row["avg"])
    conn.close()

    if not heavy_nights or not clean_nights:
        return None

    return {
        "heavy_avg": round(sum(heavy_nights) / len(heavy_nights)),
        "clean_avg": round(sum(clean_nights) / len(clean_nights)),
        "diff": round(sum(heavy_nights) / len(heavy_nights) - sum(clean_nights) / len(clean_nights)),
    }


def count_consecutive_overnight_highs():
    conn = get_db()
    nights = conn.execute(
        "SELECT date(datetime(timestamp, '-5 hours')) as d, AVG(glucose_mg_dl) as avg "
        "FROM glucose_readings "
        "WHERE strftime('%H', timestamp) >= '03' AND strftime('%H', timestamp) < '13' "
        "AND timestamp >= datetime('now', '-7 days') GROUP BY d ORDER BY d DESC LIMIT 5"
    ).fetchall()
    conn.close()
    count = 0
    for n in nights:
        if n["avg"] and n["avg"] > 155:
            count += 1
        else:
            break
    return count


# ── Summary builders ──────────────────────────────────────────────────────────

def build_morning():
    now = now_ny()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    overnight_start = today_start - timedelta(days=1)
    overnight_start = overnight_start.replace(hour=22, minute=0, second=0)

    lines = []

    # Current BG
    bg = get_current_bg()
    if bg:
        stale = f" (sensor reading is {round(bg['age_min'])}m old)" if bg["stale"] else ""
        lines.append(f"Good morning {USER_NAME} 🩺 BG is {bg['bg']} {bg['arrow']}{stale}")
    else:
        lines.append(f"Good morning {USER_NAME} 🩺 No CGM data available right now.")
    lines.append("")

    # Overnight
    overnight = get_bg_stats(overnight_start, now)
    avg_30d_overnight = get_30d_overnight_avg()
    if overnight and overnight["count"] >= 10:
        line = f"Overnight: avg {overnight['avg']}, TIR {overnight['tir']}%"
        if overnight["low_count"]:
            line += f" ⚠️ {overnight['low_count']} low(s), min {overnight['lowest']}"
        if avg_30d_overnight:
            diff = overnight["avg"] - avg_30d_overnight
            if abs(diff) >= 10:
                line += f" ({'+' if diff > 0 else ''}{diff} vs your 30d overnight avg of {avg_30d_overnight})"
        lines.append(line)

    # Yesterday
    yest = get_bg_stats(yesterday_start, today_start)
    tir_7d = get_tir_historical(7)
    tir_30d = get_tir_historical(30)
    insulin_yest = get_insulin_stats(yesterday_start, today_start)
    meals_yest = get_meals(yesterday_start, today_start)
    workout_yest = get_workout(yesterday_start, today_start)

    if yest:
        ctx = ""
        if tir_7d:
            diff = yest["tir"] - tir_7d
            if diff >= 5:
                ctx = f" ↑ above your 7d avg of {tir_7d}%"
            elif diff <= -5:
                ctx = f" ↓ below your 7d avg of {tir_7d}%"
            else:
                ctx = f" (on par with 7d avg {tir_7d}%)"
        lines.append(f"Yesterday: TIR {yest['tir']}%{ctx}, avg {yest['avg']} mg/dL")

    if insulin_yest["total"] > 0:
        i = insulin_yest
        parts = []
        if i["meal"] > 0:
            parts.append(f"{i['meal']}u meal")
        if i["correction"] > 0:
            parts.append(f"{i['correction']}u correction ({i['correction_count']}x)")
        lines.append(f"Insulin: {i['total']}u — {', '.join(parts)}" if parts else f"Insulin: {i['total']}u")

    if meals_yest["count"]:
        lines.append(f"Meals: {meals_yest['count']} logged, {meals_yest['total_carbs']}g carbs")

    if workout_yest and isinstance(workout_yest, dict):
        w = workout_yest
        wline = fmt_activity(w["activity"])
        if w["duration_min"]:
            wline += f" {w['duration_min']} min"
        if w["distance_km"]:
            wline += f", {w['distance_km']} km"
        bg_w = get_workout_bg(w)
        if bg_w and bg_w["pre"] and bg_w["during"]:
            drop = bg_w["pre"] - bg_w["during"]
            wline += f" — BG {bg_w['pre']}→{bg_w['during']} ({'+' if drop < 0 else '-'}{abs(drop)} mg/dL)"
        lines.append(f"Workout: {wline}")

    lines.append("")

    # Smart insight
    insight = None

    # Consecutive overnight highs
    consec = count_consecutive_overnight_highs()
    if overnight and overnight["avg"] > 155 and consec >= 3:
        small_corr = round((overnight["avg"] - TARGET_BG) / ISF * 0.4, 1)
        insight = (
            f"⚠️ This is night {consec} in a row with overnight avg above 155 "
            f"(last night: {overnight['avg']}). "
            f"A small correction (~{small_corr}u) when BG is above 150 at 10pm has a good track record "
            f"for flattening this out overnight."
        )

    # Low was correction-related
    if not insight and overnight and overnight["low_count"]:
        lp = insight_low_patterns()
        if lp and lp["pct"] >= 60:
            insight = (
                f"📉 You had a low overnight. Pattern: {lp['pct']}% of your last {lp['count']} lows "
                f"over 30 days happened within 3 hours of a correction. "
                f"Correction stacking is your most common low trigger — "
                f"waiting 90+ min between corrections tends to help."
            )

    # Workout → overnight high pattern
    if not insight and workout_yest and isinstance(workout_yest, dict):
        wp = insight_workout_overnight_pattern()
        if wp and wp["diff"] >= 15:
            insight = (
                f"🏃 You worked out yesterday. Over 60 days of data, your overnight avg on active days "
                f"is {wp['workout_avg']} vs {wp['rest_avg']} on rest days (+{wp['diff']} mg/dL). "
                f"Worth doing a quick BG check before bed on workout days."
            )

    # Great day
    if not insight and yest and tir_30d and yest["tir"] >= tir_30d + 8:
        correction_note = "zero corrections needed" if insulin_yest["correction_count"] == 0 else f"only {insulin_yest['correction_count']} correction(s)"
        insight = f"🌟 Yesterday was {yest['tir']}% TIR — your best recently (30d avg: {tir_30d}%). {correction_note.capitalize()}. That's the pattern to replicate."

    # TIR trend
    if not insight and tir_7d and tir_30d:
        if tir_7d > tir_30d + 3:
            insight = f"📈 7-day TIR is {tir_7d}% vs your 30d avg of {tir_30d}% — you're trending up toward the 90% goal."
        elif tir_7d < tir_30d - 4:
            insight = f"📉 7-day TIR ({tir_7d}%) has dipped below your 30d avg ({tir_30d}%). Last week was rougher — worth reviewing what changed."

    if insight:
        lines.append(insight)
        lines.append("")

    # Look ahead
    iob = get_iob()
    if iob > 0.5:
        lines.append(f"⚠️ Still {iob}u IOB from overnight — go slow with breakfast dosing.")
    if workout_yest and isinstance(workout_yest, dict):
        lines.append("💡 Active yesterday — insulin sensitivity may still be elevated today.")

    return "\n".join(lines).strip()


def build_evening():
    now = now_ny()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    lines = []

    # Current BG
    bg = get_current_bg()
    if bg:
        stale = f" (sensor {round(bg['age_min'])}m old)" if bg["stale"] else ""
        lines.append(f"Good evening 🩺 Day recap — BG is {bg['bg']} {bg['arrow']}{stale}")
    else:
        lines.append("Good evening 🩺 No CGM data right now.")
    lines.append("")

    # Today
    today = get_bg_stats(today_start, now)
    tir_30d = get_tir_historical(30)
    insulin_today = get_insulin_stats(today_start, now)
    meals_today = get_meals(today_start, now)
    workout_today = get_workout(today_start, now)

    if today and today["count"] >= 20:
        ctx = ""
        if tir_30d:
            diff = today["tir"] - tir_30d
            if abs(diff) >= 4:
                ctx = f" ({'+' if diff > 0 else ''}{diff}pts vs your {tir_30d}% avg)"
        line = f"Today: TIR {today['tir']}%{ctx}, avg {today['avg']} mg/dL"
        if today["low_count"]:
            line += f" ⚠️ {today['low_count']} low(s), min {today['lowest']}"
        lines.append(line)

    if insulin_today["total"] > 0:
        i = insulin_today
        iline = f"Insulin: {i['total']}u"
        if i["correction_count"]:
            iline += f", {i['correction_count']} correction(s) ({i['correction']}u)"
        lines.append(iline)

    if meals_today["count"]:
        lines.append(f"Meals: {meals_today['count']} logged, {meals_today['total_carbs']}g carbs")

    # Workout
    if workout_today and isinstance(workout_today, dict):
        w = workout_today
        wline = fmt_activity(w["activity"])
        if w["duration_min"]:
            wline += f" {w['duration_min']} min"
        if w["distance_km"]:
            wline += f", {w['distance_km']} km"
        lines.append(f"Workout: {wline}")

        bg_w = get_workout_bg(w)
        if bg_w and bg_w["pre"] and bg_w["during"]:
            drop = bg_w["pre"] - bg_w["during"]
            bg_line = f"BG: {bg_w['pre']}→{bg_w['during']} during"
            if bg_w["post"]:
                bg_line += f", {bg_w['post']} after"

            # Compare to historical
            conn = get_db()
            similar = conn.execute(
                "SELECT started_at, ended_at FROM workouts WHERE activity_type = ? AND started_at < ? ORDER BY started_at DESC LIMIT 5",
                (w["activity"], to_utc_str(w["started"]))
            ).fetchall()
            conn.close()

            past_drops = []
            for s in similar:
                s_start = from_utc_str(s["started_at"])
                s_end = from_utc_str(s["ended_at"])
                if not s_start or not s_end:
                    continue
                pre = get_bg_stats(s_start - timedelta(minutes=30), s_start)
                dur = get_bg_stats(s_start, s_end)
                if pre and dur and pre["avg"] and dur["avg"]:
                    past_drops.append(pre["avg"] - dur["avg"])

            if len(past_drops) >= 3:
                avg_drop = round(sum(past_drops) / len(past_drops))
                if abs(drop - avg_drop) >= 15:
                    comparison = "bigger drop than usual" if drop > avg_drop else "more stable than usual"
                    bg_line += f" ({comparison} — last {len(past_drops)} similar: avg {avg_drop} drop)"

            lines.append(bg_line)

    lines.append("")

    # Smart insight
    insight = None

    # Multiple corrections → overnight risk
    if insulin_today["correction_count"] >= 2:
        cp = insight_correction_day_pattern()
        if cp and cp["diff"] >= 8:
            insight = (
                f"📊 {insulin_today['correction_count']} corrections today ({insulin_today['correction']}u). "
                f"On correction-heavy days your overnight avg runs {cp['heavy_avg']} vs {cp['clean_avg']} on cleaner days. "
                f"The stacking tends to create volatility around 2-4am — keep an eye on it."
            )

    # Strong day
    if not insight and today and tir_30d and today["tir"] >= tir_30d + 8 and today["count"] >= 100:
        if workout_today and isinstance(workout_today, dict):
            insight = f"🌟 Strong day — {today['tir']}% TIR. Exercise days consistently outperform your rest days in the data. Keep it up."
        elif insulin_today["correction_count"] == 0:
            insight = f"🌟 {today['tir']}% TIR today — well above your {tir_30d}% avg. Zero corrections. Your dosing was dialed in today — that timing and approach is worth replicating."

    # Low today
    if not insight and today and today["low_count"]:
        lp = insight_low_patterns()
        if lp and lp["pct"] >= 60:
            insight = (
                f"⚠️ Low today. Historical pattern: {lp['pct']}% of your last {lp['count']} lows "
                f"happened within 3 hours of a correction dose. "
                f"Waiting 90+ min between corrections can help break the stacking cycle."
            )

    # TIR trending down
    if not insight and tir_30d:
        tir_7d = get_tir_historical(7)
        if tir_7d and tir_7d < tir_30d - 5:
            insight = f"📉 7-day TIR is {tir_7d}% vs your 30d avg of {tir_30d}%. This past week has been rougher — worth thinking about what shifted."

    if insight:
        lines.append(insight)
        lines.append("")

    # Overnight risk flag
    iob = get_iob()
    bg_now = bg["bg"] if bg else None
    risk_parts = []

    if workout_today and isinstance(workout_today, dict):
        dur = workout_today.get("duration_min") or 0
        wp = insight_workout_overnight_pattern()
        activity_name = fmt_activity(workout_today.get("activity"))
        if dur >= 45:
            risk = f"🏃 {activity_name} today ({dur} min) — insulin sensitivity stays elevated overnight."
            if wp and wp["diff"] >= 15:
                risk += f" Your active-day overnights avg {wp['workout_avg']} vs {wp['rest_avg']} on rest days."
            if bg_now and bg_now < 130:
                risk += f" BG is {bg_now} right now — 15-20g slow carbs before bed worth considering."
            else:
                risk += " Check BG before bed and keep carbs nearby."
            risk_parts.append(risk)

    if iob >= 1.0 and bg_now:
        projected = round(bg_now - (iob * ISF * 0.4))
        if projected < 85:
            risk_parts.append(
                f"⚠️ {iob}u IOB with BG at {bg_now} — could push toward {projected} by midnight. "
                f"A small snack (~{round(iob * 4 * 0.5)}g carbs) might be worth it."
            )

    if not risk_parts:
        if bg_now and 85 <= bg_now <= 160 and iob < 0.5:
            risk_parts.append(f"✅ Overnight looks clear — BG {bg_now}, no significant IOB. Sleep well.")
        elif bg_now and bg_now > 165 and iob < 0.5:
            small_corr = round((bg_now - TARGET_BG) / ISF, 1)
            risk_parts.append(f"⚠️ BG is {bg_now} with no IOB — a correction (~{small_corr}u) now could prevent an overnight high.")

    if risk_parts:
        lines.append("Tonight: " + " ".join(risk_parts))

    return "\n".join(lines).strip()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", choices=["morning", "evening"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    message = build_morning() if args.period == "morning" else build_evening()

    print("=" * 60)
    print(f"GENERATED {args.period.upper()} SUMMARY:")
    print("=" * 60)
    print(message)
    print("=" * 60)

    if args.dry_run:
        print("[DRY RUN — not sent]")
        return

    if not PHONE:
        print("Error: ALERT_PHONE must be set in .env")
        return

    result = subprocess.run([IMSG, "send", "--to", PHONE, "--text", message], capture_output=True, text=True)
    if result.returncode == 0:
        print("✅ Sent via iMessage")
    else:
        print(f"❌ Send failed: {result.stderr}")


if __name__ == "__main__":
    main()
