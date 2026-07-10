"""Tests for scripts/watchdog.py — the independent launchd-driven dead-man's
switch that watches the cron-driven ns_sync/poller/monitor pipeline.

scripts/ has no __init__.py, so watchdog.py is loaded directly by file path
via importlib (per CLAUDE.md's guidance for scripts using the TZ_HOME
convention). TZ_HOME is pointed at a throwaway temp directory *before*
import so the module's import-time side effects (loading .env, creating
logs/watchdog.log via RotatingFileHandler) never touch the real repo or
read the real ALERT_PHONE — no test in this file should be able to send a
real iMessage or make a real network call; subprocess.run and
urllib.request.urlopen are always mocked.

Individual tests then point the module's path constants (MONITOR_LOG_PATH,
DB_PATH, STATE_PATH, HEALTHCHECKS_URL) at pytest's tmp_path via monkeypatch,
since watchdog.run() reads those as module attributes.
"""

import importlib.util
import json
import os
import subprocess
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG_PATH = REPO_ROOT / "scripts" / "watchdog.py"

# Isolate module import from the real repo's .env/logs (see module docstring
# above) — must happen before exec_module runs watchdog.py's top-level code.
_import_tz_home = Path(tempfile.mkdtemp(prefix="watchdog_import_"))
os.environ["TZ_HOME"] = str(_import_tz_home)

_spec = importlib.util.spec_from_file_location("watchdog_under_test", WATCHDOG_PATH)
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

UTC = timezone.utc


