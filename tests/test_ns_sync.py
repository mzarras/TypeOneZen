"""ns_sync.py tests — idempotency, cross-source dedup, classification, cursors.

Uses the FakeNightscoutClient stub from conftest.py and a temp SQLite db.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeNightscoutClient

import ns_sync

UTC = ZoneInfo("UTC")


def iso_minutes_ago(minutes):
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def make_client():
    """A realistic sync payload: CGM entries + a closed-loop treatment mix
    (meal bolus, SMBs, correction, temp basal, carb-only entry)."""
    entries = [
        {"time": iso_minutes_ago(15), "sgv": 132, "direction": "Flat", "device": "loop"},
        {"time": iso_minutes_ago(10), "sgv": 138, "direction": "FortyFiveUp", "device": "loop"},
        {"time": iso_minutes_ago(5), "sgv": 145, "direction": "FortyFiveUp", "device": "loop"},
    ]
    treatments = [
        # Meal bolus: insulin + carbs in one treatment
        {"time": iso_minutes_ago(60), "event_type": "bolus", "raw_event_type": "Meal Bolus",
         "insulin": 3.5, "carbs": 45.0, "duration": None, "rate": None, "id": "t1"},
        # SMBs — the loop delivers many small boluses; stored as boluses
        {"time": iso_minutes_ago(40), "event_type": "bolus", "raw_event_type": "SMB",
         "insulin": 0.1, "carbs": None, "duration": None, "rate": None, "id": "t2"},
        {"time": iso_minutes_ago(35), "event_type": "bolus", "raw_event_type": "SMB",
         "insulin": 0.15, "carbs": None, "duration": None, "rate": None, "id": "t3"},
        # Manual correction bolus (no carbs)
        {"time": iso_minutes_ago(30), "event_type": "bolus", "raw_event_type": "Correction Bolus",
         "insulin": 1.0, "carbs": None, "duration": None, "rate": None, "id": "t4"},
        # Temp basal from the loop: units derived from rate * duration
        {"time": iso_minutes_ago(25), "event_type": "basal", "raw_event_type": "Temp Basal",
         "insulin": None, "carbs": None, "duration": 30.0, "rate": 1.2, "id": "t5"},
        # Carb-only entry (no insulin)
        {"time": iso_minutes_ago(20), "event_type": "carbs", "raw_event_type": "Carb Correction",
         "insulin": None, "carbs": 20.0, "duration": None, "rate": None, "id": "t6"},
    ]
    return FakeNightscoutClient(entries=entries, treatments=treatments)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_first_sync_inserts_everything(conn):
    counts = ns_sync.sync(make_client(), conn)

    assert counts["glucose_inserted"] == 3
    assert counts["bolus_inserted"] == 3        # meal bolus + 2 SMBs
    assert counts["correction_inserted"] == 1
    assert counts["basal_inserted"] == 1
    assert counts["meals_inserted"] == 2        # meal-bolus carbs + carb-only

    assert count(conn, "glucose_readings") == 3
    assert count(conn, "insulin_doses") == 5
    assert count(conn, "meals") == 2

    # Everything tagged with source / source_id
    row = conn.execute(
        "SELECT source, source_id FROM glucose_readings LIMIT 1"
    ).fetchone()
    assert row["source"] == "nightscout"
    assert row["source_id"].startswith("ns-entry-")


def test_second_sync_is_idempotent(conn):
    client = make_client()
    ns_sync.sync(client, conn)
    counts = ns_sync.sync(client, conn)

    for key in ("glucose_inserted", "bolus_inserted", "correction_inserted",
                "basal_inserted", "meals_inserted"):
        assert counts[key] == 0, f"{key} inserted duplicates on re-run"

    assert count(conn, "glucose_readings") == 3
    assert count(conn, "insulin_doses") == 5
    assert count(conn, "meals") == 2


def test_backfill_since_is_rerun_safe(conn):
    client = make_client()
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    ns_sync.sync(client, conn, since_override=since)
    ns_sync.sync(client, conn, since_override=since)

    assert count(conn, "glucose_readings") == 3
    assert count(conn, "insulin_doses") == 5
    assert count(conn, "meals") == 2


def test_entries_dedup_against_dexcom_poller(conn):
    """An NS entry in the same minute as an existing Dexcom reading is skipped
    (poller.py stays running as a redundant BG source)."""
    entry_time = iso_minutes_ago(5)
    conn.execute(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, source) VALUES (?, ?, 'dexcom')",
        (entry_time, 144),
    )
    conn.commit()

    client = FakeNightscoutClient(entries=[
        {"time": entry_time, "sgv": 145, "direction": "Flat", "device": "loop"},
    ])
    counts = ns_sync.sync(client, conn)

    assert counts["glucose_inserted"] == 0
    assert counts["glucose_skipped"] == 1
    assert count(conn, "glucose_readings") == 1


def test_dose_classification_and_basal_units(conn):
    ns_sync.sync(make_client(), conn)

    rows = conn.execute(
        "SELECT source_id, units, type FROM insulin_doses"
    ).fetchall()
    by_id = {r["source_id"]: r for r in rows}

    assert by_id["ns-treatment-t1"]["type"] == "bolus"       # meal bolus
    assert by_id["ns-treatment-t2"]["type"] == "bolus"       # SMB stays a bolus
    assert by_id["ns-treatment-t3"]["type"] == "bolus"
    assert by_id["ns-treatment-t4"]["type"] == "correction"
    assert by_id["ns-treatment-t5"]["type"] == "basal"
    # 1.2 U/hr for 30 min → 0.6 U
    assert abs(by_id["ns-treatment-t5"]["units"] - 0.6) < 1e-9

    meal = conn.execute(
        "SELECT carbs_g, source FROM meals WHERE source_id = 'ns-treatment-t1'"
    ).fetchone()
    assert meal["carbs_g"] == 45.0
    assert meal["source"] == "nightscout"


# ── Effective temp-basal units (stored-effective convention) ─────────

def temp_basal(t_id, minutes_ago, rate, duration):
    return {"time": iso_minutes_ago(minutes_ago), "event_type": "basal",
            "raw_event_type": "Temp Basal", "insulin": None, "carbs": None,
            "duration": duration, "rate": rate, "id": t_id}


def basal_units_by_id(conn):
    return {r["source_id"]: r["units"] for r in conn.execute(
        "SELECT source_id, units FROM insulin_doses WHERE type = 'basal'"
    )}


def test_new_basal_truncates_prior_scheduled_row(conn):
    """A temp basal superseded 10 min in delivers rate*10/60, not rate*30/60."""
    client = FakeNightscoutClient(treatments=[
        temp_basal("b1", 30, 2.0, 30.0),   # superseded 10 min later
        temp_basal("b2", 20, 1.0, 30.0),   # newest — no successor yet
    ])
    ns_sync.sync(client, conn)

    units = basal_units_by_id(conn)
    assert units["ns-treatment-b1"] == pytest.approx(2.0 * 10 / 60, abs=1e-4)
    # The in-progress row keeps its scheduled amount until superseded
    assert units["ns-treatment-b2"] == pytest.approx(0.5, abs=1e-4)


def test_non_overlapping_basal_keeps_full_scheduled_units(conn):
    """A temp basal whose successor starts after its scheduled end ran in full."""
    client = FakeNightscoutClient(treatments=[
        temp_basal("b1", 120, 1.2, 30.0),  # next starts 60 min later → full 0.6
        temp_basal("b2", 60, 0.8, 30.0),
    ])
    ns_sync.sync(client, conn)

    units = basal_units_by_id(conn)
    assert units["ns-treatment-b1"] == pytest.approx(0.6, abs=1e-4)


def test_resync_does_not_reshrink_or_grow_basal_units(conn):
    """Re-running the sync (overlap window re-fetch) leaves units unchanged."""
    client = FakeNightscoutClient(treatments=[
        temp_basal("b1", 40, 2.0, 30.0),
        temp_basal("b2", 30, 1.5, 30.0),
        temp_basal("b3", 25, 3.0, 30.0),
    ])
    ns_sync.sync(client, conn)
    first = basal_units_by_id(conn)

    counts = ns_sync.sync(client, conn)
    assert counts["basal_inserted"] == 0
    assert basal_units_by_id(conn) == first

    assert first["ns-treatment-b1"] == pytest.approx(2.0 * 10 / 60, abs=1e-4)
    assert first["ns-treatment-b2"] == pytest.approx(1.5 * 5 / 60, abs=1e-4)


def test_backfilled_older_basal_truncated_against_existing_successor(conn):
    """A basal arriving out of order (backfill) is truncated against the
    already-stored later temp basal, not left at its scheduled amount."""
    ns_sync.sync(FakeNightscoutClient(treatments=[
        temp_basal("late", 20, 1.0, 30.0),
    ]), conn)
    ns_sync.sync(FakeNightscoutClient(treatments=[
        temp_basal("early", 30, 2.4, 30.0),   # 10 min before "late"
    ]), conn, since_override=iso_minutes_ago(60))

    units = basal_units_by_id(conn)
    assert units["ns-treatment-early"] == pytest.approx(2.4 * 10 / 60, abs=1e-4)
    assert units["ns-treatment-late"] == pytest.approx(0.5, abs=1e-4)


def test_consumer_day_sum_is_effective_not_scheduled(conn, monkeypatch):
    """Consumer-level: a window of overlapping temp basals sums to effective
    units via plain SUM (daily_summary.get_insulin_stats), not scheduled."""
    import importlib.util
    from pathlib import Path

    client = FakeNightscoutClient(treatments=[
        # 18u of scheduled basal, only 0.75u effective
        temp_basal("b1", 60, 12.0, 30.0),   # superseded after 2.5 min → 0.5
        temp_basal("b2", 57.5, 6.0, 30.0),  # superseded after 2.5 min → 0.25
        temp_basal("b3", 55, 0.0, 30.0),    # zero rate, in progress → 0.0
        # plus a 2u bolus
        {"time": iso_minutes_ago(50), "event_type": "bolus",
         "raw_event_type": "Meal Bolus", "insulin": 2.0, "carbs": None,
         "duration": None, "rate": None, "id": "bol1"},
    ])
    ns_sync.sync(client, conn)

    spec = importlib.util.spec_from_file_location(
        "daily_summary_under_test",
        Path(__file__).resolve().parent.parent / "scripts" / "daily_summary.py",
    )
    daily_summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily_summary)

    import db
    monkeypatch.setattr(daily_summary, "DB_PATH", db.DB_PATH)

    now = datetime.now(UTC)
    stats = daily_summary.get_insulin_stats(now - timedelta(hours=2), now)

    # Naive scheduled SUM would be 18u basal + 2u bolus = 20u
    assert stats["basal"] == pytest.approx(0.8, abs=0.05)   # 0.5 + 0.25 + 0
    assert stats["bolus"] == pytest.approx(2.0, abs=0.01)
    assert stats["total"] == pytest.approx(2.8, abs=0.05)
    # Bug fix: breakdown uses real dose types — no phantom 'meal' type
    assert "meal" not in stats
    assert set(stats) == {"total", "bolus", "basal", "correction",
                          "correction_count", "count"}


def test_cursor_advances(conn):
    ns_sync.sync(make_client(), conn)

    entries_cursor = ns_sync.get_cursor(conn, ns_sync.CURSOR_ENTRIES)
    treatments_cursor = ns_sync.get_cursor(conn, ns_sync.CURSOR_TREATMENTS)

    assert entries_cursor is not None
    assert treatments_cursor is not None
    # Cursors point at the newest synced item in each stream
    newest_entry = conn.execute(
        "SELECT MAX(timestamp) AS t FROM glucose_readings WHERE source = 'nightscout'"
    ).fetchone()["t"]
    assert entries_cursor == newest_entry
