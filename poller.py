"""
TypeOneZen — Dexcom Share API poller.

Fetches the latest glucose reading from Dexcom Share and stores it in SQLite.
Designed to be run every 5 minutes via cron or a scheduler.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
import os

from db import get_db
from dexcom_client import fetch_latest_reading

# -- Paths --
PROJECT_DIR = Path.home() / "TypeOneZen"
LOG_DIR = PROJECT_DIR / "logs"
ENV_PATH = PROJECT_DIR / ".env"

# -- Load environment variables --
load_dotenv(dotenv_path=str(ENV_PATH))

# -- Logging setup --
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("poller")
logger.setLevel(logging.DEBUG)

# Rotating file handler: 5 MB max, keep 3 backups
file_handler = RotatingFileHandler(
    str(LOG_DIR / "poller.log"),
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(file_handler)


def poll() -> None:
    """Fetch the latest Dexcom reading and store it if new."""
    logger.info("Fetching latest reading from Dexcom Share")
    result = fetch_latest_reading()

    if result is None:
        logger.error("Failed to fetch reading from Dexcom (credentials, network, or API error)")
        print("Error: could not fetch reading from Dexcom.")
        sys.exit(1)

    timestamp = result["timestamp_iso"]
    mg_dl = result["glucose_mg_dl"]
    trend = result["trend"]
    trend_arrow = result["trend_arrow"]

    logger.info(
        "Fetched reading: %d mg/dL %s (%s) at %s",
        mg_dl, trend_arrow, trend, timestamp,
    )

    # Check for duplicate
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM glucose_readings WHERE timestamp = ?",
        (timestamp,),
    )
    existing = cursor.fetchone()

    if existing:
        logger.info("Duplicate reading — already stored for %s", timestamp)
        print("No new reading (latest already stored)")
        conn.close()
        sys.exit(0)

    # Insert new reading
    cursor.execute(
        """
        INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend, trend_arrow, source)
        VALUES (?, ?, ?, ?, 'dexcom')
        """,
        (timestamp, mg_dl, trend, trend_arrow),
    )
    conn.commit()
    conn.close()

    logger.info("Stored reading: %d mg/dL %s at %s", mg_dl, trend_arrow, timestamp)
    print(f"Reading stored: {mg_dl} mg/dL {trend_arrow} at {timestamp}")


if __name__ == "__main__":
    try:
        poll()
    except Exception as exc:
        logger.exception("Fatal error during polling: %s", exc)
        print(f"Error: {exc}")
        sys.exit(1)
