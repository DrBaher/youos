"""Warm local-model server lifecycle + HTTP client (mlx_lm.server wrapper).

All HTTP/subprocess is mocked — no real model loads. Pins health checks, the
completion/stream client parsing of mlx_lm.server's OpenAI-shaped responses,
lazy start with graceful failure, and the model_used label.
"""

from __future__ import annotations

from pathlib import Path

import app.core.model_server as ms


def _config(monkeypatch, *, enabled=True, port=8088):
    monkeypatch.setattr(ms, "get_server_config", lambda: {"enabled": enabled, "port": port})
    # These tests mock Popen/health to exercise the real ensure_running logic, so
    # bypass the "never spawn under pytest" guard.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def test_is_enabled_reads_config(monkeypatch):
    _config(monkeypatch, enabled=True)
    assert ms.is_enabled() is True
    _config(monkeypatch, enabled=False)
    assert ms.is_enabled() is False


def _valid_safetensors() -> bytes:
    """A minimal structurally-valid safetensors blob: 8-byte LE header length +
    a JSON-parseable header (b163 integrity check)."""
    header = b"{}"
    return len(header).to_bytes(8, "little") + header


def test_model_label_reflects_adapter(monkeypatch, tmp_path):
    adir = tmp_path / "latest"
    adir.mkdir()
    monkeypatch.setattr(ms, "get_adapter_path", lambda: adir)
    assert ms.model_label() == "qwen2.5-1.5b-base"  # no adapter file yet
    (adir / "adapters.safetensors").write_bytes(_valid_safetensors())
    assert ms.model_label() == "qwen2.5-1.5b-lora"


def test_corrupt_adapter_is_not_served(monkeypatch, tmp_path):
    """b163: a truncated/half-written adapters.safetensors must NOT be served —
    _adapter_arg/_adapter_sig fall back to the base model instead of handing
    mlx_lm.server a file it would choke on (wedging all drafting)."""
    adir = tmp_path / "latest"
    adir.mkdir()
    monkeypatch.setattr(ms, "get_adapter_path", lambda: adir)
    (adir / "adapters.safetensors").write_bytes(b"\x05\x00\x00\x00\x00\x00\x00\x00tru")  # header says 5 bytes, only 3 present
    assert ms._adapter_arg() is None
    assert ms._adapter_sig() is None
    assert ms.model_label() == "qwen2.5-1.5b-base"
    # a valid one IS served
    (adir / "adapters.safetensors").write_bytes(_valid_safetensors())
    assert ms._adapter_arg() == str(adir)
    assert ms._adapter_sig() is not None


def test_is_healthy_true_on_200(monkeypatch):
    _config(monkeypatch)

    class _R:
        status_code = 200

    monkeypatch.setattr(ms.httpx, "get", lambda *a, **k: _R())
    assert ms.is_healthy() is True


def test_is_healthy_false_on_error(monkeypatch):
    _config(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ms.httpx, "get", boom)
    assert ms.is_healthy() is False


def test_complete_parses_text_choice(monkeypatch):
    _config(monkeypatch)

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"text": "Sure, Thursday works."}]}

    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _R()

    monkeypatch.setattr(ms.httpx, "post", fake_post)
    out = ms.complete("PROMPT", max_tokens=50, temperature=0.3)
    assert out == "Sure, Thursday works."
    assert captured["url"].endswith("/v1/completions")
    assert captured["json"]["prompt"] == "PROMPT" and captured["json"]["stream"] is False
    assert captured["json"]["temperature"] == 0.3
    assert "top_p" not in captured["json"]  # omitted when None


def test_stream_yields_deltas(monkeypatch):
    _config(monkeypatch)

    class _Resp:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield 'data: {"choices":[{"text":"Hi "}]}'
            yield ""  # blank keepalive line, ignored
            yield 'data: {"choices":[{"text":"Sam"}]}'
            yield "data: [DONE]"
            yield 'data: {"choices":[{"text":"NOPE"}]}'  # after DONE → not yielded

    class _Ctx:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ms.httpx, "stream", lambda *a, **k: _Ctx())
    assert list(ms.stream("PROMPT")) == ["Hi ", "Sam"]


