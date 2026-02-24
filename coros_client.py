"""
TypeOneZen — COROS Training Hub API client.

Pure functions to authenticate, list activities, and download FIT files from
COROS Training Hub. No logging, no DB access, no side effects.
Used by parsers/fetch_coros.py.
"""

import hashlib
from typing import Optional

import requests

BASE_URL = "https://teamapi.coros.com"

# COROS sportType → human-readable name (for display in dry-run output)
SPORT_TYPE_MAP = {
    100: "running",
    101: "indoor_running",
    102: "trail_running",
    200: "cycling",
    201: "indoor_cycling",
    300: "swimming",
    301: "open_water_swimming",
    400: "strength_training",
    401: "gym_cardio",
    402: "yoga",
    500: "walking",
    501: "hiking",
    700: "rowing",
    701: "indoor_rowing",
    800: "skiing",
    801: "snowboarding",
    10000: "multisport",
    10001: "triathlon",
}


def sport_type_name(sport_type: int) -> str:
    """Return a human-readable name for a COROS sportType code."""
    return SPORT_TYPE_MAP.get(sport_type, f"sport_{sport_type}")


def login(email: str, password: str) -> str:
    """Authenticate with COROS and return an access token.

    Raises ValueError on auth failure, requests.RequestException on network error.
    """
    pwd_hash = hashlib.md5(password.encode()).hexdigest()
    resp = requests.post(
        f"{BASE_URL}/account/login",
        json={
            "account": email,
            "accountType": 2,
            "pwd": pwd_hash,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "0000" or "data" not in data:
        raise ValueError(f"COROS login failed: {data.get('message', 'unknown error')}")

    return data["data"]["accessToken"]


def get_activities(
    token: str,
    start_day: str,
    end_day: str,
    page: int = 1,
    size: int = 50,
) -> list[dict]:
    """Fetch one page of activities from COROS.

    start_day / end_day are YYYYMMDD strings.
    Returns a list of activity dicts (may be empty on last page).
    """
    resp = requests.get(
        f"{BASE_URL}/activity/query",
        params={
            "startDay": start_day,
            "endDay": end_day,
            "pageNumber": page,
            "size": size,
            "modeList": "",
        },
        headers={"accesstoken": token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "0000" or "data" not in data:
        return []

    return data["data"].get("dataList", [])


def get_all_activities(
    token: str,
    start_day: str,
    end_day: str,
) -> list[dict]:
    """Paginate through all activities in the date range."""
    all_activities: list[dict] = []
    page = 1
    while True:
        batch = get_activities(token, start_day, end_day, page=page)
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return all_activities


def download_activity_fit(
    token: str,
    label_id: str,
    sport_type: int,
) -> Optional[bytes]:
    """Download a FIT file for a single activity.

    Two-step process: first get the download URL, then fetch the file bytes.
    Returns FIT file bytes, or None on failure.
    """
    # Step 1: get the download URL
    resp = requests.get(
        f"{BASE_URL}/activity/detail/download",
        params={
            "labelId": label_id,
            "sportType": sport_type,
            "fileType": 4,  # 4 = FIT format
        },
        headers={"accesstoken": token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "0000" or "data" not in data:
        return None

    file_url = data["data"].get("fileUrl")
    if not file_url:
        return None

    # Step 2: download the actual FIT file
    file_resp = requests.get(file_url, timeout=60)
    file_resp.raise_for_status()
    return file_resp.content
