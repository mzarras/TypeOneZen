#!/usr/bin/env python3
"""Generate health summary files from TypeOneZen database."""

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
SUMMARY_DIR = Path.home() / "TypeOneZen" / "summaries"

# BG range thresholds (mg/dL)
BG_LOW = 70
BG_HIGH = 180

TIME_WINDOWS = [
    ("Overnight (0–6)", 0, 6),
    ("Morning (6–9)", 6, 9),
    ("Late Morning (9–12)", 9, 12),
    ("Afternoon (12–17)", 12, 17),
    ("Evening (17–21)", 17, 21),
    ("Night (21–24)", 21, 24),
]


def connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def parse_ts(ts_str):
    """Parse an ISO8601 timestamp string to a timezone-aware datetime."""
    return datetime.fromisoformat(ts_str)


def to_local(dt):
    """Convert a datetime to local (America/New_York) time."""
    return dt.astimezone(LOCAL_TZ)


# ---------- data loaders ----------

def load_glucose(conn, since=None):
    """Load glucose readings, optionally filtered to timestamps >= since."""
    if since:
        rows = conn.execute(
            "SELECT timestamp, glucose_mg_dl, trend FROM glucose_readings "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, glucose_mg_dl, trend FROM glucose_readings ORDER BY timestamp"
        ).fetchall()
    result = []
    for r in rows:
        try:
            dt = parse_ts(r["timestamp"])
            bg = r["glucose_mg_dl"]
            if bg is not None and 20 <= bg <= 500:
                result.append({"dt": dt, "bg": bg, "trend": r["trend"]})
        except (ValueError, TypeError):
            continue
    return result


def load_insulin(conn, since=None):
    if since:
        rows = conn.execute(
            "SELECT timestamp, units, type FROM insulin_doses "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, units, type FROM insulin_doses ORDER BY timestamp"
        ).fetchall()
    result = []
    for r in rows:
        try:
            dt = parse_ts(r["timestamp"])
            units = r["units"]
            if units is not None and units > 0:
                result.append({"dt": dt, "units": units, "type": r["type"] or "unknown"})
        except (ValueError, TypeError):
            continue
    return result


