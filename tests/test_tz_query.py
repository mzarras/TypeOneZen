"""tz_query.py tests — the OpenClaw-skill query CLI's new subcommands.

tz_query.py lives outside the normal package tree (examples/openclaw-skill/
scripts/), and computes its DB_PATH from the TZ_HOME env var at import time.
So each test imports a fresh copy of the module via importlib after pointing
TZ_HOME at a temp dir, and seeds that same SQLite file (via db.get_db(), with
db.DB_DIR/db.DB_PATH monkeypatched the same way tests/conftest.py's `conn`
fixture does) with known rows. Subcommands are invoked as plain functions
with a fake argparse.Namespace; output is captured via capsys and parsed
as JSON, matching how OpenClaw actually consumes this script.
"""

import importlib.util
import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import db
import monitor

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

REPO_ROOT = Path(__file__).resolve().parent.parent
TZ_QUERY_PATH = REPO_ROOT / "examples" / "openclaw-skill" / "scripts" / "tz_query.py"


def ny_to_utc_iso(y, mo, d, h, mi=0):
    """Build a UTC ISO timestamp from an NY-local wall-clock time."""
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC).isoformat()


def load_tz_query():
    """Import a fresh copy of tz_query.py (module-level TZ_HOME/DB_PATH are
    computed at import time, so each test needs its own import after
    TZ_HOME is set)."""
    spec = importlib.util.spec_from_file_location("tz_query_under_test", TZ_QUERY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrozenDatetime(datetime):
    """Subclass of datetime whose now() returns a fixed instant, so
    'today'/'now'-relative subcommands (day, overnight, week, last-bolus,
    last-meal, a1c, carbs, bg-at) are deterministic in tests."""
    _frozen_utc = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._frozen_utc.astimezone(tz)
        return cls._frozen_utc


def freeze(tzq, y, mo, d, h, mi=0, tz=NY):
    """Freeze tzq's notion of 'now' to the given wall-clock time in `tz`."""
    frozen = datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)
    Frozen = type("Frozen", (FrozenDatetime,), {"_frozen_utc": frozen})
    tzq.datetime = Frozen


