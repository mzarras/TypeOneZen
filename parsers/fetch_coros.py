#!/usr/bin/env python3
"""
TypeOneZen — COROS activity fetcher.

Authenticates with COROS Training Hub, downloads new activity FIT files,
and imports them into the workouts table via parse_fit.py.

Usage:
    python3 parsers/fetch_coros.py              # fetch last 7 days
    python3 parsers/fetch_coros.py --days 14    # fetch last 14 days
    python3 parsers/fetch_coros.py --dry-run    # list activities without downloading
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coros_client import download_activity_fit, get_all_activities, login, sport_type_name
from parsers.parse_fit import main as run_parse_fit

# -- Paths --
PROJECT_DIR = Path.home() / "TypeOneZen"
LOG_DIR = PROJECT_DIR / "logs"
ENV_PATH = PROJECT_DIR / ".env"
COROS_FIT_DIR = PROJECT_DIR / "data" / "imports" / "fit" / "coros"

# -- Load environment variables --
load_dotenv(dotenv_path=str(ENV_PATH))

# -- Logging setup --
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("fetch_coros")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    str(LOG_DIR / "fetch_coros.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(file_handler)


def activity_date_str(activity: dict) -> str:
    """Format an activity's date for display (from YYYYMMDD integer)."""
    day = str(activity.get("date", ""))
    if len(day) == 8:
        return f"{day[:4]}-{day[4:6]}-{day[6:8]}"
    return day


def fit_filename(activity: dict) -> str:
    """Generate a FIT filename from activity metadata, matching existing convention."""
    label_id = activity.get("labelId", "unknown")
    # startTime is epoch seconds; startTimezone is a TZ offset, not a timestamp
    start_ts = activity.get("startTime")
    if start_ts:
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        prefix = dt.strftime("%Y-%m-%d_%H-%M-%S")
    else:
        day = str(activity.get("date", "00000000"))
        prefix = f"{day[:4]}-{day[4:6]}-{day[6:8]}_00-00-00"
    return f"{prefix}_co_{label_id}.fit"


def existing_label_ids(fit_dir: Path) -> set[str]:
    """Scan a directory for existing COROS FIT files and return their label IDs."""
    ids: set[str] = set()
    if not fit_dir.is_dir():
        return ids
    for f in fit_dir.glob("*_co_*.fit"):
        # filename pattern: YYYY-MM-DD_HH-MM-SS_co_<labelId>.fit
        parts = f.stem.split("_co_")
        if len(parts) == 2:
            ids.add(parts[1])
    return ids


def fetch(days: int, dry_run: bool) -> None:
    email = os.getenv("COROS_EMAIL")
    password = os.getenv("COROS_PASSWORD")

    if not email or not password:
        logger.error("COROS_EMAIL and COROS_PASSWORD must be set in .env")
        print("Error: COROS_EMAIL and COROS_PASSWORD must be set in .env")
        sys.exit(1)

    # Authenticate
    logger.info("Authenticating with COROS Training Hub")
    print("Authenticating with COROS...")
    try:
        token = login(email, password)
    except Exception as e:
        logger.error("COROS login failed: %s", e)
        print(f"Error: COROS login failed: {e}")
        sys.exit(1)
    logger.info("Authenticated successfully")
    print("Authenticated successfully\n")

    # Date range
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    start_day = start_date.strftime("%Y%m%d")
    end_day = end_date.strftime("%Y%m%d")

    logger.info("Fetching activities from %s to %s", start_day, end_day)
    print(f"Fetching activities from {start_day} to {end_day}...")

    # Fetch activity list
    try:
        activities = get_all_activities(token, start_day, end_day)
    except Exception as e:
        logger.error("Failed to fetch activities: %s", e)
        print(f"Error: Failed to fetch activities: {e}")
        sys.exit(1)

    print(f"Found {len(activities)} activities\n")
    logger.info("Found %d activities", len(activities))

    if not activities:
        print("No activities to process.")
        return

    # Check which FIT files we already have (by labelId in filename)
    # Also scan the parent fit/ directory for legacy files
    COROS_FIT_DIR.mkdir(parents=True, exist_ok=True)
    parent_fit_dir = COROS_FIT_DIR.parent
    known_ids = existing_label_ids(COROS_FIT_DIR) | existing_label_ids(parent_fit_dir)

    downloaded = 0
    skipped = 0
    errors = 0

    for act in activities:
        label_id = str(act.get("labelId", ""))
        sport = act.get("sportType", 0)
        name = act.get("name") or sport_type_name(sport)
        date_str = activity_date_str(act)
        distance_km = (act.get("distance") or 0) / 1000
        duration_min = (act.get("totalTime") or 0) / 60

        display = f"{date_str}  {name:<20s}  {distance_km:6.1f} km  {duration_min:5.0f} min"

        if label_id in known_ids:
            print(f"  SKIP (exists): {display}")
            logger.debug("Skip existing labelId=%s", label_id)
            skipped += 1
            continue

        if dry_run:
            print(f"  WOULD DOWNLOAD: {display}")
            continue

        # Download FIT file
        fname = fit_filename(act)
        logger.info("Downloading %s (labelId=%s)", fname, label_id)
        try:
            fit_bytes = download_activity_fit(token, label_id, sport)
        except Exception as e:
            logger.error("Failed to download labelId=%s: %s", label_id, e)
            print(f"  ERROR downloading: {display} — {e}")
            errors += 1
            continue

        if fit_bytes is None:
            logger.warning("No FIT data returned for labelId=%s", label_id)
            print(f"  ERROR (no data): {display}")
            errors += 1
            continue

        # Save to disk
        out_path = COROS_FIT_DIR / fname
        out_path.write_bytes(fit_bytes)
        logger.info("Saved %s (%d bytes)", out_path.name, len(fit_bytes))
        print(f"  DOWNLOADED: {display}  → {fname}")
        downloaded += 1

    # Summary
    print(f"\n{'=' * 55}")
    print("FETCH SUMMARY")
    print(f"{'=' * 55}")
    print(f"Activities found:    {len(activities)}")
    print(f"Downloaded:          {downloaded}")
    print(f"Skipped (existing):  {skipped}")
    if errors:
        print(f"Errors:              {errors}")
    if dry_run:
        would = len(activities) - skipped
        print(f"Would download:      {would}")
        print("\nRe-run without --dry-run to download.")
        return

    # Import downloaded FIT files via parse_fit.py
    if downloaded > 0:
        print(f"\nImporting {downloaded} new FIT files from {COROS_FIT_DIR}...")
        logger.info("Running parse_fit on %s", COROS_FIT_DIR)
        # Monkey-patch sys.argv for parse_fit.main()
        orig_argv = sys.argv
        sys.argv = ["parse_fit.py", "--dir", str(COROS_FIT_DIR)]
        try:
            run_parse_fit()
        except SystemExit:
            pass
        finally:
            sys.argv = orig_argv
    else:
        print("\nNo new files to import.")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch COROS activities and import FIT files into TypeOneZen"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List activities without downloading",
    )
    args = parser.parse_args()

    fetch(days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        print(f"Error: {exc}")
        sys.exit(1)