def test_ensure_running_noop_when_already_healthy(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)
    started = {"popen": False}
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: started.__setitem__("popen", True))
    assert ms.ensure_running() is True
    assert started["popen"] is False  # didn't spawn — already warm


def test_ensure_running_returns_false_if_spawn_fails(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: False)
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen2.5-1.5B-Instruct")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)

    def boom(*a, **k):
        raise OSError("mlx_lm not found")

    monkeypatch.setattr(ms.subprocess, "Popen", boom)
    ms._proc = None
    assert ms.ensure_running() is False


def test_ensure_running_passes_adapter_when_present(monkeypatch):
    _config(monkeypatch)
    # Not healthy, then healthy after "start" so the poll loop exits fast.
    states = iter([False, False, True])
    monkeypatch.setattr(ms, "is_healthy", lambda **k: next(states, True))
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen2.5-1.5B-Instruct")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: "/inst/models/adapters/latest")

    captured = {}

    class _FakeProc:
        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    ms._proc = None
    assert ms.ensure_running(startup_timeout=5) is True
    assert "--adapter-path" in captured["cmd"]
    assert "/inst/models/adapters/latest" in captured["cmd"]
    assert captured["cmd"][:3] == [ms.sys.executable, "-m", "mlx_lm.server"]
    ms._proc = None  # cleanup module state


def test_ensure_running_reloads_when_adapter_changed(monkeypatch):
    """b159: when the adapter was retrained, ensure_running reaps the stale server
    and respawns UNDER THE LOCK (no longer routing through restart()), and
    re-stamps _started_adapter_sig so concurrent post-retrain threads see the new
    sig and skip a redundant restart."""
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)  # healthy throughout
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 999.0)   # retrained since load
    monkeypatch.setattr(ms, "get_base_model", lambda: "Qwen/Qwen2.5-1.5B-Instruct")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)
    ms._started_adapter_sig = 111.0

    reaped = {"old": False}

    class _OldProc:
        def poll(self):
            return 0  # already exited -> _reap_locked just wait()s it

        def wait(self, timeout=None):
            reaped["old"] = True
            return 0

    spawned = {"n": 0}
    monkeypatch.setattr(ms.subprocess, "Popen", lambda cmd, **k: spawned.__setitem__("n", spawned["n"] + 1) or type("_P", (), {"poll": lambda self: None})())

    called = {"restart": False}
    monkeypatch.setattr(ms, "restart", lambda: called.__setitem__("restart", True) or True)
    ms._proc = _OldProc()

    assert ms.ensure_running(startup_timeout=5) is True
    assert reaped["old"] is True              # stale server reaped...
    assert spawned["n"] == 1                  # ...and a fresh one spawned (not via restart)
    assert called["restart"] is False         # the reload no longer routes through restart()
    assert ms._started_adapter_sig == 999.0   # re-stamped so concurrent threads skip the reload
    ms._proc = None


def test_ensure_running_no_reload_when_adapter_unchanged(monkeypatch):
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 111.0)
    ms._started_adapter_sig = 111.0

    def no_restart():
        raise AssertionError("should not restart when the adapter is unchanged")

    monkeypatch.setattr(ms, "restart", no_restart)
    assert ms.ensure_running() is True


def test_cli_model_server_group_registered():
    from app.cli import app

    model_group = next((g for g in app.registered_groups if g.name == "model"), None)
    assert model_group is not None
    subgroups = {g.name for g in model_group.typer_instance.registered_groups}
    assert "server" in subgroups  # `youos model server …`


