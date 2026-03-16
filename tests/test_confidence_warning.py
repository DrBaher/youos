"""Tests for confidence-gated UI warning (Item 9)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.generation.service import DraftResponse


def test_feedback_generate_includes_confidence_warning():
    """POST /feedback/generate includes confidence_warning field."""
    mock_response = DraftResponse(
        draft="test draft",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="lexical_v1",
        confidence="low",
        confidence_reason="no strong matches",
        model_used="claude",
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
            with patch("app.api.feedback_routes._get_db_path") as mock_db:
                mock_db.return_value = "/tmp/nonexistent.db"
                # Skip the DB save by patching sqlite3
                with patch("app.api.feedback_routes.sqlite3"):
                    client = TestClient(app)
                    resp = client.post(
                        "/feedback/generate",
                        json={
                            "inbound_text": "Hello, can we meet?",
                        },
                    )
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["confidence"] == "low"
                    assert data["confidence_warning"] is True


def test_feedback_generate_no_warning_for_high_confidence():
    """confidence_warning is False when confidence is high."""
    mock_response = DraftResponse(
        draft="test draft",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5_bm25",
        confidence="high",
        confidence_reason="3 strong matches",
        model_used="claude",
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
                resp = client.post(
                    "/feedback/generate",
                    json={
                        "inbound_text": "Hello, can we meet?",
                    },
                )
                data = resp.json()
                assert data["confidence_warning"] is False


def test_template_has_warning_css():
    """Template includes warning CSS class."""
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    content = template.read_text()
    assert ".info.warning" in content
    assert "f0a500" in content  # amber/yellow color
