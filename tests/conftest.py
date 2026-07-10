"""Shared pytest fixtures for TypeOneZen tests.

The nightscout-client package is mocked here (never imported from the
sibling checkout): a stub module is installed in sys.modules before
ns_sync.py / monitor.py are imported, and tests drive the code with fake
clients / fake pump state. All DB access goes to a per-test temp SQLite file.
"""

import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Repo root on sys.path so `import db`, `import ns_sync`, `import monitor` work
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

UTC = ZoneInfo("UTC")


# ── Stub nightscout_client module (contract-shaped, no network) ──────

class NightscoutError(Exception):
    pass


class NightscoutConnectionError(NightscoutError):
    pass


class NightscoutAuthError(NightscoutError):
    pass


class NightscoutAPIError(NightscoutError):
    pass


def _parse_iso(ts):
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class FakeNightscoutClient:
    """In-memory stand-in matching the nightscout-client contract surface
    used by TypeOneZen (entries/treatments/now/pump)."""

    def __init__(self, entries=None, treatments=None, now_data=None,
                 pump_data=None, raise_exc=None):
        self._entries = entries or []
        self._treatments = treatments or []
        self._now = now_data or {}
        self._pump = pump_data or {}
        self._raise = raise_exc

    def _check(self):
        if self._raise is not None:
            raise self._raise

    def entries(self, since=None, until=None, count=None):
        self._check()
        items = self._entries
        if since is not None:
            items = [e for e in items if _parse_iso(e["time"]) >= _parse_iso(since)]
        if until is not None:
            items = [e for e in items if _parse_iso(e["time"]) <= _parse_iso(until)]
        if count is not None:
            items = items[:count]
        return list(items)

    def treatments(self, since=None, until=None, type=None):
        self._check()
        items = self._treatments
        if since is not None:
            items = [t for t in items if _parse_iso(t["time"]) >= _parse_iso(since)]
        if until is not None:
            items = [t for t in items if _parse_iso(t["time"]) <= _parse_iso(until)]
        if type is not None:
            items = [t for t in items if t.get("event_type") == type]
        return list(items)

    def now(self):
        self._check()
        return dict(self._now)

    def pump(self):
        self._check()
        return dict(self._pump)


def _install_stub_nightscout_client():
    """Install a stub nightscout_client package in sys.modules so imports in
    ns_sync.py / monitor.py resolve without the real package (and without
    ever importing the sibling checkout)."""
    mod = types.ModuleType("nightscout_client")
    exc_mod = types.ModuleType("nightscout_client.exceptions")

    exc_mod.NightscoutError = NightscoutError
    exc_mod.NightscoutConnectionError = NightscoutConnectionError
    exc_mod.NightscoutAuthError = NightscoutAuthError
    exc_mod.NightscoutAPIError = NightscoutAPIError

    mod.NightscoutClient = FakeNightscoutClient
    mod.exceptions = exc_mod

    sys.modules["nightscout_client"] = mod
    sys.modules["nightscout_client.exceptions"] = exc_mod


_install_stub_nightscout_client()

import db          # noqa: E402
import monitor     # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Connection to a fully initialized temp database."""
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "TypeOneZen.db")

    # alert_log/alert_snoozes/sync_state are owned by monitor.ensure_tables
    # (init_db's alert_log index requires the table to already exist)
    c = db.get_db()
    monitor.ensure_tables(c)
    c.close()

    db.init_db()

    c = db.get_db()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def reset_monitor_globals():
    """Clear monitor's per-run Nightscout caches and dry-run flag."""
    monitor._ns_state = None
    monitor._live_bg_state = None
    monitor._loop_state = None
    monitor._history_cache = None
    monitor.DRY_RUN = False
    yield
    monitor._ns_state = None
    monitor._live_bg_state = None
    monitor._loop_state = None
    monitor._history_cache = None
    monitor.DRY_RUN = False
