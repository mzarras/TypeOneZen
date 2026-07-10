"""monitor.py Nightscout pump/loop rule tests — state transitions, per-pod
dedup, and the unreachable-vs-stale distinction — plus live-BG fetch/fallback.

Pump state is injected by pre-filling monitor's per-run cache (monitor._ns_state),
so no network or real nightscout-client is involved.
"""

import subprocess
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from nightscout_client.exceptions import NightscoutConnectionError

import monitor

UTC = ZoneInfo("UTC")

POD_CHANGE_1 = "2026-07-04T12:00:00+00:00"
POD_CHANGE_2 = "2026-07-07T09:00:00+00:00"


def make_pump(**overrides):
    pump = {
        "reservoir": 40.0,
        "reservoir_exact": True,
        "reservoir_display": "40",
        "pod_age_hours": 24.0,
        "site_changed_at": POD_CHANGE_1,
        "battery_percent": 80,
        "loop_status": "looping",
        "last_loop_minutes_ago": 4.0,
        "data_age_minutes": 2.0,
    }
    pump.update(overrides)
    return pump


def set_ns_state(pump=None, unreachable=False, error=None):
    monitor._ns_state = {"pump": pump, "unreachable": unreachable, "error": error}


def log_alert(conn, alert):
    """Mimic what monitor.main() does after a rule fires."""
    conn.execute(
        "INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key) VALUES (?, ?, ?, 1, ?)",
        (alert["rule"], datetime.now(UTC).isoformat(), alert["message"],
         alert.get("dedup_key")),
    )
    conn.commit()


# ── LOW_RESERVOIR: state-transition semantics ────────────────────────

def test_low_reservoir_fires_on_downward_crossing_only(conn):
    # Above threshold → nothing
    set_ns_state(make_pump(reservoir=25.0, reservoir_display="25"))
    assert monitor.rule_low_reservoir(conn) == []

    # Crosses below 20 → fires once
    set_ns_state(make_pump(reservoir=18.0, reservoir_display="18"))
    alerts = monitor.rule_low_reservoir(conn)
    assert len(alerts) == 1
    assert alerts[0]["rule"] == "LOW_RESERVOIR"
    assert "18" in alerts[0]["message"]

    # Still below on the next polls → stays quiet
    set_ns_state(make_pump(reservoir=15.0, reservoir_display="15"))
    assert monitor.rule_low_reservoir(conn) == []
    set_ns_state(make_pump(reservoir=8.0, reservoir_display="8"))
    assert monitor.rule_low_reservoir(conn) == []


def test_low_reservoir_resets_when_back_above(conn):
    set_ns_state(make_pump(reservoir=18.0, reservoir_display="18"))
    assert len(monitor.rule_low_reservoir(conn)) == 1

    # Pod change → reservoir back above threshold → state resets, no alert
    set_ns_state(make_pump(reservoir=45.0, reservoir_display="45"))
    assert monitor.rule_low_reservoir(conn) == []

    # Next downward crossing fires again
    set_ns_state(make_pump(reservoir=19.0, reservoir_display="19"))
    assert len(monitor.rule_low_reservoir(conn)) == 1


def test_low_reservoir_ignores_inexact_50_plus(conn):
    # Omnipod reports "50+" above 50u → reservoir_exact False, never low
    set_ns_state(make_pump(reservoir=50.0, reservoir_exact=False,
                           reservoir_display="50+"))
    assert monitor.rule_low_reservoir(conn) == []


def test_low_reservoir_dry_run_does_not_persist_state(conn):
    monitor.DRY_RUN = True
    set_ns_state(make_pump(reservoir=18.0, reservoir_display="18"))
    assert len(monitor.rule_low_reservoir(conn)) == 1
    # Dry-run didn't record the crossing, so a live run still fires
    monitor.DRY_RUN = False
    assert len(monitor.rule_low_reservoir(conn)) == 1


def test_no_nightscout_no_alerts(conn):
    set_ns_state(pump=None, error="nightscout not configured")
    assert monitor.rule_low_reservoir(conn) == []
    assert monitor.rule_pod_age_warn(conn) == []
    assert monitor.rule_pod_age_urgent(conn) == []
    assert monitor.rule_loop_stale(conn) == []
    assert monitor.rule_nightscout_unreachable(conn) == []