@pytest.fixture
def tzq(tmp_path, monkeypatch):
    """Fresh tz_query module wired to a fully-initialized temp SQLite db."""
    monkeypatch.setenv("TZ_HOME", str(tmp_path))
    monkeypatch.setattr(db, "DB_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "TypeOneZen.db")

    c = db.get_db()
    monitor.ensure_tables(c)  # alert_log / alert_snoozes / sync_state
    c.close()
    db.init_db()

    module = load_tz_query()
    yield module


def run(tzq, cmd_func, capsys, **kwargs):
    cmd_func(Namespace(**kwargs))
    captured = capsys.readouterr()
    return json.loads(captured.out)


def seed_glucose(tzq, rows):
    """rows: list of (utc_iso, mg_dl) or (utc_iso, mg_dl, trend_arrow)."""
    conn = tzq.get_db()
    for row in rows:
        ts, mg = row[0], row[1]
        arrow = row[2] if len(row) > 2 else "->"
        conn.execute(
            "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend_arrow) VALUES (?, ?, ?)",
            (ts, mg, arrow),
        )
    conn.commit()
    conn.close()


def seed_insulin(tzq, rows):
    """rows: list of (utc_iso, units, type, notes)."""
    conn = tzq.get_db()
    for ts, units, dose_type, notes in rows:
        conn.execute(
            "INSERT INTO insulin_doses (timestamp, units, type, notes) VALUES (?, ?, ?, ?)",
            (ts, units, dose_type, notes),
        )
    conn.commit()
    conn.close()


def seed_meals(tzq, rows):
    """rows: list of (utc_iso, description, carbs_g)."""
    conn = tzq.get_db()
    for ts, desc, carbs in rows:
        conn.execute(
            "INSERT INTO meals (timestamp, description, carbs_g) VALUES (?, ?, ?)",
            (ts, desc, carbs),
        )
    conn.commit()
    conn.close()


def seed_workouts(tzq, rows):
    """rows: list of (started_at_utc_iso, ended_at_utc_iso_or_None, activity_type)."""
    conn = tzq.get_db()
    for started, ended, activity in rows:
        conn.execute(
            "INSERT INTO workouts (started_at, ended_at, activity_type) VALUES (?, ?, ?)",
            (started, ended, activity),
        )
    conn.commit()
    conn.close()


def seed_alerts(tzq, rows):
    """rows: list of (rule_name, triggered_at_utc_iso, message, sent)."""
    conn = tzq.get_db()
    for rule_name, ts, message, sent in rows:
        conn.execute(
            "INSERT INTO alert_log (rule_name, triggered_at, message, sent) VALUES (?, ?, ?, ?)",
            (rule_name, ts, message, sent),
        )
    conn.commit()
    conn.close()


# ── Portability ──────────────────────────────────────────────────────

def test_tz_home_env_drives_db_path(tzq, tmp_path):
    assert tzq.TZ_HOME == tmp_path
    assert tzq.DB_PATH == tmp_path / "data" / "TypeOneZen.db"
    assert tzq.DB_PATH.exists()


# ── day ──────────────────────────────────────────────────────────────

def test_day_full_recap(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 20, 0)  # 8pm NY, well inside the day

    seed_glucose(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 8, 0), 90),
        (ny_to_utc_iso(2026, 7, 8, 12, 0), 200),   # above 180
        (ny_to_utc_iso(2026, 7, 8, 15, 0), 65),    # below 70
        (ny_to_utc_iso(2026, 7, 8, 18, 0), 140),
        # Outside the NY day window (previous day) — must be excluded
        (ny_to_utc_iso(2026, 7, 7, 23, 30), 999),
    ])
    seed_insulin(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 8, 5), 3.0, "bolus", "breakfast"),
        (ny_to_utc_iso(2026, 7, 8, 8, 20), 0.1, "bolus", "SMB"),
        (ny_to_utc_iso(2026, 7, 8, 8, 25), 0.15, "bolus", "SMB"),
        (ny_to_utc_iso(2026, 7, 8, 9, 0), 0.8, "basal", None),
        (ny_to_utc_iso(2026, 7, 8, 12, 30), 1.0, "correction", None),
    ])
    seed_meals(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 8, 0), "oatmeal", 40.0),
        (ny_to_utc_iso(2026, 7, 8, 18, 0), "chicken and rice", 55.0),
    ])
    seed_workouts(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 10, 0), ny_to_utc_iso(2026, 7, 8, 10, 45), "running"),
    ])
    seed_alerts(tzq, [
        ("SUSTAINED_HIGH", ny_to_utc_iso(2026, 7, 8, 13, 0), "BG high for 60min", 1),
    ])

    result = run(tzq, tzq.cmd_day, capsys, date="2026-07-08")

    assert result["date"] == "2026-07-08"
    assert result["bg"]["count"] == 4
    assert result["bg"]["below_70"] == 1
    assert result["bg"]["above_180"] == 1
    assert result["bg"]["min"] == 65
    assert result["bg"]["max"] == 200
    assert result["bg"]["avg"] == pytest.approx((90 + 200 + 65 + 140) / 4, abs=0.05)

    # SMBs aggregated into a single bolus total, never listed individually
    # (3.0 + 0.1 + 0.15 = 3.25, which round(..., 1) banker's-rounds to 3.2)
    assert result["insulin"]["by_type"]["bolus"] == pytest.approx(3.2, abs=0.01)
    assert result["insulin"]["by_type"]["basal"] == pytest.approx(0.8, abs=0.01)
    assert result["insulin"]["by_type"]["correction"] == pytest.approx(1.0, abs=0.01)
    assert result["insulin"]["total_units"] == pytest.approx(5.0, abs=0.01)

    assert result["carbs_total"] == pytest.approx(95.0, abs=0.01)
    assert len(result["meals"]) == 2
    assert result["meals"][0]["description"] == "oatmeal"

    assert len(result["workouts"]) == 1
    assert result["workouts"][0]["activity"] == "running"
    assert result["workouts"][0]["duration_min"] == 45

    assert len(result["alerts_fired"]) == 1
    assert result["alerts_fired"][0]["rule_name"] == "SUSTAINED_HIGH"


def test_day_defaults_to_today_ny_and_degrades_with_no_data(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 12, 0)
    result = run(tzq, tzq.cmd_day, capsys, date=None)
    assert result["date"] == "2026-07-08"
    assert result["bg"]["count"] == 0
    assert result["bg"]["avg"] is None
    assert result["meals"] == []
    assert result["workouts"] == []
    assert result["alerts_fired"] == []


