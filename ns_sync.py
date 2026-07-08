"""
TypeOneZen — Nightscout sync.

Pulls CGM entries and pump treatments from a Nightscout site into SQLite.
Designed to be run every 5 minutes via cron (see run_ns_sync.sh).

What gets synced:
- Entries (CGM readings)      → glucose_readings  (source='nightscout')
- Bolus treatments            → insulin_doses     (type='bolus' or 'correction')
- Temp basal treatments       → insulin_doses     (type='basal')
- Carb treatments             → meals             (source='nightscout')

Idempotency: every synced row stores the Nightscout record ID in the
`source_id` column (unique-indexed where not null), so re-running the sync —
including full backfills — never creates duplicates. Glucose entries are
additionally deduplicated against other sources (Dexcom poller, Glooko) by
minute-level UTC timestamp, matching the parse_glooko.py convention.

Closed-loop note (Trio / Omnipod 5): the loop delivers SMBs (super micro
boluses), which arrive as many small bolus treatments. That is expected —
they are stored individually as boluses and should be aggregated in
summaries.

This retires the manual Glooko CSV import workflow (parsers/parse_glooko.py
is kept for historical imports).

Usage:
    python3 ns_sync.py                     # incremental sync from stored cursor
    python3 ns_sync.py --since 2026-01-01  # backfill from a date (re-run safe)

Credentials: NIGHTSCOUT_URL and NIGHTSCOUT_TOKEN in .env (read-only token).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os

from db import get_db, ensure_sync_schema

try:
    from nightscout_client import NightscoutClient
    from nightscout_client.exceptions import (
        NightscoutError,
        NightscoutConnectionError,
        NightscoutAuthError,
    )
except ImportError:
    print("Error: nightscout-client is not installed. See requirements.txt "
          "(pip3 install -e ../nightscout-client until the PyPI release).")
    sys.exit(1)

# -- Paths --
PROJECT_DIR = Path.home() / "TypeOneZen"
LOG_DIR = PROJECT_DIR / "logs"
ENV_PATH = PROJECT_DIR / ".env"

# -- Load environment variables --
load_dotenv(dotenv_path=str(ENV_PATH))

# -- Timezones --
UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")

# -- Sync tuning --
OVERLAP_MINUTES = 15          # re-fetch window behind the cursor (late uploads)
DEFAULT_LOOKBACK_HOURS = 24   # first run without a cursor or --since

# -- Cursor keys in sync_state --
CURSOR_ENTRIES = "nightscout_entries"
CURSOR_TREATMENTS = "nightscout_treatments"

# Nightscout direction strings → arrow characters (matches monitor.py arrows)
DIRECTION_ARROWS = {
    "DoubleUp":      "⇈",  # ⇈
    "SingleUp":      "↑",  # ↑
    "FortyFiveUp":   "↗",  # ↗
    "Flat":          "→",  # →
    "FortyFiveDown": "↘",  # ↘
    "SingleDown":    "↓",  # ↓
    "DoubleDown":    "⇊",  # ⇊
}

# -- Logging setup --
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("ns_sync")
logger.setLevel(logging.DEBUG)

# Rotating file handler: 5 MB max, keep 3 backups
file_handler = RotatingFileHandler(
    str(LOG_DIR / "ns_sync.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(file_handler)


# ── Helpers ─────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_iso_utc(ts: str) -> str:
    """Normalize an ISO timestamp string to ISO8601 UTC (repo convention)."""
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def get_cursor(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_cursor(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, utc_now().isoformat()),
    )


def compute_since(conn, cursor_key: str, since_override: str | None) -> str:
    """Determine the `since` bound for a stream.

    --since override wins; otherwise stored cursor minus an overlap window;
    otherwise a default lookback for first runs.
    """
    if since_override:
        return since_override
    cursor = get_cursor(conn, cursor_key)
    if cursor:
        dt = datetime.fromisoformat(cursor)
        return (dt - timedelta(minutes=OVERLAP_MINUTES)).isoformat()
    return (utc_now() - timedelta(hours=DEFAULT_LOOKBACK_HOURS)).isoformat()


def load_existing_glucose_minutes(conn) -> set:
    """Minute-level UTC timestamps of existing glucose readings (all sources).

    Same normalization as parse_glooko.py — prevents double-storing the same
    CGM reading that already arrived via the Dexcom Share poller.
    """
    existing = set()
    for row in conn.execute("SELECT timestamp FROM glucose_readings"):
        try:
            dt = datetime.fromisoformat(row["timestamp"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            existing.add(dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"))
        except (ValueError, TypeError):
            pass
    return existing


def source_id_exists(conn, table: str, source_id: str) -> bool:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE source_id = ? LIMIT 1", (source_id,)
    ).fetchone()
    return row is not None


# ── Sync: entries → glucose_readings ───────────────────────────────

def sync_entries(client, conn, since: str) -> tuple[int, int]:
    """Sync Nightscout entries into glucose_readings. Returns (inserted, skipped)."""
    entries = client.entries(since=since)
    logger.info("Fetched %d entries since %s", len(entries), since)

    existing_minutes = load_existing_glucose_minutes(conn)
    inserted = skipped = 0
    latest = get_cursor(conn, CURSOR_ENTRIES)

    for entry in entries:
        sgv = entry.get("sgv")
        raw_time = entry.get("time")
        if sgv is None or not raw_time:
            continue

        ts_utc = parse_iso_utc(raw_time)
        # Nightscout entries carry no _id in the client contract, so we use a
        # deterministic synthetic ID (one CGM reading per timestamp).
        source_id = f"ns-entry-{ts_utc}"

        if latest is None or ts_utc > latest:
            latest = ts_utc

        if source_id_exists(conn, "glucose_readings", source_id):
            skipped += 1
            continue

        # Cross-source dedup (Dexcom poller / Glooko), minute-level UTC
        minute_key = datetime.fromisoformat(ts_utc).strftime("%Y-%m-%dT%H:%M")
        if minute_key in existing_minutes:
            skipped += 1
            continue

        direction = entry.get("direction")
        conn.execute(
            """
            INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend, trend_arrow, source, source_id)
            VALUES (?, ?, ?, ?, 'nightscout', ?)
            """,
            (ts_utc, int(sgv), direction, DIRECTION_ARROWS.get(direction), source_id),
        )
        existing_minutes.add(minute_key)
        inserted += 1

    if latest:
        set_cursor(conn, CURSOR_ENTRIES, latest)

    return inserted, skipped


# ── Sync: treatments → insulin_doses + meals ───────────────────────

def classify_bolus(treatment: dict) -> str:
    """Classify a bolus treatment as 'bolus' or 'correction'.

    Mirrors the parse_glooko.py convention: a correction has no carbs.
    SMBs from the closed loop stay as plain boluses — they arrive as many
    small boluses, which is expected.
    """
    raw = (treatment.get("raw_event_type") or "").lower()
    carbs = treatment.get("carbs") or 0
    if "correction" in raw and not carbs:
        return "correction"
    return "bolus"


def sync_treatments(client, conn, since: str) -> dict:
    """Sync Nightscout treatments into insulin_doses and meals.

    Returns counts dict: bolus_inserted, correction_inserted, basal_inserted,
    doses_skipped, meals_inserted, meals_skipped.
    """
    treatments = client.treatments(since=since)
    logger.info("Fetched %d treatments since %s", len(treatments), since)

    counts = {
        "bolus_inserted": 0,
        "correction_inserted": 0,
        "basal_inserted": 0,
        "doses_skipped": 0,
        "meals_inserted": 0,
        "meals_skipped": 0,
    }
    latest = get_cursor(conn, CURSOR_TREATMENTS)

    for t in treatments:
        t_id = t.get("id")
        raw_time = t.get("time")
        if not t_id or not raw_time:
            continue

        ts_utc = parse_iso_utc(raw_time)
        if latest is None or ts_utc > latest:
            latest = ts_utc

        event_type = t.get("event_type")
        raw_event_type = t.get("raw_event_type") or event_type or ""
        insulin = t.get("insulin")
        carbs = t.get("carbs")

        # -- Basal (temp basal records from the loop) → insulin_doses --
        if event_type == "basal":
            source_id = f"ns-treatment-{t_id}"
            if source_id_exists(conn, "insulin_doses", source_id):
                counts["doses_skipped"] += 1
                continue

            rate = t.get("rate")
            duration = t.get("duration")
            units = insulin
            if units is None and rate is not None and duration is not None:
                # rate is U/hr, duration is minutes → delivered = rate * (duration / 60)
                units = round(rate * (duration / 60.0), 4)
            if units is None:
                continue

            notes = f"{raw_event_type}, rate={rate} U/hr, duration={duration} min"
            conn.execute(
                """
                INSERT INTO insulin_doses (timestamp, units, type, notes, source_id)
                VALUES (?, ?, 'basal', ?, ?)
                """,
                (ts_utc, units, notes, source_id),
            )
            counts["basal_inserted"] += 1
            continue

        # -- Insulin component (bolus / correction / SMB) → insulin_doses --
        if insulin is not None and insulin > 0:
            source_id = f"ns-treatment-{t_id}"
            if source_id_exists(conn, "insulin_doses", source_id):
                counts["doses_skipped"] += 1
            else:
                dose_type = classify_bolus(t)
                notes_parts = [raw_event_type] if raw_event_type else []
                if carbs:
                    notes_parts.append(f"carbs={carbs:g}g")
                notes = ", ".join(notes_parts) if notes_parts else None

                conn.execute(
                    """
                    INSERT INTO insulin_doses (timestamp, units, type, notes, source_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ts_utc, insulin, dose_type, notes, source_id),
                )
                counts[f"{dose_type}_inserted"] += 1

        # -- Carb component → meals (same treatment may carry both) --
        if carbs is not None and carbs > 0:
            source_id = f"ns-treatment-{t_id}"
            if source_id_exists(conn, "meals", source_id):
                counts["meals_skipped"] += 1
            else:
                conn.execute(
                    """
                    INSERT INTO meals (timestamp, description, carbs_g, source, notes, source_id)
                    VALUES (?, ?, ?, 'nightscout', ?, ?)
                    """,
                    (ts_utc, f"{carbs:g}g carbs (pump-logged)", carbs,
                     raw_event_type or None, source_id),
                )
                counts["meals_inserted"] += 1

    if latest:
        set_cursor(conn, CURSOR_TREATMENTS, latest)

    return counts


