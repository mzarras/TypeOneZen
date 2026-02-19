# TypeOneZen Database Schema

**Database:** `~/TypeOneZen/data/TypeOneZen.db` (SQLite, WAL mode)
**Timestamps:** All stored as ISO8601 UTC. Display in `America/New_York`.

## Tables

### glucose_readings
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT NOT NULL | ISO8601 UTC |
| glucose_mg_dl | INTEGER NOT NULL | BG value in mg/dL |
| trend | TEXT | e.g. Flat, FortyFiveUp, SingleUp |
| trend_arrow | TEXT | e.g. →, ↗, ↑ |
| source | TEXT | Default 'dexcom' |
| created_at | TEXT | Auto-set |

### insulin_doses
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT NOT NULL | ISO8601 UTC |
| units | REAL NOT NULL | Dose amount |
| type | TEXT | bolus, basal, correction |
| notes | TEXT | May contain meal context |
| created_at | TEXT | Auto-set |

### meals
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT NOT NULL | ISO8601 UTC |
| description | TEXT NOT NULL | e.g. 'oatmeal with banana' |
| carbs_g | REAL | Total carbs in grams |
| protein_g | REAL | |
| fat_g | REAL | |
| fiber_g | REAL | Fiber blunts BG impact |
| calories | INTEGER | |
| glycemic_load | REAL | Optional |
| source | TEXT | 'manual', 'photo', 'message' |
| notes | TEXT | Extra context |
| created_at | TEXT | Auto-set |

### workouts
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| started_at | TEXT NOT NULL | ISO8601 UTC |
| ended_at | TEXT | ISO8601 UTC |
| activity_type | TEXT | e.g. running, cycling, strength |
| intensity | TEXT | e.g. low, moderate, high |
| notes | TEXT | |
| created_at | TEXT | Auto-set |

### notes
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| timestamp | TEXT NOT NULL | ISO8601 UTC |
| body | TEXT NOT NULL | Free-text content |
| tags | TEXT | Comma-separated |
| created_at | TEXT | Auto-set |

### alert_log
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| rule_name | TEXT NOT NULL | POST_MEAL_SPIKE, SUSTAINED_HIGH, etc. |
| triggered_at | TEXT NOT NULL | ISO8601 UTC |
| message | TEXT NOT NULL | Alert message sent |
| sent | INTEGER | 1 = sent, 0 = failed |

## Indexes

```
idx_glucose_timestamp     → glucose_readings(timestamp)
idx_insulin_timestamp     → insulin_doses(timestamp)
idx_meals_timestamp       → meals(timestamp)
idx_workouts_started      → workouts(started_at)
idx_alert_log_rule_time   → alert_log(rule_name, triggered_at)
```

## Common Query Patterns

### Current BG
```sql
SELECT glucose_mg_dl, trend, trend_arrow, timestamp
FROM glucose_readings ORDER BY timestamp DESC LIMIT 1;
```

### BG average over last N hours
```sql
SELECT AVG(glucose_mg_dl) as avg_bg, COUNT(*) as count
FROM glucose_readings
WHERE timestamp > datetime('now', '-N hours');
```

### Time in range (70-180) over last 24h
```sql
SELECT
  ROUND(100.0 * SUM(CASE WHEN glucose_mg_dl BETWEEN 70 AND 180 THEN 1 ELSE 0 END) / COUNT(*), 1) as tir_pct
FROM glucose_readings
WHERE timestamp > datetime('now', '-24 hours');
```

### Insulin totals by type
```sql
SELECT type, SUM(units) as total, COUNT(*) as doses
FROM insulin_doses
WHERE timestamp > datetime('now', '-24 hours')
GROUP BY type;
```

### Meals with BG impact (peak BG 60-120min after)
```sql
SELECT m.description, m.carbs_g, m.timestamp,
  (SELECT MAX(g.glucose_mg_dl) FROM glucose_readings g
   WHERE g.timestamp BETWEEN datetime(m.timestamp, '+60 minutes')
                         AND datetime(m.timestamp, '+120 minutes')) as peak_bg
FROM meals m
WHERE m.timestamp > datetime('now', '-24 hours')
ORDER BY m.timestamp DESC;
```

### Recent alerts
```sql
SELECT rule_name, triggered_at, message, sent
FROM alert_log ORDER BY triggered_at DESC LIMIT 10;
```

## Timezone Conversion

Timestamps are stored in UTC. To display in Eastern:

**In Python:**
```python
from datetime import datetime
from zoneinfo import ZoneInfo
dt = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=ZoneInfo("UTC"))
ny_str = dt.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M%p")
```

**In SQLite (approximate — no timezone support):**
```sql
-- UTC to ET is -5h (EST) or -4h (EDT). Use Python for accuracy.
SELECT datetime(timestamp, '-5 hours') as est_time FROM glucose_readings;
```