# ── overnight ────────────────────────────────────────────────────────

def test_overnight_window_and_lows(tzq, capsys):
    # 8am NY: the 11pm-7am window that just completed started the previous day
    freeze(tzq, 2026, 7, 9, 8, 0)

    seed_glucose(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 23, 30), 110),
        (ny_to_utc_iso(2026, 7, 9, 2, 0), 65),   # low
        (ny_to_utc_iso(2026, 7, 9, 4, 0), 130),
        (ny_to_utc_iso(2026, 7, 9, 6, 45), 95),
        # Outside window
        (ny_to_utc_iso(2026, 7, 9, 8, 30), 999),
    ])
    seed_alerts(tzq, [
        ("LOW_WARNING", ny_to_utc_iso(2026, 7, 9, 2, 5), "BG low overnight", 1),
    ])

    result = run(tzq, tzq.cmd_overnight, capsys)

    assert result["count"] == 4
    assert result["any_alerts"] is True
    assert len(result["lows"]) == 1
    assert result["lows"][0]["glucose_mg_dl"] == 65
    assert result["min"] == 65
    assert result["max"] == 130


def test_overnight_no_alerts_when_none_logged(tzq, capsys):
    freeze(tzq, 2026, 7, 9, 8, 0)
    seed_glucose(tzq, [(ny_to_utc_iso(2026, 7, 9, 1, 0), 100)])
    result = run(tzq, tzq.cmd_overnight, capsys)
    assert result["any_alerts"] is False


# ── week ─────────────────────────────────────────────────────────────

def test_week_compares_last_and_prior_periods(tzq, capsys):
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    freeze(tzq, now.year, now.month, now.day, now.hour, now.minute, tz=UTC)

    # Last 7 days: higher avg, better TIR
    for days_ago in range(1, 6):
        ts = (now - timedelta(days=days_ago)).isoformat()
        seed_glucose(tzq, [(ts, 150)])
        seed_insulin(tzq, [(ts, 1.0, "bolus", None)])
    seed_workouts(tzq, [((now - timedelta(days=2)).isoformat(), None, "cycling")])

    # Prior 7 days (8-13 days ago): lower BG values
    for days_ago in range(8, 13):
        ts = (now - timedelta(days=days_ago)).isoformat()
        seed_glucose(tzq, [(ts, 100)])
        seed_insulin(tzq, [(ts, 2.0, "bolus", None)])

    result = run(tzq, tzq.cmd_week, capsys)

    assert result["last_7d"]["avg"] == pytest.approx(150.0, abs=0.1)
    assert result["prior_7d"]["avg"] == pytest.approx(100.0, abs=0.1)
    assert result["last_7d"]["workout_count"] == 1
    assert result["prior_7d"]["workout_count"] == 0
    assert result["last_7d"]["total_insulin_units"] == pytest.approx(5.0, abs=0.01)
    assert result["prior_7d"]["total_insulin_units"] == pytest.approx(10.0, abs=0.01)
    assert result["avg_change"] == pytest.approx(50.0, abs=0.1)
    assert result["tir_change"] == pytest.approx(0.0, abs=0.1)


# ── last-bolus ───────────────────────────────────────────────────────

def test_last_bolus_and_today_total(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 12, 0)

    seed_insulin(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 8, 0), 3.0, "bolus", "breakfast"),
        (ny_to_utc_iso(2026, 7, 8, 8, 20), 0.1, "bolus", "SMB"),
        (ny_to_utc_iso(2026, 7, 8, 11, 50), 0.2, "bolus", "SMB"),
        (ny_to_utc_iso(2026, 7, 8, 9, 0), 0.8, "basal", None),
        # Yesterday — must not count toward today's total
        (ny_to_utc_iso(2026, 7, 7, 12, 0), 5.0, "bolus", "dinner"),
    ])

    result = run(tzq, tzq.cmd_last_bolus, capsys)

    assert result["units"] == pytest.approx(0.2, abs=0.001)
    assert result["notes"] == "SMB"
    assert result["today_bolus_total"] == pytest.approx(3.3, abs=0.01)
    assert result["minutes_ago"] == pytest.approx(10.0, abs=0.5)