# ── POD_AGE_WARN / POD_AGE_URGENT: once each per pod ─────────────────

def test_pod_age_below_thresholds_silent(conn):
    set_ns_state(make_pump(pod_age_hours=60.0))
    assert monitor.rule_pod_age_warn(conn) == []
    assert monitor.rule_pod_age_urgent(conn) == []


def test_pod_age_warn_fires_once_per_pod(conn):
    set_ns_state(make_pump(pod_age_hours=73.0))
    alerts = monitor.rule_pod_age_warn(conn)
    assert len(alerts) == 1
    assert alerts[0]["rule"] == "POD_AGE_WARN"
    assert alerts[0]["dedup_key"] == POD_CHANGE_1
    log_alert(conn, alerts[0])

    # Same pod, later poll → no repeat
    set_ns_state(make_pump(pod_age_hours=74.0))
    assert monitor.rule_pod_age_warn(conn) == []
    # Urgent threshold not reached yet
    assert monitor.rule_pod_age_urgent(conn) == []


def test_pod_age_urgent_fires_separately(conn):
    # Warn already fired earlier for this pod
    set_ns_state(make_pump(pod_age_hours=73.0))
    log_alert(conn, monitor.rule_pod_age_warn(conn)[0])

    # ≥78h → urgent fires once, with its own alert key
    set_ns_state(make_pump(pod_age_hours=78.5))
    alerts = monitor.rule_pod_age_urgent(conn)
    assert len(alerts) == 1
    assert alerts[0]["rule"] == "POD_AGE_URGENT"
    assert "hard stop" in alerts[0]["message"].lower()
    log_alert(conn, alerts[0])

    set_ns_state(make_pump(pod_age_hours=79.0))
    assert monitor.rule_pod_age_urgent(conn) == []
    assert monitor.rule_pod_age_warn(conn) == []  # warn already fired for this pod


def test_pod_age_warn_fires_again_for_new_pod(conn):
    set_ns_state(make_pump(pod_age_hours=73.0, site_changed_at=POD_CHANGE_1))
    log_alert(conn, monitor.rule_pod_age_warn(conn)[0])

    # New pod (new site_changed_at) that ages past 72h → fires again
    set_ns_state(make_pump(pod_age_hours=72.5, site_changed_at=POD_CHANGE_2))
    alerts = monitor.rule_pod_age_warn(conn)
    assert len(alerts) == 1
    assert alerts[0]["dedup_key"] == POD_CHANGE_2


# ── LOOP_STALE vs NIGHTSCOUT_UNREACHABLE ─────────────────────────────

def test_loop_stale_fires_when_devicestatus_old(conn):
    set_ns_state(make_pump(last_loop_minutes_ago=45.0))
    alerts = monitor.rule_loop_stale(conn)
    assert len(alerts) == 1
    assert alerts[0]["rule"] == "LOOP_STALE"
    assert "45 min" in alerts[0]["message"]
    log_alert(conn, alerts[0])

    # 2h dedup window (existing machinery) suppresses re-fires
    set_ns_state(make_pump(last_loop_minutes_ago=50.0))
    assert monitor.rule_loop_stale(conn) == []


def test_loop_stale_falls_back_to_data_age(conn):
    set_ns_state(make_pump(last_loop_minutes_ago=None, data_age_minutes=40.0))
    assert len(monitor.rule_loop_stale(conn)) == 1


def test_loop_fresh_is_silent(conn):
    set_ns_state(make_pump(last_loop_minutes_ago=10.0))
    assert monitor.rule_loop_stale(conn) == []


def test_unreachable_is_distinct_from_stale(conn):
    # Connection error: unreachable fires, stale does NOT (no pump data)
    set_ns_state(pump=None, unreachable=True, error="connection timed out")
    stale = monitor.rule_loop_stale(conn)
    unreachable = monitor.rule_nightscout_unreachable(conn)

    assert stale == []
    assert len(unreachable) == 1
    assert unreachable[0]["rule"] == "NIGHTSCOUT_UNREACHABLE"
    assert "unreachable" in unreachable[0]["message"].lower()
    log_alert(conn, unreachable[0])

    # Dedup window suppresses repeats
    assert monitor.rule_nightscout_unreachable(conn) == []

    # Reachable-but-stale produces the other alert key/message
    monitor._ns_state = None
    set_ns_state(make_pump(last_loop_minutes_ago=45.0))
    stale = monitor.rule_loop_stale(conn)
    assert len(stale) == 1
    assert stale[0]["rule"] == "LOOP_STALE"
    assert "unreachable" not in stale[0]["message"].lower()
    assert monitor.rule_nightscout_unreachable(conn) == []


