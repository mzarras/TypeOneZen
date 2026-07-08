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


def ensure_sync_schema(conn: sqlite3.Connection) -> None:
    """Migrate schema for external-sync support (Nightscout). Safe to re-run.

    - Adds a nullable `source_id` column (external record ID, e.g. the
      Nightscout `_id`) to glucose_readings, insulin_doses, and meals.
      Existing rows are preserved (ALTER TABLE ADD COLUMN leaves them
      intact with source_id = NULL).
    - Creates unique indexes on source_id where not null, so synced rows
      are idempotent.
    - Creates the `sync_state` table (per-stream sync cursors).
    """
    # Add source_id column if the tables already existed without it
    for table in ("glucose_readings", "insulin_doses", "meals"):
        cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if cols and "source_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source_id TEXT")

    # Unique where not null — manual/Dexcom/Glooko rows (source_id NULL) are unaffected
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_glucose_source_id
        ON glucose_readings(source_id) WHERE source_id IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_insulin_source_id
        ON insulin_doses(source_id) WHERE source_id IS NOT NULL
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meals_source_id
        ON meals(source_id) WHERE source_id IS NOT NULL
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            key         TEXT PRIMARY KEY,  -- e.g. 'nightscout_entries'
            value       TEXT NOT NULL,     -- cursor / state value
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()


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
            source_id   TEXT,              -- external record ID (e.g. Nightscout _id)
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
            source_id   TEXT,              -- external record ID (e.g. Nightscout _id)
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
            source          TEXT    DEFAULT 'manual',  -- 'manual', 'photo', 'message', 'nightscout'
            notes           TEXT,              -- extra context
            source_id       TEXT,              -- external record ID (e.g. Nightscout _id)
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

    # -- Migrations for pre-existing databases (preserve existing rows) --
    ensure_sync_schema(conn)

    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