def test_last_bolus_errors_gracefully_with_no_data(tzq, capsys):
    result = run(tzq, tzq.cmd_last_bolus, capsys)
    assert "error" in result


# ── alerts ───────────────────────────────────────────────────────────

def test_alerts_window_and_order(tzq, capsys):
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    freeze(tzq, now.year, now.month, now.day, now.hour, tz=UTC)

    seed_alerts(tzq, [
        ("SUSTAINED_HIGH", (now - timedelta(hours=1)).isoformat(), "recent", 1),
        ("LOW_WARNING", (now - timedelta(hours=10)).isoformat(), "within 24h", 1),
        ("POD_AGE_WARN", (now - timedelta(hours=30)).isoformat(), "too old", 0),
    ])

    result = run(tzq, tzq.cmd_alerts, capsys, hours=24)
    assert result["count"] == 2
    assert result["alerts"][0]["rule_name"] == "SUSTAINED_HIGH"  # most recent first
    assert result["alerts"][1]["rule_name"] == "LOW_WARNING"
    assert result["alerts"][1]["sent"] is True


# ── bg-at ────────────────────────────────────────────────────────────

def test_bg_at_finds_nearest_reading(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 10, 0)
    seed_glucose(tzq, [
        (ny_to_utc_iso(2026, 7, 8, 3, 0), 88, "Flat"),
        (ny_to_utc_iso(2026, 7, 8, 3, 10), 92, "FortyFiveUp"),
    ])

    result = run(tzq, tzq.cmd_bg_at, capsys, time="3am")
    assert result["glucose_mg_dl"] == 88
    assert result["minutes_off"] == pytest.approx(0.0, abs=0.01)

    result2 = run(tzq, tzq.cmd_bg_at, capsys, time="3:08am")
    assert result2["glucose_mg_dl"] == 92


def test_bg_at_yesterday_and_iso(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 10, 0)
    seed_glucose(tzq, [(ny_to_utc_iso(2026, 7, 7, 3, 0), 77)])

    result = run(tzq, tzq.cmd_bg_at, capsys, time="yesterday 3am")
    assert result["glucose_mg_dl"] == 77

    result2 = run(tzq, tzq.cmd_bg_at, capsys, time="2026-07-07 03:00")
    assert result2["glucose_mg_dl"] == 77


def test_bg_at_errors_when_too_far(tzq, capsys):
    freeze(tzq, 2026, 7, 8, 10, 0)
    seed_glucose(tzq, [(ny_to_utc_iso(2026, 7, 8, 3, 0), 88)])
    result = run(tzq, tzq.cmd_bg_at, capsys, time="5am")
    assert "error" in result


# ── a1c ──────────────────────────────────────────────────────────────

def test_a1c_gmi_estimate(tzq, capsys):
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    freeze(tzq, now.year, now.month, now.day, now.hour, tz=UTC)

    rows = []
    for days_ago in range(3):
        for hour_offset in (2, 8):
            ts = (now - timedelta(days=days_ago, hours=hour_offset)).isoformat()
            rows.append((ts, 150))
    seed_glucose(tzq, rows)

    result = run(tzq, tzq.cmd_a1c, capsys, days=90)

    assert result["mean"] == pytest.approx(150.0, abs=0.1)
    assert result["gmi_estimate"] == pytest.approx(3.31 + 0.02392 * 150.0, abs=0.01)
    assert result["count"] == len(rows)
    assert result["days_requested"] == 90
    assert result["days_with_data"] >= 1


def test_a1c_errors_gracefully_with_no_data(tzq, capsys):
    result = run(tzq, tzq.cmd_a1c, capsys, days=90)
    assert "error" in result


# ── carbs ────────────────────────────────────────────────────────────

def test_carbs_totals_window(tzq, capsys):
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    freeze(tzq, now.year, now.month, now.day, now.hour, tz=UTC)

    seed_meals(tzq, [
        ((now - timedelta(hours=2)).isoformat(), "lunch", 50.0),
        ((now - timedelta(hours=10)).isoformat(), "breakfast", 30.0),
        ((now - timedelta(hours=30)).isoformat(), "yesterday dinner", 60.0),
    ])

    result = run(tzq, tzq.cmd_carbs, capsys, hours=24)
    assert result["count"] == 2
    assert result["total_carbs_g"] == pytest.approx(80.0, abs=0.01)