# ── Live current BG: Nightscout-first with SQLite fallback ───────────

class _FakeLiveClient:
    """Minimal client exposing entries() for the live-BG path."""

    def __init__(self, entries=None, raise_exc=None):
        self._entries = entries or []
        self._raise = raise_exc

    def entries(self, since=None, until=None, count=None):
        if self._raise is not None:
            raise self._raise
        items = list(self._entries)
        if count is not None:
            items = items[:count]
        return items


def install_live_client(monkeypatch, client):
    """Point monitor's live-BG fetch at a fake client."""
    monkeypatch.setenv("NIGHTSCOUT_URL", "https://ns.example.test")
    monkeypatch.setattr(monitor, "NIGHTSCOUT_AVAILABLE", True)
    monkeypatch.setattr(monitor, "NightscoutClient",
                        types.SimpleNamespace(from_env=lambda: client))
    monitor._live_bg_state = None


def insert_reading(conn, ts, bg, trend=None):
    conn.execute(
        "INSERT INTO glucose_readings (timestamp, glucose_mg_dl, trend) VALUES (?, ?, ?)",
        (ts, bg, trend),
    )
    conn.commit()


def test_live_bg_used_when_fresher_than_sqlite(conn, monkeypatch):
    # The 2026-07-08 race: SQLite still holds a 6-min-old 79 while Nightscout
    # already has a fresh 71 — the alert must evaluate and cite the 71.
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=6)).isoformat(), 79)
    install_live_client(monkeypatch, _FakeLiveClient(entries=[
        {"time": now.isoformat(), "sgv": 71, "direction": "SingleDown"},
    ]))

    current = monitor.get_current_reading(conn)
    assert current["glucose_mg_dl"] == 71
    assert current["trend"] == "falling"

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "71" in alerts[0]["message"]
    assert "79" not in alerts[0]["message"]


def test_live_bg_falls_back_when_nightscout_unreachable(conn, monkeypatch):
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=4)).isoformat(), 72, trend="falling")
    install_live_client(monkeypatch, _FakeLiveClient(
        raise_exc=NightscoutConnectionError("connection timed out")))

    assert monitor.fetch_live_bg() is None
    assert monitor.get_current_reading(conn)["glucose_mg_dl"] == 72

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "72" in alerts[0]["message"]


def test_live_bg_stale_or_empty_falls_back(conn, monkeypatch):
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=4)).isoformat(), 85)

    # Stale live reading (older than LIVE_BG_MAX_AGE_MINUTES) → SQLite wins
    install_live_client(monkeypatch, _FakeLiveClient(entries=[
        {"time": (now - timedelta(minutes=45)).isoformat(), "sgv": 60,
         "direction": "Flat"},
    ]))
    assert monitor.fetch_live_bg() is None
    assert monitor.get_current_reading(conn)["glucose_mg_dl"] == 85

    # Empty result → SQLite wins
    install_live_client(monkeypatch, _FakeLiveClient(entries=[]))
    assert monitor.fetch_live_bg() is None
    assert monitor.get_current_reading(conn)["glucose_mg_dl"] == 85


def test_live_bg_degrades_gracefully_under_stub(conn):
    # conftest's stub NightscoutClient has no from_env(); the fetch must not
    # raise, and rules must fall back to SQLite as before.
    insert_reading(conn, datetime.now(UTC).isoformat(), 100)
    assert monitor.fetch_live_bg() is None
    assert monitor.get_current_reading(conn)["glucose_mg_dl"] == 100


# ── Loop-aware low alerting ──────────────────────────────────────────
#
# The 2026-07-09/10 false-alarm night: 8 LOW_WARNING + 2 RAPID_DROP alerts
# while Trio had basal suspended and BG never went below 71. New behavior:
# defer to Trio's own predictions; only page when the loop can't fix it.

