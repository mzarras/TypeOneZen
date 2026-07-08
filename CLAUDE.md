# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TypeOneZen is a self-hosted Type 1 Diabetes data collection and monitoring system. It syncs CGM readings and pump treatments from Nightscout (Trio + Omnipod 5 closed loop), polls a Dexcom G7 CGM via the Dexcom Share API as a redundant source, stores everything in SQLite, imports data from Garmin (FIT files) and legacy Glooko CSVs, runs rule-based BG + pump alerts via iMessage, and generates health summary reports.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Initialize/reset the database
python3 db.py

# Run the Dexcom poller (normally runs every 5 min via cron)
python3 poller.py

# Sync Nightscout entries + treatments (normally runs every 5 min via cron)
python3 ns_sync.py
python3 ns_sync.py --since 2026-01-01   # backfill from a date (re-run safe)

# Run the BG monitor (sends iMessage alerts)
python3 monitor.py
python3 monitor.py --dry-run    # print alerts without sending

# Run tests (temp SQLite db + mocked nightscout_client, no network)
python3 -m pytest tests/

# Import data
python3 parsers/parse_glooko.py   # import Glooko CSVs from data/imports/glooko/ (historical backfill; ns_sync.py covers ongoing data)
python3 parsers/parse_fit.py      # import Garmin FIT files from data/imports/fit/

# Generate health summary report
python3 parsers/generate_summary.py        # outputs to summaries/
python3 parsers/refresh_summary.py         # silent wrapper for cron

# Data enrichment
python3 parsers/backfill_meals_from_bolus.py   # extract meals from bolus notes
```

## Architecture

**Runtime:** Python 3.9+ on macOS. No web server or framework — scripts run directly or via cron.

**Database:** SQLite at `data/TypeOneZen.db` with WAL journal mode. All timestamps stored as ISO8601 UTC. Seven tables: `glucose_readings`, `insulin_doses`, `workouts`, `meals`, `notes`, `alert_log`, `sync_state`. Rows synced from Nightscout carry the external record ID in `source_id` (unique-indexed where not null) for idempotency; `db.ensure_sync_schema()` holds the migration.

**Core modules (project root):**
- `db.py` — Schema definition, `get_db()` connection helper, table creation, sync-schema migration
- `poller.py` — Dexcom Share API polling with deduplication. Credentials from `.env`. Kept as a redundant BG source alongside the Nightscout sync
- `ns_sync.py` — Nightscout sync via `nightscout-client`: entries → `glucose_readings` (source `nightscout`), boluses/SMBs/temp basals → `insulin_doses`, pump-logged carbs → `meals`. Per-stream cursors in `sync_state`, idempotent via `source_id`, `--since` for backfill. Covers ongoing data; `parse_glooko.py` remains the backfill path for history that predates the Nightscout site
- `monitor.py` — Rule-based BG alert rules (post-meal spike, sustained high, rapid drop, pre-workout low risk, overnight high, low warning) plus Nightscout pump rules (low reservoir with state-transition semantics, pod age 72h/78h, loop stale >30 min, Nightscout unreachable — the last two are deliberately distinct alerts). Sends iMessage via `/opt/homebrew/bin/imsg`. 2-hour dedup window prevents alert spam. Pump rules no-op if `nightscout-client` isn't installed/configured

**Parsers (`parsers/`):**
- `parse_glooko.py` — Imports CGM, BG, bolus, and basal data from Glooko CSV exports. Converts local NY time to UTC
- `parse_fit.py` — Imports Garmin FIT workout files with activity type mapping and intensity derivation from heart rate
- `generate_summary.py` — Produces `summaries/health_context.md` and `summaries/stats_cache.json` with BG statistics, time-in-range, insulin totals, workout/meal-BG correlations, and auto-generated insights
- `backfill_meals_from_bolus.py` — Extracts meal data from bolus insulin notes

**Data flow:** Nightscout → `ns_sync.py` → SQLite ← `poller.py` (Dexcom API) ← `parse_fit.py` (FIT imports) ← `parse_glooko.py` (historical CSV backfill). Then `monitor.py` reads SQLite (plus live Nightscout pump state) for alerting and `generate_summary.py` reads it for reporting.

## Key Conventions

- All timestamps are stored in UTC (ISO8601). Display/analysis uses `America/New_York` timezone via `zoneinfo.ZoneInfo`
- Database connections use `db.get_db()` which sets `row_factory = sqlite3.Row` and enables WAL mode
- All parsers deduplicate before inserting (typically by timestamp match)
- Credentials are in `.env` (git-ignored); see `.env.example` for template
- Logging uses `RotatingFileHandler` (5 MB max, 3 backups) in `logs/`
- Monitor alerts go to the phone number configured via `ALERT_PHONE` in `.env`, sent via macOS `imsg` CLI tool
- BG range thresholds: low < 70 mg/dL, high > 180 mg/dL
- Closed-loop context: SMBs arrive from Nightscout as many small boluses stored as `type='bolus'` — aggregate them in summaries, don't treat them as anomalies
- Tests live in `tests/` (pytest). They use a temp SQLite db and a stub `nightscout_client` installed in `sys.modules` by `tests/conftest.py` — never the real package or the sibling checkout
