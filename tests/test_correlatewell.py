"""parse_correlatewell.py tests — insert counts, minute-dedup, idempotency,
workout proximity dedup, and dry-run.

Uses the `conn` fixture from conftest.py (temp SQLite db, full schema via
db.init_db()). CSV fixtures are written to tmp_path and imported via
parse_correlatewell.run_import() directly (same function the CLI calls).
"""

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# parsers/ has no __init__.py (matches export_correlatewell.py / parse_correlatewell.py's
# own flat-import style for `db`), so import it directly off sys.path rather than as a package.
PARSERS_DIR = Path(__file__).resolve().parent.parent / "parsers"
sys.path.insert(0, str(PARSERS_DIR))

import parse_correlatewell as cw  # noqa: E402

UTC = timezone.utc

GLUCOSE_HEADERS = [
    "id", "user_id", "timestamp_utc", "glucose_value", "glucose_unit",
    "trend_arrow", "source_app", "reading_type", "is_manual_entry",
]

WORKOUT_HEADERS = [
    "id", "user_id", "start_time_utc", "end_time_utc", "workout_type",
    "duration_seconds", "duration_minutes", "avg_heart_rate", "max_heart_rate",
    "calories_burned", "distance_meters", "source_app", "intensity", "notes",
]


def write_glucose_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GLUCOSE_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in GLUCOSE_HEADERS})


def write_workouts_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=WORKOUT_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in WORKOUT_HEADERS})


