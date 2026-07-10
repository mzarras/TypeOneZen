"""Tests for scripts/weekly_summary.py.

Uses tests/conftest.py's `conn` fixture (temp SQLite db, tables created via
db.init_db() + monitor.ensure_tables()). Two synthetic weeks are seeded with
known properties so TIR/avg/GMI/CV, low-episode dedup, and the "where to
improve" observations can be checked against independently computed values
rather than the module's own math.
"""

import statistics as st
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import weekly_summary as ws  # noqa: E402

NY = ZoneInfo("America/New_York")

# "This week" = Mon Jul 6 - Sun Jul 12, 2026 (NY). "Last week" = the 7 days
# before it. Both are firmly inside EDT, no DST edge cases.
WEEK_ENDING = date(2026, 7, 12)
THIS_START = date(2026, 7, 6)
LAST_START = date(2026, 6, 29)


def ny_dt(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=NY)


def five_min_series(start_date, days=7):
    """All 5-minute NY timestamps across `days` days starting at start_date
    00:00 (exclusive end) -> 2016 slots for a full week."""
    out = []
    cur = ny_dt(start_date, 0, 0)
    end = cur + timedelta(days=days)
    while cur < end:
        out.append(cur)
        cur += timedelta(minutes=5)
    return out


def build_this_week_values():
    """Baseline 120 (in range) everywhere, plus:
    - a daily 2pm-5pm block at 220 (planted high pattern, all 7 days)
    - two low bursts on Monday 10 minutes apart (03:00-03:10, 03:20-03:25)
      that should dedup into ONE low episode
    - one isolated low burst on Tuesday (04:00-04:05) -> a second episode
    """
    values = {}
    for ts in five_min_series(THIS_START):
        values[ts] = 220 if 14 <= ts.hour < 17 else 120

    monday = THIS_START
    tuesday = THIS_START + timedelta(days=1)
    for h, m, v in [(3, 0, 65), (3, 5, 60), (3, 10, 58), (3, 20, 68), (3, 25, 64)]:
        values[ny_dt(monday, h, m)] = v
    for h, m, v in [(4, 0, 66), (4, 5, 62)]:
        values[ny_dt(tuesday, h, m)] = v

    return values


def build_last_week_values():
    """Constant 130 all week -> avg=130, TIR=100%, CV=0, a clean baseline."""
    return {ts: 130 for ts in five_min_series(LAST_START)}


def seed_glucose(conn, values):
    rows = [(ws.to_utc_str(ts), bg) for ts, bg in values.items()]
    conn.executemany(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl) VALUES (?, ?)",
        rows,
    )
    conn.commit()


