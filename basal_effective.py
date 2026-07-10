"""
TypeOneZen — effective temp-basal delivery math.

Temp-basal treatments from the Trio closed loop arrive with a SCHEDULED
duration (usually 30 min, sometimes 60/90/120), but the loop supersedes
each temp basal with the next one after only a few minutes on average.
Units actually delivered by a temp basal are therefore:

    rate (U/hr) x min(scheduled_duration, minutes_until_next_temp_basal) / 60

The last (most recent) temp basal has no successor yet; counting it in
full would overcount an in-progress basal, so it is capped at the time
elapsed so far:

    rate (U/hr) x min(scheduled_duration, minutes_elapsed_until_now) / 60

STORAGE CONVENTION: `insulin_doses` rows with type='basal' store these
EFFECTIVE units (ns_sync.py truncates the preceding row whenever a new
temp basal arrives, and parsers/backfill_basal_effective.py performed the
one-time historical rewrite). Consumers can therefore use a plain
SUM(units) over basal rows. Each row's `notes` field keeps the raw
schedule ("Temp Basal, rate=0.75 U/hr, duration=30.0 min") so the
effective value can always be recomputed deterministically.

NOTE: the OpenClaw skill scripts (examples/openclaw-skill/scripts/) must
stay self-contained ($TZ_HOME only, no repo imports) — they rely on the
stored-effective convention rather than importing this module.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Matches the notes ns_sync.py writes: "Temp Basal, rate=0.75 U/hr, duration=30.0 min"
RE_RATE = re.compile(r"rate\s*=\s*(\d+(?:\.\d+)?)\s*U/hr", re.IGNORECASE)
RE_DURATION = re.compile(r"duration\s*=\s*(\d+(?:\.\d+)?)\s*min", re.IGNORECASE)


def parse_rate_duration(notes):
    """Extract (rate_u_per_hr, scheduled_duration_min) from a basal row's notes.

    Returns (rate, duration) floats, or None if either is missing/unparseable.
    """
    if not notes:
        return None
    m_rate = RE_RATE.search(notes)
    m_dur = RE_DURATION.search(notes)
    if not m_rate or not m_dur:
        return None
    return float(m_rate.group(1)), float(m_dur.group(1))


def parse_ts_utc(ts):
    """Parse an ISO8601 timestamp (string or datetime) to an aware UTC datetime."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def effective_units(rate, scheduled_duration_min, minutes_until_next):
    """Units actually delivered by one temp basal before it was superseded.

    rate:                  U/hr
    scheduled_duration_min scheduled duration in minutes
    minutes_until_next:    minutes until the next temp basal started (or, for
                           the current/last one, minutes elapsed until now)
    """
    delivered_min = min(float(scheduled_duration_min), max(0.0, float(minutes_until_next)))
    return round(float(rate) * delivered_min / 60.0, 4)


def compute_effective_units(rows, now=None):
    """Per-row effective delivered units for a time-sorted list of temp basals.

    rows: sequence of (timestamp, rate, scheduled_duration_min) tuples,
          sorted ascending by timestamp. Timestamps may be ISO8601 strings
          or datetimes (naive treated as UTC).
    now:  aware datetime used to cap the last/in-progress row
          (defaults to the current UTC time).

    Returns a list of effective units (floats), one per input row.
    """
    if not rows:
        return []
    if now is None:
        now = datetime.now(timezone.utc)
    now = parse_ts_utc(now)

    starts = [parse_ts_utc(r[0]) for r in rows]
    result = []
    for i, (_, rate, duration) in enumerate(rows):
        if i + 1 < len(rows):
            gap_min = (starts[i + 1] - starts[i]).total_seconds() / 60.0
        else:
            gap_min = (now - starts[i]).total_seconds() / 60.0
        result.append(effective_units(rate, duration, gap_min))
    return result
