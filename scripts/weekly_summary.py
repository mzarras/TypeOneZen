#!/usr/bin/env python3
"""
TypeOneZen weekly health summary.
Sends a Sunday-evening iMessage comparing the trailing 7 days to the 7 days
before it: headline numbers, overnight, insulin (when coverage allows), best
and worst day, and a couple of data-backed "where to improve" observations.

Usage:
    python3 weekly_summary.py
    python3 weekly_summary.py --dry-run
    python3 weekly_summary.py --week-ending 2026-07-06 --dry-run
"""

import argparse
import logging
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_DIR = Path.home() / "TypeOneZen"
load_dotenv(dotenv_path=str(PROJECT_DIR / ".env"))

DB_PATH = PROJECT_DIR / "data" / "TypeOneZen.db"
LOG_DIR = PROJECT_DIR / "logs"
IMSG = "/opt/homebrew/bin/imsg"
PHONE = os.getenv("ALERT_PHONE", "")
USER_NAME = os.getenv("USER_NAME", "there")
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

TIR_LOW = 70
TIR_HIGH = 180
TIR_VERY_HIGH = 250
CV_FLAG_PCT = 36
EXPECTED_READINGS_PER_WEEK = 7 * 24 * 60 / 5  # 5-min CGM cadence
COVERAGE_MIN_PCT = 50  # below this, qualify/skip week-over-week comparisons

