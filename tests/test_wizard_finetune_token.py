"""Run fine-tune + create API token from the wizard.

POST /api/finetune spawns export+finetune in the background (with a running
guard); /api/finetune/status reports running/done. POST /api/token mints an
API token. Subprocess + token creation are mocked so tests don't launch a
real fine-tune or write real tokens.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.core.auth as auth
from app.api import stats_routes as sr
from app.main import app

client = TestClient(app)


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


# --- fine-tune -------------------------------------------------------------


def test_finetune_spawns_when_idle(monkeypatch):
    monkeypatch.setattr(sr, "_finetune_proc", None)
    monkeypatch.setattr(sr.subprocess, "Popen", lambda *a, **k: _FakeProc())
    r = client.post("/api/finetune")
    assert r.status_code == 200 and r.json() == {"started": True}


def test_finetune_409_when_running(monkeypatch):
    monkeypatch.setattr(sr, "_finetune_proc", _FakeProc(alive=True))
    assert client.post("/api/finetune").status_code == 409


def test_finetune_status_running(monkeypatch):
    monkeypatch.setattr(sr, "_finetune_proc", _FakeProc(alive=True))
    assert client.get("/api/finetune/status").json()["status"] == "running"


def test_finetune_status_idle_then_done(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "_finetune_proc", None)
    monkeypatch.setattr(sr, "get_adapter_path", lambda: tmp_path)
    assert client.get("/api/finetune/status").json() == {"status": "idle", "adapter_ready": False}
    (tmp_path / "adapters.safetensors").write_text("x")
    assert client.get("/api/finetune/status").json() == {"status": "done", "adapter_ready": True}


# --- benchmark (validate the adapter so the readiness gate can clear) ------


def test_benchmark_spawns_when_idle(monkeypatch):
    monkeypatch.setattr(sr, "_benchmark_proc", None)
    monkeypatch.setattr(sr, "_finetune_proc", None)
    monkeypatch.setattr(sr.subprocess, "Popen", lambda *a, **k: _FakeProc())
    r = client.post("/api/benchmark")
    assert r.status_code == 200 and r.json() == {"started": True}


def test_benchmark_409_when_running(monkeypatch):
    monkeypatch.setattr(sr, "_benchmark_proc", _FakeProc(alive=True))
    monkeypatch.setattr(sr, "_finetune_proc", None)
    assert client.post("/api/benchmark").status_code == 409


def test_benchmark_409_when_finetune_running(monkeypatch):
    monkeypatch.setattr(sr, "_benchmark_proc", None)
    monkeypatch.setattr(sr, "_finetune_proc", _FakeProc(alive=True))
    assert client.post("/api/benchmark").status_code == 409


def test_readiness_reports_benchmarking_while_benchmark_runs(monkeypatch, tmp_path):
    # Adapter trained + a benchmark running → readiness phase "benchmarking".
    adir = tmp_path / "latest"
    adir.mkdir()
    (adir / "adapters.safetensors").write_text("x")
    monkeypatch.setattr("app.core.stats._resolve_adapter_path", lambda: adir)
    monkeypatch.setattr(sr, "_finetune_proc", None)
    monkeypatch.setattr(sr, "_benchmark_proc", _FakeProc(alive=True))
    body = client.get("/api/model/readiness").json()
    assert body["phase"] == "benchmarking" and body["ready"] is False


# --- token -----------------------------------------------------------------


def test_token_endpoint_returns_token(monkeypatch):
    monkeypatch.setattr(auth, "add_api_token", lambda: "TKN-abc123")
    r = client.post("/api/token")
    assert r.status_code == 200 and r.json() == {"token": "TKN-abc123"}


# --- wizard wiring ---------------------------------------------------------


def test_wizard_has_finetune_and_token_actions():
    body = client.get("/welcome").text
    for marker in ('id="runFinetune"', '/api/finetune', 'id="createToken"', '/api/token', 'id="tokenBox"'):
        assert marker in body, f"missing {marker}"
