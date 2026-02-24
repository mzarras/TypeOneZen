#!/usr/bin/env python3
"""Batch import .fit files into the TypeOneZen workouts table.

Usage:
    python3 parsers/parse_fit.py [--dir <fit_directory>]

Defaults to ~/TypeOneZen/data/imports/fit/ if no --dir is given.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fitparse import FitFile
from fitparse.profile import BASE_TYPES, MESSAGE_TYPES
from fitparse.base import (
    DefinitionMessage,
    DevFieldDefinition,
    FieldDefinition,
    get_dev_type,
)

# ---------------------------------------------------------------------------
# Monkey-patch fitparse to tolerate non-standard FIT files (e.g. COROS ski/
# cardio activities) that declare a field size that isn't a multiple of the
# base type size.  Upstream fitparse raises FitParseError; the comment in
# the source says "we could fall back to byte encoding".  We do exactly that.
# ---------------------------------------------------------------------------
_BASE_TYPE_BYTE = BASE_TYPES[13]  # byte type, size=1


def _lenient_parse_definition_message(self, header):
    endian = ">" if self._read_struct("xB") else "<"
    global_mesg_num, num_fields = self._read_struct("HB", endian=endian)
    mesg_type = MESSAGE_TYPES.get(global_mesg_num)
    field_defs = []

    for _ in range(num_fields):
        field_def_num, field_size, base_type_num = self._read_struct(
            "3B", endian=endian
        )
        field = mesg_type.fields.get(field_def_num) if mesg_type else None
        base_type = BASE_TYPES.get(base_type_num, _BASE_TYPE_BYTE)

        if (field_size % base_type.size) != 0:
            base_type = _BASE_TYPE_BYTE  # fall back instead of raising

        if field and field.components:
            for component in field.components:
                if component.accumulate:
                    accumulators = self._accumulators.setdefault(
                        global_mesg_num, {}
                    )
                    accumulators[component.def_num] = 0

        field_defs.append(
            FieldDefinition(
                field=field,
                def_num=field_def_num,
                base_type=base_type,
                size=field_size,
            )
        )

    dev_field_defs = []
    if header.is_developer_data:
        num_dev_fields = self._read_struct("B", endian=endian)
        for _ in range(num_dev_fields):
            field_def_num, field_size, dev_data_index = self._read_struct(
                "3B", endian=endian
            )
            field = get_dev_type(dev_data_index, field_def_num)
            dev_field_defs.append(
                DevFieldDefinition(
                    field=field,
                    dev_data_index=dev_data_index,
                    def_num=field_def_num,
                    size=field_size,
                )
            )

    def_mesg = DefinitionMessage(
        header=header,
        endian=endian,
        mesg_type=mesg_type,
        mesg_num=global_mesg_num,
        field_defs=field_defs,
        dev_field_defs=dev_field_defs,
    )
    self._local_mesgs[header.local_mesg_num] = def_mesg
    return def_mesg


FitFile._parse_definition_message = _lenient_parse_definition_message

# FIT sport enum → human-readable activity type
# Reference: FIT SDK Profile.xlsx sport enum
SPORT_MAP = {
    "running": "running",
    "cycling": "cycling",
    "swimming": "swimming",
    "walking": "walking",
    "hiking": "hiking",
    "fitness_equipment": "strength_training",
    "training": "strength_training",
    "rock_climbing": "climbing",
    "rowing": "rowing",
    "mountaineering": "mountaineering",
    "paddling": "paddling",
    "stand_up_paddleboarding": "stand_up_paddleboarding",
    "surfing": "surfing",
    "yoga": "yoga",
    "pilates": "pilates",
    "elliptical": "elliptical",
    "transition": "transition",
    "multisport": "multisport",
    "e_biking": "e_biking",
    "alpine_skiing": "alpine_skiing",
    "cross_country_skiing": "cross_country_skiing",
    "snowboarding": "snowboarding",
    "generic": "other",
}

# Sub-sport overrides (more specific than the sport field)
SUB_SPORT_OVERRIDES = {
    ("cycling", "indoor_cycling"): "indoor_cycling",
    ("running", "treadmill"): "treadmill_running",
    ("running", "trail"): "trail_running",
    ("swimming", "open_water"): "open_water_swimming",
    ("swimming", "lap_swimming"): "lap_swimming",
    ("fitness_equipment", "strength_training"): "strength_training",
    ("fitness_equipment", "cardio_training"): "cardio_training",
    ("walking", "casual_walking"): "walking",
}


def fit_timestamp_to_utc_iso(dt: datetime) -> str:
    """Convert a fitparse datetime (UTC but naive) to ISO8601 with +00:00."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "+00:00")  # ensure consistent format


def derive_intensity(avg_hr) -> str | None:
    """Derive intensity from average heart rate."""
    if avg_hr is None:
        return None
    if avg_hr < 120:
        return "low"
    elif avg_hr < 150:
        return "moderate"
    elif avg_hr < 170:
        return "high"
    else:
        return "very_high"


def map_activity_type(sport: str | None, sub_sport: str | None) -> str:
    """Map FIT sport/sub_sport to a human-readable activity type."""
    if sport and sub_sport:
        key = (sport, sub_sport)
        if key in SUB_SPORT_OVERRIDES:
            return SUB_SPORT_OVERRIDES[key]
    if sport and sport in SPORT_MAP:
        return SPORT_MAP[sport]
    return sport or "unknown"


