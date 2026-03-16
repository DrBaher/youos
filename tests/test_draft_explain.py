"""Tests for draft explain endpoint and trace storage."""

from datetime import datetime
from unittest.mock import MagicMock

from app.api.routes import _draft_traces, _store_trace


def test_store_trace_returns_draft_id():
    _draft_traces.clear()
    response = MagicMock()
    response.precedent_used = [
        {"source_id": "s1", "score": 5.0, "quality_score": 1.0, "title": "Subject 1", "snippet": "some text"},
    ]
    response.confidence = "high"
    response.model_used = "qwen2.5-1.5b-lora"
    response.detected_mode = "work"

    draft_id = _store_trace(
        inbound_text="Hello, can we meet?",
        sender="john@example.com",
        response=response,
        intent="meeting_request",
    )

    assert len(draft_id) == 8
    assert len(_draft_traces) == 1
    trace = _draft_traces[0]
    assert trace["draft_id"] == draft_id
    assert trace["inbound_text"] == "Hello, can we meet?"
    assert trace["sender"] == "john@example.com"
    assert trace["confidence"] == "high"
    assert trace["intent"] == "meeting_request"
    assert trace["model_used"] == "qwen2.5-1.5b-lora"
    assert len(trace["exemplars"]) == 1
    assert trace["exemplars"][0]["source_id"] == "s1"


def test_store_trace_max_20():
    _draft_traces.clear()
    response = MagicMock()
    response.precedent_used = []
    response.confidence = "low"
    response.model_used = "claude"
    response.detected_mode = "work"

    for i in range(25):
        _store_trace(inbound_text=f"msg {i}", sender=None, response=response)

    assert len(_draft_traces) == 20
    # Oldest should be msg 5 (25 - 20)
    assert _draft_traces[0]["inbound_text"] == "msg 5"


def test_trace_has_created_at():
    _draft_traces.clear()
    response = MagicMock()
    response.precedent_used = []
    response.confidence = "medium"
    response.model_used = "ollama:mistral"
    response.detected_mode = "personal"

    _store_trace(inbound_text="test", sender=None, response=response)
    trace = _draft_traces[0]
    assert "created_at" in trace
    # Should be ISO format
    datetime.fromisoformat(trace["created_at"])


def test_trace_exemplars_capped_at_5():
    _draft_traces.clear()
    response = MagicMock()
    response.precedent_used = [{"source_id": f"s{i}", "score": float(i), "quality_score": 1.0, "title": f"Title {i}", "snippet": "text"} for i in range(10)]
    response.confidence = "high"
    response.model_used = "test"
    response.detected_mode = "work"

    _store_trace(inbound_text="test", sender=None, response=response)
    assert len(_draft_traces[0]["exemplars"]) == 5


def test_trace_without_intent():
    _draft_traces.clear()
    response = MagicMock()
    response.precedent_used = []
    response.confidence = "low"
    response.model_used = "test"
    response.detected_mode = "unknown"

    _store_trace(inbound_text="hi", sender=None, response=response)
    assert _draft_traces[0]["intent"] is None
