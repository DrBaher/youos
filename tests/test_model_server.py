"""Warm local-model server lifecycle + HTTP client (mlx_lm.server wrapper).

All HTTP/subprocess is mocked — no real model loads. Pins health checks, the
completion/stream client parsing of mlx_lm.server's OpenAI-shaped responses,
lazy start with graceful failure, and the model_used label.
"""

from __future__ import annotations

import app.core.model_server as ms


def _config(monkeypatch, *, enabled=True, port=8088):
    monkeypatch.setattr(ms, "get_server_config", lambda: {"enabled": enabled, "port": port})


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


def test_cli_model_server_group_registered():
    from app.cli import app

    model_group = next((g for g in app.registered_groups if g.name == "model"), None)
    assert model_group is not None
    subgroups = {g.name for g in model_group.typer_instance.registered_groups}
    assert "server" in subgroups  # `youos model server …`
