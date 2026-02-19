"""
TypeOneZen — Dexcom Share API client.

Pure function to fetch the latest glucose reading. No logging, no DB access,
no side effects. Used by poller.py and tz_query.py.
"""

from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import os

from pydexcom import Dexcom, Region


def fetch_latest_reading(timeout: float = 5.0) -> Optional[dict]:
    """Fetch the latest glucose reading from Dexcom Share.

    Returns a dict with keys: timestamp_iso, glucose_mg_dl, trend, trend_arrow
    Returns None on any error (bad credentials, network, API down, etc.)
    """
    env_path = Path.home() / "TypeOneZen" / ".env"
    load_dotenv(dotenv_path=str(env_path))

    username = os.getenv("DEXCOM_USERNAME")
    password = os.getenv("DEXCOM_PASSWORD")
    outside_us = os.getenv("DEXCOM_OUTSIDE_US", "false").lower() == "true"

    if not username or not password:
        return None

    try:
        region = Region.OUS if outside_us else Region.US
        dexcom = Dexcom(username=username, password=password, region=region)

        reading = dexcom.get_current_glucose_reading()
        if reading is None:
            return None

        return {
            "timestamp_iso": reading.datetime.isoformat(),
            "glucose_mg_dl": reading.value,
            "trend": reading.trend_description,
            "trend_arrow": reading.trend_arrow,
        }
    except Exception:
        return None