def iso_bare(dt: datetime) -> str:
    """Format a UTC-aware datetime as a bare (offset-free) timestamp, matching
    what export_correlatewell.py's `AT TIME ZONE 'UTC'` cast produces."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def base_glucose_row(ts: datetime, **overrides) -> dict:
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "timestamp_utc": iso_bare(ts),
        "glucose_value": "145",
        "glucose_unit": "mg/dL",
        "trend_arrow": "flat",
        "source_app": "dexcom",
        "reading_type": "cgm_continuous",
        "is_manual_entry": "false",
    }
    row.update(overrides)
    return row


def base_workout_row(start: datetime, end: datetime, **overrides) -> dict:
    row = {
        "id": "33333333-3333-3333-3333-333333333333",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "start_time_utc": iso_bare(start),
        "end_time_utc": iso_bare(end),
        "workout_type": "running",
        "duration_seconds": "1800",
        "duration_minutes": "30",
        "avg_heart_rate": "140",
        "max_heart_rate": "165",
        "calories_burned": "300",
        "distance_meters": "5000",
        "source_app": "strava",
        "intensity": "moderate",
        "notes": "Morning run",
    }
    row.update(overrides)
    return row


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ── Basic insert counts ─────────────────────────────────────────────

def test_basic_import_inserts_glucose_and_workouts(conn, tmp_path):
    now = datetime(2025, 10, 3, 14, 0, 0, tzinfo=UTC)
    write_glucose_csv(tmp_path / "glucose.csv", [
        base_glucose_row(now),
        base_glucose_row(now + timedelta(minutes=5), glucose_value="150", trend_arrow="single_up"),
        base_glucose_row(now + timedelta(minutes=10), glucose_value="110.0", glucose_unit="mmol/L"),
    ])
    write_workouts_csv(tmp_path / "workouts.csv", [
        base_workout_row(now, now + timedelta(minutes=30)),
    ])

    counts = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert counts["glucose_inserted"] == 3
    assert counts["glucose_skipped"] == 0
    assert counts["workouts_inserted"] == 1
    assert counts["workouts_skipped"] == 0
    assert count(conn, "glucose_readings") == 3
    assert count(conn, "workouts") == 1

    row = conn.execute(
        "SELECT source, glucose_mg_dl, trend, trend_arrow FROM glucose_readings ORDER BY timestamp LIMIT 1"
    ).fetchone()
    assert row["source"] == "correlatewell"
    assert row["glucose_mg_dl"] == 145
    assert row["trend"] == "Flat"
    assert row["trend_arrow"] == "→"

    # mmol/L conversion: 110.0 mmol/L * 18.018 ≈ 1982 mg/dL
    mmol_row = conn.execute(
        "SELECT glucose_mg_dl FROM glucose_readings ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    assert mmol_row["glucose_mg_dl"] == round(110.0 * 18.018)


# ── Glucose minute-dedup against pre-seeded (other-source) readings ──

def test_glucose_dedup_against_existing_minute(conn, tmp_path):
    seeded_time = datetime(2025, 10, 3, 14, 5, 30, tzinfo=UTC)
    conn.execute(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, source) VALUES (?, ?, 'nightscout')",
        (seeded_time.isoformat(), 148),
    )
    conn.commit()

    # Same UTC minute (14:05), different second — should be treated as a duplicate
    cw_time = seeded_time.replace(second=2)
    write_glucose_csv(tmp_path / "glucose.csv", [base_glucose_row(cw_time)])

    counts = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert counts["glucose_inserted"] == 0
    assert counts["glucose_skipped"] == 1
    assert count(conn, "glucose_readings") == 1  # only the seeded nightscout row


def test_glucose_not_deduped_outside_minute_window(conn, tmp_path):
    seeded_time = datetime(2025, 10, 3, 14, 5, 30, tzinfo=UTC)
    conn.execute(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, source) VALUES (?, ?, 'nightscout')",
        (seeded_time.isoformat(), 148),
    )
    conn.commit()

    cw_time = seeded_time + timedelta(minutes=1)  # 14:06 — different minute
    write_glucose_csv(tmp_path / "glucose.csv", [base_glucose_row(cw_time)])

    counts = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert counts["glucose_inserted"] == 1
    assert counts["glucose_skipped"] == 0
    assert count(conn, "glucose_readings") == 2


# ── Idempotent re-run ──────────────────────────────────────────────

def test_rerun_is_idempotent(conn, tmp_path):
    now = datetime(2025, 10, 3, 14, 0, 0, tzinfo=UTC)
    write_glucose_csv(tmp_path / "glucose.csv", [
        base_glucose_row(now),
        base_glucose_row(now + timedelta(minutes=5)),
    ])
    write_workouts_csv(tmp_path / "workouts.csv", [
        base_workout_row(now, now + timedelta(minutes=30)),
    ])

    first = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()
    second = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert first["glucose_inserted"] == 2
    assert first["workouts_inserted"] == 1

    assert second["glucose_inserted"] == 0
    assert second["glucose_skipped"] == 2
    assert second["workouts_inserted"] == 0
    assert second["workouts_skipped"] == 1

    assert count(conn, "glucose_readings") == 2
    assert count(conn, "workouts") == 1


# ── Workout dedup by proximity (+/- 2 minutes) ────────────────────

def test_workout_dedup_within_two_minutes(conn, tmp_path):
    existing_start = datetime(2025, 10, 3, 7, 0, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO workouts (started_at, ended_at, activity_type) VALUES (?, ?, 'running')",
        (existing_start.isoformat(), (existing_start + timedelta(minutes=30)).isoformat()),
    )
    conn.commit()

    # 90 seconds later — within the 2-minute window, should be skipped
    near_start = existing_start + timedelta(seconds=90)
    write_workouts_csv(tmp_path / "workouts.csv", [
        base_workout_row(near_start, near_start + timedelta(minutes=30)),
    ])

    counts = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert counts["workouts_inserted"] == 0
    assert counts["workouts_skipped"] == 1
    assert count(conn, "workouts") == 1


def test_workout_not_deduped_outside_two_minutes(conn, tmp_path):
    existing_start = datetime(2025, 10, 3, 7, 0, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO workouts (started_at, ended_at, activity_type) VALUES (?, ?, 'running')",
        (existing_start.isoformat(), (existing_start + timedelta(minutes=30)).isoformat()),
    )
    conn.commit()

    far_start = existing_start + timedelta(minutes=3)
    write_workouts_csv(tmp_path / "workouts.csv", [
        base_workout_row(far_start, far_start + timedelta(minutes=30)),
    ])

    counts = cw.run_import(tmp_path, conn, None, dry_run=False)
    conn.commit()

    assert counts["workouts_inserted"] == 1
    assert counts["workouts_skipped"] == 0
    assert count(conn, "workouts") == 2


# ── Dry run ─────────────────────────────────────────────────────────

def test_dry_run_inserts_nothing(conn, tmp_path):
    now = datetime(2025, 10, 3, 14, 0, 0, tzinfo=UTC)
    write_glucose_csv(tmp_path / "glucose.csv", [base_glucose_row(now)])
    write_workouts_csv(tmp_path / "workouts.csv", [
        base_workout_row(now, now + timedelta(minutes=30)),
    ])

    counts = cw.run_import(tmp_path, conn, None, dry_run=True)
    conn.rollback()

    # Counts still reflect what *would* have been inserted...
    assert counts["glucose_inserted"] == 1
    assert counts["workouts_inserted"] == 1
    # ...but nothing was actually written.
    assert count(conn, "glucose_readings") == 0
    assert count(conn, "workouts") == 0
