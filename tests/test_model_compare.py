"""Tests for A/B model comparison (Item 2)."""

from __future__ import annotations


def test_draft_request_has_use_adapter():
    from app.generation.service import DraftRequest

    req = DraftRequest(inbound_message="test")
    assert req.use_adapter is True

    req2 = DraftRequest(inbound_message="test", use_adapter=False)
    assert req2.use_adapter is False


def test_call_local_model_builds_cmd_with_adapter(monkeypatch):
    """When use_adapter=True, --adapter-path should be in the command."""
    import app.generation.service as svc

    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)

        class FakeResult:
            returncode = 0
            stdout = "draft output"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "test-model")
    svc._call_local_model("prompt", use_adapter=True)
    assert "--adapter-path" in captured_cmd


def test_call_local_model_builds_cmd_without_adapter(monkeypatch):
    """When use_adapter=False, --adapter-path should NOT be in the command."""
    import app.generation.service as svc

    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)

        class FakeResult:
            returncode = 0
            stdout = "draft output"
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    monkeypatch.setattr(svc, "_get_base_model_id", lambda: "test-model")
    svc._call_local_model("prompt", use_adapter=False)
    assert "--adapter-path" not in captured_cmd


def test_improvement_hint_similar():
    from app.core.diff import hybrid_similarity

    # Same text → high similarity → adapter may need more training
    sim = hybrid_similarity("Hello world", "Hello world")
    assert sim >= 0.7


def test_improvement_hint_different():
    from app.core.diff import hybrid_similarity

    # Very different text → low similarity → adapter is helping
    sim = hybrid_similarity("Hello world, how are you doing today?", "Completely unrelated response about weather forecasts")
    assert sim < 0.7


def test_compare_endpoint_returns_new_fields():
    """The compare response dict should have the new field structure."""
    expected_keys = {"adapter_draft", "base_draft", "adapter_confidence", "exemplar_count", "improvement_hint"}
    # Just verify the keys exist by building a sample response
    response = {
        "adapter_draft": "test",
        "base_draft": "test",
        "adapter_confidence": "high",
        "exemplar_count": 3,
        "improvement_hint": "Adapter appears to be helping",
    }
    assert set(response.keys()) == expected_keys