# ── Main ────────────────────────────────────────────────────────────

def sync(client, conn, since_override: str | None = None) -> dict:
    """Run one full sync pass (entries + treatments). Returns counts dict."""
    ensure_sync_schema(conn)

    entries_since = compute_since(conn, CURSOR_ENTRIES, since_override)
    glucose_inserted, glucose_skipped = sync_entries(client, conn, entries_since)

    treatments_since = compute_since(conn, CURSOR_TREATMENTS, since_override)
    counts = sync_treatments(client, conn, treatments_since)

    counts["glucose_inserted"] = glucose_inserted
    counts["glucose_skipped"] = glucose_skipped

    conn.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Nightscout entries and treatments into TypeOneZen")
    parser.add_argument("--since", type=str, metavar="YYYY-MM-DD",
                        help="Backfill from this date (overrides the stored cursor)")
    args = parser.parse_args()

    since_override = None
    if args.since:
        try:
            # Interpret the date as local (NY) midnight, store bound as UTC
            dt_local = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=NY)
            since_override = dt_local.astimezone(UTC).isoformat()
        except ValueError:
            print(f"Error: invalid --since date '{args.since}'. Use YYYY-MM-DD.")
            sys.exit(1)

    if not os.getenv("NIGHTSCOUT_URL"):
        logger.error("NIGHTSCOUT_URL not set in .env")
        print("Error: NIGHTSCOUT_URL not set in .env (see .env.example).")
        sys.exit(1)

    logger.info("Starting Nightscout sync%s",
                f" (backfill since {args.since})" if args.since else "")

    conn = get_db()
    try:
        client = NightscoutClient.from_env()
        counts = sync(client, conn, since_override)
    except NightscoutConnectionError as exc:
        logger.error("Nightscout unreachable: %s", exc)
        print(f"Error: Nightscout unreachable — {exc}")
        sys.exit(1)
    except NightscoutAuthError as exc:
        logger.error("Nightscout auth failed (check NIGHTSCOUT_TOKEN): %s", exc)
        print(f"Error: Nightscout auth failed — {exc}")
        sys.exit(1)
    except NightscoutError as exc:
        logger.error("Nightscout API error: %s", exc)
        print(f"Error: Nightscout API error — {exc}")
        sys.exit(1)
    finally:
        conn.close()

    logger.info("Sync complete: %s", counts)
    print(f"Glucose:  {counts['glucose_inserted']} inserted, "
          f"{counts['glucose_skipped']} skipped (duplicate)")
    print(f"Insulin:  {counts['bolus_inserted']} bolus + "
          f"{counts['correction_inserted']} correction + "
          f"{counts['basal_inserted']} basal inserted, "
          f"{counts['doses_skipped']} skipped (duplicate)")
    print(f"Meals:    {counts['meals_inserted']} inserted, "
          f"{counts['meals_skipped']} skipped (duplicate)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Fatal error during sync: %s", exc)
        print(f"Error: {exc}")
        sys.exit(1)