def make_loop(**overrides):
    loop = {
        "timestamp": datetime.now(UTC).isoformat(),
        "device": "Trio",
        "bg": 90.0,
        "eventual_bg": 95.0,
        "iob": 0.5,
        "cob": 0.0,
        "isf": 42.0,
        "temp_rate": 0.0,
        "temp_duration": 60.0,
        "enacted": True,
        "reason": "minPredBG 85, IOBpredBG 95; Eventual BG 95 >= 70",
        "pred_bgs": {"IOB": [90, 88, 86, 85, 86, 88, 91, 95],
                     "ZT": [90, 87, 85, 85, 87, 90, 94, 99]},
        "data_age_minutes": 2.0,
    }
    loop.update(overrides)
    return loop


def set_loop_state(loop):
    monitor._loop_state = {"loop": loop}


def test_low_suppressed_when_trio_predicts_recovery(conn):
    # The screenshot case: BG 93 falling -22/15min, loop zero-temped and
    # predicting a nadir of 85 → old rule paged, new rule stays quiet.
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 115)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 104)
    insert_reading(conn, now.isoformat(), 93, trend="falling")
    set_loop_state(make_loop(bg=93.0))

    assert monitor.rule_low_warning(conn) == []


def test_low_fires_on_trio_carbs_req(conn):
    now = datetime.now(UTC)
    insert_reading(conn, now.isoformat(), 76, trend="falling")
    set_loop_state(make_loop(
        bg=76.0, eventual_bg=55.0,
        reason="minGuardBG 52<66 add 15g carbs req within 30m",
        pred_bgs={"IOB": [76, 70, 64, 58, 54, 52, 53, 56]},
    ))

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "15g" in alerts[0]["message"]
    assert "Trio" in alerts[0]["message"]


def test_low_fires_when_near_low_and_trio_predicts_below_70(conn):
    now = datetime.now(UTC)
    insert_reading(conn, now.isoformat(), 76, trend="falling")
    set_loop_state(make_loop(
        bg=76.0, eventual_bg=80.0,
        pred_bgs={"IOB": [76, 72, 68, 65, 66, 70, 75, 80]},
    ))

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "65" in alerts[0]["message"]  # predicted nadir cited


def test_low_below_70_always_alerts_even_with_healthy_loop(conn):
    now = datetime.now(UTC)
    insert_reading(conn, now.isoformat(), 64, trend="falling")
    set_loop_state(make_loop(bg=64.0, eventual_bg=90.0,
                             pred_bgs={"IOB": [64, 70, 78, 85, 90]}))

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "LOW" in alerts[0]["message"]
    assert "64" in alerts[0]["message"]


def test_followup_reverifies_full_conditions(conn):
    # Regression: "Still trending low: BG 108↘" — a follow-up must re-run
    # the whole assessment, not just check BG < 80 + stable.
    log_alert(conn, {"rule": "LOW_WARNING", "message": "first alert"})
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 118)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 112)
    insert_reading(conn, now.isoformat(), 108, trend="falling slightly")
    set_loop_state(make_loop(bg=108.0, eventual_bg=105.0,
                             pred_bgs={"IOB": [108, 106, 104, 103, 104, 106]}))

    assert monitor.rule_low_warning(conn) == []


def test_low_fallback_fires_without_loop_when_near_low_falling(conn):
    # No loop visibility + BG 74 falling → CGM-only backstop still pages.
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 92)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 83)
    insert_reading(conn, now.isoformat(), 74, trend="falling")
    set_loop_state(None)

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "no loop data" in alerts[0]["message"]


def test_low_fallback_pattern_gate_suppresses_self_resolving_drops(conn):
    # Seed 12 historical episodes shaped like the current drop (BG ~90,
    # ~-30/15min) that all bottom out at 80 and recover — the pattern gate
    # should conclude drops like this don't reach 70 and stay quiet.
    base = datetime.now(UTC) - timedelta(days=30)
    for ep in range(12):
        t0 = base + timedelta(hours=3 * ep)
        for j, bg in enumerate([110, 100, 90, 84, 80, 82, 88, 95, 103, 110]):
            insert_reading(conn, (t0 + timedelta(minutes=5 * j)).isoformat(), bg)

    now = datetime.now(UTC)
    # No Dexcom trend string → the rate is computed from the readings (-30/15min)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 110)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 100)
    insert_reading(conn, now.isoformat(), 90)
    set_loop_state(None)

    assert monitor.rule_low_warning(conn) == []


