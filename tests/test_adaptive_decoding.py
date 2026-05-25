"""Adaptive / configurable decoding (Draft PR C).

Temperature/top_p were hardcoded (Ollama 0.7) or absent (MLX). These pin the
`generation.decoding` resolver (per-intent override + per-confidence delta) and
that the params plumb through the MLX and Ollama call paths — while the default
(no config) preserves each backend's current behavior.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request

from app.generation import service as svc
from app.generation.service import _resolve_decoding


def _cfg(monkeypatch, decoding):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"decoding": decoding}})


# --- resolver --------------------------------------------------------------


def test_no_config_returns_none(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    assert _resolve_decoding("scheduling", "high") == (None, None)


def test_base_temperature_and_top_p(monkeypatch):
    _cfg(monkeypatch, {"temperature": 0.6, "top_p": 0.95})
    assert _resolve_decoding(None, None) == (0.6, 0.95)


def test_per_intent_override(monkeypatch):
    _cfg(monkeypatch, {"temperature": 0.5, "intent_temperature": {"brainstorm": 0.9}})
    assert _resolve_decoding("brainstorm", None)[0] == 0.9
    assert _resolve_decoding("scheduling", None)[0] == 0.5  # falls back to base


def test_high_confidence_delta_lowers_temp_and_clamps(monkeypatch):
    _cfg(monkeypatch, {"temperature": 0.7, "high_confidence_temperature_delta": -0.2})
    assert round(_resolve_decoding(None, "high")[0], 3) == 0.5
    # clamp at 0
    _cfg(monkeypatch, {"temperature": 0.1, "high_confidence_temperature_delta": -0.5})
    assert _resolve_decoding(None, "high")[0] == 0.0


def test_low_confidence_delta(monkeypatch):
    _cfg(monkeypatch, {"temperature": 0.6, "low_confidence_temperature_delta": 0.2})
    assert round(_resolve_decoding(None, "low")[0], 3) == 0.8


def test_delta_ignored_without_base_temperature(monkeypatch):
    _cfg(monkeypatch, {"top_p": 0.9, "high_confidence_temperature_delta": -0.2})
    assert _resolve_decoding(None, "high") == (None, 0.9)


def test_malformed_config_does_not_raise(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"decoding": "nope"}})
    assert _resolve_decoding("x", "high") == (None, None)


# --- MLX plumbing ----------------------------------------------------------


def _capture_mlx(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "M")

    def fake_run(cmd, *, timeout=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="hello", stderr="")

    monkeypatch.setattr(svc, "_run_subprocess", fake_run)
    return captured


def test_mlx_omits_sampling_flags_by_default(monkeypatch):
    captured = _capture_mlx(monkeypatch)
    svc._call_local_model("prompt", max_tokens=100, use_adapter=False)
    assert "--temp" not in captured["cmd"] and "--top-p" not in captured["cmd"]


def test_mlx_includes_sampling_flags_when_set(monkeypatch):
    captured = _capture_mlx(monkeypatch)
    svc._call_local_model("prompt", max_tokens=100, use_adapter=False, temperature=0.4, top_p=0.9)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--temp") + 1] == "0.4"
    assert cmd[cmd.index("--top-p") + 1] == "0.9"


# --- Ollama plumbing -------------------------------------------------------


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def _capture_ollama(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["options"] = json.loads(req.data)["options"]
        return _FakeResp(json.dumps({"response": "hi"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def test_ollama_keeps_default_temperature(monkeypatch):
    captured = _capture_ollama(monkeypatch)
    svc._generate_via_ollama("p", num_predict=50)  # temperature unset
    assert captured["options"]["temperature"] == 0.7
    assert "top_p" not in captured["options"]


def test_ollama_applies_configured_params(monkeypatch):
    captured = _capture_ollama(monkeypatch)
    svc._generate_via_ollama("p", num_predict=50, temperature=0.3, top_p=0.8)
    assert captured["options"]["temperature"] == 0.3
    assert captured["options"]["top_p"] == 0.8
