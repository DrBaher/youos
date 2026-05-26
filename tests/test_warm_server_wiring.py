"""Generation prefers the warm model server, with graceful fallback.

_call_local_model uses the warm server for the global-adapter case (no per-draft
reload), falls back to the subprocess on failure, and never uses it when an
explicit base draft (use_adapter=False) or a per-persona adapter_path is needed.
"""

from __future__ import annotations

import subprocess

from app.generation import service as svc


def _stub_server(monkeypatch, *, enabled=True, running=True, complete=None):
    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: enabled)
    monkeypatch.setattr("app.core.model_server.ensure_running", lambda **k: running)
    if complete is not None:
        monkeypatch.setattr("app.core.model_server.complete", complete)


def _stub_subprocess(monkeypatch, stdout):
    """Make the subprocess fallback return framed mlx output."""
    cp = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return cp

    monkeypatch.setattr(svc, "_run_subprocess", fake)
    return calls


def test_call_local_model_uses_warm_server(monkeypatch):
    _stub_server(monkeypatch, complete=lambda p, **k: "SERVER DRAFT")
    calls = _stub_subprocess(monkeypatch, "==========\nSUBPROC\n==========\n")
    out = svc._call_local_model("PROMPT", use_adapter=True)
    assert out == "SERVER DRAFT"
    assert calls["n"] == 0  # subprocess not spawned


def test_call_local_model_falls_back_when_server_errors(monkeypatch):
    def boom(p, **k):
        raise RuntimeError("server down mid-call")

    _stub_server(monkeypatch, complete=boom)
    calls = _stub_subprocess(monkeypatch, "==========\nSUBPROC\n==========\n")
    out = svc._call_local_model("PROMPT", use_adapter=True)
    assert out == "SUBPROC"
    assert calls["n"] == 1  # fell back to the subprocess


def test_call_local_model_skips_server_for_base_request(monkeypatch):
    # use_adapter=False asks for the BASE model, but the server has the adapter
    # loaded — so it must use the subprocess instead, not the server.
    used = {"server": False}

    def complete(p, **k):
        used["server"] = True
        return "SERVER"

    _stub_server(monkeypatch, complete=complete)
    _stub_subprocess(monkeypatch, "==========\nBASE\n==========\n")
    out = svc._call_local_model("PROMPT", use_adapter=False)
    assert out == "BASE" and used["server"] is False


def test_call_local_model_skips_server_for_persona_adapter(monkeypatch, tmp_path):
    used = {"server": False}

    def complete(p, **k):
        used["server"] = True
        return "SERVER"

    _stub_server(monkeypatch, complete=complete)
    _stub_subprocess(monkeypatch, "==========\nPERSONA\n==========\n")
    out = svc._call_local_model("PROMPT", use_adapter=True, adapter_path=tmp_path / "personas" / "internal")
    assert out == "PERSONA" and used["server"] is False


def test_call_local_model_skips_server_when_disabled(monkeypatch):
    _stub_server(monkeypatch, enabled=False, complete=lambda p, **k: "SERVER")
    _stub_subprocess(monkeypatch, "==========\nSUBPROC\n==========\n")
    assert svc._call_local_model("PROMPT", use_adapter=True) == "SUBPROC"