def make_db(path: Path, timestamp: str | None):
    """Create a minimal glucose_readings table, optionally with one row."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE glucose_readings (id INTEGER PRIMARY KEY, timestamp TEXT)")
    if timestamp is not None:
        conn.execute("INSERT INTO glucose_readings (timestamp) VALUES (?)", (timestamp,))
    conn.commit()
    conn.close()


# ── Rule 1: heartbeat check (logs/monitor.log mtime) ────────────────────

def test_check_heartbeat_fresh_passes(tmp_path):
    log = tmp_path / "monitor.log"
    log.write_text("ok\n")
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_heartbeat(log, now)

    assert ok is True
    assert msg is None
    assert age is not None and age < 1


def test_check_heartbeat_stale_flags(tmp_path):
    log = tmp_path / "monitor.log"
    log.write_text("ok\n")
    stale_time = (datetime.now(UTC) - timedelta(minutes=25)).timestamp()
    os.utime(log, (stale_time, stale_time))
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_heartbeat(log, now)

    assert ok is False
    assert age is not None and age >= 20
    assert "hasn't run" in msg
    assert "check the Mac" in msg


def test_check_heartbeat_missing_file_flags(tmp_path):
    log = tmp_path / "does_not_exist.log"
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_heartbeat(log, now)

    assert ok is False
    assert age is None
    assert "doesn't exist" in msg


# ── Rule 2: data freshness check (SQLite, read-only) ────────────────────

def test_check_data_freshness_fresh_passes(tmp_path):
    db = tmp_path / "db.sqlite"
    now = datetime.now(UTC)
    make_db(db, (now - timedelta(minutes=5)).isoformat())

    ok, msg, age = watchdog.check_data_freshness(db, now)

    assert ok is True
    assert msg is None


def test_check_data_freshness_stale_flags(tmp_path):
    db = tmp_path / "db.sqlite"
    now = datetime.now(UTC)
    make_db(db, (now - timedelta(minutes=90)).isoformat())

    ok, msg, age = watchdog.check_data_freshness(db, now)

    assert ok is False
    assert age is not None and age >= 60
    assert "no CGM data" in msg
    assert "check sensor/phone/Mac" in msg


def test_check_data_freshness_empty_table_flags(tmp_path):
    db = tmp_path / "db.sqlite"
    make_db(db, None)
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_data_freshness(db, now)

    assert ok is False
    assert "no CGM data in the database" in msg


def test_check_data_freshness_missing_db_flags(tmp_path):
    db = tmp_path / "missing.sqlite"
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_data_freshness(db, now)

    assert ok is False
    assert "can't read the database" in msg


def test_check_data_freshness_corrupt_db_flags(tmp_path):
    db = tmp_path / "corrupt.sqlite"
    db.write_text("this is not a sqlite database file")
    now = datetime.now(UTC)

    ok, msg, age = watchdog.check_data_freshness(db, now)

    assert ok is False
    assert "can't read the database" in msg


# ── Throttle logic (pure, operates on a plain dict) ──────────────────────

def test_should_alert_fires_on_first_failure():
    state = {}
    now = datetime.now(UTC)

    assert watchdog.should_alert(state, "heartbeat", now) is True
    assert "heartbeat" in state


def test_should_alert_suppresses_second_within_cooldown():
    state = {}
    now = datetime.now(UTC)
    assert watchdog.should_alert(state, "heartbeat", now) is True

    later = now + timedelta(minutes=30)
    assert watchdog.should_alert(state, "heartbeat", later) is False


def test_should_alert_fires_again_after_cooldown_elapses():
    state = {}
    now = datetime.now(UTC)
    watchdog.should_alert(state, "heartbeat", now)

    later = now + timedelta(hours=2, minutes=1)
    assert watchdog.should_alert(state, "heartbeat", later) is True


def test_clear_throttle_allows_immediate_realert():
    state = {}
    now = datetime.now(UTC)
    watchdog.should_alert(state, "heartbeat", now)

    soon = now + timedelta(minutes=5)
    assert watchdog.should_alert(state, "heartbeat", soon) is False  # still throttled

    watchdog.clear_throttle(state, "heartbeat")
    assert watchdog.should_alert(state, "heartbeat", soon) is True  # recovered -> refailed


def test_throttle_keys_are_independent():
    state = {}
    now = datetime.now(UTC)
    watchdog.should_alert(state, "heartbeat", now)

    # A different check type must not be throttled by heartbeat's state.
    assert watchdog.should_alert(state, "data", now) is True


# ── State file persistence ────────────────────────────────────────────────

def test_save_and_load_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = {"heartbeat": {"last_alert": "2026-07-10T12:00:00+00:00"}}

    watchdog.save_state(path, state)
    loaded = watchdog.load_state(path)

    assert loaded == state


def test_load_state_missing_file_returns_empty_dict(tmp_path):
    assert watchdog.load_state(tmp_path / "nope.json") == {}


def test_load_state_corrupt_file_returns_empty_dict(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    assert watchdog.load_state(path) == {}


# ── .env fallback parser ──────────────────────────────────────────────────

def test_manual_parse_env_reads_key_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'ALERT_PHONE=+15551234567\n'
        '# a comment\n'
        '\n'
        'HEALTHCHECKS_URL="https://hc-ping.com/some-uuid"\n'
    )

    values = watchdog._manual_parse_env(env_file)

    assert values["ALERT_PHONE"] == "+15551234567"
    assert values["HEALTHCHECKS_URL"] == "https://hc-ping.com/some-uuid"


def test_manual_parse_env_missing_file_returns_empty(tmp_path):
    assert watchdog._manual_parse_env(tmp_path / "nope.env") == {}


# ── send_alert: mockable, retries with backoff, never invokes real imsg ──

def test_send_alert_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, check, capture_output):
        calls["n"] += 1
        if calls["n"] < 2:
            raise subprocess.CalledProcessError(1, cmd)
        return None

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    monkeypatch.setattr(watchdog, "ALERT_PHONE", "+15551234567")

    ok = watchdog.send_alert("test message")

    assert ok is True
    assert calls["n"] == 2


def test_send_alert_gives_up_after_three_attempts(monkeypatch):
    calls = {"n": 0}

    def always_fail(cmd, check, capture_output):
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, cmd)

    sleeps = []
    monkeypatch.setattr(watchdog.subprocess, "run", always_fail)
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(watchdog, "ALERT_PHONE", "+15551234567")

    ok = watchdog.send_alert("test message")

    assert ok is False
    assert calls["n"] == 3
    assert sleeps == [10, 30]


def test_send_alert_without_phone_configured_noops(monkeypatch):
    monkeypatch.setattr(watchdog, "ALERT_PHONE", "")

    def unexpected_call(*a, **k):
        raise AssertionError("imsg should never be invoked with no ALERT_PHONE")

    monkeypatch.setattr(watchdog.subprocess, "run", unexpected_call)

    ok = watchdog.send_alert("test message")

    assert ok is False


# ── ping_healthchecks: best-effort, never raises ──────────────────────────

def test_ping_healthchecks_calls_urlopen_with_timeout(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))

        class _Resp:
            def close(self):
                pass

        return _Resp()

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    watchdog.ping_healthchecks("https://hc-ping.com/some-uuid")

    assert calls == [("https://hc-ping.com/some-uuid", 10)]


def test_ping_healthchecks_empty_url_is_noop(monkeypatch):
    def unexpected_call(*a, **k):
        raise AssertionError("urlopen should never be called with an empty URL")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", unexpected_call)

    watchdog.ping_healthchecks("")  # must not raise


def test_ping_healthchecks_swallows_errors(monkeypatch):
    def raising_urlopen(url, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", raising_urlopen)

    watchdog.ping_healthchecks("https://hc-ping.com/some-uuid")  # must not raise


# ── run(): end-to-end orchestration with everything mocked ──────────────

def _patch_run_paths(monkeypatch, monitor_log, db, state_path, healthchecks_url=""):
    monkeypatch.setattr(watchdog, "MONITOR_LOG_PATH", monitor_log)
    monkeypatch.setattr(watchdog, "DB_PATH", db)
    monkeypatch.setattr(watchdog, "STATE_PATH", state_path)
    monkeypatch.setattr(watchdog, "HEALTHCHECKS_URL", healthchecks_url)


def test_run_healthy_sends_nothing_and_pings_healthchecks(tmp_path, monkeypatch):
    monitor_log = tmp_path / "monitor.log"
    monitor_log.write_text("ok\n")
    db = tmp_path / "db.sqlite"
    now = datetime.now(UTC)
    make_db(db, (now - timedelta(minutes=5)).isoformat())
    state_path = tmp_path / "state.json"

    _patch_run_paths(monkeypatch, monitor_log, db, state_path,
                      healthchecks_url="https://hc-ping.com/fake-uuid")

    sent = []
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: (sent.append(msg), True)[1])

    pinged = []

    def fake_urlopen(url, timeout=None):
        pinged.append(url)

        class _Resp:
            def close(self):
                pass

        return _Resp()

    monkeypatch.setattr(watchdog.urllib.request, "urlopen", fake_urlopen)

    result = watchdog.run(now=now)

    assert result == 0
    assert sent == []
    assert pinged == ["https://hc-ping.com/fake-uuid"]
    # A healthy run must not persist any throttle entries.
    assert watchdog.load_state(state_path) == {}


def test_run_unhealthy_sends_alert_with_expected_content(tmp_path, monkeypatch):
    monitor_log = tmp_path / "monitor.log"
    monitor_log.write_text("ok\n")
    now = datetime.now(UTC)
    stale_time = (now - timedelta(minutes=30)).timestamp()
    os.utime(monitor_log, (stale_time, stale_time))

    db = tmp_path / "db.sqlite"
    make_db(db, (now - timedelta(minutes=5)).isoformat())
    state_path = tmp_path / "state.json"

    _patch_run_paths(monkeypatch, monitor_log, db, state_path)

    sent = []
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: (sent.append(msg), True)[1])

    watchdog.run(now=now)

    assert len(sent) == 1
    assert "hasn't run" in sent[0]
    assert "check the Mac" in sent[0]

    state = watchdog.load_state(state_path)
    assert "heartbeat" in state
    assert "data" not in state  # data check was healthy


def test_run_does_not_ping_healthchecks_when_any_check_fails(tmp_path, monkeypatch):
    monitor_log = tmp_path / "monitor.log"
    monitor_log.write_text("ok\n")
    now = datetime.now(UTC)
    stale_time = (now - timedelta(minutes=30)).timestamp()
    os.utime(monitor_log, (stale_time, stale_time))

    db = tmp_path / "db.sqlite"
    make_db(db, (now - timedelta(minutes=5)).isoformat())
    state_path = tmp_path / "state.json"

    _patch_run_paths(monkeypatch, monitor_log, db, state_path,
                      healthchecks_url="https://hc-ping.com/fake-uuid")
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: True)

    pinged = []
    monkeypatch.setattr(watchdog.urllib.request, "urlopen",
                         lambda url, timeout=None: pinged.append(url))

    watchdog.run(now=now)

    assert pinged == []


def test_run_throttles_repeated_failure_then_realerts_after_cooldown(tmp_path, monkeypatch):
    monitor_log = tmp_path / "monitor.log"
    monitor_log.write_text("ok\n")
    now0 = datetime.now(UTC)
    stale_time = (now0 - timedelta(minutes=30)).timestamp()
    os.utime(monitor_log, (stale_time, stale_time))  # stays stale for the whole test

    db = tmp_path / "db.sqlite"
    make_db(db, (now0 - timedelta(minutes=5)).isoformat())
    state_path = tmp_path / "state.json"

    _patch_run_paths(monkeypatch, monitor_log, db, state_path)

    sent = []
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: (sent.append(msg), True)[1])

    def heartbeat_alerts():
        return [m for m in sent if "hasn't run" in m]

    watchdog.run(now=now0)
    assert len(heartbeat_alerts()) == 1

    watchdog.run(now=now0 + timedelta(minutes=30))
    assert len(heartbeat_alerts()) == 1  # still within 2h cooldown

    # Refresh the CGM reading so only the heartbeat channel is under test at
    # the 2h-later mark (the data check has its own independent throttle).
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE glucose_readings SET timestamp = ?",
        ((now0 + timedelta(hours=2, minutes=1) - timedelta(minutes=5)).isoformat(),),
    )
    conn.commit()
    conn.close()

    watchdog.run(now=now0 + timedelta(hours=2, minutes=1))
    assert len(heartbeat_alerts()) == 2  # cooldown elapsed -> re-alerts


def test_run_realerts_immediately_after_recovery_and_new_failure(tmp_path, monkeypatch):
    monitor_log = tmp_path / "monitor.log"
    monitor_log.write_text("ok\n")
    now0 = datetime.now(UTC)

    db = tmp_path / "db.sqlite"
    make_db(db, (now0 - timedelta(minutes=5)).isoformat())
    state_path = tmp_path / "state.json"

    _patch_run_paths(monkeypatch, monitor_log, db, state_path)

    sent = []
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: (sent.append(msg), True)[1])

    # First failure: stale monitor.log.
    stale_time = (now0 - timedelta(minutes=30)).timestamp()
    os.utime(monitor_log, (stale_time, stale_time))
    watchdog.run(now=now0)
    assert len(sent) == 1

    # Recovery: monitor.log is fresh again.
    recovered_at = now0 + timedelta(minutes=5)
    os.utime(monitor_log, (recovered_at.timestamp(), recovered_at.timestamp()))
    watchdog.run(now=recovered_at)
    assert len(sent) == 1  # no new alert on recovery

    # New failure moments later -- well within the original 2h cooldown --
    # must still alert immediately, since the throttle was cleared on recovery.
    refail_at = recovered_at + timedelta(minutes=1)
    restale_time = (refail_at - timedelta(minutes=30)).timestamp()
    os.utime(monitor_log, (restale_time, restale_time))
    watchdog.run(now=refail_at)
    assert len(sent) == 2


def test_run_never_raises_and_always_returns_zero(tmp_path, monkeypatch):
    # Point checks at paths that will raise unexpected errors internally,
    # and confirm run()/main() still behave (exit-0 contract).
    monkeypatch.setattr(watchdog, "MONITOR_LOG_PATH", tmp_path / "missing.log")
    monkeypatch.setattr(watchdog, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(watchdog, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watchdog, "HEALTHCHECKS_URL", "")
    monkeypatch.setattr(watchdog, "send_alert", lambda msg: True)

    assert watchdog.run(now=datetime.now(UTC)) == 0
    assert watchdog.main() == 0


def test_main_swallows_unexpected_exceptions(monkeypatch):
    def boom():
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(watchdog, "run", boom)

    assert watchdog.main() == 0