def parse_fit_file(fit_path: Path) -> dict | None:
    """Parse a single .fit file and return a workout dict, or None on error."""
    try:
        ff = FitFile(str(fit_path))
        session_data = {}
        for record in ff.get_messages("session"):
            for field in record.fields:
                session_data[field.name] = field.value
    except Exception as e:
        print(f"  ERROR parsing {fit_path.name}: {e}")
        return None

    if not session_data:
        print(f"  SKIP {fit_path.name}: no session record found")
        return None

    # Extract start time
    start_time = session_data.get("start_time")
    if start_time is None:
        print(f"  SKIP {fit_path.name}: no start_time in session")
        return None

    started_at = fit_timestamp_to_utc_iso(start_time)

    # Extract end time
    timestamp = session_data.get("timestamp")
    total_elapsed = session_data.get("total_elapsed_time")

    if timestamp is not None:
        ended_at = fit_timestamp_to_utc_iso(timestamp)
    elif total_elapsed is not None:
        end_dt = start_time + timedelta(seconds=float(total_elapsed))
        ended_at = fit_timestamp_to_utc_iso(end_dt)
    else:
        ended_at = None

    # Activity type
    sport = session_data.get("sport")
    sub_sport = session_data.get("sub_sport")
    activity_type = map_activity_type(sport, sub_sport)

    # Intensity from avg HR
    avg_hr = session_data.get("avg_heart_rate")
    intensity = derive_intensity(avg_hr)

    # Notes: capture useful extra fields as JSON
    notes_fields = {
        "total_distance": session_data.get("total_distance"),
        "total_calories": session_data.get("total_calories"),
        "avg_heart_rate": session_data.get("avg_heart_rate"),
        "max_heart_rate": session_data.get("max_heart_rate"),
        "avg_speed": session_data.get("enhanced_avg_speed") or session_data.get("avg_speed"),
        "max_speed": session_data.get("enhanced_max_speed") or session_data.get("max_speed"),
        "total_ascent": session_data.get("total_ascent"),
        "total_descent": session_data.get("total_descent"),
        "total_elapsed_time": session_data.get("total_elapsed_time"),
        "sport": sport,
        "sub_sport": sub_sport if sub_sport and sub_sport != "generic" else None,
        "avg_running_cadence": session_data.get("avg_running_cadence"),
        "avg_power": session_data.get("avg_power"),
        "total_strides": session_data.get("total_strides"),
        "source_file": fit_path.name,
    }
    # Filter out None values
    notes = {k: v for k, v in notes_fields.items() if v is not None}
    notes_json = json.dumps(notes)

    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "activity_type": activity_type,
        "intensity": intensity,
        "notes": notes_json,
    }


def main():
    parser = argparse.ArgumentParser(description="Import .fit files into TypeOneZen workouts table")
    parser.add_argument(
        "--dir",
        type=str,
        default=str(Path.home() / "TypeOneZen" / "data" / "imports" / "fit"),
        help="Directory containing .fit files",
    )
    args = parser.parse_args()

    fit_dir = Path(args.dir).expanduser().resolve()
    db_path = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"

    if not fit_dir.is_dir():
        print(f"ERROR: Directory not found: {fit_dir}")
        return

    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        return

    fit_files = sorted(fit_dir.glob("*.fit"))
    print(f"Found {len(fit_files)} .fit files in {fit_dir}\n")

    if not fit_files:
        print("No .fit files to process.")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    inserted = 0
    skipped = 0
    errors = 0
    activity_counts: dict[str, int] = {}
    dates: list[str] = []

    try:
        for fit_path in fit_files:
            workout = parse_fit_file(fit_path)
            if workout is None:
                errors += 1
                continue

            # Check for duplicate
            cursor.execute(
                "SELECT COUNT(*) FROM workouts WHERE started_at = ?",
                (workout["started_at"],),
            )
            if cursor.fetchone()[0] > 0:
                print(f"  SKIP (duplicate): {fit_path.name}")
                skipped += 1
                continue

            cursor.execute(
                """INSERT INTO workouts (started_at, ended_at, activity_type, intensity, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    workout["started_at"],
                    workout["ended_at"],
                    workout["activity_type"],
                    workout["intensity"],
                    workout["notes"],
                ),
            )
            inserted += 1
            dates.append(workout["started_at"])
            act = workout["activity_type"]
            activity_counts[act] = activity_counts.get(act, 0) + 1
            print(f"  INSERT: {fit_path.name} → {act} ({workout['intensity'] or 'no HR'})")

        conn.commit()
        print(f"\n{'=' * 50}")
        print(f"SUMMARY")
        print(f"{'=' * 50}")
        print(f"Total .fit files found:  {len(fit_files)}")
        print(f"Workouts inserted:       {inserted}")
        print(f"Skipped (duplicate):     {skipped}")
        print(f"Errors:                  {errors}")
        if dates:
            print(f"Date range:              {min(dates)[:10]} to {max(dates)[:10]}")
        if activity_counts:
            print(f"\nActivity breakdown:")
            for act, count in sorted(activity_counts.items(), key=lambda x: -x[1]):
                print(f"  {act}: {count}")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR during import, rolled back: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
