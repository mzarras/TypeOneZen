"""scripts/daily_summary.py tests.

Covers the three data-quality fixes:

1. Evening outlook is loop-first (quotes Trio's own eventual_bg/predicted
   min/insulin_req) instead of naive `bg_now - (iob * ISF * 0.4)` math, with
   a clearly-labeled fallback when there's no fresh loop data.
2. The morning "consecutive overnight highs" insight sizes its suggested
   correction off Trio's autosens ISF when fresh, not the static constant.
3. Overnight-window queries are computed via zoneinfo in Python (DST-safe)
   instead of a hardcoded '-5 hours' EST offset that silently drifts an
   hour wrong for the ~8 EDT months of the year.

scripts/ isn't on sys.path by default and daily_summary.py isn't part of
the normal package tree, so it's imported here the same way
tests/test_tz_query.py imports examples/openclaw-skill/scripts/tz_query.py:
scripts/ added to sys.path, then a plain `import daily_summary`. Its
DB_PATH is a plain module attribute read fresh on every get_db() call (not
baked in at import time), so — unlike tz_query.py — a single shared import
plus monkeypatching daily_summary.DB_PATH per test (mirroring
tests/conftest.py's `conn` fixture) is enough; no need to reload the module.
"""

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import daily_summary as ds  # noqa: E402

import db  # noqa: E402

UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def reset_loop_cache():
    """fetch_loop_state() caches per-run in a module global — clear it
    between tests so one test's monkeypatched loop can't leak into another."""
    ds._loop_cache = None
    yield
    ds._loop_cache = None


@pytest.fixture
def ds_conn(tmp_path, monkeypatch):
    """Point daily_summary.get_db() (and db.get_db()) at a fresh temp
    SQLite db, initialized the same way tests/conftest.py's `conn` fixture
    initializes the shared test DB — db.init_db() alone is sufficient here
    since it creates alert_log/glucose_readings/insulin_doses/workouts/meals
    itself (no dependency on monitor.py)."""
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "TypeOneZen.db")
    monkeypatch.setattr(ds, "DB_PATH", tmp_path / "TypeOneZen.db")
    db.init_db()

    c = db.get_db()
    yield c
    c.close()


def insert_reading(conn, ts_iso, bg):
    conn.execute(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend) VALUES (?, ?, 'Flat')",
        (ts_iso, bg),
    )
    conn.commit()


# ── build_evening_outlook: loop-first design ────────────────────────────

def make_loop(**overrides):
    loop = {
        "timestamp": "2026-07-10T01:00:00+00:00",
        "eventual_bg": 120.0,
        "iob": 0.8,
        "cob": 0.0,
        "isf": 45.0,
        "temp_rate": 0.6,
        "insulin_req": 0.0,
        "reason": "Eventual BG 120 >= 100",
        "pred_bgs": {"IOB": [118, 115, 112, 115, 120],
                     "ZT": [118, 110, 105, 108, 115]},
        "data_age_minutes": 3.0,
    }
    loop.update(overrides)
    return loop


def test_outlook_fresh_loop_quotes_eventual_bg_when_clear():
    loop = make_loop(eventual_bg=115.0, pred_bgs={"IOB": [110, 105, 100, 105, 115]})
    text = ds.build_evening_outlook(120, 0.8, loop)
    assert "115" in text
    assert "landing" in text.lower()
    assert "overnight looks clear" in text.lower()
    # No naive math constants should leak into a loop-first message.
    assert "midnight" not in text.lower()


def test_outlook_snack_only_when_predicted_min_below_80():
    # Predicted low of 65 (< 80) should trigger the snack suggestion.
    loop = make_loop(eventual_bg=110.0, pred_bgs={"IOB": [100, 80, 65, 70, 90]})
    text = ds.build_evening_outlook(110, 0.5, loop)
    assert "snack" in text.lower()
    assert "65" in text  # cites Trio's own predicted min, not a local guess

    # Predicted min of 82 (>= 80) should NOT trigger a snack note.
    loop_ok = make_loop(eventual_bg=110.0, pred_bgs={"IOB": [100, 90, 82, 88, 95]})
    text_ok = ds.build_evening_outlook(110, 0.5, loop_ok)
    assert "snack" not in text_ok.lower()


def test_outlook_correction_flag_only_above_180_cites_insulin_req():
    loop = make_loop(eventual_bg=210.0, insulin_req=1.4,
                      pred_bgs={"IOB": [200, 205, 210, 208, 210]})
    text = ds.build_evening_outlook(190, 1.0, loop)
    assert "correction" not in text.lower() or "loop is already working" in text.lower()
    assert "210" in text
    assert "1.4" in text  # insulin_req, not local ISF math
    assert "ISF" not in text

    # eventual_bg <= 180 should not raise the correction flag.
    loop_ok = make_loop(eventual_bg=150.0, insulin_req=0.0,
                         pred_bgs={"IOB": [140, 145, 150, 148, 150]})
    text_ok = ds.build_evening_outlook(150, 1.0, loop_ok)
    assert "insulin_req" not in text_ok.lower()
    assert "1.4" not in text_ok


