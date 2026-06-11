"""b249: the startup health poll no longer holds ``_lock``.

Previously the full startup_timeout (default 40s) was held under the lock:
every concurrent ensure_running, stop() and lifespan shutdown queued behind
the starter, each then paying its own full timeout — K stacked request
threads ≈ K×40s of pinned anyio threadpool workers when the server was slow
or wedged. Now followers wait on a start event and judge by the outcome, and
stop() returns promptly mid-start.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

import pytest

from app.core import model_server as ms


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
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
    if ms._proc is not None and getattr(ms._proc, "pid", None):
        try:
            os.killpg(os.getpgid(ms._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, AttributeError):
            pass
    ms._proc = None
    ms._started_adapter_sig = None
    ms._shutting_down = False
    ms._consecutive_startup_timeouts = 0
    ms._respawn_blocked_until = 0.0
    ms._start_event = None


def test_follower_waits_instead_of_double_spawning(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    healthy = {"v": False}
    monkeypatch.setattr(ms, "is_healthy", lambda **k: healthy["v"])
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 42.0)
    spawned = {"n": 0}

    class _P:
        def poll(self):
            return None

    def fake_popen(*a, **k):
        spawned["n"] += 1
        return _P()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    results: dict[str, bool] = {}

    def starter():
        results["a"] = ms.ensure_running(startup_timeout=10)

    def follower():
        results["b"] = ms.ensure_running(startup_timeout=10)

    ta = threading.Thread(target=starter)
    ta.start()
    # wait until the starter has actually spawned and is polling
    for _ in range(100):
        if spawned["n"] == 1 and ms._start_event is not None:
            break
        time.sleep(0.02)
    assert spawned["n"] == 1

    tb = threading.Thread(target=follower)
    tb.start()
    time.sleep(0.3)  # follower should now be waiting on the event, not spawning
    assert spawned["n"] == 1

    healthy["v"] = True  # "model finished loading"
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert results == {"a": True, "b": True}
    assert spawned["n"] == 1  # exactly one spawn for both callers
    assert ms._start_event is None  # event cleaned up
    ms._proc = None


def test_stop_returns_promptly_during_inflight_start(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: False)  # never healthy
    monkeypatch.setattr(ms, "_adapter_sig", lambda: None)
    spawned = {"n": 0}
    real_popen = subprocess.Popen  # ms.subprocess IS this module — capture first

    def fake_popen(*a, **k):
        spawned["n"] += 1
        # A REAL child (own session) so stop()'s killpg/wait path is exercised.
        return real_popen(["/bin/sleep", "30"], start_new_session=True)

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    results: dict[str, bool] = {}
    t = threading.Thread(target=lambda: results.__setitem__("a", ms.ensure_running(startup_timeout=8)))
    t.start()
    for _ in range(100):
        if spawned["n"] == 1:
            break
        time.sleep(0.02)
    assert spawned["n"] == 1

    t0 = time.monotonic()
    ms.stop()  # previously queued behind the starter's whole health poll
    stop_duration = time.monotonic() - t0
    assert stop_duration < 3  # not the starter's 8s timeout

    t.join(timeout=10)
    assert results["a"] is False  # starter noticed its child was reaped
    assert ms._proc is None
    assert ms._start_event is None


def test_request_path_fast_path_unaffected_during_start(monkeypatch):
    """A caller whose adapter sig matches the stamped one returns True on the
    lock-free fast path even while a (re)start is in flight elsewhere."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 7.0)
    ms._started_adapter_sig = 7.0
    ms._start_event = threading.Event()  # simulate an in-flight start
    spawned = {"n": 0}
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("n", 1))
    assert ms.ensure_running(startup_timeout=5) is True
    assert spawned["n"] == 0
    ms._start_event = None
