#!/usr/bin/env python3
"""Import CorrelateWell CSV exports (glucose.csv, workouts.csv) into TypeOneZen.

CorrelateWell is a separate full-stack app (Node/Express + PostgreSQL) that
holds this user's Dexcom CGM + Strava/HealthKit workout history back to fall
2025. Its data doesn't come in through Nightscout, so this is a one-time (or
occasionally re-run) historical backfill path, analogous to parse_glooko.py
for pre-Nightscout CGM history and parse_fit.py for Garmin FIT workouts.

Expects the two CSVs produced by parsers/export_correlatewell.py:
    glucose.csv   — id, user_id, timestamp_utc, glucose_value, glucose_unit,
                     trend_arrow, source_app, reading_type, is_manual_entry
    workouts.csv  — id, user_id, start_time_utc, end_time_utc, workout_type,
                     duration_seconds, duration_minutes, avg_heart_rate,
                     max_heart_rate, calories_burned, distance_meters,
                     source_app, intensity, notes

Timezone assumption: CorrelateWell stores timestamps in Postgres
TIMESTAMP WITH TIME ZONE columns, and export_correlatewell.py explicitly
casts every timestamp with `AT TIME ZONE 'UTC'` before writing the CSV. So
by construction, `timestamp_utc` / `start_time_utc` / `end_time_utc` are
already UTC wall-clock values with NO timezone suffix (e.g.
'2025-10-03T14:22:01.500000', not '...+00:00' or '...-04:00'). This script
therefore treats a bare (offset-free) timestamp as already-UTC and attaches
UTC tzinfo directly — it does NOT assume local time the way parse_glooko.py
does for Glooko's NY-local exports.

If a CSV instead has explicit UTC offsets (e.g. it was produced by hand with
a plain `\\copy (SELECT timestamp FROM ...)` and no AT TIME ZONE cast, so
values reflect the exporting session's TimeZone GUC), those offsets are
respected and converted to UTC as normal. --tz is provided as an escape
hatch: if you know a manually-produced CSV's bare timestamps are actually in
some other zone (not UTC), pass --tz to reinterpret them before converting.
Verify this assumption against a few known readings before trusting a bulk
import against real data.

Dedup:
  - glucose_readings: a CorrelateWell row is skipped if ANY existing
    glucose_readings row (any source — dexcom poller, Nightscout, Glooko)
    falls in the same UTC minute. This is the same cross-source, minute-
    granularity approach ns_sync.py and parse_glooko.py use. Re-running this
    script is idempotent because the in-memory dedup set is updated as rows
    are inserted, and reloaded fresh (including prior CorrelateWell inserts)
    on every run.
  - workouts: TypeOneZen's workouts table has no source/source_id column, so
    dedup is by proximity: a CorrelateWell workout is skipped if any
    existing workout's started_at is within +/-2 minutes of it. Same
    self-and-cross-run idempotency approach as glucose.

Usage:
    python3 parsers/export_correlatewell.py         # (elsewhere first, produces the CSVs)
    python3 parsers/parse_correlatewell.py
    python3 parsers/parse_correlatewell.py --dir data/imports/correlatewell --dry-run
    python3 parsers/parse_correlatewell.py --tz America/New_York   # only if CSV timestamps are bare + non-UTC
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Add project root to path so we can import db (works from any checkout location)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import get_db  # noqa: E402

UTC = timezone.utc

DEFAULT_IMPORT_DIR = Path.home() / "TypeOneZen" / "data" / "imports" / "correlatewell"

WORKOUT_DEDUP_WINDOW = timedelta(minutes=2)

# CorrelateWell trend_arrow enum → (Nightscout-style trend string, arrow glyph),
# matching the convention ns_sync.py uses for Nightscout's own direction strings.
TREND_MAP = {
    "double_up":       ("DoubleUp", "⇈"),   # ⇈
    "single_up":       ("SingleUp", "↑"),   # ↑
    "forty_five_up":   ("FortyFiveUp", "↗"),  # ↗
    "flat":            ("Flat", "→"),        # →
    "forty_five_down": ("FortyFiveDown", "↘"),  # ↘
    "single_down":     ("SingleDown", "↓"),  # ↓
    "double_down":     ("DoubleDown", "⇊"),  # ⇊
}

MMOL_TO_MGDL = 18.018  # matches CorrelateWell's own GlucoseReading.getGlucoseInMgDl() factor


def parse_cw_timestamp(ts_str: str, tz_override: ZoneInfo | None) -> str:
    """Convert a CorrelateWell CSV timestamp to ISO8601 UTC string.

    Bare (offset-free) timestamps are treated as already-UTC, unless
    --tz is given, in which case they're reinterpreted as being in that zone
    first. Timestamps that already carry an offset are honored as-is and
    just normalized to UTC.
    """
    ts_str = ts_str.strip()
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_override or UTC)
    return dt.astimezone(UTC).isoformat()


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_existing_glucose_minutes(conn) -> set[str]:
    """Minute-level UTC timestamps of existing glucose readings (all sources).

    Same normalization as parse_glooko.py / ns_sync.py — prevents
    double-storing a CGM reading that already arrived via another source.
    """
    existing = set()
    for row in conn.execute("SELECT timestamp FROM glucose_readings"):
        ts = row[0]
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            existing.add(dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"))
        except (ValueError, TypeError):
            pass
    return existing


def load_existing_workout_starts(conn) -> list[datetime]:
    """All existing workouts.started_at values as UTC-aware datetimes."""
    existing = []
    for row in conn.execute("SELECT started_at FROM workouts"):
        ts = row[0]
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            existing.append(dt.astimezone(UTC))
        except (ValueError, TypeError):
            pass
    return existing


def is_near_existing_workout(candidate: datetime, existing_starts: list[datetime]) -> bool:
    return any(abs(candidate - t) <= WORKOUT_DEDUP_WINDOW for t in existing_starts)


def import_glucose(
    rows: list[dict],
    conn,
    existing_minutes: set[str],
    tz_override: ZoneInfo | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Import glucose.csv rows into glucose_readings. Returns (inserted, skipped)."""
    inserted = skipped = 0
    for row in rows:
        ts_raw = (row.get("timestamp_utc") or "").strip()
        value_raw = (row.get("glucose_value") or "").strip()
        if not ts_raw or not value_raw:
            continue

        try:
            value = float(value_raw)
        except (ValueError, TypeError):
            continue

        unit = (row.get("glucose_unit") or "mg/dL").strip()
        glucose_mg_dl = round(value * MMOL_TO_MGDL) if unit == "mmol/L" else round(value)

        ts_utc = parse_cw_timestamp(ts_raw, tz_override)
        minute_key = datetime.fromisoformat(ts_utc).strftime("%Y-%m-%dT%H:%M")

        if minute_key in existing_minutes:
            skipped += 1
            continue

        trend_raw = (row.get("trend_arrow") or "").strip()
        trend, trend_arrow = TREND_MAP.get(trend_raw, (None, None))

        if not dry_run:
            conn.execute(
                """
                INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend, trend_arrow, source)
                VALUES (?, ?, ?, ?, 'correlatewell')
                """,
                (ts_utc, glucose_mg_dl, trend, trend_arrow),
            )
        existing_minutes.add(minute_key)
        inserted += 1

    return inserted, skipped


