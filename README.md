# TypeOneZen

A self-hosted Type 1 Diabetes data collection and monitoring system. Polls a Dexcom G7 CGM via the Dexcom Share API, stores glucose readings in SQLite, imports insulin and workout data from Glooko and Garmin, runs rule-based BG alerts via iMessage, and generates health summary reports.

Built for personal use by a T1D runner who wanted a single place to correlate glucose, insulin, and workout data — the full picture that no single app provides.

Designed to work with [OpenClaw](https://github.com/openclaw/openclaw) — an always-on AI agent framework. TypeOneZen provides the data layer, and an OpenClaw skill gives your agent natural-language access to query BG, log meals, check insulin, and review workouts without writing SQL from scratch. See [OpenClaw Integration](#openclaw-integration) below.

## Features

- **Live CGM polling** — Fetches Dexcom G7 readings every 5 minutes via the Share API
- **Rule-based BG alerts** — Post-meal spikes, sustained highs, rapid drops, overnight highs — sent via iMessage with 2-hour dedup
- **Multi-source data import** — Glooko CSV exports (insulin + BG), Garmin FIT files (workouts with HR zones)
- **Health summaries** — Auto-generated reports with time-in-range, insulin totals, workout-BG correlations, and pattern insights
- **Meal logging** — Track meals with macros (carbs, protein, fat, fiber, calories)
- **SQLite storage** — Everything in one local database, indexed for fast time-range queries
- **OpenClaw skill** — Pre-built agent skill for natural-language BG queries, meal logging, and more

## Architecture

```
Dexcom G7 CGM
      |
      v
  poller.py ──────> SQLite DB <────── parse_glooko.py (Glooko CSV)
  (every 5 min)        |       <────── parse_fit.py (Garmin FIT)
                        |
              ┌─────────┼──────────┐
              v         v          v
         monitor.py  generate_    tz_query.py
         (alerts)    summary.py   (ad-hoc queries)
              |         |
              v         v
          iMessage   summaries/
```

No web server, no framework — just Python scripts run directly or via cron on macOS.

## Setup

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your Dexcom Share username and password. Set `DEXCOM_OUTSIDE_US=true` if using the international Dexcom server.

### 3. Initialize the database

```bash
python3 db.py
```

Creates `data/TypeOneZen.db` with all tables and indexes.

### 4. Test the poller

```bash
python3 poller.py
```

You should see:
```
Reading stored: 120 mg/dL -> at 2025-11-21T14:30:00
```

### 5. Schedule via cron

```cron
# Poll Dexcom every 5 minutes
*/5 * * * * cd ~/TypeOneZen && python3 poller.py >> logs/cron.log 2>&1

# Run BG monitor every 5 minutes
*/5 * * * * cd ~/TypeOneZen && python3 monitor.py >> logs/monitor.log 2>&1

# Regenerate health summary daily at 3am
0 3 * * * cd ~/TypeOneZen && python3 parsers/refresh_summary.py >> logs/cron.log 2>&1
```

## Usage

### BG Monitor

```bash
python3 monitor.py            # Run all alert rules, send iMessage alerts
python3 monitor.py --dry-run  # Print what would alert, without sending
```

**Alert rules:**
| Rule | Trigger |
|------|---------|
| Post-meal spike | BG peaks >60 mg/dL above pre-meal baseline within 30-120 min |
| Sustained high | Avg BG >200 for 90 min and current >180 |
| Rapid drop | BG drops >30 mg/dL in 30 min |
| Overnight high | BG >160 for >60 min between 11pm-7am |
| Pre-workout low risk | BG <120 near typical workout time (manual trigger only) |

### Data Import

```bash
# Import Glooko CSV exports (place files in data/imports/glooko/)
python3 parsers/parse_glooko.py

# Import Garmin FIT workout files (place files in data/imports/fit/)
python3 parsers/parse_fit.py

# Extract meal data from bolus insulin notes
python3 parsers/backfill_meals_from_bolus.py
```

### Health Summary

```bash
python3 parsers/generate_summary.py    # Generate summaries/health_context.md + stats_cache.json
```

Produces time-in-range stats, insulin totals by type, workout-BG correlations, and auto-generated insights.

## Database Schema

SQLite database at `data/TypeOneZen.db`. All timestamps stored as ISO8601 UTC.

| Table | Description |
|-------|-------------|
| `glucose_readings` | CGM readings — timestamp, glucose_mg_dl, trend, trend_arrow, source |
| `insulin_doses` | Bolus, basal, and correction doses — timestamp, units, type, notes |
| `meals` | Meals with macros — description, carbs_g, protein_g, fat_g, fiber_g, calories |
| `workouts` | Garmin workouts — started_at, ended_at, activity_type, intensity |
| `notes` | Free-text notes with tags |
| `alert_log` | Fired alert history — rule_name, triggered_at, message, sent status |

## OpenClaw Integration

TypeOneZen is designed to work with [OpenClaw](https://github.com/openclaw/openclaw), an always-on AI agent framework. The included skill gives your OpenClaw agent the ability to query and log T1D data conversationally — "What's my BG?" or "Log lunch: chicken salad, 25g carbs" — without burning tokens on raw SQL generation.

### Installing the Skill

Copy the example skill into your OpenClaw workspace:

```bash
cp -r examples/openclaw-skill ~/.openclaw/workspace/skills/typeonezen
```

With `nativeSkills: "auto"` in your `openclaw.json` (the default), OpenClaw will auto-discover the skill on the next session start.

### What the Skill Provides

**Read queries** (`tz_query.py`) — all return compact JSON:

```bash
tz_query.py now            # Current BG + trend + minutes since last reading
tz_query.py range 24       # BG stats over last N hours (avg, min, max, TIR)
tz_query.py insulin 24     # Insulin doses over last N hours (by type, totals)
tz_query.py meals 24       # Recent meals with macros
tz_query.py workouts 7     # Recent workouts with pre/during/post BG averages
tz_query.py summary        # Cached health summary (no recomputation)
tz_query.py monitor        # Dry-run BG alert rules
```

**Write operations** (`tz_log.py`):

```bash
tz_log.py meal --desc "oatmeal with banana" --carbs 45 --protein 8 --fiber 5
tz_log.py note --body "Felt shaky before lunch" --tags "hypo,symptom"
```

**Schema reference** (`references/schema.md`) — full table definitions and copy-paste SQL patterns for custom queries.

### Example: Agent Response

```
You: "What's my BG?"
Agent: runs tz_query.py now
Agent: "118 mg/dL, flat. Last reading 3 minutes ago at 2:35pm."
```

### Customization

The `SKILL.md` file controls when the skill triggers and how your agent responds. Edit the triggers list, operational context, and response style to match your setup — different CGM, different pump, different alert thresholds.

## Project Structure

```
TypeOneZen/
  db.py                  # Schema definition, get_db() helper, table + index creation
  poller.py              # Dexcom Share API poller
  monitor.py             # Rule-based BG alerting via iMessage
  run_poller.sh          # Cron wrapper script
  requirements.txt
  .env.example           # Credential template
  parsers/
    parse_glooko.py      # Glooko CSV import
    parse_fit.py         # Garmin FIT import
    generate_summary.py  # Health summary report generator
    refresh_summary.py   # Silent cron wrapper for summary generation
    backfill_meals_from_bolus.py  # Extract meals from bolus notes
  examples/
    openclaw-skill/      # OpenClaw agent skill (copy to ~/.openclaw/workspace/skills/)
      SKILL.md           # Skill definition — triggers, instructions, context
      scripts/
        tz_query.py      # Read queries (now, range, insulin, meals, workouts, summary, monitor)
        tz_log.py        # Write operations (meal, note)
      references/
        schema.md        # Database schema + query patterns
  data/
    TypeOneZen.db        # SQLite database (git-ignored)
    imports/              # Drop zone for CSV and FIT imports (git-ignored)
  logs/                  # Rotating log files (git-ignored)
  summaries/             # Generated reports (git-ignored)
```

## Requirements

- Python 3.9+
- macOS (for iMessage alerting via `imsg` CLI)
- Dexcom G7 with Share enabled
- Optional: Glooko account, Garmin watch with COROS/Garmin Connect export

## License

MIT