def test_outlook_quotes_temp_basal_and_cob():
    # Trigger the snack branch (predicted min 70 < 80) so the full forecast
    # sentence (which carries temp-basal state + COB) is included — the
    # terse "clean" message intentionally omits those details.
    loop = make_loop(eventual_bg=130.0, temp_rate=0.0, cob=25.0,
                      pred_bgs={"IOB": [125, 90, 70, 90, 130]})
    text = ds.build_evening_outlook(130, 0.3, loop)
    assert "suspended" in text.lower()
    assert "25" in text  # COB grams


def test_outlook_fallback_labels_itself_as_estimate_when_no_loop():
    # bg high, no IOB -> naive-correction fallback branch.
    text = ds.build_evening_outlook(200, 0.1, None)
    assert "rough estimate" in text.lower()
    assert "no loop data" in text.lower()

    # bg in range, low IOB -> "sleep well" fallback branch.
    text_clear = ds.build_evening_outlook(120, 0.1, None)
    assert "rough estimate" in text_clear.lower()

    # High IOB pushing a low BG projection -> snack fallback branch, uses
    # the module's static ISF/AIT constants (100 - 3.0*35*0.4 = 58 < 85).
    text_iob = ds.build_evening_outlook(100, 3.0, None)
    assert "rough estimate" in text_iob.lower()
    assert "snack" in text_iob.lower()


def test_outlook_returns_empty_when_nothing_to_say_and_no_bg():
    assert ds.build_evening_outlook(None, 0, None) == ""


# ── effective_isf: fresh / stale / missing loop ─────────────────────────

def test_effective_isf_uses_trio_when_fresh():
    loop = make_loop(isf=48.0, data_age_minutes=4.0)
    assert ds.effective_isf(loop) == 48.0


def test_effective_isf_falls_back_when_stale():
    loop = make_loop(isf=48.0, data_age_minutes=45.0)  # > LOOP_MAX_AGE_MINUTES
    assert ds.effective_isf(loop) == ds.ISF


def test_effective_isf_falls_back_when_missing():
    assert ds.effective_isf(None) == ds.ISF


# ── overnight window helper: DST regression test ────────────────────────

def test_overnight_bounds_utc_edt_date():
    # July 9 2026 is EDT (UTC-4): 10pm NY -> 02:00 UTC next day,
    # 8am NY -> 12:00 UTC same day. The old '-5 hours' arithmetic would
    # have produced 03:00/13:00 here — an hour late for the whole summer.
    start, end = ds.overnight_bounds_utc(date(2026, 7, 9))
    assert start == "2026-07-10T02:00:00"
    assert end == "2026-07-10T12:00:00"


def test_overnight_bounds_utc_est_date():
    # January 9 2026 is EST (UTC-5): 10pm NY -> 03:00 UTC next day,
    # 8am NY -> 13:00 UTC same day (matches the original hardcoded band).
    start, end = ds.overnight_bounds_utc(date(2026, 1, 9))
    assert start == "2026-01-10T03:00:00"
    assert end == "2026-01-10T13:00:00"


def test_overnight_windows_most_recent_first():
    end_ny = ds.datetime(2026, 7, 15, 10, 0, tzinfo=NY)  # 10am -> last night already ended
    windows = ds.overnight_windows(3, end_ny=end_ny)
    dates = [w[0] for w in windows]
    assert dates == [date(2026, 7, 14), date(2026, 7, 13), date(2026, 7, 12)]


# ── DST fix end-to-end through a real query ─────────────────────────────

def test_get_30d_overnight_avg_includes_edt_overnight_reading(ds_conn):
    # 10:30pm NY EDT (July, UTC-4) is 02:30 UTC the next day. The old
    # hardcoded '-5 hours'/hour-band query only matched UTC hour >= '03',
    # so during EDT it silently missed the 10-11pm NY hour of every
    # overnight window (it effectively only saw an 11pm-9am NY window
    # instead of the intended 10pm-8am). The DST-safe rewrite must include
    # this reading.
    ts = ds.to_utc_str(ds.datetime(2026, 7, 9, 22, 30, tzinfo=NY))  # 10:30pm EDT
    insert_reading(ds_conn, ts, 160)
    avg = ds.get_30d_overnight_avg()
    assert avg == 160
