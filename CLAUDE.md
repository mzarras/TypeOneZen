# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TypeOneZen is a self-hosted Type 1 Diabetes data collection and monitoring system. It polls a Dexcom G7 CGM via the Dexcom Share API, stores glucose readings in SQLite, imports data from Glooko (CSV) and Garmin (FIT files), runs rule-based BG alerts via iMessage, and generates health summary reports.

## Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Initialize/reset the database
python3 db.py

# Run the Dexcom poller (normally runs every 5 min via cron)
python3 poller.py

# Run the BG monitor (sends iMessage alerts)
python3 monitor.py
python3 monitor.py --dry-run    # print alerts without sending

# Import data
python3 parsers/parse_glooko.py   # import Glooko CSV exports from data/imports/glooko/
python3 parsers/parse_fit.py      # import Garmin FIT files from data/imports/fit/

# Generate health summary report
python3 parsers/generate_summary.py        # outputs to summaries/
python3 parsers/refresh_summary.py         # silent wrapper for cron

# Data enrichment
python3 parsers/backfill_meals_from_bolus.py   # extract meals from bolus notes
```

## Architecture

**Runtime:** Python 3.9+ on macOS. No web server or framework — scripts run directly or via cron.

**Database:** SQLite at `data/TypeOneZen.db` with WAL journal mode. All timestamps stored as ISO8601 UTC. Six tables: `glucose_readings`, `insulin_doses`, `workouts`, `meals`, `notes`, `alert_log`.

**Core modules (project root):**
- `db.py` — Schema definition, `get_db()` connection helper, table creation
- `poller.py` — Dexcom Share API polling with deduplication. Credentials from `.env`
- `monitor.py` — Five rule-based BG alert rules (post-meal spike, sustained high, rapid drop, pre-workout low risk, overnight high). Sends iMessage via `/opt/homebrew/bin/imsg`. 2-hour dedup window prevents alert spam

**Parsers (`parsers/`):**
- `parse_glooko.py` — Imports CGM, BG, bolus, and basal data from Glooko CSV exports. Converts local NY time to UTC
- `parse_fit.py` — Imports Garmin FIT workout files with activity type mapping and intensity derivation from heart rate
- `generate_summary.py` — Produces `summaries/health_context.md` and `summaries/stats_cache.json` with BG statistics, time-in-range, insulin totals, workout/meal-BG correlations, and auto-generated insights
- `backfill_meals_from_bolus.py` — Extracts meal data from bolus insulin notes

**Data flow:** Dexcom API → `poller.py` → SQLite ← `parse_glooko.py` (CSV imports) ← `parse_fit.py` (FIT imports). Then `monitor.py` reads SQLite for alerting and `generate_summary.py` reads it for reporting.

## Key Conventions

- All timestamps are stored in UTC (ISO8601). Display/analysis uses `America/New_York` timezone via `zoneinfo.ZoneInfo`
- Database connections use `db.get_db()` which sets `row_factory = sqlite3.Row` and enables WAL mode
- All parsers deduplicate before inserting (typically by timestamp match)
- Credentials are in `.env` (git-ignored); see `.env.example` for template
- Logging uses `RotatingFileHandler` (5 MB max, 3 backups) in `logs/`
- Monitor alerts go to the phone number configured via `ALERT_PHONE` in `.env`, sent via macOS `imsg` CLI tool
- BG range thresholds: low < 70 mg/dL, high > 180 mg/dL
- No test suite exists currently