def test_low_fallback_pattern_gate_allows_historically_real_drops(conn):
    # Same shape but history says these drops DO reach the 50s → alert.
    base = datetime.now(UTC) - timedelta(days=30)
    for ep in range(12):
        t0 = base + timedelta(hours=3 * ep)
        for j, bg in enumerate([110, 100, 90, 78, 68, 58, 55, 60, 70, 82]):
            insert_reading(conn, (t0 + timedelta(minutes=5 * j)).isoformat(), bg)

    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 110)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 100)
    insert_reading(conn, now.isoformat(), 90)
    set_loop_state(None)

    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "Similar drops" in alerts[0]["message"]


# ── Rapid drop: loop-blind backstop only ─────────────────────────────

def _seed_rapid_drop(conn, current_bg=94):
    now = datetime.now(UTC)
    for j, bg in enumerate([128, 120, 112, 104, 98, current_bg]):
        insert_reading(conn, (now - timedelta(minutes=25 - 5 * j)).isoformat(), bg)


def test_rapid_drop_suppressed_when_loop_is_fresh(conn):
    # This morning's 128→94 false alarm: Trio saw it and had zero-temped.
    _seed_rapid_drop(conn)
    set_loop_state(make_loop(bg=94.0))
    assert monitor.rule_rapid_drop(conn) == []


def test_rapid_drop_fires_without_loop_when_heading_low(conn):
    _seed_rapid_drop(conn)
    set_loop_state(None)
    alerts = monitor.rule_rapid_drop(conn)
    assert len(alerts) == 1
    assert "no loop" in alerts[0]["message"].lower()


def test_rapid_drop_ignores_high_range_drops(conn):
    # 200→160 is a correction working, not a low emergency.
    now = datetime.now(UTC)
    for j, bg in enumerate([200, 192, 184, 176, 168, 160]):
        insert_reading(conn, (now - timedelta(minutes=25 - 5 * j)).isoformat(), bg)
    set_loop_state(None)
    assert monitor.rule_rapid_drop(conn) == []


def test_rapid_drop_defers_to_recent_low_warning(conn):
    _seed_rapid_drop(conn)
    set_loop_state(None)
    log_alert(conn, {"rule": "LOW_WARNING", "message": "already paged"})
    assert monitor.rule_rapid_drop(conn) == []


# ── carbsReq reason parsing ──────────────────────────────────────────

def test_parse_carbs_req_formats():
    assert monitor.parse_carbs_req("minGuardBG 52<66 add 15g carbs req within 30m") == 15
    assert monitor.parse_carbs_req("30 add'l carbs req w/in 45m") == 30
    assert monitor.parse_carbs_req("carbsReq: 20") == 20
    assert monitor.parse_carbs_req("Eventual BG 110 >= 100, insulinReq 0.24") is None
    assert monitor.parse_carbs_req(None) is None


# ── Loop-aware stuck-high alerting ───────────────────────────────────

def seed_high(conn, minutes, bg=240, end_offset_min=0):
    """Insert a flat high episode ending end_offset_min ago."""
    now = datetime.now(UTC)
    n = minutes // 5 + 1
    for j in range(n):
        ts = now - timedelta(minutes=end_offset_min + 5 * (n - 1 - j))
        insert_reading(conn, ts.isoformat(), bg)


def test_high_suppressed_when_trio_predicts_landing_in_range(conn):
    # Jul 9 real case: BG 242 but Trio's eventual_bg said 173 — it resolved.
    seed_high(conn, 60, bg=240)
    set_loop_state(make_loop(bg=240.0, eventual_bg=150.0, temp_rate=3.0,
                             pred_bgs=None))
    assert monitor.rule_high_stuck(conn) == []


