#!/usr/bin/env python3
"""TypeOneZen logging script — insert meals and notes into the database."""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path.home() / "TypeOneZen" / "data" / "TypeOneZen.db"
UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def utc_now_iso():
    return datetime.now(UTC).isoformat()


def out(data):
    print(json.dumps(data, indent=2))


def cmd_meal(args):
    """Log a meal."""
    conn = get_db()
    ts = utc_now_iso()

    conn.execute("""
        INSERT INTO meals (timestamp, description, carbs_g, protein_g, fat_g,
                           fiber_g, calories, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts,
        args.desc,
        args.carbs,
        args.protein,
        args.fat,
        args.fiber,
        args.calories,
        args.source,
    ))
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    ny_time = datetime.fromisoformat(ts).astimezone(NY).strftime("%-I:%M%p").lower()

    out({
        "status": "ok",
        "id": row_id,
        "timestamp_utc": ts,
        "time_ny": ny_time,
        "description": args.desc,
        "carbs_g": args.carbs,
    })


def cmd_note(args):
    """Log a note."""
    conn = get_db()
    ts = utc_now_iso()

    conn.execute("""
        INSERT INTO notes (timestamp, body, tags)
        VALUES (?, ?, ?)
    """, (ts, args.body, args.tags))
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    ny_time = datetime.fromisoformat(ts).astimezone(NY).strftime("%-I:%M%p").lower()

    out({
        "status": "ok",
        "id": row_id,
        "timestamp_utc": ts,
        "time_ny": ny_time,
        "body": args.body,
        "tags": args.tags,
    })


def main():
    parser = argparse.ArgumentParser(description="TypeOneZen data logger")
    sub = parser.add_subparsers(dest="command", required=True)

    p_meal = sub.add_parser("meal", help="Log a meal")
    p_meal.add_argument("--desc", required=True, help="Meal description")
    p_meal.add_argument("--carbs", type=float, required=True, help="Carbs in grams")
    p_meal.add_argument("--protein", type=float, default=None, help="Protein in grams")
    p_meal.add_argument("--fat", type=float, default=None, help="Fat in grams")
    p_meal.add_argument("--fiber", type=float, default=None, help="Fiber in grams")
    p_meal.add_argument("--calories", type=int, default=None, help="Total calories")
    p_meal.add_argument("--source", default="manual", help="Data source (default: manual)")

    p_note = sub.add_parser("note", help="Log a note")
    p_note.add_argument("--body", required=True, help="Note body text")
    p_note.add_argument("--tags", default=None, help="Comma-separated tags")

    args = parser.parse_args()

    cmds = {
        "meal": cmd_meal,
        "note": cmd_note,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
