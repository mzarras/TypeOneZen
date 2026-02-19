"""
TypeOneZen — SQLite database setup.

Creates and manages the local database at ~/TypeOneZen/data/TypeOneZen.db.
Run this file directly to initialize all tables.
"""

import sqlite3
from pathlib import Path

# Database lives in ~/TypeOneZen/data/
DB_DIR = Path.home() / "TypeOneZen" / "data"
DB_PATH = DB_DIR / "TypeOneZen.db"


def get_db() -> sqlite3.Connection:
    """Return a connection to the TypeOneZen database."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create all tables if they do not already exist."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glucose_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,  -- ISO8601 UTC
            glucose_mg_dl INTEGER NOT NULL,
            trend       TEXT,              -- e.g. Flat, FortyFiveUp
            trend_arrow TEXT,              -- e.g. ->
            source      TEXT    DEFAULT 'dexcom',
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS insulin_doses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,  -- ISO8601 UTC
            units       REAL    NOT NULL,
            type        TEXT,              -- bolus, basal, correction
            notes       TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT    NOT NULL,  -- ISO8601 UTC
            ended_at      TEXT,
            activity_type TEXT,
            intensity     TEXT,
            notes         TEXT,
            created_at    TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,  -- ISO8601 UTC
            description     TEXT    NOT NULL,  -- e.g. 'oatmeal with banana'
            carbs_g         REAL,              -- total carbohydrates in grams
            protein_g       REAL,
            fat_g           REAL,
            fiber_g         REAL,              -- fiber blunts BG impact
            calories        INTEGER,
            glycemic_load   REAL,              -- optional, can be computed later
            source          TEXT    DEFAULT 'manual',  -- 'manual', 'photo', 'message'
            notes           TEXT,              -- extra context
            created_at      TEXT    DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,  -- ISO8601 UTC
            body        TEXT    NOT NULL,
            tags        TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)

    # -- Indexes for time-range queries --
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_glucose_timestamp ON glucose_readings(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_insulin_timestamp ON insulin_doses(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_meals_timestamp ON meals(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workouts_started ON workouts(started_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_log_rule_time ON alert_log(rule_name, triggered_at)")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
