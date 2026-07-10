"""
One-time backfill: rewrite temp-basal rows to EFFECTIVE delivered units.

Historical type='basal' rows in insulin_doses were stored with the
SCHEDULED amount (rate x scheduled_duration/60), but the Trio closed loop
supersedes each temp basal after a few minutes, so a naive SUM(units)
overcounted basal ~4-5x. This script recomputes every basal row's units as

    rate x min(scheduled_duration, minutes_until_next_temp_basal) / 60

(the last row is capped at minutes elapsed until now), parsing rate and
duration from each row's notes ("Temp Basal, rate=0.75 U/hr,
duration=30.0 min"). Going forward, ns_sync.py maintains this invariant on
every sync (see reconcile_basal_neighbors), so this backfill only needs to
run once. Re-running it is safe — the recompute is deterministic.

BACK UP THE DATABASE FIRST, e.g.:
    sqlite3 data/TypeOneZen.db ".backup data/TypeOneZen.db.pre-basal-backfill.bak"

Usage:
    python3 parsers/backfill_basal_effective.py --dry-run   # per-day before/after report only
    python3 parsers/backfill_basal_effective.py             # apply the rewrite
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_db
from basal_effective import compute_effective_units, parse_rate_duration, parse_ts_utc

NY = ZoneInfo("America/New_York")


def run(dry_run: bool) -> None:
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, timestamp, units, notes
            FROM insulin_doses
            WHERE type = 'basal'
            ORDER BY timestamp
        """).fetchall()

        parsed = []       # (id, timestamp, old_units, rate, duration) in time order
        unparseable = []  # rows left untouched (no rate/duration in notes)
        for r in rows:
            rd = parse_rate_duration(r["notes"])
            if rd is None:
                unparseable.append(r)
                continue
            parsed.append((r["id"], r["timestamp"], r["units"], rd[0], rd[1]))

        effective = compute_effective_units([(ts, rate, dur) for _, ts, _, rate, dur in parsed])

        # Per-NY-day before/after totals
        before = defaultdict(float)
        after = defaultdict(float)
        changed = 0
        for (row_id, ts, old_units, _, _), new_units in zip(parsed, effective):
            day = parse_ts_utc(ts).astimezone(NY).date().isoformat()
            before[day] += old_units
            after[day] += new_units
            if abs(new_units - old_units) > 1e-9:
                changed += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE insulin_doses SET units = ? WHERE id = ?",
                        (new_units, row_id),
                    )

        if not dry_run:
            conn.commit()

        mode = "DRY RUN — no changes written" if dry_run else "APPLIED"
        print(f"Basal effective-units backfill ({mode})")
        print(f"  basal rows: {len(rows)}  recomputed: {len(parsed)}  "
              f"changed: {changed}  unparseable notes (left as-is): {len(unparseable)}")
        print(f"  {'NY day':<12} {'before (u)':>11} {'after (u)':>10} {'delta (u)':>10}")
        for day in sorted(before):
            print(f"  {day:<12} {before[day]:>11.2f} {after[day]:>10.2f} "
                  f"{after[day] - before[day]:>+10.2f}")
        total_before = sum(before.values())
        total_after = sum(after.values())
        print(f"  {'TOTAL':<12} {total_before:>11.2f} {total_after:>10.2f} "
              f"{total_after - total_before:>+10.2f}")
        for r in unparseable:
            print(f"  WARNING: could not parse rate/duration for row id={r['id']} "
                  f"ts={r['timestamp']} notes={r['notes']!r}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rewrite type='basal' insulin_doses rows to effective delivered units")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print per-day before/after totals without writing")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
