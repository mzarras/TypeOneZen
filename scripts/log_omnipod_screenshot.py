#!/usr/bin/env python3
"""
Log Omnipod daily summary data extracted from app screenshots.

Called by Zenbot after parsing an Omnipod History > Summary screenshot.
Logs daily basal total to insulin_doses and saves full summary as a note.
Deduplicates by date — safe to run multiple times for the same day.

Usage:
    python3 log_omnipod_screenshot.py \\
        --date 2026-02-24 \\
        --total-insulin 31.55 \\
        --basal 17.55 \\
        --bolus 14.0 \\
        --carbs 55 \\
        --tir 94.51 \\
        --avg-bg 119.79
"""

import warnings
warnings.filterwarnings("ignore")

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
NY = ZoneInfo("America/New_York")


def main():
    parser = argparse.ArgumentParser(description="Log Omnipod screenshot summary to TypeOneZen DB")
    parser.add_argument("--date", required=True, help="Date of summary (YYYY-MM-DD)")
    parser.add_argument("--total-insulin", type=float, help="Total insulin delivered (units)")
    parser.add_argument("--basal", type=float, required=True, help="Basal insulin (units)")
    parser.add_argument("--bolus", type=float, help="Bolus insulin (units)")
    parser.add_argument("--carbs", type=float, help="Total carbs logged by pump (grams)")
    parser.add_argument("--tir", type=float, help="Time in range %% (70-180 mg/dL)")
    parser.add_argument("--avg-bg", type=float, help="Average sensor glucose (mg/dL)")
    parser.add_argument("--above-range", type=float, help="%% time above 180 mg/dL")
    parser.add_argument("--below-range", type=float, help="%% time below 70 mg/dL")
    args = parser.parse_args()

    # Parse the target date — use noon NY time as the canonical daily timestamp
    try:
        date = datetime.strptime(args.date, "%Y-%m-%d").replace(
            hour=12, minute=0, second=0, tzinfo=NY
        )
    except ValueError:
        print(f"ERROR: Invalid date format '{args.date}'. Use YYYY-MM-DD.")
        return 1

    # Convert to UTC ISO for storage
    ts_utc = date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── Deduplicate: check if basal already logged for this date ──────────────
    existing = conn.execute(
        """SELECT id FROM insulin_doses
           WHERE type = 'basal'
             AND date(datetime(timestamp, 'utc')) = date(?)""",
        (ts_utc,)
    ).fetchone()

    if existing:
        print(f"⚠️  Basal already logged for {args.date} (id={existing['id']}). Updating...")
        conn.execute(
            "UPDATE insulin_doses SET units = ?, notes = ? WHERE id = ?",
            (
                args.basal,
                json.dumps({"source": "omnipod_screenshot", "total_insulin": args.total_insulin,
                            "bolus": args.bolus, "carbs": args.carbs, "tir": args.tir,
                            "avg_bg": args.avg_bg}),
                existing["id"]
            )
        )
        action = "updated"
    else:
        conn.execute(
            """INSERT INTO insulin_doses (timestamp, units, type, notes)
               VALUES (?, ?, 'basal', ?)""",
            (
                ts_utc,
                args.basal,
                json.dumps({"source": "omnipod_screenshot", "total_insulin": args.total_insulin,
                            "bolus": args.bolus, "carbs": args.carbs, "tir": args.tir,
                            "avg_bg": args.avg_bg})
            )
        )
        action = "inserted"

    # ── Log full summary as a note ────────────────────────────────────────────
    summary_parts = [f"Omnipod daily summary for {args.date}:"]
    if args.total_insulin:
        summary_parts.append(f"Total insulin: {args.total_insulin}u")
    summary_parts.append(f"Basal: {args.basal}u")
    if args.bolus:
        summary_parts.append(f"Bolus: {args.bolus}u")
    if args.carbs:
        summary_parts.append(f"Carbs logged by pump: {args.carbs}g")
    if args.tir:
        summary_parts.append(f"TIR (Omnipod): {args.tir}%")
    if args.avg_bg:
        summary_parts.append(f"Avg sensor BG: {args.avg_bg} mg/dL")
    if args.above_range:
        summary_parts.append(f"Above range: {args.above_range}%")
    if args.below_range:
        summary_parts.append(f"Below range: {args.below_range}%")

    note_body = " | ".join(summary_parts)

    # Deduplicate notes too — delete existing Omnipod note for this date if present
    conn.execute(
        """DELETE FROM notes WHERE tags LIKE '%omnipod_screenshot%'
           AND date(datetime(timestamp, 'utc')) = date(?)""",
        (ts_utc,)
    )
    conn.execute(
        "INSERT INTO notes (timestamp, body, tags) VALUES (?, ?, ?)",
        (ts_utc, note_body, "omnipod_screenshot,insulin,basal,daily_summary")
    )

    conn.commit()
    conn.close()

    # ── Print result ──────────────────────────────────────────────────────────
    print(f"✅ Basal {action}: {args.basal}u for {args.date}")
    if args.total_insulin:
        pct_basal = round(args.basal / args.total_insulin * 100, 1)
        pct_bolus = round((args.bolus or 0) / args.total_insulin * 100, 1)
        print(f"   Total: {args.total_insulin}u ({pct_basal}% basal / {pct_bolus}% bolus)")
    if args.tir:
        print(f"   Omnipod TIR: {args.tir}% | Avg BG: {args.avg_bg or '?'} mg/dL")
    if args.carbs:
        print(f"   Carbs (pump-logged): {args.carbs}g")
    print(f"   Note saved with tags: omnipod_screenshot,insulin,basal,daily_summary")
    return 0


if __name__ == "__main__":
    exit(main())
