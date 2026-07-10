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
python3 parsers/parse_correlatewell.py  # import CorrelateWell CSVs from data/imports/correlatewell/ (--dry-run supported)

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
- `monitor.py` — Rule-based BG alert rules (no-recent-data, stuck high, rapid drop, pre-workout low risk, low warning) plus Nightscout pump rules (low reservoir with state-transition semantics, pod age 72h/78h, loop stale >30 min, Nightscout unreachable — the last two are deliberately distinct alerts). Sends iMessage via `/opt/homebrew/bin/imsg`. Only `sent=1` alerts count toward escalation/cooldowns, so failed sends retry on the next run. Pump rules no-op if `nightscout-client` isn't installed/configured. **BG alerting is loop-aware**: both `assess_low_risk` and `assess_high_risk` defer to Trio's live loop computation from Nightscout devicestatus (net IOB, prediction arrays, eventual_bg, insulin_req, carbsReq/maxSafeBasal in the reason string) and only page when the loop can't fix it. Lows: BG < 70, Trio's carbsReq, or a near-low with Trio-predicted sub-70 nadir; borderline no-loop cases gated by `similar_drop_history` (the old projection triggers were 76–84% false alarms in backtesting). Highs: HIGH_STUCK replaces the old post-meal-spike/sustained-high/overnight-high rules — one episode per contiguous run above 180 (episode start = escalation dedup_key), paging only on URGENT (≥300 for 30 min), SITE_SUSPECT (3h+ high with delivery maxed), STUCK (45+ min, not falling, Trio's eventual_bg > 180 and insulin_req ≥ 0.5u), or a CGM-only fallback when loop data is missing (the old rules would have sent ~27 messages/week with 62% self-resolving). RAPID_DROP fires only with no fresh loop data (CGM-only backstop). Trio's net IOB / autosens ISF are preferred over the bolus-decay estimate everywhere IOB is displayed

**Parsers (`parsers/`):**
- `parse_glooko.py` — Imports CGM, BG, bolus, and basal data from Glooko CSV exports. Converts local NY time to UTC
- `parse_correlatewell.py` — Imports glucose + workout CSVs exported from CorrelateWell (the author's separate health app); minute-granularity cross-source dedup, idempotent re-runs. Its companion exporter (`export_correlatewell.py`) is deliberately gitignored — personal tooling, never committed
- `parse_fit.py` — Imports Garmin FIT workout files with activity type mapping and intensity derivation from heart rate
- `generate_summary.py` — Produces `summaries/health_context.md` and `summaries/stats_cache.json` with BG statistics, time-in-range, insulin totals, workout/meal-BG correlations, and auto-generated insights

**Scripts (`scripts/`):**
- `daily_summary.py` — Morning (8am) and evening (9pm) iMessage summaries via cron. The evening outlook quotes Trio's live forecast (eventual_bg, predicted overnight min, temp basal, COB via `fetch_loop_state()`); falls back to a clearly-labeled rough estimate without loop data. Overnight windows are computed in Python with zoneinfo (DST-safe) — never do timezone arithmetic inside SQLite
- `weekly_summary.py` — Sunday 6pm iMessage: trailing 7 days vs the 7 before (TIR, avg/GMI, CV, low episodes, highs, overnight, TDD with insulin-coverage qualification, best/worst day, ranked improvement observations). Pure core (`build_weekly_stats`/`build_message`) with sending only in `main()`; supports `--dry-run` and `--week-ending`
- `watchdog.py` — Independent dead-man's switch run by launchd (NOT cron; installed via `setup/install_watchdog.sh`, plist in `setup/`). Pages directly via imsg when `logs/monitor.log` goes stale >20 min (pipeline dead) or the newest CGM reading is >60 min old (total blindness backstop below monitor's own NO_RECENT_DATA rule). Read-only on the DB, throttled per check to once per 2h (throttle not consumed on failed delivery), optional `HEALTHCHECKS_URL` ping for external coverage
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
- The OpenClaw skill scripts (`examples/openclaw-skill/scripts/`) and `scripts/log_omnipod_screenshot.py` resolve the data directory via `$TZ_HOME` (default `~/TypeOneZen`); core cron scripts still assume the repo lives at `~/TypeOneZen`
- `setup/SETUP.md` is the full new-machine deployment runbook (written to be executed by a Claude Code session on the target Mac)
