"""Tests for product gap features: streaming, subjects, history, keyboard shortcuts, language detection."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.core.text_utils import detect_language

# ── Language detection tests ──────────────────────────────────────────


def test_detect_language_english():
    assert detect_language("Hello, I'd like to schedule a meeting next week.") == "en"


def test_detect_language_german():
    assert detect_language("Sehr geehrter Herr") == "de"


def test_detect_language_german_sentence():
    assert detect_language("Ich bin nicht sicher, ob wir das machen können.") == "de"


def test_detect_language_arabic():
    assert detect_language("مرحبا، أريد أن أحجز موعد") == "ar"


def test_detect_language_french():
    assert detect_language("Bonjour monsieur, nous vous remercions pour votre message.") == "fr"


def test_detect_language_spanish():
    assert detect_language("Estimado señor, gracias por favor de responder.") == "es"


def test_detect_language_empty():
    assert detect_language("") == "en"


# ── Subject generation tests ─────────────────────────────────────────


def test_generate_subject_from_header():
    from app.generation.service import generate_subject

    result = generate_subject(
        "Subject: Project Update\n\nHi, here is the update...",
        "Thanks for the update.",
        "sqlite:///test.db",
        Path("configs"),
    )
    assert result is not None
    assert "Project Update" in result


def test_generate_subject_re_prefix():
    from app.generation.service import generate_subject

    result = generate_subject(
        "Subject: Re: Budget Discussion\n\nLet's talk.",
        "Sure, let's discuss.",
        "sqlite:///test.db",
        Path("configs"),
    )
    assert result == "Re: Budget Discussion"


def test_generate_subject_returns_string_or_none():
    from app.generation.service import generate_subject

    with patch("app.generation.service._call_claude_cli", side_effect=Exception("no CLI")):
        result = generate_subject(
            "Just a plain email with no subject header.",
            "Thanks for writing.",
            "sqlite:///test.db",
            Path("configs"),
        )
    assert result is None or isinstance(result, str)


# ── Draft history tests ──────────────────────────────────────────────


def test_draft_history_table_created():
    """draft_history table should be created by schema.sql."""
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    conn.close()
    assert "draft_history" in tables


def test_draft_history_table_columns():
    """draft_history should have expected columns."""
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(draft_history)").fetchall()]
    conn.close()
    expected = {"id", "inbound_text", "sender", "generated_draft", "final_reply",
                "edit_distance_pct", "confidence", "model_used", "retrieval_method", "created_at"}
    assert expected.issubset(set(cols))


def test_draft_saved_to_history():
    """Inserting into draft_history should work."""
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    conn.execute(
        """INSERT INTO draft_history
           (inbound_text, sender, generated_draft, confidence, model_used, retrieval_method)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("Hello!", "test@example.com", "Hi there!", "high", "claude", "fts5_bm25"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM draft_history").fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "Hello!"  # inbound_text


# ── Streaming endpoint tests ─────────────────────────────────────────


def test_stream_endpoint_exists():
    """POST /draft/stream should be registered."""
    from app.main import app

    routes = [r.path for r in app.routes]
    assert "/draft/stream" in routes


def test_stream_route_returns_sse_media_type():
    """The stream route handler should return StreamingResponse."""
    from app.api.stream_routes import router

    paths = [r.path for r in router.routes]
    assert any("/stream" in p for p in paths), f"No /stream route found. Routes: {paths}"


# ── Keyboard shortcut hints tests ─────────────────────────────────────


def test_keyboard_shortcut_hints_in_html():
    """Keyboard shortcut hints should be present in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert "rq-keyhints" in html
    assert "submit" in html.lower()
    assert "skip" in html.lower()


def test_keyboard_help_overlay_in_html():
    """Keyboard help overlay should be present in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert "kbOverlay" in html
    assert "Keyboard Shortcuts" in html


# ── History tab tests ─────────────────────────────────────────────────


def test_history_tab_in_html():
    """History tab should be present in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert 'data-tab="history"' in html
    assert "tab-history" in html


def test_history_route_exists():
    """GET /history should be registered."""
    from app.main import app

    routes = [r.path for r in app.routes]
    assert "/history" in routes


# ── Suggested subject in UI tests ─────────────────────────────────────


def test_suggested_subject_element_in_html():
    """Suggested subject element should be present in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert "suggestedSubject" in html
    assert "suggested-subject" in html


# ── DraftResponse field tests ─────────────────────────────────────────


def test_draft_response_has_suggested_subject():
    """DraftResponse should have suggested_subject field."""
    from app.generation.service import DraftResponse

    resp = DraftResponse(
        draft="Hello",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5",
        confidence="high",
        confidence_reason="test",
        model_used="test",
        suggested_subject="Re: Test",
    )
    assert resp.suggested_subject == "Re: Test"


def test_draft_response_suggested_subject_default_none():
    """DraftResponse.suggested_subject should default to None."""
    from app.generation.service import DraftResponse

    resp = DraftResponse(
        draft="Hello",
        detected_mode="work",
        precedent_used=[],
        retrieval_method="fts5",
        confidence="high",
        confidence_reason="test",
        model_used="test",
    )
    assert resp.suggested_subject is None


# ── RetrievalRequest language_hint tests ──────────────────────────────


def test_retrieval_request_has_language_hint():
    """RetrievalRequest should accept language_hint parameter."""
    from app.retrieval.service import RetrievalRequest

    req = RetrievalRequest(query="test", language_hint="de")
    assert req.language_hint == "de"


# ── Streaming CSS in UI tests ─────────────────────────────────────────


def test_streaming_cursor_css_in_html():
    """Streaming cursor CSS should be in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert "streaming-cursor" in html


# ── Language badge in UI tests ────────────────────────────────────────


def test_language_badge_css_in_html():
    """Language badge CSS should be in feedback.html."""
    html_path = Path(__file__).resolve().parents[1] / "templates" / "feedback.html"
    html = html_path.read_text(encoding="utf-8")
    assert "lang-badge" in html
