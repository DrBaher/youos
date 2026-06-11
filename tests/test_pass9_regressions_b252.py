"""b252: pass-9 regression tests for previously-fixed cross-component drift.

1. Launcher/origin port consistency (the b165 class): the prod launcher's
   hardcoded YOUOS_PORT default must equal the config module's
   DEFAULT_SERVER_PORT, and the Origin allowlist must track the served port
   through the YOUOS_PORT env var. The original bug 403'd every
   authenticated POST the moment a PIN was set.
2. Concurrent post-retrain reload (the b159/b242/b249 class): N threads
   calling ensure_running while the adapter sig changes must produce exactly
   ONE reload spawn and stamp the new sig.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from app.core import model_server as ms
from app.core.auth import compute_allowed_origins
from app.core.config import DEFAULT_SERVER_PORT, resolve_server_port

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_default_port_matches_config_default():
    """run_youos.sh hardcodes its YOUOS_PORT fallback; config.py promises (in
    a comment) to stay in sync. Enforce the promise — drift here recreates
    the b165 lockout where the Origin allowlist never matched the bind port."""
    launcher = (ROOT / "scripts" / "run_youos.sh").read_text()
    m = re.search(r'PORT="\$\{YOUOS_PORT:-(\d+)\}"', launcher)
    assert m, "run_youos.sh no longer sets PORT from YOUOS_PORT — update this test and resolve_server_port's docs"
    assert int(m.group(1)) == DEFAULT_SERVER_PORT


def test_allowed_origins_track_served_port_env(monkeypatch):
    """End-to-end through the env var: with YOUOS_PORT set (as the launcher
    does) and a STALE config port, the allowlist must contain the served
    origin — the exact b165 failure shape."""
    monkeypatch.setenv("YOUOS_PORT", "8765")
    config = {"server": {"pin": "hashed", "port": 8901}}  # stale config port
    assert resolve_server_port(config) == 8765
    origins = compute_allowed_origins(config)
    assert "http://127.0.0.1:8765" in origins


def test_malformed_youos_port_falls_through(monkeypatch):
    monkeypatch.setenv("YOUOS_PORT", "not-a-port")
    assert resolve_server_port({"server": {"port": 9000}}) == 9000
    monkeypatch.delenv("YOUOS_PORT")
    assert resolve_server_port({}) == DEFAULT_SERVER_PORT


@pytest.fixture
def _ms_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "get_server_config", lambda: {"enabled": True, "port": 8088})
    monkeypatch.setattr(ms, "_pidfile", lambda: tmp_path / "model_server.pid")
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen3-4B-Instruct-2507")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)
    ms._proc = None
    ms._started_adapter_sig = None
    ms._shutting_down = False
    ms._consecutive_startup_timeouts = 0
    ms._respawn_blocked_until = 0.0
    ms._start_event = None
    yield
    ms._proc = None
    ms._started_adapter_sig = None
    ms._shutting_down = False
    ms._consecutive_startup_timeouts = 0
    ms._respawn_blocked_until = 0.0
    ms._start_event = None


def test_concurrent_postretrain_threads_reload_exactly_once(_ms_state, monkeypatch):
    """The retrain thundering herd: the warm server is healthy on the OLD
    adapter when a promotion lands (sig 1.0 → 2.0) and six request threads
    call ensure_running at once. Exactly one must tear down + respawn; the
    rest follow. No deadlock, everyone True, new sig stamped."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)  # old AND new server healthy
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 2.0)  # promotion already landed
    ms._started_adapter_sig = 1.0  # what the running server loaded

    class _OldProc:
        def poll(self):
            return 0  # already exited → reap is a cheap wait()

        def wait(self, timeout=None):
            return 0

    ms._proc = _OldProc()
    spawned = {"n": 0}
    spawn_lock = threading.Lock()

    class _NewProc:
        def poll(self):
            return None

    def fake_popen(*a, **k):
        with spawn_lock:
            spawned["n"] += 1
        return _NewProc()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    results: list[bool] = []
    res_lock = threading.Lock()

    def caller():
        r = ms.ensure_running(startup_timeout=10)
        with res_lock:
            results.append(r)

    threads = [threading.Thread(target=caller) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)  # no deadlock
    assert results == [True] * 6
    assert spawned["n"] == 1  # exactly one reload for the whole herd
    assert ms._started_adapter_sig == 2.0
    ms._proc = None
