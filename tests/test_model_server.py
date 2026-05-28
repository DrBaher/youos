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


def test_model_label_reflects_adapter(monkeypatch, tmp_path):
    adir = tmp_path / "latest"
    adir.mkdir()
    monkeypatch.setattr(ms, "get_adapter_path", lambda: adir)
    assert ms.model_label() == "qwen2.5-1.5b-base"  # no adapter file yet
    (adir / "adapters.safetensors").write_text("x")
    assert ms.model_label() == "qwen2.5-1.5b-lora"


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
    _config(monkeypatch)
    monkeypatch.setattr(ms, "is_healthy", lambda **k: True)  # already running
    monkeypatch.setattr(ms, "_adapter_sig", lambda: 999.0)   # adapter retrained since load
    ms._started_adapter_sig = 111.0
    called = {"restart": False}
    monkeypatch.setattr(ms, "restart", lambda: called.__setitem__("restart", True) or True)
    assert ms.ensure_running() is True
    assert called["restart"] is True


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
