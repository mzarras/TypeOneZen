"""basal_effective.py tests — effective temp-basal delivery math.

Covers the shared helper used by ns_sync.py and the one-time backfill:
superseded (truncated), non-overlapping (full scheduled), and in-progress
(capped at elapsed-until-now) temp basals, plus notes parsing.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from basal_effective import (
    compute_effective_units,
    effective_units,
    parse_rate_duration,
    parse_ts_utc,
)

UTC = ZoneInfo("UTC")

T0 = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def ts(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


# ── effective_units ──────────────────────────────────────────────────

def test_superseded_before_scheduled_end():
    # 1.2 U/hr scheduled for 30 min but superseded after 10 → 0.2 U
    assert effective_units(1.2, 30.0, 10.0) == pytest.approx(0.2)


def test_runs_to_scheduled_end_when_next_is_later():
    # Next temp basal starts after the scheduled end → full scheduled amount
    assert effective_units(1.2, 30.0, 45.0) == pytest.approx(0.6)


def test_zero_rate_delivers_nothing():
    assert effective_units(0.0, 90.0, 10.0) == 0.0


def test_negative_gap_clamps_to_zero():
    assert effective_units(2.0, 30.0, -5.0) == 0.0


# ── compute_effective_units ──────────────────────────────────────────

def test_chain_of_superseded_basals():
    rows = [
        (ts(0), 2.0, 30.0),    # superseded after 10 min → 2.0 * 10/60 = 0.3333
        (ts(10), 1.5, 30.0),   # superseded after 5 min  → 1.5 * 5/60  = 0.125
        (ts(15), 3.0, 30.0),   # last row — see `now` below
    ]
    now = parse_ts_utc(ts(21))  # last row has run 6 of its 30 min
    result = compute_effective_units(rows, now=now)
    assert result[0] == pytest.approx(2.0 * 10 / 60, abs=1e-4)
    assert result[1] == pytest.approx(1.5 * 5 / 60, abs=1e-4)
    assert result[2] == pytest.approx(3.0 * 6 / 60, abs=1e-4)


def test_non_overlapping_rows_keep_full_scheduled_units():
    rows = [
        (ts(0), 1.0, 30.0),    # next starts 60 min later → full 30 min = 0.5
        (ts(60), 0.8, 30.0),   # last row, now is past its scheduled end
    ]
    now = parse_ts_utc(ts(200))
    result = compute_effective_units(rows, now=now)
    assert result[0] == pytest.approx(0.5)
    assert result[1] == pytest.approx(0.4)  # capped at scheduled 30 min


def test_in_progress_last_row_capped_at_elapsed():
    rows = [(ts(0), 3.0, 90.0)]
    now = parse_ts_utc(ts(8))
    result = compute_effective_units(rows, now=now)
    assert result[0] == pytest.approx(3.0 * 8 / 60, abs=1e-4)


def test_empty_input():
    assert compute_effective_units([]) == []


def test_accepts_string_and_datetime_timestamps():
    rows = [
        (T0, 1.0, 30.0),                       # datetime
        ((T0 + timedelta(minutes=12)).isoformat(), 1.0, 30.0),  # string
    ]
    result = compute_effective_units(rows, now=T0 + timedelta(minutes=20))
    assert result[0] == pytest.approx(0.2)


# ── parse_rate_duration ──────────────────────────────────────────────

def test_parse_rate_duration_from_ns_sync_notes():
    assert parse_rate_duration(
        "Temp Basal, rate=0.75 U/hr, duration=30.0 min"
    ) == (0.75, 30.0)
    assert parse_rate_duration(
        "Temp Basal, rate=0.0 U/hr, duration=120.0 min"
    ) == (0.0, 120.0)


def test_parse_rate_duration_rejects_bad_notes():
    assert parse_rate_duration(None) is None
    assert parse_rate_duration("") is None
    assert parse_rate_duration("Meal Bolus, carbs=45g") is None
    assert parse_rate_duration("Temp Basal, rate=None U/hr, duration=None min") is None
