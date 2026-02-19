"""
Parse Glooko CSV export files and import into TypeOneZen database.

Handles: CGM glucose, manual BG, bolus insulin, basal insulin.
Skips: daily summaries (insulin_data), alarms, carbs-only, manual data with no rows.

Glooko CSV format:
  Row 1: metadata (Name:..., Date Range:...)
  Row 2: column headers
  Row 3+: data

Timestamps in Glooko exports are local time (no timezone).
We assume America/New_York and convert to UTC for storage.
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Add project root to path so we can import db
sys.path.insert(0, str(Path.home() / "TypeOneZen"))
from db import get_db

LOCAL_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")


def parse_glooko_timestamp(ts_str: str) -> str:
    """Convert a Glooko local-time timestamp to ISO8601 UTC string.

    Glooko format: '2026-02-18 16:05'
    Output: '2026-02-18T21:05:00+00:00' (UTC)
    """
    dt_naive = datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M")
    dt_local = dt_naive.replace(tzinfo=LOCAL_TZ)
    dt_utc = dt_local.astimezone(UTC_TZ)
    return dt_utc.isoformat()


def read_glooko_csv(filepath: Path) -> tuple[list[str], list[dict]]:
    """Read a Glooko CSV, skip the metadata row, return (headers, rows).

    Returns empty lists if the file has no data rows.
    """
    with open(filepath, "r", encoding="utf-8-sig") as f:
        # Skip metadata row (Name:..., Date Range:...)
        f.readline()
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)
    return headers, rows


def identify_file_type(filepath: Path, headers: list[str]) -> str:
    """Identify the type of Glooko CSV based on filename and headers."""
    name = filepath.name.lower()

    if "cgm_data" in name:
        return "cgm"
    if "bg_data" in name:
        return "bg"
    if "bolus_data" in name:
        return "bolus"
    if "basal_data" in name:
        return "basal"
    # insulin_data is daily summary — skip
    if "insulin_data" in name:
        return "summary"
    if "alarms_data" in name:
        return "alarms"
    if "carbs_data" in name:
        return "carbs"

    # Fallback: check headers
    if "CGM Glucose Value (mg/dl)" in headers:
        return "cgm"
    if "Glucose Value (mg/dl)" in headers:
        return "bg"
    if "Insulin Delivered (U)" in headers and "Carbs Input (g)" in headers:
        return "bolus"
    if "Rate" in headers and "Duration (minutes)" in headers:
        return "basal"
    if "Total Bolus (U)" in headers:
        return "summary"

    return "unknown"


def discover_csv_files(directory: Path) -> list[Path]:
    """Recursively find all CSV files in the directory."""
    return sorted(directory.rglob("*.csv"))


def load_existing_glucose_timestamps(conn) -> set[str]:
    """Load all existing glucose_readings timestamps, normalized to minute-level UTC.

    Existing Dexcom data has format like '2026-02-18T15:55:36.327000-05:00'.
    We normalize to 'YYYY-MM-DDTHH:MM' in UTC for comparison with Glooko
    minute-level timestamps.
    """
    cursor = conn.execute("SELECT timestamp FROM glucose_readings")
    existing = set()
    for row in cursor:
        ts = row[0]
        try:
            dt = datetime.fromisoformat(ts)
            dt_utc = dt.astimezone(UTC_TZ)
            # Normalize to minute-level for comparison
            existing.add(dt_utc.strftime("%Y-%m-%dT%H:%M"))
        except (ValueError, TypeError):
            pass
    return existing


def load_existing_insulin_timestamps(conn) -> set[tuple[str, str]]:
    """Load existing insulin_doses as (timestamp_minute_utc, type) tuples."""
    cursor = conn.execute("SELECT timestamp, type FROM insulin_doses")
    existing = set()
    for row in cursor:
        ts, itype = row[0], row[1]
        try:
            dt = datetime.fromisoformat(ts)
            dt_utc = dt.astimezone(UTC_TZ)
            existing.add((dt_utc.strftime("%Y-%m-%dT%H:%M"), itype))
        except (ValueError, TypeError):
            pass
    return existing


def import_cgm(rows: list[dict], conn, existing_glucose: set[str], source_file: str) -> tuple[int, int]:
    """Import CGM glucose readings. Returns (inserted, skipped)."""
    inserted = skipped = 0
    for row in rows:
        ts_raw = row.get("Timestamp", "").strip()
        glucose_raw = row.get("CGM Glucose Value (mg/dl)", "").strip()
        if not ts_raw or not glucose_raw:
            continue

        try:
            glucose = int(float(glucose_raw))
        except (ValueError, TypeError):
            continue

        ts_utc = parse_glooko_timestamp(ts_raw)
        # Minute-level key for dedup
        dt_utc = datetime.fromisoformat(ts_utc)
        minute_key = dt_utc.strftime("%Y-%m-%dT%H:%M")

        if minute_key in existing_glucose:
            skipped += 1
            continue

        conn.execute(
            "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, source) VALUES (?, ?, ?)",
            (ts_utc, glucose, "glooko"),
        )
        existing_glucose.add(minute_key)
        inserted += 1

    return inserted, skipped


def import_bg(rows: list[dict], conn, existing_glucose: set[str]) -> tuple[int, int]:
    """Import manual BG readings. Returns (inserted, skipped)."""
    inserted = skipped = 0
    for row in rows:
        ts_raw = row.get("Timestamp", "").strip()
        glucose_raw = row.get("Glucose Value (mg/dl)", "").strip()
        if not ts_raw or not glucose_raw:
            continue

        try:
            glucose = int(float(glucose_raw))
        except (ValueError, TypeError):
            continue

        ts_utc = parse_glooko_timestamp(ts_raw)
        dt_utc = datetime.fromisoformat(ts_utc)
        minute_key = dt_utc.strftime("%Y-%m-%dT%H:%M")

        if minute_key in existing_glucose:
            skipped += 1
            continue

        conn.execute(
            "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, source) VALUES (?, ?, ?)",
            (ts_utc, glucose, "glooko"),
        )
        existing_glucose.add(minute_key)
        inserted += 1

    return inserted, skipped


def import_bolus(rows: list[dict], conn, existing_insulin: set[tuple[str, str]]) -> tuple[int, int, int]:
    """Import bolus insulin doses. Returns (bolus_inserted, correction_inserted, skipped).

    Bolus CSV columns:
      Timestamp, Insulin Type, Blood Glucose Input (mg/dl), Carbs Input (g),
      Carbs Ratio, Insulin Delivered (U), Initial Delivery (U),
      Extended Delivery (U), Serial Number

    Classification:
      - If BG input > 0 and carbs = 0 → correction bolus
      - Otherwise → regular bolus (meal bolus, or combo meal+correction)
    """
    bolus_inserted = correction_inserted = skipped = 0
    for row in rows:
        ts_raw = row.get("Timestamp", "").strip()
        units_raw = row.get("Insulin Delivered (U)", "").strip()
        if not ts_raw or not units_raw:
            continue

        try:
            units = float(units_raw)
        except (ValueError, TypeError):
            continue

        if units <= 0:
            continue

        # Classify bolus type
        bg_input = float(row.get("Blood Glucose Input (mg/dl)", "0") or "0")
        carbs_input = float(row.get("Carbs Input (g)", "0") or "0")

        if bg_input > 0 and carbs_input == 0:
            dose_type = "correction"
        else:
            dose_type = "bolus"

        ts_utc = parse_glooko_timestamp(ts_raw)
        dt_utc = datetime.fromisoformat(ts_utc)
        minute_key = dt_utc.strftime("%Y-%m-%dT%H:%M")

        if (minute_key, dose_type) in existing_insulin:
            skipped += 1
            continue

        # Build notes with context
        notes_parts = []
        if bg_input > 0:
            notes_parts.append(f"BG={int(bg_input)}")
        if carbs_input > 0:
            notes_parts.append(f"carbs={int(carbs_input)}g")
        carb_ratio = row.get("Carbs Ratio", "").strip()
        if carb_ratio:
            notes_parts.append(f"ratio=1:{carb_ratio}")
        notes = ", ".join(notes_parts) if notes_parts else None

        conn.execute(
            "INSERT INTO insulin_doses (timestamp, units, type, notes) VALUES (?, ?, ?, ?)",
            (ts_utc, units, dose_type, notes),
        )
        existing_insulin.add((minute_key, dose_type))

        if dose_type == "correction":
            correction_inserted += 1
        else:
            bolus_inserted += 1

    return bolus_inserted, correction_inserted, skipped


def import_basal(rows: list[dict], conn, existing_insulin: set[tuple[str, str]]) -> tuple[int, int]:
    """Import basal insulin data. Returns (inserted, skipped).

    Basal CSV columns:
      Timestamp, Insulin Type, Duration (minutes), Percentage (%),
      Rate, Insulin Delivered (U), Serial Number

    The "Rate" column is the basal rate in U/hr.
    "Duration (minutes)" is how long that rate was active.
    "Insulin Delivered (U)" is usually empty for scheduled basals.
    We store the rate as units (U/hr) and note the duration.
    For suspend events (Rate=0), we still record them.
    """
    inserted = skipped = 0
    for row in rows:
        ts_raw = row.get("Timestamp", "").strip()
        if not ts_raw:
            continue

        insulin_type = row.get("Insulin Type", "").strip()
        rate_raw = row.get("Rate", "").strip()
        duration_raw = row.get("Duration (minutes)", "").strip()
        delivered_raw = row.get("Insulin Delivered (U)", "").strip()

        # Calculate actual insulin delivered
        # If "Insulin Delivered" is provided, use it; otherwise compute from rate * duration
        units = None
        if delivered_raw:
            try:
                units = float(delivered_raw)
            except (ValueError, TypeError):
                pass

        if units is None and rate_raw and duration_raw:
            try:
                rate = float(rate_raw)
                duration_min = float(duration_raw)
                # rate is U/hr, duration is minutes → delivered = rate * (duration / 60)
                units = round(rate * (duration_min / 60.0), 4)
            except (ValueError, TypeError):
                continue

        if units is None:
            continue

        ts_utc = parse_glooko_timestamp(ts_raw)
        dt_utc = datetime.fromisoformat(ts_utc)
        minute_key = dt_utc.strftime("%Y-%m-%dT%H:%M")

        if (minute_key, "basal") in existing_insulin:
            skipped += 1
            continue

        notes = f"{insulin_type}, rate={rate_raw} U/hr, duration={duration_raw} min"

        conn.execute(
            "INSERT INTO insulin_doses (timestamp, units, type, notes) VALUES (?, ?, ?, ?)",
            (ts_utc, units, "basal", notes),
        )
        existing_insulin.add((minute_key, "basal"))
        inserted += 1

    return inserted, skipped


def main():
    parser = argparse.ArgumentParser(description="Import Glooko CSV exports into TypeOneZen")
    parser.add_argument(
        "--dir",
        type=str,
        default=str(Path.home() / "TypeOneZen" / "data" / "imports" / "glooko"),
        help="Directory containing Glooko CSV files",
    )
    args = parser.parse_args()

    import_dir = Path(args.dir)
    if not import_dir.exists():
        print(f"Error: directory not found: {import_dir}")
        sys.exit(1)

    csv_files = discover_csv_files(import_dir)
    if not csv_files:
        print(f"No CSV files found in {import_dir}")
        sys.exit(1)

    print(f"Found {len(csv_files)} CSV files in {import_dir}\n")

    # Categorize files
    file_map: dict[str, list[tuple[Path, list[str], list[dict]]]] = {}
    for fp in csv_files:
        headers, rows = read_glooko_csv(fp)
        ftype = identify_file_type(fp, headers)
        file_map.setdefault(ftype, []).append((fp, headers, rows))
        data_rows = len(rows)
        print(f"  {fp.relative_to(import_dir)}  →  {ftype}  ({data_rows} rows)")

    print()

    # Connect to DB
    conn = get_db()

    # Load existing data for dedup
    existing_glucose = load_existing_glucose_timestamps(conn)
    existing_insulin = load_existing_insulin_timestamps(conn)
    print(f"Existing DB: {len(existing_glucose)} glucose timestamps, "
          f"{len(existing_insulin)} insulin timestamps\n")

    # Counters
    total_glucose_inserted = 0
    total_glucose_skipped = 0
    total_bolus_inserted = 0
    total_correction_inserted = 0
    total_bolus_skipped = 0
    total_basal_inserted = 0
    total_basal_skipped = 0

    try:
        # Import CGM data
        for fp, headers, rows in file_map.get("cgm", []):
            print(f"Importing CGM from {fp.name}...")
            ins, skip = import_cgm(rows, conn, existing_glucose, fp.name)
            total_glucose_inserted += ins
            total_glucose_skipped += skip
            print(f"  → {ins} inserted, {skip} skipped (duplicate)")

        # Import manual BG data
        for fp, headers, rows in file_map.get("bg", []):
            print(f"Importing BG from {fp.name}...")
            ins, skip = import_bg(rows, conn, existing_glucose)
            total_glucose_inserted += ins
            total_glucose_skipped += skip
            print(f"  → {ins} inserted, {skip} skipped (duplicate)")

        # Import bolus data
        for fp, headers, rows in file_map.get("bolus", []):
            print(f"Importing bolus from {fp.name}...")
            b_ins, c_ins, skip = import_bolus(rows, conn, existing_insulin)
            total_bolus_inserted += b_ins
            total_correction_inserted += c_ins
            total_bolus_skipped += skip
            print(f"  → {b_ins} bolus + {c_ins} correction inserted, {skip} skipped")

        # Import basal data
        for fp, headers, rows in file_map.get("basal", []):
            print(f"Importing basal from {fp.name}...")
            ins, skip = import_basal(rows, conn, existing_insulin)
            total_basal_inserted += ins
            total_basal_skipped += skip
            print(f"  → {ins} inserted, {skip} skipped")

        # Skipped file types
        for ftype in ["summary", "alarms", "carbs", "unknown"]:
            for fp, headers, rows in file_map.get(ftype, []):
                print(f"Skipping {fp.name} ({ftype}, {len(rows)} rows)")

        conn.commit()
        print("\nTransaction committed successfully.")

    except Exception as e:
        conn.rollback()
        print(f"\nError during import — transaction rolled back: {e}")
        raise
    finally:
        conn.close()

    # Final summary
    print("\n" + "=" * 60)
    print("IMPORT SUMMARY")
    print("=" * 60)
    print(f"Glucose readings:  {total_glucose_inserted} inserted, "
          f"{total_glucose_skipped} skipped (duplicate)")
    print(f"Bolus doses:       {total_bolus_inserted} inserted")
    print(f"Correction doses:  {total_correction_inserted} inserted")
    print(f"Bolus skipped:     {total_bolus_skipped} (duplicate)")
    print(f"Basal doses:       {total_basal_inserted} inserted")
    print(f"Basal skipped:     {total_basal_skipped} (duplicate)")
    print("=" * 60)


if __name__ == "__main__":
    main()