def test_server_enabled_by_default(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    cfg = ms.get_server_config()
    assert cfg["enabled"] is True and cfg["port"] == 8088


def test_review_draft_model_defaults_to_auto():
    from app.core.config import get_review_draft_model

    assert get_review_draft_model({}) == "auto"  # local-when-ready, else Claude


def test_lifespan_prewarms_and_stops_server():
    # The server is pre-warmed on startup and stopped on shutdown (source-level —
    # the async lifespan is awkward to drive in a unit test).
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "model_server.ensure_running" in src  # pre-warm
    assert "model_server.stop()" in src          # clean shutdown


def test_ensure_running_skipped_under_pytest(monkeypatch):
    # The guard: inside the test suite, ensure_running must never spawn — it just
    # reports health. (PYTEST_CURRENT_TEST is set by the running pytest.)
    import os

    assert os.environ.get("PYTEST_CURRENT_TEST")  # sanity: we're under pytest
    # Force the unhealthy-server view so this test doesn't flake when the dev
    # machine has an actual mlx_lm.server running on the configured port (which
    # makes is_healthy() return True and ensure_running() return True without
    # spawning — the intent of the test is still satisfied, but the return-value
    # assertion would fail). Pin the precondition explicitly.
    monkeypatch.setattr(ms, "is_healthy", lambda: False)
    # No spawn happens (PYTEST_CURRENT_TEST guard) → returns False.
    assert ms.ensure_running() is False


def test_stop_reaps_sigterm_honoring_child(monkeypatch):
    """b154: stop() must wait()/reap the child after SIGTERM so the long-lived
    FastAPI parent doesn't accumulate zombies on every retrain/restart."""
    import signal as _signal

    signals: list[int] = []

    class _Proc:
        pid = 4321

        def __init__(self):
            self._alive = True
            self.waited = False

        def poll(self):
            return None if self._alive else 0

        def wait(self, timeout=None):
            self.waited = True
            self._alive = False  # honoring child dies on SIGTERM
            return 0

    monkeypatch.setattr(ms.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ms.os, "killpg", lambda pgid, sig: signals.append(sig))
    proc = _Proc()
    ms._proc = proc

    ms.stop()

    assert _signal.SIGTERM in signals
    assert _signal.SIGKILL not in signals  # honoring child needs no escalation
    assert proc.waited is True             # reaped, not orphaned
    assert ms._proc is None


def test_stop_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    """b154: a worker that ignores SIGTERM must be SIGKILLed (no ~3GB stray)."""
    import signal as _signal

    signals: list[int] = []

    class _Proc:
        pid = 4322

        def poll(self):
            return None  # still alive when stop() begins

        def wait(self, timeout=None):
            if _signal.SIGKILL in signals:
                return -9  # dies only after SIGKILL
            raise ms.subprocess.TimeoutExpired(cmd="mlx_lm.server", timeout=timeout)

    monkeypatch.setattr(ms.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ms.os, "killpg", lambda pgid, sig: signals.append(sig))
    ms._proc = _Proc()

    ms.stop()

    assert _signal.SIGTERM in signals
    assert _signal.SIGKILL in signals  # escalated after the ignored SIGTERM
    assert ms._proc is None


def test_ensure_running_reaps_wedged_child_on_timeout(monkeypatch):
    """b159: if startup times out with the child still ALIVE (wedged / booting
    forever), reap it inline so _proc is cleared and the NEXT call respawns
    instead of re-skipping the spawn and blocking the full timeout forever."""
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: False)  # never becomes healthy
    monkeypatch.setattr(ms, "get_base_model", lambda: "M")
    monkeypatch.setattr(ms, "_adapter_arg", lambda: None)

    killed = {"n": 0}
    monkeypatch.setattr(ms.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(ms.os, "killpg", lambda pgid, sig: killed.__setitem__("n", killed["n"] + 1))

    class _AliveProc:
        pid = 7777

        def poll(self):
            return None  # stays alive on its own

        def wait(self, timeout=None):
            return 0  # dies on the kill

    spawned = {"n": 0}
    monkeypatch.setattr(ms.subprocess, "Popen", lambda cmd, **k: spawned.__setitem__("n", spawned["n"] + 1) or _AliveProc())

    ms._proc = None
    assert ms.ensure_running(startup_timeout=0.0) is False
    assert ms._proc is None        # wedged child reaped, handle cleared (not leaked)
    assert killed["n"] >= 1        # actually SIGTERM'd, not orphaned
    assert spawned["n"] == 1

    # the NEXT call respawns fresh — proving the handle wasn't left stuck
    assert ms.ensure_running(startup_timeout=0.0) is False
    assert spawned["n"] == 2
    ms._proc = None
