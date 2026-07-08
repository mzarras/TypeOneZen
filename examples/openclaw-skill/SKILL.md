---
name: typeonezen
description: Query and log Type 1 Diabetes data from the TypeOneZen system
triggers:
  - BG
  - blood glucose
  - blood sugar
  - CGM
  - insulin
  - bolus
  - basal
  - carbs
  - meal
  - workout
  - exercise
  - time in range
  - TIR
  - A1C
  - health summary
  - monitor status
  - Dexcom
  - glucose
  - COROS
  - watch
  - sync workouts
  - Nightscout
  - pump
  - pod
  - reservoir
  - loop
  - IOB
---

# TypeOneZen Skill

Query and log Type 1 Diabetes data. All data lives in a SQLite database at `~/TypeOneZen/data/TypeOneZen.db`. Timestamps are stored as ISO8601 UTC; display in `America/New_York`.

## Hard Rules (always follow, survive context resets)

- **Always run `tz_query.py now` proactively** whenever the user mentions food, insulin, a correction, symptoms, a low, a high, or anything T1D-related. Never ask them what their BG is — just look it up. `now` also returns a live `nightscout` block (IOB, COB, loop status, reservoir, pod age, data age) when Nightscout is configured; it is `null` (or `{"error": ...}`) when Nightscout is unavailable — the SQLite BG data still comes back either way.
- **Never present BG data older than 5 minutes** without explicitly flagging it as stale. If `freshness` is `"stale"`, note it clearly in your response.
- **Never ask questions you can answer yourself** — check the data first, then respond.
- **SMBs are expected**: the closed loop (Trio + Omnipod 5) delivers super micro boluses that arrive as many small boluses via Nightscout sync. When summarizing insulin, aggregate them (totals by type) — never list each 0.1u SMB individually or flag them as unusual.
- **(LEGACY) When an Omnipod app screenshot comes through** (History → Summary screen showing Total Insulin / Basal / Bolus / TIR), parse it with the image tool and immediately call `python3 ~/TypeOneZen/scripts/log_omnipod_screenshot.py` with the extracted values. Script args: `--date YYYY-MM-DD --basal X --total-insulin X --bolus X --carbs X --tir X --avg-bg X --above-range X --below-range X`. This workflow is legacy: insulin doses (boluses, SMBs, temp basals) now arrive automatically via the Nightscout sync (`ns_sync.py`). Only use the screenshot workflow if Nightscout has been down.

## Quick Start — Use Scripts First

For common queries, run the pre-built scripts. They return compact JSON (fewer tokens than raw SQL).

### Read Queries (`tz_query.py`)

```bash
# Current BG + trend + minutes since last reading, plus live Nightscout
# context (IOB, COB, loop status, reservoir, pod age, data_age_minutes)
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py now

# Live pump status from Nightscout: reservoir (Omnipod shows "50+" above 50u),
# pod age, site change time, loop status, minutes since the loop last ran
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py pump

# BG stats over last N hours (avg, min, max, TIR, count)
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py range 24

# Insulin doses over last N hours (by type, totals)
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py insulin 24

# Recent meals with macros
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py meals 24

# Recent workouts with BG correlation
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py workouts 7

# Latest health summary (reads cached stats, no recomputation)
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py summary

# Run BG monitor rules (dry-run, no alerts sent)
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_query.py monitor
```

### Write Operations (`tz_log.py`)

```bash
# Log a meal
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_log.py meal --desc "oatmeal with banana" --carbs 45 --protein 8 --fiber 5

# Log a note
python3 ~/.openclaw/workspace/skills/typeonezen/scripts/tz_log.py note --body "Felt shaky before lunch" --tags "hypo,symptom"
```

## When Scripts Don't Cover It

For custom queries beyond what the scripts provide, write SQL directly against the database. See `references/schema.md` for table definitions and query patterns.

```bash
sqlite3 ~/TypeOneZen/data/TypeOneZen.db "SELECT ..."
```

## Operational Context

- **Closed loop (Trio + Omnipod 5)**: The user runs a Trio closed loop on an Omnipod 5 pod, reporting to Nightscout. The loop auto-adjusts basal and delivers SMBs (super micro boluses) — so insulin history contains many small boluses. That's normal; aggregate them in summaries.
- **Nightscout sync is live**: `ns_sync.py` runs every 5 min via cron. It pulls CGM entries → `glucose_readings` (source='nightscout'), boluses/SMBs/temp basals → `insulin_doses`, and pump-logged carbs → `meals`. Doses no longer need manual logging or screenshots. Synced rows carry a `source_id` (Nightscout record ID) so re-syncs never duplicate.
- **Live pump state**: `tz_query.py pump` returns reservoir, pod age (pods hard-stop at 80h), loop status, and minutes since the loop last ran. Use it whenever the user asks about their pod, reservoir, or whether the loop is working.
- **BG monitoring is live**: `monitor.py` runs every 15 min via cron. It checks post-meal spikes, sustained highs, rapid drops, and overnight highs — plus pump rules from Nightscout: low reservoir (<20u), pod age (72h warn / 78h urgent), loop-not-looping (>30 min stale), and Nightscout-unreachable. Alerts go via iMessage. No need to duplicate this logic.
- **Pre-workout rule is disabled** in the auto-monitor. Only mention workout BG risk if the user explicitly says they're about to exercise.
- **Fake-carb correction pattern**: Some users log a "fake carb" bolus to correct highs without actual food (to bypass conservative pump IOB limits). If you see a bolus noted as a correction with no corresponding meal, that's what's happening. Log as `type='correction'`, not a meal.
- **TIR goal**: 90%+ time in range (70–180 mg/dL).
- **Dexcom poller**: Runs every 5 min via cron (`poller.py`). Readings may lag up to 5 min.
- **Health summaries**: Generated by `generate_summary.py` → `~/TypeOneZen/summaries/stats_cache.json` and `health_context.md`. Use `tz_query.py summary` to read cached stats rather than regenerating.
- **(LEGACY) Omnipod basal data via screenshots**: Basal and bolus data now arrive automatically via the Nightscout sync. The old workflow — daily screenshots of the Omnipod app (History → Summary), parsed with the image tool and logged via `python3 ~/TypeOneZen/scripts/log_omnipod_screenshot.py --date YYYY-MM-DD --basal X --total-insulin X --bolus X --carbs X --tir X --avg-bg X` — is kept only as a fallback for when Nightscout is down. Logs to `insulin_doses` with `type='basal'`. Deduplicates by date — safe to re-run.
- **COROS workout sync**: `parsers/fetch_coros.py` runs every 6 hours via cron. It authenticates with COROS Training Hub, downloads new FIT files to `data/imports/fit/coros/`, and imports them via `parse_fit.py`. Deduplicates by activity ID on disk. If the user says they just finished a workout or wants to sync their watch, run: `python3 ~/TypeOneZen/parsers/fetch_coros.py --days 3`. Use `--dry-run` to preview without downloading. Imported workouts appear in the same `workouts` table and are returned by `tz_query.py workouts N`.
- **Daily summaries**: `scripts/daily_summary.py` sends personalized iMessage summaries at 8am (morning recap) and 9pm (evening recap + overnight risk flag) via cron. Each summary includes data-backed insights comparing today's events to historical patterns.

## Response Style

- Lead with the number (e.g., "104 mg/dL, flat"). Don't bury the answer.
- Use mg/dL, not mmol/L.
- Times in Eastern (America/New_York).
- Keep it conversational — not a medical dashboard.
