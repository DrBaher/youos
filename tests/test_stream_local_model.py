"""The Draft Reply streaming path drafts with the local LoRA, not Claude.

When mlx_lm is on PATH and an adapter is trained, /draft/stream must stream from
the local fine-tuned model (parsing mlx_lm's ===== framing) and report
model_used=qwen2.5-1.5b-lora — only falling back to the Claude CLI when there's
no adapter yet.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import app.api.stream_routes as sr
from app.api.stream_routes import StreamBody, _iter_mlx_body, _stream_generate


class _FakeProc:
    def __init__(self, lines):
        # read()-able stream so the chunk-based mlx parser works.
        self.stdout = io.StringIO("".join(lines))
        self.returncode = 0
        self.pid = 4242

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0  # finished → finally-block won't try to kill it

    def kill(self):
        pass


def test_feedback_has_cold_start_loading_overlay():
    """The draft area shows a loading overlay until the first token streams in,
    masking the local model's ~3s cold start."""
    from pathlib import Path

    content = (Path(__file__).resolve().parents[1] / "templates" / "feedback.html").read_text()
    assert 'id="draftLoading"' in content                 # overlay element
    assert "showDraftLoading()" in content and "hideDraftLoading()" in content
    assert "Warming up your local model" in content       # cold-start explainer
    # Hidden as soon as a token arrives.
    assert "hideDraftLoading();" in content


def test_iter_mlx_body_strips_framing_and_streams():
    # Prelude + opening delim + body (multi-chunk) + closing delim + stats.
    raw = (
        "Fetching 7 files...\n"
        "==========\n"
        "Hi Sam,\n\nThursday at 2pm works for me — see you then.\n"
        "==========\n"
        "Prompt: 12 tokens, 100 tok/sec\nGeneration: 18 tokens\n"
    )
    body = "".join(_iter_mlx_body(io.StringIO(raw)))
    # The \n before the closing delimiter is framing, not body (matches _strip_mlx_output).
    assert body == "Hi Sam,\n\nThursday at 2pm works for me — see you then."
    assert "Fetching" not in body and "Prompt:" not in body and "=====" not in body


def test_iter_mlx_body_handles_delimiter_split_across_chunks():
    # Body text that itself contains '=' but not a full delimiter line must survive.
    raw = "==========\na = b in the config\n==========\nGeneration: 5 tokens\n"
    body = "".join(_iter_mlx_body(io.StringIO(raw)))
    assert body == "a = b in the config"


def _stub_pipeline(monkeypatch):
    """Neutralize retrieval/exemplar/persona so only the model branch matters."""
    rr = MagicMock()
    rr.reply_pairs = []
    rr.detected_mode = None
    monkeypatch.setattr(sr, "retrieve_context", lambda *a, **k: rr)
    # Imported inside _stream_generate → patch at the source module.
    monkeypatch.setattr("app.core.intent.classify_intents_multi", lambda _t: ["general"])
    monkeypatch.setattr(sr, "_get_cached_exemplar_ids", lambda *a, **k: ([], False, "k"))
    monkeypatch.setattr(sr, "_apply_cached_order", lambda rps, ids: rps)
    monkeypatch.setattr(sr, "_top_exemplar_source_ids", lambda rps: [])
    monkeypatch.setattr(sr, "_update_exemplar_cache", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_score_confidence", lambda rps: ("medium", None))
    monkeypatch.setattr(sr, "assemble_prompt", lambda **k: "PROMPT")
    monkeypatch.setattr(sr, "_load_prompts", lambda _d: {})
    monkeypatch.setattr(sr, "_load_persona", lambda _d: {})


def _collect(gen):
    tokens, done = [], None
    for ev in gen:
        payload = json.loads(ev[len("data: "):].strip())
        if payload.get("done"):
            done = payload
        elif "token" in payload:
            tokens.append(payload["token"])
    return tokens, done


def test_stream_uses_local_lora_when_adapter_ready(monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(sr, "_local_model_available", lambda: True)
    monkeypatch.setattr(sr, "_adapter_available", lambda: True)
    monkeypatch.setattr(sr, "_get_base_model_id", lambda: "Qwen/Qwen2.5-1.5B-Instruct")
    monkeypatch.setattr("app.core.settings.get_adapter_path", lambda: __import__("pathlib").Path("/tmp/adapters/latest"))

    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        # mlx_lm framing: prelude, =====, body, =====, stats
        return _FakeProc([
            "Fetching model...\n",
            "==========\n",
            "Hi Sam,\n",
            "Thursday at 2pm works.\n",
            "==========\n",
            "Prompt: 12 tokens, 100 tok/sec\n",
        ])

    monkeypatch.setattr(sr.subprocess, "Popen", fake_popen)

    tokens, done = _collect(_stream_generate(StreamBody(inbound_text="Can we meet Thursday?"), MagicMock()))

    body = "".join(tokens)
    assert captured["cmd"][0] == "mlx_lm"                 # local model, not claude
    assert "--adapter-path" in captured["cmd"]            # with the LoRA
    assert body == "Hi Sam,\nThursday at 2pm works."      # framing stripped, body streamed
    assert "Fetching" not in body                         # prelude suppressed
    assert "Prompt:" not in body                          # stats suppressed
    assert done["model_used"] == "qwen2.5-1.5b-lora"


def test_stream_uses_warm_server_when_enabled(monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(sr, "_local_model_available", lambda: True)
    monkeypatch.setattr(sr, "_adapter_available", lambda: True)
    # Warm server enabled + healthy → stream from it, no subprocess.
    monkeypatch.setattr("app.core.model_server.is_enabled", lambda: True)
    monkeypatch.setattr("app.core.model_server.ensure_running", lambda **k: True)
    monkeypatch.setattr("app.core.model_server.stream", lambda *a, **k: iter(["Hi ", "Sam."]))
    monkeypatch.setattr("app.core.model_server.model_label", lambda: "qwen2.5-1.5b-lora")

    def no_popen(*a, **k):
        raise AssertionError("subprocess must not be spawned when the warm server serves")

    monkeypatch.setattr(sr.subprocess, "Popen", no_popen)

    tokens, done = _collect(_stream_generate(StreamBody(inbound_text="hi"), MagicMock()))
    assert "".join(tokens) == "Hi Sam."
    assert done["model_used"] == "qwen2.5-1.5b-lora"


def test_stream_falls_back_to_claude_without_adapter(monkeypatch):
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(sr, "_local_model_available", lambda: True)
    monkeypatch.setattr(sr, "_adapter_available", lambda: False)  # no adapter yet

    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc(["Hi there.\n"])  # claude streams raw lines (no ===== framing)

    monkeypatch.setattr(sr.subprocess, "Popen", fake_popen)

    tokens, done = _collect(_stream_generate(StreamBody(inbound_text="hi"), MagicMock()))

    assert captured["cmd"][0] == "claude"
    assert done["model_used"] == "claude"
    assert "Hi there.\n" in tokens