def import_workouts(
    rows: list[dict],
    conn,
    existing_starts: list[datetime],
    tz_override: ZoneInfo | None,
    dry_run: bool,
) -> tuple[int, int]:
    """Import workouts.csv rows into workouts. Returns (inserted, skipped)."""
    inserted = skipped = 0
    for row in rows:
        start_raw = (row.get("start_time_utc") or "").strip()
        if not start_raw:
            continue

        started_at = parse_cw_timestamp(start_raw, tz_override)
        started_dt = datetime.fromisoformat(started_at)

        if is_near_existing_workout(started_dt, existing_starts):
            skipped += 1
            continue

        end_raw = (row.get("end_time_utc") or "").strip()
        ended_at = parse_cw_timestamp(end_raw, tz_override) if end_raw else None

        activity_type = (row.get("workout_type") or "other").strip()
        intensity = (row.get("intensity") or "").strip() or None

        notes_fields = {
            "source_app": (row.get("source_app") or "").strip() or None,
            "avg_heart_rate": row.get("avg_heart_rate") or None,
            "max_heart_rate": row.get("max_heart_rate") or None,
            "calories_burned": row.get("calories_burned") or None,
            "distance_meters": row.get("distance_meters") or None,
            "duration_seconds": row.get("duration_seconds") or None,
            "cw_notes": (row.get("notes") or "").strip() or None,
        }
        notes = {k: v for k, v in notes_fields.items() if v not in (None, "")}
        notes_json = json.dumps(notes) if notes else None

        if not dry_run:
            conn.execute(
                """INSERT INTO workouts (started_at, ended_at, activity_type, intensity, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (started_at, ended_at, activity_type, intensity, notes_json),
            )
        existing_starts.append(started_dt)
        inserted += 1

    return inserted, skipped


def run_import(import_dir: Path, conn, tz_override: ZoneInfo | None, dry_run: bool) -> dict:
    """Import both CSVs. Returns a summary counts dict. Caller commits/closes conn."""
    glucose_rows = read_csv_rows(import_dir / "glucose.csv")
    workout_rows = read_csv_rows(import_dir / "workouts.csv")

    existing_minutes = load_existing_glucose_minutes(conn)
    existing_starts = load_existing_workout_starts(conn)

    glucose_inserted, glucose_skipped = import_glucose(
        glucose_rows, conn, existing_minutes, tz_override, dry_run
    )
    workouts_inserted, workouts_skipped = import_workouts(
        workout_rows, conn, existing_starts, tz_override, dry_run
    )

    return {
        "glucose_rows": len(glucose_rows),
        "glucose_inserted": glucose_inserted,
        "glucose_skipped": glucose_skipped,
        "workout_rows": len(workout_rows),
        "workouts_inserted": workouts_inserted,
        "workouts_skipped": workouts_skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import CorrelateWell CSV exports (glucose.csv, workouts.csv) into TypeOneZen"
    )
    parser.add_argument(
        "--dir", type=str, default=str(DEFAULT_IMPORT_DIR),
        help=f"Directory containing glucose.csv / workouts.csv (default: {DEFAULT_IMPORT_DIR})",
    )
    parser.add_argument(
        "--tz", type=str, default=None,
        help="Reinterpret bare (offset-free) CSV timestamps as this zone instead of UTC "
             "(e.g. America/New_York). Only needed for manually-produced CSVs that skipped "
             "the AT TIME ZONE 'UTC' cast in export_correlatewell.py.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print what would be inserted, without writing to the database",
    )
    args = parser.parse_args()

    import_dir = Path(args.dir).expanduser().resolve()
    if not import_dir.exists():
        print(f"Error: directory not found: {import_dir}")
        sys.exit(1)

    glucose_path = import_dir / "glucose.csv"
    workouts_path = import_dir / "workouts.csv"
    if not glucose_path.exists() and not workouts_path.exists():
        print(f"Error: neither glucose.csv nor workouts.csv found in {import_dir}")
        sys.exit(1)

    tz_override = ZoneInfo(args.tz) if args.tz else None

    conn = get_db()
    try:
        counts = run_import(import_dir, conn, tz_override, args.dry_run)
        if args.dry_run:
            print("DRY RUN — no changes written.\n")
            conn.rollback()
        else:
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\nError during import — transaction rolled back: {e}")
        raise
    finally:
        conn.close()

    print("=" * 60)
    print("CORRELATEWELL IMPORT SUMMARY")
    print("=" * 60)
    print(f"Glucose:  {counts['glucose_rows']} rows read, "
          f"{counts['glucose_inserted']} inserted, "
          f"{counts['glucose_skipped']} skipped (duplicate)")
    print(f"Workouts: {counts['workout_rows']} rows read, "
          f"{counts['workouts_inserted']} inserted, "
          f"{counts['workouts_skipped']} skipped (duplicate)")
    print("=" * 60)


if __name__ == "__main__":
    main()
