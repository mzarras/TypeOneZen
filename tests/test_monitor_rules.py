"""monitor.py Nightscout pump/loop rule tests — state transitions, per-pod
dedup, and the unreachable-vs-stale distinction.

Pump state is injected by pre-filling monitor's per-run cache (monitor._ns_state),
so no network or real nightscout-client is involved.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
