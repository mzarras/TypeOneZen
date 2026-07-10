#!/usr/bin/env python3
"""
TypeOneZen — render a BG comparison graphic (PNG) for iMessage.

Compares two adjacent periods of CGM data — an hourly median profile overlay
plus headline-stat dumbbells (TIR, avg BG, time low, time high). Used by:
  - scripts/weekly_summary.py (attaches a this-week-vs-last-week image)
  - the OpenClaw zenbot skill ("make me a graphic of my sugars")

Usage:
    python3 render_bg_chart.py --mode compare --days 3            # last 3 days vs the 3 before
    python3 render_bg_chart.py --mode compare --days 3 --end 2026-07-09
    python3 render_bg_chart.py --mode week                        # trailing week vs prior week
    python3 render_bg_chart.py --mode week --week-ending 2026-07-09
    python3 render_bg_chart.py ... --out /path/to/out.png

Prints a JSON object to stdout: {"png": path, "current": {...}, "prior": {...}}.
Exits non-zero (with an "error" JSON) when there's no CGM data in the window.

Design notes (kept deliberately, don't "fix" these):
  - Current period = blue #2a78d6, prior period = neutral gray #6f6d69. The
    gray is a *reference* series, meant to read as neutral baseline — both
    lines carry direct labels, the palette pair passes CVD separation
    (ΔE 55) and 3:1 surface contrast.
  - One y-axis; horizontal-only grid; target band 70-180 as a quiet fill.
  - Static PNG for a phone: large type, no legend box (direct labels).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

TZ_HOME = Path(os.environ.get("TZ_HOME", Path.home() / "TypeOneZen"))
DB_PATH = TZ_HOME / "data" / "TypeOneZen.db"
DEFAULT_OUT_DIR = TZ_HOME / "media" / "charts"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

TIR_LOW, TIR_HIGH = 70, 180

# Palette — validated pair (dataviz six-checks): current blue / reference gray
COL_CURRENT = "#2a78d6"
COL_PRIOR = "#6f6d69"
COL_SURFACE = "#fcfcfb"
COL_BAND = "#f1f0ee"
COL_TEXT = "#0b0b0b"
COL_TEXT_2 = "#52514e"
COL_GRID = "#e5e4e1"
COL_GOOD = "#1e7d32"
COL_BAD = "#b3261e"


# ── Data ─────────────────────────────────────────────────────────────────────

def to_utc_str(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def ny_midnight(d):
    return datetime(d.year, d.month, d.day, tzinfo=NY)


def fetch_period(conn, start_ny, end_ny):
    """Readings in [start_ny, end_ny) as (ny_datetime, glucose) tuples."""
    rows = conn.execute(
        "SELECT timestamp, glucose_mg_dl FROM glucose_readings "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
        (to_utc_str(start_ny), to_utc_str(end_ny)),
    ).fetchall()
    out = []
    for ts, bg in rows:
        try:
            s = ts.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            out.append((dt.astimezone(NY), bg))
        except ValueError:
            continue
    return out


def period_stats(readings):
    vals = [bg for _, bg in readings]
    if not vals:
        return None
    n = len(vals)
    return {
        "n": n,
        "avg": round(sum(vals) / n, 1),
        "tir": round(sum(1 for v in vals if TIR_LOW <= v <= TIR_HIGH) / n * 100, 1),
        "low_pct": round(sum(1 for v in vals if v < TIR_LOW) / n * 100, 1),
        "high_pct": round(sum(1 for v in vals if v > TIR_HIGH) / n * 100, 1),
    }


def hourly_median(readings):
    """Median BG per NY hour-of-day → list of 24 values (None where empty)."""
    buckets = [[] for _ in range(24)]
    for ts, bg in readings:
        buckets[ts.hour].append(bg)
    return [statistics.median(b) if b else None for b in buckets]


# ── Rendering ────────────────────────────────────────────────────────────────

def _fmt_date(d):
    return f"{d.strftime('%b')} {d.day}"


def _dumbbell_rows(stats_prior, stats_current):
    """(label, prior_val, current_val, unit, lower_is_better) rows."""
    return [
        ("Time in Range", stats_prior["tir"], stats_current["tir"], "%", False),
        ("Avg Glucose", stats_prior["avg"], stats_current["avg"], " mg/dL", True),
        ("Time Below 70", stats_prior["low_pct"], stats_current["low_pct"], "%", True),
        ("Time Above 180", stats_prior["high_pct"], stats_current["high_pct"], "%", True),
    ]


def render_compare_png(readings_prior, readings_current, label_prior, label_current,
                       title, subtitle, footer, out_path):
    """Render the comparison graphic. Returns (out_path, stats dict)."""
    stats_p = period_stats(readings_prior)
    stats_c = period_stats(readings_current)
    if stats_c is None:
        raise ValueError("no CGM data in the current period")

    hours = list(range(24))
    med_p = hourly_median(readings_prior) if readings_prior else [None] * 24
    med_c = hourly_median(readings_current)

    fig = plt.figure(figsize=(8, 9.6), dpi=200)
    fig.patch.set_facecolor(COL_SURFACE)

    # ── Header ──
    fig.text(0.09, 0.965, title, fontsize=21, fontweight="bold", color=COL_TEXT)
    fig.text(0.09, 0.938, subtitle, fontsize=11.5, color=COL_TEXT_2)

    # ── Hourly profile ──
    ax = fig.add_axes([0.10, 0.52, 0.66, 0.375])
    ax.set_facecolor(COL_SURFACE)
    ax.axhspan(TIR_LOW, TIR_HIGH, color=COL_BAND, zorder=0)
    ax.text(0.35, TIR_HIGH - 6, "target range 70-180", fontsize=8.5,
            color=COL_TEXT_2, va="top")

    def plot_series(med, color, lw, z):
        xs = [h for h, v in zip(hours, med) if v is not None]
        ys = [v for v in med if v is not None]
        if xs:
            ax.plot(xs, ys, color=color, linewidth=lw, zorder=z,
                    solid_capstyle="round")
        return (xs[-1], ys[-1]) if xs else None

    end_p = plot_series(med_p, COL_PRIOR, 2.0, 2)
    end_c = plot_series(med_c, COL_CURRENT, 3.2, 3)

    all_vals = [v for v in med_p + med_c if v is not None]
    ax.set_ylim(min(60, min(all_vals) - 10), max(200, max(all_vals) + 15))

    # Direct labels at line ends (identity never color-alone). When the two
    # lines end close together, nudge the labels apart vertically.
    dy_c = dy_p = 0
    if end_c and end_p:
        span = ax.get_ylim()[1] - ax.get_ylim()[0] if all_vals else 1
        if abs(end_c[1] - end_p[1]) < span * 0.06:
            if end_c[1] >= end_p[1]:
                dy_c, dy_p = 7, -9
            else:
                dy_c, dy_p = -9, 7
    if end_c:
        ax.annotate(label_current, xy=end_c, xytext=(8, dy_c),
                    textcoords="offset points", fontsize=10.5, fontweight="bold",
                    color=COL_CURRENT, va="center", annotation_clip=False)
    if end_p:
        ax.annotate(label_prior, xy=end_p, xytext=(8, dy_p),
                    textcoords="offset points", fontsize=10.5,
                    color=COL_PRIOR, va="center", annotation_clip=False)

    ax.set_xlim(-0.3, 23.3)
    ax.set_xticks([0, 3, 6, 9, 12, 15, 18, 21, 23])
    ax.set_xticklabels(["12am", "3am", "6am", "9am", "12pm", "3pm", "6pm", "9pm", "11pm"],
                       fontsize=9.5, color=COL_TEXT_2)
    ax.tick_params(axis="y", labelsize=9.5, colors=COL_TEXT_2, length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=COL_GRID, linewidth=0.8, zorder=1)
    ax.set_ylabel("mg/dL", fontsize=10, color=COL_TEXT_2)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(COL_GRID)
    ax.margins(x=0)

    # ── Stat dumbbells ──
    if stats_p is not None:
        axs = fig.add_axes([0.10, 0.075, 0.80, 0.375])
        axs.set_facecolor(COL_SURFACE)
        axs.set_xlim(0, 1)
        axs.set_ylim(0, 1)
        axs.axis("off")

        # panel legend (series identity, once)
        axs.text(0.02, 1.00, label_prior, fontsize=10.5, color=COL_PRIOR,
                 fontweight="bold", va="top")
        axs.text(0.30, 1.00, "vs", fontsize=10.5, color=COL_TEXT_2, va="top")
        axs.text(0.37, 1.00, label_current, fontsize=10.5, color=COL_CURRENT,
                 fontweight="bold", va="top")

        rows = _dumbbell_rows(stats_p, stats_c)
        # dumbbell x-position scaled per-row between the two values
        for i, (label, pv, cv, unit, lower_better) in enumerate(rows):
            y = 0.86 - i * 0.235
            axs.text(0.02, y, label, fontsize=12.5, fontweight="bold", color=COL_TEXT)

            lo, hi = min(pv, cv), max(pv, cv)
            span = (hi - lo) or 1.0
            pad = span * 0.25
            x0, x1 = lo - pad, hi + pad

            def sx(v):
                return 0.42 + 0.54 * ((v - x0) / (x1 - x0))

            axs.plot([sx(pv), sx(cv)], [y, y], color=COL_GRID, linewidth=3,
                     zorder=1, solid_capstyle="round")
            axs.scatter([sx(pv)], [y], s=70, color=COL_PRIOR, zorder=2)
            axs.scatter([sx(cv)], [y], s=90, color=COL_CURRENT, zorder=3)

            diff = round(cv - pv, 1)
            improved = (diff < 0) if lower_better else (diff > 0)
            col = COL_GOOD if improved else (COL_BAD if diff != 0 else COL_TEXT_2)
            arrow = "↓" if diff < 0 else ("↑" if diff > 0 else "→")
            delta = f"({arrow}{abs(diff):g}{unit.strip() or ''})" if diff != 0 else "(no change)"
            val_txt = axs.text(0.02, y - 0.085,
                               f"{pv:g}{unit} → {cv:g}{unit}", fontsize=11.5,
                               color=COL_TEXT)
            axs.annotate(delta, xycoords=val_txt, xy=(1, 0), xytext=(7, 0),
                         textcoords="offset points", fontsize=11.5, color=col,
                         fontweight="bold", va="bottom")

    fig.text(0.09, 0.022, footer, fontsize=9, color=COL_TEXT_2)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=COL_SURFACE, bbox_inches=None)
    plt.close(fig)
    return out_path, {"current": stats_c, "prior": stats_p}


def render_comparison(conn, end_date_ny, days, label_prior=None, label_current=None,
                      title=None, out_path=None):
    """Fetch two adjacent periods ending at end_date_ny (inclusive) and render.

    Returns (png_path, stats). Raises ValueError when the current period has
    no CGM data.
    """
    cur_start = end_date_ny - timedelta(days=days - 1)
    prior_start = cur_start - timedelta(days=days)

    readings_c = fetch_period(conn, ny_midnight(cur_start),
                              ny_midnight(end_date_ny + timedelta(days=1)))
    readings_p = fetch_period(conn, ny_midnight(prior_start), ny_midnight(cur_start))

    label_prior = label_prior or f"Prior {days} days"
    label_current = label_current or f"Last {days} days"
    title = title or f"Last {days} Days vs Prior {days}"
    subtitle = (f"Hourly median glucose · {_fmt_date(cur_start)}–{_fmt_date(end_date_ny)} "
                f"vs {_fmt_date(prior_start)}–{_fmt_date(cur_start - timedelta(days=1))}")
    footer = (f"Prior: {len(readings_p)} readings · Current: {len(readings_c)} readings "
              f"· TypeOneZen {datetime.now(NY).strftime('%b %-d, %Y')}")

    if out_path is None:
        out_path = DEFAULT_OUT_DIR / f"bg_compare_{days}d_{end_date_ny.isoformat()}.png"

    return render_compare_png(readings_p, readings_c, label_prior, label_current,
                              title, subtitle, footer, out_path)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Render a BG comparison PNG")
    parser.add_argument("--mode", choices=("compare", "week"), default="compare")
    parser.add_argument("--days", type=int, default=3,
                        help="period length in days (compare mode)")
    parser.add_argument("--end", type=str, default=None,
                        help="NY date YYYY-MM-DD, last day of the current period (default: today)")
    parser.add_argument("--week-ending", type=str, default=None,
                        help="week mode: NY date of the last day of the trailing week (default: yesterday)")
    parser.add_argument("--out", type=str, default=None)
    args = parser.parse_args()

    now_ny = datetime.now(NY)
    if args.mode == "week":
        days = 7
        end_str = args.week_ending
        end_date = (datetime.strptime(end_str, "%Y-%m-%d").date() if end_str
                    else (now_ny - timedelta(days=1)).date())
        labels = ("Last week", "This week")
        title = "Weekly Report"
    else:
        days = args.days
        end_date = (datetime.strptime(args.end, "%Y-%m-%d").date() if args.end
                    else now_ny.date())
        labels = (f"Prior {days} days", f"Last {days} days")
        title = None

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        png, stats = render_comparison(
            conn, end_date, days,
            label_prior=labels[0], label_current=labels[1], title=title,
            out_path=args.out,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    finally:
        conn.close()

    print(json.dumps({"png": str(png), **stats}))


if __name__ == "__main__":
    main()
