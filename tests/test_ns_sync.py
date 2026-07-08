"""ns_sync.py tests — idempotency, cross-source dedup, classification, cursors.

Uses the FakeNightscoutClient stub from conftest.py and a temp SQLite db.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
