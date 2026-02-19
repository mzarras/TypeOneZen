"""
Backfill meals table from bolus insulin_doses notes.

Parses carb counts, pre-bolus BG, and carb ratios from bolus notes
and inserts estimated meal entries for any bolus without a matching meal
within ±15 minutes.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_db

# ── Regex patterns for parsing bolus notes ──────────────────────────
# Matches: 'carbs=65g', 'carbs: 20', 'carbs=8g'
RE_CARBS_EQ = re.compile(r"carbs\s*[=:]\s*(\d+)\s*g?", re.IGNORECASE)
# Matches: '65g carbs'
RE_CARBS_SUFFIX = re.compile(r"(\d+)\s*g\s+carbs", re.IGNORECASE)
# Matches: 'BG=107', 'BG=194'
RE_BG = re.compile(r"BG\s*=\s*(\d+)", re.IGNORECASE)
# Matches: 'ratio=1:4.0', 'ratio ~1:4', 'ratio=1:5.0'
RE_RATIO = re.compile(r"ratio\s*[=~]\s*1:(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_carbs(notes: str) -> Optional[float]:
    """Extract carb grams from a bolus note string."""
    m = RE_CARBS_EQ.search(notes)
    if m:
        return float(m.group(1))
    m = RE_CARBS_SUFFIX.search(notes)
    if m:
        return float(m.group(1))
    return None


def parse_pre_bg(notes: str) -> Optional[int]:
    """Extract pre-bolus BG from a bolus note string."""
    m = RE_BG.search(notes)
    return int(m.group(1)) if m else None


def parse_carb_ratio(notes: str) -> Optional[float]:
    """Extract carb ratio (g per unit) from a bolus note string."""
    m = RE_RATIO.search(notes)
    return float(m.group(1)) if m else None


def run():
    conn = get_db()
    cur = conn.cursor()

    # Fetch all bolus entries with carb info in notes
    rows = cur.execute("""
        SELECT id, timestamp, units, notes
        FROM insulin_doses
        WHERE type = 'bolus' AND notes LIKE '%carb%'
        ORDER BY timestamp
    """).fetchall()

    inserted = 0
    skipped = 0

    try:
        for row in rows:
            bolus_id = row["id"]
            ts = row["timestamp"]
            units = row["units"]
            notes = row["notes"] or ""

            carbs_g = parse_carbs(notes)
            if carbs_g is None:
                skipped += 1
                continue

            pre_bg = parse_pre_bg(notes)
            carb_ratio = parse_carb_ratio(notes)

            # Check if a meal already exists within ±15 minutes
            existing = cur.execute("""
                SELECT id FROM meals
                WHERE abs(strftime('%s', timestamp) - strftime('%s', ?)) <= 900
                LIMIT 1
            """, (ts,)).fetchone()

            if existing:
                skipped += 1
                continue

            # Build notes JSON for the new meal
            meal_notes = {
                "bolus_units": units,
                "original_bolus_note": notes,
            }
            if carb_ratio is not None:
                meal_notes["carb_ratio"] = carb_ratio
            if pre_bg is not None:
                meal_notes["pre_bg"] = pre_bg

            cur.execute("""
                INSERT INTO meals (timestamp, description, carbs_g, source, notes)
                VALUES (?, 'Estimated meal (from bolus data)', ?, 'bolus_backfill', ?)
            """, (ts, carbs_g, json.dumps(meal_notes)))

            inserted += 1
            if inserted % 50 == 0:
                print(f"  ... {inserted} meals inserted so far")

        conn.commit()
        print(f"\nBackfill complete: {inserted} meals inserted, {skipped} skipped.")

    except Exception as e:
        conn.rollback()
        print(f"Error during backfill — rolled back: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
