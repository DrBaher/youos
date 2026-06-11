"""b242: model-server process ownership, adapter-sig TOCTOU, shutdown flag,
startup-timeout cooldown, full-size safetensors check, atomic rollback.

The HIGH finding: a child that survives a parent crash (own session) answers
/health for the restarted parent's bind-failed duplicate spawn — the stale
weights then get stamped as current forever. Ownership is now recorded in a
pidfile so a later process can kill the previous incarnation's child.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from app.core import model_server as ms
from app.evaluation.promotion import restore_adapter


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "get_server_config", lambda: {"enabled": True, "port": 8088})
    monkeypatch.setattr(ms, "_pidfile", lambda: tmp_path / "model_server.pid")
    ms._proc = None
    ms._started_adapter_sig = None
    ms._shutting_down = False
    ms._consecutive_startup_timeouts = 0
    ms._respawn_blocked_until = 0.0
    yield
    ms._proc = None
    ms._started_adapter_sig = None
    ms._shutting_down = False
    ms._consecutive_startup_timeouts = 0
    ms._respawn_blocked_until = 0.0


def _tensor_file(data_len: int, *, declared_end: int = 8) -> bytes:
    header = json.dumps({"w": {"dtype": "F32", "shape": [2], "data_offsets": [0, declared_end]}}).encode()
    return len(header).to_bytes(8, "little") + header + b"\x00" * data_len


def test_safetensors_ok_rejects_data_truncation(tmp_path):
    f = tmp_path / "adapters.safetensors"
    f.write_bytes(_tensor_file(8))  # complete: declared 8 bytes, 8 present
    assert ms._safetensors_ok(f) is True
    f.write_bytes(_tensor_file(4))  # truncated mid-tensor-data
    assert ms._safetensors_ok(f) is False
    f.write_bytes(b"")
    assert ms._safetensors_ok(f) is False


def test_ensure_running_refuses_after_shutdown(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    spawned = {"n": 0}
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1))
    monkeypatch.setattr(ms, "is_healthy", lambda **k: False)
    ms._shutting_down = True
    assert ms.ensure_running() is False
    assert spawned["n"] == 0


def test_stamp_uses_sig_captured_at_spawn(monkeypatch):
    """TOCTOU: a promotion landing during the model load must NOT be stamped
    as already-served — the next call has to see the mismatch and reload."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen3-4B-Instruct-2507")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)
    health = iter([False, False, True])
    monkeypatch.setattr(ms, "is_healthy", lambda **k: next(health, True))
    sigs = iter([100.0])  # read once, at spawn time
    monkeypatch.setattr(ms, "_adapter_sig", lambda: next(sigs, 200.0))  # 200.0 = promoted mid-load

    class _P:
        def poll(self):
            return None

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: _P())
    assert ms.ensure_running(startup_timeout=5) is True
    assert ms._started_adapter_sig == 100.0  # what the server LOADED, not what's on disk now


def test_unowned_healthy_server_is_killed_before_spawn(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen3-4B-Instruct-2507")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 50.0)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)  # someone answers, but _proc is None
    killed = {"n": 0}
    monkeypatch.setattr(ms, "_kill_recorded_server", lambda: killed.__setitem__("n", killed["n"] + 1))

    class _P:
        def poll(self):
            return None

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: _P())
    assert ms.ensure_running(startup_timeout=5) is True
    assert killed["n"] == 1  # the orphan was killed instead of silently adopted


def test_kill_recorded_server_kills_recorded_pid(monkeypatch, tmp_path):
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        (tmp_path / "model_server.pid").write_text(json.dumps({"pid": sleeper.pid, "port": 8088}))

        class _PsResult:
            stdout = "python -m mlx_lm.server --port 8088"

        monkeypatch.setattr(ms.subprocess, "run", lambda *a, **k: _PsResult())
        ms._kill_recorded_server()
        # killpg(SIGTERM) hit the sleeper's (own) session
        assert sleeper.wait(timeout=5) != 0
        assert not (tmp_path / "model_server.pid").exists()
    finally:
        if sleeper.poll() is None:
            os.killpg(os.getpgid(sleeper.pid), signal.SIGKILL)


def test_kill_recorded_server_ignores_reused_pid(tmp_path):
    # Record OUR OWN pid: alive, but ps shows pytest — not an mlx_lm.server.
    (tmp_path / "model_server.pid").write_text(json.dumps({"pid": os.getpid(), "port": 8088}))
    ms._kill_recorded_server()  # must not kill us
    assert not (tmp_path / "model_server.pid").exists()  # stale record cleared


def test_repeated_startup_timeouts_enter_cooldown(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen3-4B-Instruct-2507")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)
    monkeypatch.setattr(ms, "_adapter_sig", lambda: None)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: False)  # never becomes healthy
    monkeypatch.setattr(ms, "_reap_locked", lambda: setattr(ms, "_proc", None))
    spawned = {"n": 0}

    class _P:
        def poll(self):
            return None

    def fake_popen(*a, **k):
        spawned["n"] += 1
        return _P()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    assert ms.ensure_running(startup_timeout=0.1) is False  # timeout 1
    assert ms.ensure_running(startup_timeout=0.1) is False  # timeout 2 → cooldown
    assert spawned["n"] == 2
    assert ms._respawn_blocked_until > time.monotonic()
    assert ms.ensure_running(startup_timeout=0.1) is False  # fast-fail, no spawn
    assert spawned["n"] == 2


def test_restore_adapter_atomic_and_removes_strays(tmp_path):
    prev = tmp_path / "previous"
    prev.mkdir()
    (prev / "adapters.safetensors").write_bytes(b"GOOD")
    (prev / "adapter_config.json").write_text("{}")
    live = tmp_path / "latest"
    live.mkdir()
    (live / "adapters.safetensors").write_bytes(b"BAD")
    (live / "stray.bin").write_bytes(b"leftover-from-bad-train")

    assert restore_adapter(prev, live) is True
    assert (live / "adapters.safetensors").read_bytes() == b"GOOD"
    assert (live / "adapter_config.json").exists()
    assert not (live / "stray.bin").exists()
    assert not list(live.glob("*.tmp"))