# -- Logging --
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("weekly_summary")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        str(LOG_DIR / "weekly_summary.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)


# ── Time helpers ─────────────────────────────────────────────────────────────

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


def ny_midnight(d):
    """NY-local midnight datetime for a date object."""
    return datetime(d.year, d.month, d.day, tzinfo=NY)


def fmt_hour(h):
    h = h % 24
    period = "am" if h < 12 else "pm"
    h12 = h % 12
    h12 = 12 if h12 == 0 else h12
    return f"{h12}{period}"


def fmt_date_short(d):
    return f"{d.strftime('%b')} {d.day}"


def fmt_weekday(d):
    return d.strftime("%a")


def get_db():
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ── Data fetch ───────────────────────────────────────────────────────────────

def fetch_glucose(conn, start_ny, end_ny):
    """Readings in [start_ny, end_ny) as ascending (ts_ny, glucose) tuples."""
    rows = conn.execute(
        "SELECT timestamp, glucose_mg_dl FROM glucose_readings "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (to_utc_str(start_ny), to_utc_str(end_ny)),
    ).fetchall()
    out = []
    for r in rows:
        ts = from_utc_str(r["timestamp"])
        if ts is None:
            continue
        out.append((ts, r["glucose_mg_dl"]))
    return out


def fetch_insulin(conn, start_ny, end_ny):
    rows = conn.execute(
        "SELECT timestamp, units, type FROM insulin_doses "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (to_utc_str(start_ny), to_utc_str(end_ny)),
    ).fetchall()
    return rows


# ── BG math ──────────────────────────────────────────────────────────────────

def bg_summary(vals):
    n = len(vals)
    if n == 0:
        return None
    avg = sum(vals) / n
    tir = sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / n * 100
    low_pct = sum(1 for v in vals if v < TIR_LOW) / n * 100
    high_pct = sum(1 for v in vals if v > TIR_HIGH) / n * 100
    vhigh_pct = sum(1 for v in vals if v > TIR_VERY_HIGH) / n * 100
    gmi = 3.31 + 0.02392 * avg
    cv = (statistics.pstdev(vals) / avg * 100) if avg else 0.0
    return {
        "n": n,
        "avg": round(avg, 1),
        "tir": round(tir, 1),
        "low_pct": round(low_pct, 1),
        "high_pct": round(high_pct, 1),
        "vhigh_pct": round(vhigh_pct, 1),
        "gmi": round(gmi, 2),
        "cv": round(cv, 1),
    }


def count_low_episodes(readings_sorted, dedup_minutes=30):
    """Consecutive-reading-<70 runs, merged if the gap between runs is
    <= dedup_minutes."""
    episodes = []
    start = None
    end = None
    for ts, bg in readings_sorted:
        if bg < TIR_LOW:
            if start is None:
                start = ts
            end = ts
        else:
            if start is not None:
                episodes.append((start, end))
                start = None
    if start is not None:
        episodes.append((start, end))

    merged = []
    for ep in episodes:
        if merged and (ep[0] - merged[-1][1]).total_seconds() <= dedup_minutes * 60:
            merged[-1] = (merged[-1][0], ep[1])
        else:
            merged.append(ep)
    return len(merged)


def hour_window_stats(readings, window_hours):
    """Sliding hour windows (start=0..23, wrapping) -> bg_summary of the
    values whose NY hour falls in [start, start+window_hours)."""
    buckets = defaultdict(list)
    for ts, bg in readings:
        h = ts.hour
        for i in range(window_hours):
            start = (h - i) % 24
            buckets[start].append(bg)
    return {start: bg_summary(vals) for start, vals in buckets.items() if vals}


def coverage_pct(n):
    return round(n / EXPECTED_READINGS_PER_WEEK * 100, 1)


# ── Insulin ──────────────────────────────────────────────────────────────────

def insulin_stats(conn, start_ny, end_ny):
    rows = fetch_insulin(conn, start_ny, end_ny)
    if not rows:
        return {"total": 0.0, "bolus": 0.0, "basal": 0.0, "count": 0, "earliest_ny": None}
    bolus = sum(r["units"] for r in rows if r["type"] in ("bolus", "correction"))
    basal = sum(r["units"] for r in rows if r["type"] == "basal")
    earliest = from_utc_str(rows[0]["timestamp"])
    return {
        "total": round(bolus + basal, 1),
        "bolus": round(bolus, 1),
        "basal": round(basal, 1),
        "count": len(rows),
        "earliest_ny": earliest,
    }


def insulin_coverage_complete(stats, window_start_ny, tolerance_hours=6):
    """True if the earliest dose in the window is at (or close to) the
    window start, i.e. dosing data covers the whole week rather than
    starting partway through it."""
    if stats["count"] == 0 or stats["earliest_ny"] is None:
        return False
    return (stats["earliest_ny"] - window_start_ny) <= timedelta(hours=tolerance_hours)


# ── "Where to improve" candidates ───────────────────────────────────────────

def worst_block_candidate(readings_this, week_stat, window_hours=3, min_n=100):
    windows = hour_window_stats(readings_this, window_hours)
    candidates = [(start, s) for start, s in windows.items() if s["n"] >= min_n]
    if not candidates:
        return None
    start, block = min(candidates, key=lambda kv: kv[1]["tir"])
    excess = (100 - block["tir"]) - (100 - week_stat["tir"])
    if excess < 15:
        return None
    label = f"{fmt_hour(start)}-{fmt_hour((start + window_hours) % 24)}"
    dominant_high = block["high_pct"] >= block["low_pct"]
    impact_minutes = max(0.0, excess / 100 * block["n"] * 5)
    if dominant_high:
        text = f"⏰ {label} ran {block['high_pct']:.0f}% above range this week — your toughest window."
    else:
        text = f"⏰ {label} ran {block['low_pct']:.0f}% below range this week — your toughest window."
    return {"type": "worst_block", "text": text, "impact_minutes": impact_minutes}


def recurring_low_candidate(readings_by_day, window_hours=2, min_days=3):
    day_windows = defaultdict(set)  # start -> set of dates with a low there
    low_reading_count = defaultdict(int)
    for d, day_readings in readings_by_day.items():
        starts_today = set()
        for ts, bg in day_readings:
            if bg < TIR_LOW:
                h = ts.hour
                for i in range(window_hours):
                    start = (h - i) % 24
                    starts_today.add(start)
                    low_reading_count[start] += 1
        for start in starts_today:
            day_windows[start].add(d)

    if not day_windows:
        return None
    start, days = max(day_windows.items(), key=lambda kv: len(kv[1]))
    if len(days) < min_days:
        return None
    label = f"{fmt_hour(start)}-{fmt_hour((start + window_hours) % 24)}"
    impact_minutes = low_reading_count[start] * 5
    text = f"📉 Lows keep showing up {label} — {len(days)} of your 7 days this week. Worth a look at dosing/timing around there."
    return {"type": "recurring_low", "text": text, "impact_minutes": impact_minutes}


def weekday_weekend_candidate(readings_this, min_gap=10, min_n=100):
    weekday_vals = [bg for ts, bg in readings_this if ts.weekday() < 5]
    weekend_vals = [bg for ts, bg in readings_this if ts.weekday() >= 5]
    wd = bg_summary(weekday_vals)
    we = bg_summary(weekend_vals)
    if not wd or not we or wd["n"] < min_n or we["n"] < min_n:
        return None
    gap = wd["tir"] - we["tir"]
    if abs(gap) < min_gap:
        return None
    worse = we if gap > 0 else wd
    better_label, worse_label = ("weekdays", "weekends") if gap > 0 else ("weekends", "weekdays")
    impact_minutes = abs(gap) / 100 * worse["n"] * 5
    text = (f"📅 {worse_label.capitalize()} run {abs(gap):.0f}pts lower TIR than {better_label} "
            f"({we['tir']:.0f}% vs {wd['tir']:.0f}%) — the harder stretch of your week.")
    return {"type": "weekday_weekend", "text": text, "impact_minutes": impact_minutes}


def overnight_daytime_candidate(readings_this, min_gap=15, min_n=100):
    overnight_vals = [bg for ts, bg in readings_this if ts.hour >= 23 or ts.hour < 7]
    daytime_vals = [bg for ts, bg in readings_this if 7 <= ts.hour < 23]
    on = bg_summary(overnight_vals)
    dt = bg_summary(daytime_vals)
    if not on or not dt or on["n"] < min_n or dt["n"] < min_n:
        return None
    gap = dt["tir"] - on["tir"]
    if abs(gap) < min_gap:
        return None
    worse = on if gap > 0 else dt
    impact_minutes = abs(gap) / 100 * worse["n"] * 5
    if gap > 0:
        text = f"🌙 Overnight TIR ({on['tir']:.0f}%) trails daytime ({dt['tir']:.0f}%) by {gap:.0f}pts."
    else:
        text = f"🌙 Daytime TIR ({dt['tir']:.0f}%) trails overnight ({on['tir']:.0f}%) by {abs(gap):.0f}pts."
    return {"type": "overnight_daytime", "text": text, "impact_minutes": impact_minutes}


def recent_weeks_tir(conn, week_ending_date, weeks=5):
    results = []
    for i in range(weeks):
        we = week_ending_date - timedelta(days=7 * i)
        start = we - timedelta(days=6)
        vals = [bg for _, bg in fetch_glucose(conn, ny_midnight(start), ny_midnight(we + timedelta(days=1)))]
        results.append(round(sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / len(vals) * 100, 1) if vals else None)
    return results


# ── Core stats builder ───────────────────────────────────────────────────────

def build_weekly_stats(conn, end_date_ny):
    """end_date_ny: date object, the last (inclusive) NY day of the trailing
    week. Returns a stats dict, or None if there's no CGM data this week."""
    if isinstance(end_date_ny, datetime):
        end_date_ny = end_date_ny.date()

    this_start = end_date_ny - timedelta(days=6)
    this_end_excl = end_date_ny + timedelta(days=1)
    last_start = this_start - timedelta(days=7)
    last_end_excl = this_start

    readings_this = fetch_glucose(conn, ny_midnight(this_start), ny_midnight(this_end_excl))
    if not readings_this:
        return None
    readings_last = fetch_glucose(conn, ny_midnight(last_start), ny_midnight(last_end_excl))

    vals_this = [bg for _, bg in readings_this]
    vals_last = [bg for _, bg in readings_last]

    this_stat = bg_summary(vals_this)
    last_stat = bg_summary(vals_last) if vals_last else None

    this_cov = coverage_pct(this_stat["n"])
    last_cov = coverage_pct(last_stat["n"]) if last_stat else 0.0
    this_ok = this_cov >= COVERAGE_MIN_PCT
    last_ok = last_stat is not None and last_cov >= COVERAGE_MIN_PCT
    comparison_ok = this_ok and last_ok

    this_low_episodes = count_low_episodes(readings_this)
    last_low_episodes = count_low_episodes(readings_last) if readings_last else None

    # Overnight (11pm-7am NY)
    overnight_this = bg_summary([bg for ts, bg in readings_this if ts.hour >= 23 or ts.hour < 7])
    overnight_last = bg_summary([bg for ts, bg in readings_last if ts.hour >= 23 or ts.hour < 7]) if readings_last else None

    # Insulin
    insulin_this = insulin_stats(conn, ny_midnight(this_start), ny_midnight(this_end_excl))
    insulin_last = insulin_stats(conn, ny_midnight(last_start), ny_midnight(last_end_excl))
    insulin_complete_this = insulin_coverage_complete(insulin_this, ny_midnight(this_start))
    insulin_complete_last = insulin_coverage_complete(insulin_last, ny_midnight(last_start))

    # Per-day breakdown for best/worst day
    readings_by_day = defaultdict(list)
    for ts, bg in readings_this:
        readings_by_day[ts.date()].append((ts, bg))

    day_stats = {}
    for d, day_readings in readings_by_day.items():
        s = bg_summary([bg for _, bg in day_readings])
        if s and s["n"] >= 100:
            day_stats[d] = s

    best_day = None
    worst_day = None
    if day_stats:
        best_d = max(day_stats, key=lambda d: day_stats[d]["tir"])
        worst_d = min(day_stats, key=lambda d: day_stats[d]["tir"])
        best_day = {"date": best_d, "tir": day_stats[best_d]["tir"]}

        worst_readings = readings_by_day[worst_d]
        worst_s = day_stats[worst_d]
        dominant = "high" if worst_s["high_pct"] >= worst_s["low_pct"] else "low"
        cond = (lambda bg: bg > TIR_HIGH) if dominant == "high" else (lambda bg: bg < TIR_LOW)
        windows = hour_window_stats(worst_readings, 3)
        window_label = None
        if windows:
            # pick the 3h window with the most dominant-condition readings
            best_start = None
            best_count = -1
            for start in windows:
                lo_hours = {(start + i) % 24 for i in range(3)}
                cnt = sum(1 for ts, bg in worst_readings if ts.hour in lo_hours and cond(bg))
                if cnt > best_count:
                    best_count = cnt
                    best_start = start
            if best_start is not None:
                window_label = f"{fmt_hour(best_start)}-{fmt_hour((best_start + 3) % 24)}"
        worst_day = {
            "date": worst_d,
            "tir": worst_s["tir"],
            "dominant": dominant,
            "hours_high": round(worst_s["high_pct"] / 100 * 24, 1),
            "hours_low": round(worst_s["low_pct"] / 100 * 24, 1),
            "window": window_label,
        }

    # Where-to-improve candidates
    candidates = []
    c = worst_block_candidate(readings_this, this_stat)
    if c:
        candidates.append(c)
    c = recurring_low_candidate(readings_by_day)
    if c:
        candidates.append(c)
    c = weekday_weekend_candidate(readings_this)
    if c:
        candidates.append(c)
    c = overnight_daytime_candidate(readings_this)
    if c:
        candidates.append(c)
    candidates.sort(key=lambda c: c["impact_minutes"], reverse=True)
    improvements = candidates[:3]

    great_week = None
    if not improvements:
        recent = recent_weeks_tir(conn, end_date_ny, weeks=5)
        populated = [t for t in recent if t is not None]
        n_weeks = len(populated)
        if recent[0] is not None and populated and recent[0] >= max(populated):
            great_week = f"Best week in your last {n_weeks} — keep doing what you're doing."
        else:
            great_week = "No major patterns to flag this week — solid, steady week."

    return {
        "week_ending": end_date_ny,
        "this_start": this_start,
        "this_end": end_date_ny,
        "last_start": last_start,
        "last_end": last_start + timedelta(days=6),
        "this": this_stat,
        "last": last_stat,
        "this_coverage_pct": this_cov,
        "last_coverage_pct": last_cov,
        "comparison_ok": comparison_ok,
        "this_low_episodes": this_low_episodes,
        "last_low_episodes": last_low_episodes,
        "overnight_this": overnight_this,
        "overnight_last": overnight_last,
        "insulin_this": insulin_this,
        "insulin_last": insulin_last,
        "insulin_complete_this": insulin_complete_this,
        "insulin_complete_last": insulin_complete_last,
        "best_day": best_day,
        "worst_day": worst_day,
        "improvements": improvements,
        "great_week": great_week,
    }


# ── Message builder ──────────────────────────────────────────────────────────

def delta_str(this_val, last_val, unit="", higher_is_better=True, decimals=0):
    if last_val is None:
        return ""
    diff = this_val - last_val
    if round(diff, decimals) == 0:
        return " (flat vs last week)"
    arrow = "↑" if diff > 0 else "↓"
    good = (diff > 0) == higher_is_better
    fmt = f"{abs(diff):.{decimals}f}" if decimals else f"{abs(round(diff))}"
    tag = "🟢" if good else ""
    return f" ({arrow}{fmt}{unit} vs last week{(' ' + tag) if tag else ''})"


def build_message(stats):
    if not stats:
        return None

    s = stats
    lines = []

    header = f"📊 Weekly report — {fmt_date_short(s['this_start'])}–{fmt_date_short(s['this_end'])}"
    lines.append(header)
    lines.append("")

    this = s["this"]
    last = s["last"]
    cmp_ok = s["comparison_ok"]

    if not cmp_ok:
        if s["this_coverage_pct"] < COVERAGE_MIN_PCT:
            lines.append(f"⚠️ Partial CGM data this week ({s['this_coverage_pct']:.0f}% of expected readings) — comparisons vs last week skipped.")
        elif last is None or s["last_coverage_pct"] < COVERAGE_MIN_PCT:
            lines.append("⚠️ Last week's CGM data is incomplete — comparisons skipped.")
        lines.append("")

    tir_delta = delta_str(this["tir"], last["tir"], "pts", True) if cmp_ok else ""
    lines.append(f"TIR (70-180): {this['tir']:.0f}%{tir_delta}")

    avg_delta = delta_str(this["avg"], last["avg"], "", False) if cmp_ok else ""
    gmi_delta = delta_str(this["gmi"], last["gmi"], "%", False, 1) if cmp_ok else ""
    lines.append(f"Avg BG: {this['avg']:.0f} mg/dL{avg_delta} · GMI (est. A1c): {this['gmi']:.1f}%{gmi_delta}")

    cv_flag = " ⚠️ high variability" if this["cv"] > CV_FLAG_PCT else ""
    cv_delta = delta_str(this["cv"], last["cv"], "pts", False, 1) if cmp_ok else ""
    lines.append(f"CV: {this['cv']:.0f}%{cv_delta}{cv_flag}")

    low_delta = delta_str(this["low_pct"], last["low_pct"], "pts", False) if cmp_ok else ""
    ep_delta = ""
    if cmp_ok and s["last_low_episodes"] is not None:
        diff = s["this_low_episodes"] - s["last_low_episodes"]
        if diff != 0:
            ep_delta = f" ({'+' if diff > 0 else ''}{diff} vs last week)"
    lines.append(f"Time <70: {this['low_pct']:.0f}%{low_delta} — {s['this_low_episodes']} low episode(s){ep_delta}")

    high_delta = delta_str(this["high_pct"], last["high_pct"], "pts", False) if cmp_ok else ""
    vhigh_delta = delta_str(this["vhigh_pct"], last["vhigh_pct"], "pts", False) if cmp_ok else ""
    lines.append(f"Time >180: {this['high_pct']:.0f}%{high_delta} · >250: {this['vhigh_pct']:.0f}%{vhigh_delta}")
    lines.append("")

    on = s["overnight_this"]
    on_last = s["overnight_last"]
    if on:
        on_delta_avg = delta_str(on["avg"], on_last["avg"], "", False) if (cmp_ok and on_last) else ""
        on_delta_tir = delta_str(on["tir"], on_last["tir"], "pts", True) if (cmp_ok and on_last) else ""
        lines.append(f"Overnight (11pm-7am): avg {on['avg']:.0f}{on_delta_avg}, TIR {on['tir']:.0f}%{on_delta_tir}")
        lines.append("")

    # Insulin
    ins_this = s["insulin_this"]
    if ins_this and ins_this["count"] > 0:
        tdd_day = round(ins_this["total"] / 7, 1)
        bolus_day = round(ins_this["bolus"] / 7, 1)
        basal_day = round(ins_this["basal"] / 7, 1)
        if s["insulin_complete_this"] and s["insulin_complete_last"] and cmp_ok and s["insulin_last"]["count"] > 0:
            ins_last = s["insulin_last"]
            last_tdd_day = round(ins_last["total"] / 7, 1)
            tdd_delta = delta_str(tdd_day, last_tdd_day, "u", False, 1)
            lines.append(f"Insulin: {tdd_day}u/day{tdd_delta} ({bolus_day}u bolus + {basal_day}u basal)")
        else:
            lines.append(f"Insulin: {tdd_day}u/day ({bolus_day}u bolus + {basal_day}u basal) — partial data since Jul 8")
        lines.append("")

    best_day = s["best_day"]
    worst_day = s["worst_day"]
    if best_day and worst_day:
        line = f"Best day: {fmt_weekday(best_day['date'])} ({best_day['tir']:.0f}% TIR) · Worst day: {fmt_weekday(worst_day['date'])} ({worst_day['tir']:.0f}% TIR"
        if worst_day["window"]:
            problem = "mostly high" if worst_day["dominant"] == "high" else "mostly low"
            line += f", {problem} {worst_day['window']}"
        line += ")"
        lines.append(line)
        lines.append("")

    lines.append("Where to improve:")
    if s["improvements"]:
        for imp in s["improvements"]:
            lines.append(imp["text"])
    else:
        lines.append(f"🌟 {s['great_week']}")

    message = "\n".join(lines).strip()
    if len(message) > 1600:
        message = message[:1597].rstrip() + "..."
    return message


# ── Sending ──────────────────────────────────────────────────────────────────

def send_imessage(message):
    if not PHONE:
        print("Error: ALERT_PHONE must be set in .env")
        logger.error("ALERT_PHONE not set; cannot send weekly summary")
        return False

    attempts = 3
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            [IMSG, "send", "--to", PHONE, "--text", message],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("✅ Sent via iMessage")
            logger.info("Weekly summary sent via iMessage")
            return True
        detail = " | ".join(p for p in (result.stdout.strip(), result.stderr.strip()) if p)
        msg = f"❌ Send failed (attempt {attempt}/{attempts}, exit {result.returncode}): {detail or '(no output)'}"
        print(msg)
        logger.warning(msg)
        if attempt < attempts:
            time.sleep(30)
    logger.error("Weekly summary failed to send after %d attempts", attempts)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print, don't send")
    parser.add_argument("--week-ending", type=str, default=None,
                         help="NY date (YYYY-MM-DD) of the last day of the trailing week; default: yesterday")
    args = parser.parse_args()

    if args.week_ending:
        try:
            week_ending = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: --week-ending must be YYYY-MM-DD, got {args.week_ending!r}")
            sys.exit(1)
    else:
        week_ending = (now_ny() - timedelta(days=1)).date()

    conn = get_db()
    try:
        stats = build_weekly_stats(conn, week_ending)
    finally:
        conn.close()

    if stats is None:
        logger.info("No CGM data for week ending %s — skipping weekly summary", week_ending)
        print(f"No CGM data for week ending {week_ending} — skipping.")
        return

    message = build_message(stats)

    print("=" * 60)
    print("GENERATED WEEKLY SUMMARY:")
    print("=" * 60)
    print(message)
    print("=" * 60)
    print(f"[{len(message)} chars]")

    if args.dry_run:
        print("[DRY RUN — not sent]")
        logger.info("Dry run for week ending %s — not sent", week_ending)
        return

    send_imessage(message)


if __name__ == "__main__":
    main()