def seed_insulin_partial(conn):
    """Doses only from Jul 9 onward, well after this week's Jul 6 start ->
    partial coverage for 'this week'; none at all in 'last week'."""
    rows = [
        (ws.to_utc_str(ny_dt(date(2026, 7, 9), 8, 0)), 3.0, "bolus"),
        (ws.to_utc_str(ny_dt(date(2026, 7, 9), 18, 0)), 3.0, "bolus"),
        (ws.to_utc_str(ny_dt(date(2026, 7, 10), 8, 0)), 3.0, "bolus"),
        (ws.to_utc_str(ny_dt(date(2026, 7, 9), 0, 0)), 10.0, "basal"),
        (ws.to_utc_str(ny_dt(date(2026, 7, 10), 0, 0)), 10.0, "basal"),
    ]
    conn.executemany(
        "INSERT INTO insulin_doses (timestamp, units, type) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


@pytest.fixture
def seeded(conn):
    seed_glucose(conn, build_this_week_values())
    seed_glucose(conn, build_last_week_values())
    seed_insulin_partial(conn)
    return conn


def _tir_line(msg):
    for line in msg.splitlines():
        if line.startswith("TIR (70-180):"):
            return line
    return ""


# ── Core BG math ─────────────────────────────────────────────────────────────

def test_tir_avg_gmi_cv_match_independent_calc(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    assert stats is not None

    vals = list(build_this_week_values().values())
    n = len(vals)
    expected_avg = sum(vals) / n
    expected_tir = sum(1 for v in vals if 70 <= v <= 180) / n * 100
    expected_gmi = 3.31 + 0.02392 * expected_avg
    expected_cv = st.pstdev(vals) / expected_avg * 100

    this = stats["this"]
    assert this["n"] == n
    assert this["avg"] == pytest.approx(expected_avg, abs=0.05)
    assert this["tir"] == pytest.approx(expected_tir, abs=0.05)
    assert this["gmi"] == pytest.approx(expected_gmi, abs=0.01)
    assert this["cv"] == pytest.approx(expected_cv, abs=0.05)


def test_last_week_is_flat_baseline(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    last = stats["last"]
    assert last["avg"] == pytest.approx(130, abs=0.01)
    assert last["tir"] == pytest.approx(100.0, abs=0.01)
    assert last["cv"] == pytest.approx(0.0, abs=0.01)


def test_delta_signs(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    assert stats["comparison_ok"] is True
    assert stats["this"]["tir"] < stats["last"]["tir"]  # planted highs/lows hurt TIR

    msg = ws.build_message(stats)
    assert "↓" in _tir_line(msg)  # TIR fell vs last week -> down arrow


def test_low_episode_dedup(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    # Monday's two bursts (10 min apart) merge into one episode; Tuesday's
    # isolated burst is a second -> 2 total, not the raw 3 runs.
    assert stats["this_low_episodes"] == 2


# ── Insulin coverage qualification ──────────────────────────────────────────

def test_insulin_qualified_when_partial(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    assert stats["insulin_complete_this"] is False
    msg = ws.build_message(stats)
    assert "partial data" in msg


def test_insulin_omitted_when_no_data(conn):
    seed_glucose(conn, build_this_week_values())
    seed_glucose(conn, build_last_week_values())
    stats = ws.build_weekly_stats(conn, WEEK_ENDING)
    msg = ws.build_message(stats)
    assert "Insulin" not in msg


# ── Message content ──────────────────────────────────────────────────────────

def test_message_has_headline_and_improvement(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    msg = ws.build_message(stats)
    assert "TIR" in msg
    assert "GMI" in msg
    assert "Where to improve" in msg
    assert stats["improvements"] or stats["great_week"]
    # the planted 2pm-5pm high block should surface as an observation
    assert "2pm-5pm" in msg


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_empty_week_returns_none(conn):
    stats = ws.build_weekly_stats(conn, date(2025, 1, 5))
    assert stats is None
    assert ws.build_message(stats) is None


def test_partial_cgm_coverage_skips_comparison(conn):
    # Only ~1/3 of expected readings this week -> below the 50% coverage
    # floor, so week-over-week comparisons should be skipped.
    values = {ts: 120 for i, ts in enumerate(five_min_series(THIS_START)) if i % 3 == 0}
    seed_glucose(conn, values)
    seed_glucose(conn, build_last_week_values())

    stats = ws.build_weekly_stats(conn, WEEK_ENDING)
    assert stats["this_coverage_pct"] < 50
    assert stats["comparison_ok"] is False

    msg = ws.build_message(stats)
    assert "vs last week" not in _tir_line(msg)
    assert "⚠️ Partial CGM data" in msg


def test_dry_run_never_sends(seeded, monkeypatch):
    row = seeded.execute("PRAGMA database_list").fetchone()
    monkeypatch.setattr(ws, "DB_PATH", row["file"])

    called = {"sent": False}

    def fake_send(message):
        called["sent"] = True
        return True

    monkeypatch.setattr(ws, "send_imessage", fake_send)
    monkeypatch.setattr(sys, "argv", [
        "weekly_summary.py", "--dry-run", "--week-ending", WEEK_ENDING.isoformat(),
    ])

    ws.main()

    assert called["sent"] is False


# ── Fitness section ──────────────────────────────────────────────────────────

def insert_workout(conn, started_ny, minutes, activity="running", distance_m=None):
    import json as _json
    ended_ny = started_ny + timedelta(minutes=minutes)
    notes = _json.dumps({"total_distance": distance_m} if distance_m else {})
    conn.execute(
        "INSERT INTO workouts (started_at, ended_at, activity_type, intensity, notes)"
        " VALUES (?, ?, ?, 'high', ?)",
        (ws.to_utc_str(started_ny), ws.to_utc_str(ended_ny), activity, notes),
    )
    conn.commit()


def test_workout_stats_aggregation(conn):
    insert_workout(conn, ny_dt(THIS_START + timedelta(days=1), 17, 0), 45, "running", 8000)
    insert_workout(conn, ny_dt(THIS_START + timedelta(days=3), 17, 0), 30, "running", 5000)
    insert_workout(conn, ny_dt(THIS_START + timedelta(days=5), 9, 0), 60, "cycling", 24000)

    stats = ws.workout_stats(conn, ws.ny_midnight(THIS_START),
                             ws.ny_midnight(WEEK_ENDING + timedelta(days=1)))
    assert stats["count"] == 3
    assert round(stats["total_min"]) == 135
    assert stats["by_activity"]["running"]["count"] == 2
    assert round(stats["by_activity"]["running"]["km"], 1) == 13.0
    assert stats["by_activity"]["cycling"]["count"] == 1
    assert len(stats["days"]) == 3


def test_message_includes_fitness_line(seeded):
    insert_workout(seeded, ny_dt(THIS_START + timedelta(days=1), 17, 0), 45, "running", 8000)
    insert_workout(seeded, ny_dt(THIS_START + timedelta(days=3), 17, 0), 30, "running", 5000)

    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    msg = ws.build_message(stats)
    assert "🏃 Activity: 2 workouts" in msg
    assert "2 runs (13.0 km)" in msg
    assert "1h 15m" in msg
    assert "(+2 vs last week)" in msg


def test_fitness_line_notes_missing_week(seeded):
    insert_workout(seeded, ny_dt(LAST_START + timedelta(days=1), 17, 0), 45)

    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    msg = ws.build_message(stats)
    assert "no workouts this week (last week: 1)" in msg


def test_fitness_section_absent_when_both_weeks_empty(seeded):
    stats = ws.build_weekly_stats(seeded, WEEK_ENDING)
    msg = ws.build_message(stats)
    assert "🏃" not in msg


def test_workout_day_tir_comparison(conn):
    # Rest days carry the seeded daily 2-5pm high block; workout days are
    # overwritten to flat 120 (100% TIR) -> comparison line should appear
    # and favor workout days.
    values = build_this_week_values()
    workout_days = {THIS_START + timedelta(days=2), THIS_START + timedelta(days=4)}
    for ts in list(values):
        if ts.date() in workout_days:
            values[ts] = 120
    seed_glucose(conn, values)
    seed_glucose(conn, build_last_week_values())
    for d in workout_days:
        insert_workout(conn, ny_dt(d, 17, 0), 45, "running", 8000)

    stats = ws.build_weekly_stats(conn, WEEK_ENDING)
    assert stats["workout_bg"] is not None
    assert stats["workout_bg"]["workout_tir"] > stats["workout_bg"]["rest_tir"]
    msg = ws.build_message(stats)
    assert "Workout days:" in msg and "rest days" in msg