def test_high_stuck_fires_with_trio_context(conn):
    seed_high(conn, 60, bg=240)
    set_loop_state(make_loop(bg=240.0, eventual_bg=210.0, temp_rate=3.0,
                             iob=2.8, cob=0.0, pred_bgs=None,
                             reason="insulinReq 1.4"))
    monitor._loop_state["loop"]["insulin_req"] = 1.4

    alerts = monitor.rule_high_stuck(conn)
    assert len(alerts) == 1
    msg = alerts[0]["message"]
    assert "stuck" in msg.lower()
    assert "1.4u" in msg
    assert "210" in msg
    assert alerts[0]["dedup_key"] is not None


def test_high_not_yet_persisted_stays_quiet(conn):
    seed_high(conn, 20, bg=240)
    set_loop_state(make_loop(bg=240.0, eventual_bg=210.0, pred_bgs=None))
    monitor._loop_state["loop"]["insulin_req"] = 1.4
    assert monitor.rule_high_stuck(conn) == []


def test_high_falling_is_suppressed(conn):
    # High but dropping fast — Trio's correction is landing; don't page.
    now = datetime.now(UTC)
    seed_high(conn, 60, bg=250, end_offset_min=15)
    insert_reading(conn, (now - timedelta(minutes=10)).isoformat(), 245)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 232)
    insert_reading(conn, now.isoformat(), 220)
    set_loop_state(make_loop(bg=220.0, eventual_bg=210.0, pred_bgs=None))
    monitor._loop_state["loop"]["insulin_req"] = 1.0
    assert monitor.rule_high_stuck(conn) == []


def test_high_urgent_fires_even_with_healthy_loop(conn):
    seed_high(conn, 45, bg=250, end_offset_min=40)   # lead-in outside urgent window
    seed_high(conn, 35, bg=310)
    set_loop_state(make_loop(bg=310.0, eventual_bg=120.0, pred_bgs=None))

    alerts = monitor.rule_high_stuck(conn)
    assert len(alerts) == 1
    assert "310" in alerts[0]["message"]
    assert "pod site" in alerts[0]["message"].lower()


def test_high_site_suspect_when_maxed_for_hours(conn):
    seed_high(conn, 200, bg=230)
    set_loop_state(make_loop(
        bg=230.0, eventual_bg=205.0, temp_rate=3.0, pred_bgs=None,
        reason="adj. req. rate: 4.5 to maxSafeBasal: 3, insulinReq 1.9",
    ))
    monitor._loop_state["loop"]["insulin_req"] = 1.9

    alerts = monitor.rule_high_stuck(conn)
    assert len(alerts) == 1
    msg = alerts[0]["message"]
    assert "maxed" in msg.lower()
    assert "pod" in msg.lower()


def test_high_loop_blind_fallback(conn):
    seed_high(conn, 90, bg=230)
    set_loop_state(None)

    alerts = monitor.rule_high_stuck(conn)
    assert len(alerts) == 1
    assert "no loop visibility" in alerts[0]["message"].lower()


def test_high_new_episode_gets_fresh_escalation(conn):
    # An alert from a long-resolved episode must not raise the level or
    # delay the first page of a new episode (old SUSTAINED_HIGH bug).
    old_key = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    conn.execute(
        "INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key)"
        " VALUES (?, ?, ?, 1, ?)",
        ("HIGH_STUCK", (datetime.now(UTC) - timedelta(hours=5)).isoformat(),
         "old episode", old_key),
    )
    conn.commit()

    seed_high(conn, 60, bg=240)
    set_loop_state(make_loop(bg=240.0, eventual_bg=210.0, pred_bgs=None))
    monitor._loop_state["loop"]["insulin_req"] = 1.4

    alerts = monitor.rule_high_stuck(conn)
    assert len(alerts) == 1
    assert alerts[0]["dedup_key"] != old_key
    # Level 0 message: no "first alert" backreference
    assert "first alert" not in alerts[0]["message"]


def test_single_sensor_dip_does_not_split_episode(conn):
    now = datetime.now(UTC)
    seed_high(conn, 40, bg=235, end_offset_min=25)
    insert_reading(conn, (now - timedelta(minutes=20)).isoformat(), 178)  # one dip
    seed_high(conn, 15, bg=235)
    set_loop_state(make_loop(bg=235.0, eventual_bg=210.0, pred_bgs=None))
    monitor._loop_state["loop"]["insulin_req"] = 1.0

    episode = monitor.current_high_episode(conn)
    assert episode is not None
    assert episode["duration_min"] >= 60  # spans the dip


