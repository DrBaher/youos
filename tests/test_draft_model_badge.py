"""Per-draft model badge — surface which model wrote each draft.

So a base-model or cloud-fallback draft in the review queue is visible, not
silently mistaken for the user's fine-tuned voice. Pins that /feedback/generate
returns model_used and that the template renders a badge from it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.generation.service import DraftResponse


def test_feedback_generate_includes_model_used():
    mock_response = DraftResponse(
        draft="test draft",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5_bm25",
        confidence="high",
        confidence_reason="3 strong matches",
        model_used="qwen2.5-1.5b-lora",
    )
    with patch("app.api.feedback_routes.generate_draft", return_value=mock_response):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.feedback_routes import router

        app = FastAPI()
        app.include_router(router)
        app.state.settings = MagicMock()
        app.state.settings.database_url = "sqlite:///test.db"
        app.state.settings.configs_dir = "/tmp"

        with patch("app.api.feedback_routes.draft_limiter") as mock_limiter:
            mock_limiter.is_allowed.return_value = True
            with patch("app.api.feedback_routes.sqlite3"):
                client = TestClient(app)
                resp = client.post("/feedback/generate", json={"inbound_text": "Hello, can we meet?"})
                assert resp.status_code == 200
                assert resp.json()["model_used"] == "qwen2.5-1.5b-lora"


def test_feedback_template_renders_model_badge():
    content = (Path(__file__).resolve().parents[1] / "templates" / "feedback.html").read_text()
    # Badge logic in renderDraftMeta keys off model_used and distinguishes the
    # three states a user needs to tell apart.
    assert "data.model_used" in content
    assert "your fine-tuned model" in content
    assert "base model (not personalized)" in content
    assert "cloud fallback" in content


def test_stream_meta_carries_model_used():
    """The streaming done-payload includes model_used (the stream path uses
    Claude; the fallback reports its own)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "stream_routes.py").read_text()
    assert '"model_used": model_used' in src
    assert 'model_used = "claude"' in src  # streamed via the Claude CLI
