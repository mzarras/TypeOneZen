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

from pydexcom import Dexcom, Region

from db import get_db

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
    username = os.getenv("DEXCOM_USERNAME")
    password = os.getenv("DEXCOM_PASSWORD")
    outside_us = os.getenv("DEXCOM_OUTSIDE_US", "false").lower() == "true"

    if not username or not password:
        logger.error("DEXCOM_USERNAME and DEXCOM_PASSWORD must be set in .env")
        print("Error: missing Dexcom credentials. See .env.example.")
        sys.exit(1)

    # Connect to Dexcom Share
    region = Region.OUS if outside_us else Region.US
    logger.info("Connecting to Dexcom Share (region=%s)", region)
    dexcom = Dexcom(username=username, password=password, region=region)

    # Fetch the latest reading
    reading = dexcom.get_current_glucose_reading()
    if reading is None:
        logger.warning("No glucose reading returned from Dexcom")
        print("No reading available from Dexcom.")
        sys.exit(0)

    # Convert timestamp to ISO8601 UTC string
    timestamp = reading.datetime.isoformat()
    mg_dl = reading.value
    trend = reading.trend_description
    trend_arrow = reading.trend_arrow

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