def test_failed_sends_do_not_consume_escalation_slots(conn):
    # Deploy-night bug: sent=0 rows throttled retries of undelivered alerts.
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key)"
        " VALUES (?, ?, ?, 0, NULL)",
        ("LOW_WARNING", (now - timedelta(minutes=5)).isoformat(), "never delivered"),
    )
    conn.commit()

    insert_reading(conn, now.isoformat(), 64, trend="falling")
    set_loop_state(make_loop(bg=64.0))

    # With sent=0 counted this would wait 15 min; it must fire now instead.
    alerts = monitor.rule_low_warning(conn)
    assert len(alerts) == 1
    assert "first alert" not in alerts[0]["message"]


# ── NO_RECENT_DATA: silent-outage backstop ────────────────────────────
#
# The June 2026 bug: a silent 4.8h overnight CGM gap produced zero alerts
# because every other rule needs a current reading to evaluate at all.

def test_no_recent_data_fires_when_stale_and_no_live(conn):
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=45)).isoformat(), 118)

    alerts = monitor.rule_no_recent_data(conn)
    assert len(alerts) == 1
    msg = alerts[0]["message"]
    assert "118" in msg
    assert "45 min" in msg


def test_no_recent_data_silent_with_fresh_db_reading(conn):
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=5)).isoformat(), 100)
    assert monitor.rule_no_recent_data(conn) == []


def test_no_recent_data_silent_with_fresh_live_reading(conn, monkeypatch):
    # Even though the DB row is stale, a fresh live Nightscout reading
    # means data isn't actually silent.
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=45)).isoformat(), 118)
    install_live_client(monkeypatch, _FakeLiveClient(entries=[
        {"time": now.isoformat(), "sgv": 100, "direction": "Flat"},
    ]))
    assert monitor.rule_no_recent_data(conn) == []


def test_no_recent_data_fires_when_db_empty(conn):
    alerts = monitor.rule_no_recent_data(conn)
    assert len(alerts) == 1
    assert "no cgm data" in alerts[0]["message"].lower()


def test_no_recent_data_two_hour_cooldown(conn):
    now = datetime.now(UTC)
    insert_reading(conn, (now - timedelta(minutes=45)).isoformat(), 118)

    alerts = monitor.rule_no_recent_data(conn)
    assert len(alerts) == 1
    log_alert(conn, {"rule": "NO_RECENT_DATA", "message": alerts[0]["message"]})

    # Outage persists, but the 2h cooldown should suppress an immediate re-fire.
    assert monitor.rule_no_recent_data(conn) == []


def test_no_recent_data_failed_send_does_not_suppress_retry(conn):
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO alert_log (rule_name, triggered_at, message, sent, dedup_key)"
        " VALUES (?, ?, ?, 0, NULL)",
        ("NO_RECENT_DATA", (now - timedelta(minutes=5)).isoformat(), "never delivered"),
    )
    conn.commit()
    insert_reading(conn, (now - timedelta(minutes=45)).isoformat(), 118)

    alerts = monitor.rule_no_recent_data(conn)
    assert len(alerts) == 1


# ── send_imsg: retry with backoff ──────────────────────────────────────
#
# Safety priority inversion fix: the routine daily-summary script retried
# sends 3x while this safety-critical alerter didn't retry at all.

def test_send_imsg_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(monitor, "PHONE", "+15555550100")

    calls = {"run": 0}
    sleeps = []

    def fake_run(*args, **kwargs):
        calls["run"] += 1
        if calls["run"] < 3:
            raise subprocess.CalledProcessError(1, args[0] if args else "imsg")
        return None

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: sleeps.append(s))

    monitor.send_imsg("test message")

    assert calls["run"] == 3
    assert sleeps == [10, 30]


def test_send_imsg_reraises_after_three_failures(monkeypatch):
    monkeypatch.setattr(monitor, "PHONE", "+15555550100")

    calls = {"run": 0}
    sleeps = []

    def fake_run(*args, **kwargs):
        calls["run"] += 1
        raise subprocess.CalledProcessError(1, args[0] if args else "imsg")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(subprocess.CalledProcessError):
        monitor.send_imsg("test message")

    assert calls["run"] == 3
    assert sleeps == [10, 30]