def load_meals(conn, since=None):
    if since:
        rows = conn.execute(
            "SELECT timestamp, description, carbs_g, protein_g, fat_g, fiber_g, "
            "calories, glycemic_load, source, notes FROM meals "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (since.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, description, carbs_g, protein_g, fat_g, fiber_g, "
            "calories, glycemic_load, source, notes FROM meals ORDER BY timestamp"
        ).fetchall()
    result = []
    for r in rows:
        try:
            dt = parse_ts(r["timestamp"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            result.append({
                "dt": dt,
                "description": r["description"],
                "carbs_g": r["carbs_g"],
                "protein_g": r["protein_g"],
                "fat_g": r["fat_g"],
                "fiber_g": r["fiber_g"],
                "calories": r["calories"],
                "glycemic_load": r["glycemic_load"],
                "source": r["source"],
                "notes": r["notes"],
            })
        except (ValueError, TypeError):
            continue
    return result


def load_workouts(conn, since=None):
    if since:
        rows = conn.execute(
            "SELECT started_at, ended_at, activity_type, intensity, notes FROM workouts "
            "WHERE started_at >= ? ORDER BY started_at",
            (since.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT started_at, ended_at, activity_type, intensity, notes FROM workouts ORDER BY started_at"
        ).fetchall()
    result = []
    for r in rows:
        try:
            start = parse_ts(r["started_at"])
            end = parse_ts(r["ended_at"]) if r["ended_at"] else None
            notes = {}
            if r["notes"]:
                try:
                    notes = json.loads(r["notes"])
                except json.JSONDecodeError:
                    pass
            result.append({
                "start": start,
                "end": end,
                "activity_type": r["activity_type"] or "unknown",
                "intensity": r["intensity"] or "unknown",
                "notes": notes,
            })
        except (ValueError, TypeError):
            continue
    return result


# ---------- stats computation ----------

def compute_bg_stats(readings):
    """Compute BG stats from a list of reading dicts."""
    if not readings:
        return {"avg_bg": None, "tir": None, "below_70": None, "above_180": None, "count": 0}
    bgs = [r["bg"] for r in readings]
    n = len(bgs)
    avg = statistics.mean(bgs)
    in_range = sum(1 for b in bgs if BG_LOW <= b <= BG_HIGH)
    below = sum(1 for b in bgs if b < BG_LOW)
    above = sum(1 for b in bgs if b > BG_HIGH)
    return {
        "avg_bg": round(avg, 1),
        "tir": round(100 * in_range / n, 1),
        "below_70": round(100 * below / n, 1),
        "above_180": round(100 * above / n, 1),
        "count": n,
    }


def compute_insulin_stats(doses):
    """Compute insulin stats from a list of dose dicts."""
    if not doses:
        return {"total_units": 0, "bolus_units": 0, "basal_units": 0, "correction_units": 0, "count": 0}
    total = sum(d["units"] for d in doses)
    bolus = sum(d["units"] for d in doses if d["type"] == "bolus")
    basal = sum(d["units"] for d in doses if d["type"] == "basal")
    correction = sum(d["units"] for d in doses if d["type"] == "correction")
    return {
        "total_units": round(total, 1),
        "bolus_units": round(bolus, 1),
        "basal_units": round(basal, 1),
        "correction_units": round(correction, 1),
        "count": len(doses),
    }


def compute_workout_summary(workouts):
    """Summarize workouts: count by type."""
    if not workouts:
        return {"count": 0, "by_type": {}}
    by_type = {}
    for w in workouts:
        t = w["activity_type"]
        by_type[t] = by_type.get(t, 0) + 1
    return {"count": len(workouts), "by_type": by_type}


def compute_time_of_day(readings):
    """Compute avg BG for each time-of-day window using local time."""
    buckets = {label: [] for label, _, _ in TIME_WINDOWS}
    for r in readings:
        local_dt = to_local(r["dt"])
        h = local_dt.hour
        for label, start_h, end_h in TIME_WINDOWS:
            if start_h <= h < end_h:
                buckets[label].append(r["bg"])
                break
    result = {}
    for label, _, _ in TIME_WINDOWS:
        vals = buckets[label]
        result[label] = round(statistics.mean(vals), 1) if vals else None
    return result


def compute_workout_bg_correlation(workouts, all_glucose):
    """For each workout, find avg BG 2h before, during, and 3h after."""
    if not workouts or not all_glucose:
        return {"pre_avg": None, "during_avg": None, "post_avg": None, "workout_count": 0}

    # Sort glucose by time for efficient lookups
    sorted_glucose = sorted(all_glucose, key=lambda r: r["dt"])

    pre_all = []
    during_all = []
    post_all = []

    for w in workouts:
        start = w["start"]
        end = w["end"] or start + timedelta(hours=1)
        pre_start = start - timedelta(hours=2)
        post_end = end + timedelta(hours=3)

        pre_bgs = [r["bg"] for r in sorted_glucose if pre_start <= r["dt"] < start]
        during_bgs = [r["bg"] for r in sorted_glucose if start <= r["dt"] <= end]
        post_bgs = [r["bg"] for r in sorted_glucose if end < r["dt"] <= post_end]

        if pre_bgs:
            pre_all.append(statistics.mean(pre_bgs))
        if during_bgs:
            during_all.append(statistics.mean(during_bgs))
        if post_bgs:
            post_all.append(statistics.mean(post_bgs))

    return {
        "pre_avg": round(statistics.mean(pre_all), 1) if pre_all else None,
        "during_avg": round(statistics.mean(during_all), 1) if during_all else None,
        "post_avg": round(statistics.mean(post_all), 1) if post_all else None,
        "workout_count": len(workouts),
    }


def compute_meal_stats(meals):
    """Compute meal/nutrition stats from a list of meal dicts."""
    if not meals:
        return {"count": 0, "avg_carbs": None, "avg_protein": None, "avg_fat": None,
                "avg_fiber": None, "avg_calories": None, "top_descriptions": []}
    n = len(meals)
    carbs = [m["carbs_g"] for m in meals if m["carbs_g"] is not None]
    protein = [m["protein_g"] for m in meals if m["protein_g"] is not None]
    fat = [m["fat_g"] for m in meals if m["fat_g"] is not None]
    fiber = [m["fiber_g"] for m in meals if m["fiber_g"] is not None]
    cals = [m["calories"] for m in meals if m["calories"] is not None]

    # Top 5 most common descriptions
    desc_counts = {}
    for m in meals:
        d = m["description"].strip().lower()
        desc_counts[d] = desc_counts.get(d, 0) + 1
    top = sorted(desc_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "count": n,
        "avg_carbs": round(statistics.mean(carbs), 1) if carbs else None,
        "avg_protein": round(statistics.mean(protein), 1) if protein else None,
        "avg_fat": round(statistics.mean(fat), 1) if fat else None,
        "avg_fiber": round(statistics.mean(fiber), 1) if fiber else None,
        "avg_calories": round(statistics.mean(cals)) if cals else None,
        "top_descriptions": [{"description": d, "count": c} for d, c in top],
    }


def compute_food_bg_correlation(meals, all_glucose):
    """For each meal, compute avg BG 2h after vs 2h before. Also split by fiber."""
    if not meals or not all_glucose:
        return {"avg_bg_rise": None, "meal_count": 0,
                "high_fiber_avg_rise": None, "low_fiber_avg_rise": None,
                "high_fiber_count": 0, "low_fiber_count": 0}

    sorted_glucose = sorted(all_glucose, key=lambda r: r["dt"])
    rises = []
    high_fiber_rises = []
    low_fiber_rises = []

    for m in meals:
        meal_time = m["dt"]
        pre_start = meal_time - timedelta(hours=2)
        post_end = meal_time + timedelta(hours=2)

        pre_bgs = [r["bg"] for r in sorted_glucose if pre_start <= r["dt"] < meal_time]
        post_bgs = [r["bg"] for r in sorted_glucose if meal_time < r["dt"] <= post_end]

        if pre_bgs and post_bgs:
            rise = statistics.mean(post_bgs) - statistics.mean(pre_bgs)
            rises.append(rise)
            if m["fiber_g"] is not None:
                if m["fiber_g"] > 5:
                    high_fiber_rises.append(rise)
                else:
                    low_fiber_rises.append(rise)

    return {
        "avg_bg_rise": round(statistics.mean(rises), 1) if rises else None,
        "meal_count": len(rises),
        "high_fiber_avg_rise": round(statistics.mean(high_fiber_rises), 1) if high_fiber_rises else None,
        "low_fiber_avg_rise": round(statistics.mean(low_fiber_rises), 1) if low_fiber_rises else None,
        "high_fiber_count": len(high_fiber_rises),
        "low_fiber_count": len(low_fiber_rises),
    }


def generate_insights(stats):
    """Generate top 3 plain-English insights from computed stats."""
    insights = []

    # Insight: workout BG drop
    wc = stats.get("workout_correlation", {})
    if wc.get("pre_avg") and wc.get("post_avg") and wc["pre_avg"] > 0:
        drop_pct = round(100 * (wc["pre_avg"] - wc["post_avg"]) / wc["pre_avg"], 1)
        if drop_pct > 0:
            insights.append(
                f"BG tends to drop {drop_pct}% in the 3 hours after a workout "
                f"(pre: {wc['pre_avg']} → post: {wc['post_avg']} mg/dL)"
            )
        elif drop_pct < -5:
            insights.append(
                f"BG tends to rise {abs(drop_pct)}% in the 3 hours after a workout "
                f"(pre: {wc['pre_avg']} → post: {wc['post_avg']} mg/dL)"
            )

    # Insight: time-of-day patterns
    tod = stats.get("time_of_day", {})
    tod_valid = {k: v for k, v in tod.items() if v is not None}
    if tod_valid:
        peak_window = max(tod_valid, key=tod_valid.get)
        low_window = min(tod_valid, key=tod_valid.get)
        insights.append(
            f"Highest average BG is during {peak_window}: {tod_valid[peak_window]} mg/dL; "
            f"lowest during {low_window}: {tod_valid[low_window]} mg/dL"
        )

    # Insight: recent trend
    trend = stats.get("recent_trend", {})
    if trend.get("seven_day_avg") and trend.get("thirty_day_avg"):
        diff = round(trend["seven_day_avg"] - trend["thirty_day_avg"], 1)
        direction = "lower" if diff < 0 else "higher"
        if abs(diff) > 2:
            insights.append(
                f"Last 7-day avg BG ({trend['seven_day_avg']}) is {abs(diff)} mg/dL "
                f"{direction} than the 30-day avg ({trend['thirty_day_avg']}), "
                f"{'an improvement' if diff < 0 else 'trending higher'}"
            )
        else:
            insights.append(
                f"Last 7-day avg BG ({trend['seven_day_avg']}) is holding steady "
                f"vs. the 30-day avg ({trend['thirty_day_avg']})"
            )

    # Insight: time below range
    last7 = stats.get("last_7_days", {})
    if last7.get("bg", {}).get("below_70") is not None:
        below = last7["bg"]["below_70"]
        if below > 4:
            insights.append(f"Time below range (< 70) in last 7 days is {below}% — worth monitoring")
        elif below < 1:
            insights.append(f"Minimal hypoglycemia in last 7 days ({below}% below 70 mg/dL)")

    return insights[:3]


# ---------- coverage ----------

def compute_coverage(conn):
    tables = {
        "glucose_readings": ("timestamp", "timestamp"),
        "insulin_doses": ("timestamp", "timestamp"),
        "workouts": ("started_at", "started_at"),
        "meals": ("timestamp", "timestamp"),
    }
    coverage = {}
    for table, (col, _) in tables.items():
        row = conn.execute(
            f"SELECT COUNT(*) as cnt, MIN({col}) as mn, MAX({col}) as mx FROM {table}"
        ).fetchone()
        coverage[table] = {
            "count": row["cnt"],
            "earliest": row["mn"],
            "latest": row["mx"],
        }
    return coverage


# ---------- output generation ----------

def fmt_pct(val):
    return f"{val}%" if val is not None else "N/A"


def fmt_num(val, unit=""):
    if val is None:
        return "N/A"
    return f"{val}{unit}"


def generate_markdown(stats):
    s = stats
    now_str = s["generated_at"]
    cov = s["coverage"]

    lines = [
        "# TypeOneZen Health Summary",
        "",
        f"**Generated:** {now_str}",
        "",
        "## Data Coverage",
        "",
        "| Table | Rows | Earliest | Latest |",
        "|-------|------|----------|--------|",
    ]
    for table in ("glucose_readings", "insulin_doses", "workouts", "meals"):
        c = cov[table]
        lines.append(f"| {table} | {c['count']:,} | {c['earliest'] or 'N/A'} | {c['latest'] or 'N/A'} |")

    # Period stats helper
    def period_section(title, period_key):
        p = s.get(period_key, {})
        bg = p.get("bg", {})
        ins = p.get("insulin", {})
        wo = p.get("workouts", {})
        ml = p.get("meals", {})
        section = [
            "",
            f"## {title}",
            "",
            f"- **Avg BG:** {fmt_num(bg.get('avg_bg'), ' mg/dL')}",
            f"- **Time in Range (70–180):** {fmt_pct(bg.get('tir'))}",
            f"- **Time Below 70:** {fmt_pct(bg.get('below_70'))}",
            f"- **Time Above 180:** {fmt_pct(bg.get('above_180'))}",
            f"- **Readings count:** {bg.get('count', 0):,}",
            f"- **Total Insulin:** {fmt_num(ins.get('total_units'), 'u')}  "
            f"(bolus: {fmt_num(ins.get('bolus_units'), 'u')}, "
            f"basal: {fmt_num(ins.get('basal_units'), 'u')}, "
            f"correction: {fmt_num(ins.get('correction_units'), 'u')})",
            f"- **Workouts:** {wo.get('count', 0)}",
        ]
        if wo.get("by_type"):
            types_str = ", ".join(f"{k}: {v}" for k, v in wo["by_type"].items())
            section.append(f"  - Types: {types_str}")
        # Meals subsection
        meal_count = ml.get("count", 0)
        if meal_count > 0:
            section.append(f"- **Meals logged:** {meal_count}")
            section.append(
                f"  - Avg macros per meal: "
                f"carbs {fmt_num(ml.get('avg_carbs'), 'g')}, "
                f"protein {fmt_num(ml.get('avg_protein'), 'g')}, "
                f"fat {fmt_num(ml.get('avg_fat'), 'g')}, "
                f"fiber {fmt_num(ml.get('avg_fiber'), 'g')}"
            )
            if ml.get("avg_calories") is not None:
                section.append(f"  - Avg calories per meal: {ml['avg_calories']}")
            top = ml.get("top_descriptions", [])
            if top:
                section.append("  - Most common meals: " + ", ".join(
                    f"{t['description']} ({t['count']}x)" for t in top
                ))
        else:
            section.append("- **Meals:** No meals logged yet")
        return section

    lines += period_section("Last 7 Days", "last_7_days")
    lines += period_section("Last 90 Days", "last_90_days")

    # Time of day
    tod = s.get("time_of_day", {})
    lines += [
        "",
        "## Time-of-Day BG Pattern (local NY time)",
        "",
        "| Window | Avg BG (mg/dL) |",
        "|--------|----------------|",
    ]
    for label, _, _ in TIME_WINDOWS:
        lines.append(f"| {label} | {fmt_num(tod.get(label))} |")

    # Workout correlation
    wc = s.get("workout_correlation", {})
    lines += [
        "",
        "## Workout BG Correlation",
        "",
        f"Across **{wc.get('workout_count', 0)} workouts**:",
        "",
        f"- **Avg BG 2h before:** {fmt_num(wc.get('pre_avg'), ' mg/dL')}",
        f"- **Avg BG during:** {fmt_num(wc.get('during_avg'), ' mg/dL')}",
        f"- **Avg BG 3h after:** {fmt_num(wc.get('post_avg'), ' mg/dL')}",
    ]

    # Food-BG correlation
    fbc = s.get("food_bg_correlation", {})
    lines += [
        "",
        "## Food-BG Correlation",
        "",
    ]
    if fbc.get("meal_count", 0) > 0:
        lines.append(f"Across **{fbc['meal_count']} meals** with BG data:")
        lines.append("")
        lines.append(f"- **Avg BG rise post-meal (2h):** {fmt_num(fbc.get('avg_bg_rise'), ' mg/dL')}")
        if fbc.get("high_fiber_count", 0) > 0 or fbc.get("low_fiber_count", 0) > 0:
            lines.append(
                f"- **High-fiber meals (>5g):** avg rise {fmt_num(fbc.get('high_fiber_avg_rise'), ' mg/dL')} "
                f"({fbc['high_fiber_count']} meals)"
            )
            lines.append(
                f"- **Low-fiber meals (≤5g):** avg rise {fmt_num(fbc.get('low_fiber_avg_rise'), ' mg/dL')} "
                f"({fbc['low_fiber_count']} meals)"
            )
    else:
        lines.append("No meals with matching BG data yet.")

    # Recent trend
    trend = s.get("recent_trend", {})
    lines += [
        "",
        "## Recent Trend",
        "",
        f"- **7-day avg BG:** {fmt_num(trend.get('seven_day_avg'), ' mg/dL')}",
        f"- **30-day avg BG:** {fmt_num(trend.get('thirty_day_avg'), ' mg/dL')}",
        f"- **Direction:** {trend.get('direction', 'N/A')}",
    ]

    # Insights
    ins_list = s.get("insights", [])
    if ins_list:
        lines += ["", "## Top Insights", ""]
        for i, insight in enumerate(ins_list, 1):
            lines.append(f"{i}. {insight}")

    lines.append("")
    return "\n".join(lines)


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="Generate TypeOneZen health summary")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout output")
    args = parser.parse_args()

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)

    conn = connect()
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    # Cutoff dates
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)
    ninety_days_ago = now - timedelta(days=90)

    # Load all data (for correlations and time-of-day)
    all_glucose = load_glucose(conn)
    all_workouts = load_workouts(conn)
    all_meals = load_meals(conn)

    # Period-specific data
    glucose_7d = load_glucose(conn, since=seven_days_ago)
    glucose_30d = load_glucose(conn, since=thirty_days_ago)
    glucose_90d = load_glucose(conn, since=ninety_days_ago)
    insulin_7d = load_insulin(conn, since=seven_days_ago)
    insulin_90d = load_insulin(conn, since=ninety_days_ago)
    workouts_7d = load_workouts(conn, since=seven_days_ago)
    workouts_90d = load_workouts(conn, since=ninety_days_ago)
    meals_7d = load_meals(conn, since=seven_days_ago)
    meals_90d = load_meals(conn, since=ninety_days_ago)

    # Coverage
    coverage = compute_coverage(conn)
    conn.close()

    # Compute stats
    bg_7d = compute_bg_stats(glucose_7d)
    bg_90d = compute_bg_stats(glucose_90d)
    bg_30d = compute_bg_stats(glucose_30d)
    ins_7d = compute_insulin_stats(insulin_7d)
    ins_90d = compute_insulin_stats(insulin_90d)
    wo_7d = compute_workout_summary(workouts_7d)
    wo_90d = compute_workout_summary(workouts_90d)

    tod = compute_time_of_day(all_glucose)
    workout_corr = compute_workout_bg_correlation(all_workouts, all_glucose)
    meals_7d_stats = compute_meal_stats(meals_7d)
    meals_90d_stats = compute_meal_stats(meals_90d)
    food_bg_corr = compute_food_bg_correlation(all_meals, all_glucose)

    # Recent trend
    seven_avg = bg_7d["avg_bg"]
    thirty_avg = bg_30d["avg_bg"]
    if seven_avg is not None and thirty_avg is not None:
        diff = round(seven_avg - thirty_avg, 1)
        if diff < -3:
            direction = "improving (lower)"
        elif diff > 3:
            direction = "trending higher"
        else:
            direction = "stable"
    else:
        diff = None
        direction = "insufficient data"

    recent_trend = {
        "seven_day_avg": seven_avg,
        "thirty_day_avg": thirty_avg,
        "diff": diff,
        "direction": direction,
    }

    stats = {
        "generated_at": now_str,
        "coverage": coverage,
        "last_7_days": {"bg": bg_7d, "insulin": ins_7d, "workouts": wo_7d, "meals": meals_7d_stats},
        "last_90_days": {"bg": bg_90d, "insulin": ins_90d, "workouts": wo_90d, "meals": meals_90d_stats},
        "time_of_day": tod,
        "workout_correlation": workout_corr,
        "food_bg_correlation": food_bg_corr,
        "recent_trend": recent_trend,
    }

    # Generate insights
    stats["insights"] = generate_insights(stats)

    # Write markdown
    md_path = SUMMARY_DIR / "health_context.md"
    md_path.write_text(generate_markdown(stats), encoding="utf-8")

    # Write JSON
    json_path = SUMMARY_DIR / "stats_cache.json"
    json_path.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")

    if not args.quiet:
        print(f"Health summary generated at {now_str}")
        print(f"  Markdown: {md_path}")
        print(f"  JSON:     {json_path}")
        print()
        print(f"  7-day avg BG:     {fmt_num(seven_avg, ' mg/dL')}")
        print(f"  7-day TIR:        {fmt_pct(bg_7d['tir'])}")
        print(f"  7-day below 70:   {fmt_pct(bg_7d['below_70'])}")
        print(f"  7-day above 180:  {fmt_pct(bg_7d['above_180'])}")
        print(f"  7-day insulin:    {fmt_num(ins_7d['total_units'], 'u')}")
        print(f"  7-day workouts:   {wo_7d['count']}")
        print(f"  Trend:            {direction}")
        for i, insight in enumerate(stats["insights"], 1):
            print(f"  Insight {i}: {insight}")


if __name__ == "__main__":
    main()
