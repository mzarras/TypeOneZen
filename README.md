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

## Talk to Your Data Over iMessage

TypeOneZen pairs with [OpenClaw](https://github.com/openclaw/openclaw) to give you something no diabetes app offers: **a personal AI agent you text over iMessage that actually knows your numbers.**

Snap a photo of your lunch — it estimates macros and logs the meal. Send a pump screenshot — it reads the bolus and logs it correctly. Ask "how's my BG?" at 2am — it answers with your actual data, not a generic chart. It's your diabetes notebook, your pattern spotter, and your second brain, all in a conversation thread you already check every day.

### What it looks like

```
You:     [photo of tacos]
Zenbot:  Logging: tacos — 52g carbs, 24g protein, 18g fat. Confirm?
You:     yep
Zenbot:  Logged. BG is 142 ↗ — I'll watch for a spike.

You:     what's my bg
Zenbot:  118 mg/dL → flat. Last reading 2 min ago.

You:     how was my overnight?
Zenbot:  Overnight avg was 138, TIR 91%. You dipped to 74 at 3am
         but recovered on your own. No lows flagged.

You:     should I correct?
Zenbot:  BG is 215 rising slowly. Last bolus was 2h ago, IOB ~0.4u.
         You've been above 180 for 45 min. Here's the context —
         your call.
```

### Proactive alerts, not just answers

The system doesn't wait for you to ask. It texts you when something matters:

- **Post-meal spike** — BG rockets >60 mg/dL above your pre-meal baseline
- **Sustained high** — Stuck above 200 for 90+ minutes
- **Rapid drop** — Falling >30 mg/dL in 30 minutes
- **Overnight high** — Above 160 for an hour between 11pm–7am
- **Morning & evening summaries** — Data-backed daily briefings with TIR, insulin totals, workout impact, pattern insights, and overnight risk assessment

All alerts deduplicate on a 2-hour window so you never get spammed.

### Why iMessage?

No app to install. No dashboard to check. No login to remember. The data comes to you in the same place your friends and family text you. You respond in plain English. That's it.

> **Important:** Zenbot never gives medical advice. It provides data-driven context — your numbers, your patterns, your history — to support your own decisions. Always consult your endocrinologist for treatment changes.

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

TypeOneZen is designed to work with [OpenClaw](https://github.com/openclaw/openclaw), an always-on AI agent framework. With the included skill installed, your OpenClaw agent ("Zenbot") becomes a personal T1D assistant — it can log meals from photos, read pump screenshots, monitor your BG in real time, and answer questions using your actual data. Think of it as a conversational layer on top of your diabetes data, not a medical device.

> **Important:** Zenbot never gives medical advice. It provides data-driven context — your numbers, your patterns, your history — to support your own decisions. Always consult your endocrinologist for treatment changes.

### Installing the Skill

Copy the example skill into your OpenClaw workspace:

```bash
cp -r examples/openclaw-skill ~/.openclaw/workspace/skills/typeonezen
```

With `nativeSkills: "auto"` in your `openclaw.json` (the default), OpenClaw will auto-discover the skill on the next session start.

### Food Logging via Photo or Message

Send Zenbot a photo of your meal and it uses vision to estimate macros (carbs, protein, fat, fiber, calories), then logs it to the meals table. No app to open, no manual entry — just snap and send.

Or describe it in a message:

```
You: "just had oatmeal with banana, 45g carbs"
Zenbot: "Got it — logging oatmeal with banana, 45g carbs. Confirm?"
You: "yes"
Zenbot: "Logged. BG is 134 flat — I'll watch for a spike."
```

Zenbot always confirms before writing to the database. If the vision estimate looks off, just correct it ("more like 60g carbs") and it'll update before logging.

### Insulin Logging via Pump Screenshot

Send a screenshot from the Omnipod 5 app (or any pump app) and Zenbot reads the bolus amount, carbs entered, IOB, and BG at time of dose. It logs the dose correctly as `bolus` or `correction` based on what it sees.

Or just message it:

```
You: "just took 3.5 units correction"
Zenbot: "Logging 3.5u correction. Current BG is 235, IOB from last bolus was ~1.2u. Confirm?"
You: "yep"
Zenbot: "Logged."
```

#### Manual Override Corrections (Omnipod 5)

Omnipod 5 users know the frustration: the system subtracts IOB from correction boluses, often zeroing them out entirely when you know you need insulin. The common workaround is to enter a small fake carb amount (e.g. 5g) to force a meal bolus that delivers the insulin you actually want.

Zenbot understands this pattern. When it sees a pump screenshot with a suspiciously small carb entry (say 5g) paired with a bolus that looks like a correction — high BG, no real meal context — it logs the dose as `type='correction'`, not as a meal. No fake meals cluttering your data.

### Real-Time BG Monitoring

The monitor runs every 5 minutes via cron and sends proactive iMessage alerts:

| Alert | What it catches |
|-------|----------------|
| Post-meal spike | BG peaks >60 mg/dL above pre-meal baseline within 30–120 min |
| Sustained high | Avg BG >200 for 90 min and current >180 |
| Rapid drop | BG drops >30 mg/dL in 30 min |
| Overnight high | BG >160 for >60 min between 11pm–7am |
| Pre-workout low risk | BG <120 near typical workout time |

Alerts deduplicate on a 2-hour window so you don't get spammed. If an alert fires and you don't want to hear about it again for a while:

```bash
python3 monitor.py --snooze RULE_NAME
```

### Natural Conversation

Ask Zenbot anything about your data and it answers using your actual history, not generic advice:

```
You: "what's my BG?"
Zenbot: "118 mg/dL, flat. Last reading 3 minutes ago."

You: "how have I been doing this week?"
Zenbot: "Time in range was 74% — up from 68% last week. Overnights
         have been better since you bumped your basal. Two post-meal
         spikes over 250, both after dinner."

You: "should I correct now?"
Zenbot: "BG is 215 and rising slowly. Last bolus was 2h ago, IOB is
         about 0.4u. You've been running high for 45 min. Here's the
         context — your call."
```

Zenbot pulls from `glucose_readings`, `insulin_doses`, `meals`, and `workouts` to give you the full picture. It never tells you what to do — it gives you the numbers so you can decide.

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
