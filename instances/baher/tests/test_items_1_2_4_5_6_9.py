"""Tests for items 1, 2, 4, 5, 6, 9."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.core.text_utils import strip_quoted_text
from app.main import create_app

ROOT_DIR = Path(__file__).resolve().parents[1]


def _seed_db(db_path: Path, *, num_reply_pairs: int = 20) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.execute(
            """
            INSERT INTO documents (source_type, source_id, title, author, content, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("gmail_thread", "test-doc-1", "Re: Integration", "Test", "content",
             '{"account_email": "baher@medicus.ai"}'),
        )
        doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        senders = [
            ("John Smith <john@crelio.com>", "external_client"),
            ("Jane Doe <jane@medicus.ai>", "internal"),
            ("Bob <bob@gmail.com>", "personal"),
            ("Alice <alice@bigcorp.com>", "external_client"),
            ("Charlie <charlie@gmail.com>", "personal"),
        ]
        for i in range(num_reply_pairs):
            sender_name, _ = senders[i % len(senders)]
            inbound = f"This is test inbound email number {i} with enough text to pass the length filter easily here."
            paired_at = "2025-12-15T10:00:00" if i < 10 else "2024-01-15T10:00:00"
            conn.execute(
                """
                INSERT INTO reply_pairs
                    (source_type, source_id, document_id, inbound_text, reply_text,
                     inbound_author, paired_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "gmail_reply", f"test-rp-{i}", doc_id, inbound,
                    f"Reply to email {i} with enough text",
                    sender_name, paired_at, "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_client(monkeypatch, db_path: Path) -> TestClient:
    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    return TestClient(create_app())


def _mock_generate(inbound_message, **kwargs):
    from app.generation.service import DraftResponse
    return DraftResponse(
        draft=f"Draft reply to: {inbound_message[:30]}",
        detected_mode="business",
        precedent_used=[{"source_id": "test", "title": "test", "snippet": "test", "score": 9.0}],
        retrieval_method="mock",
        confidence="high",
        confidence_reason="mocked",
        model_used="mock",
    )


# ── Item 1: Bookmarklet page ──────────────────────────────────────


def test_bookmarklet_page_loads(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/bookmarklet")
    assert response.status_code == 200
    assert "BaherOS Draft" in response.text
    assert "bookmarks bar" in response.text


def test_feedback_url_params_prefill(monkeypatch, tmp_path: Path) -> None:
    """Check that /feedback page HTML includes URL param handling JS."""
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert response.status_code == 200
    html = response.text
    # The page should have the URL param pre-fill script
    assert "URLSearchParams" in html
    assert "inbound" in html
    assert "sender" in html


# ── Item 4: Strip quoted text ──────────────────────────────────────


def test_strip_quoted_text_on_date_wrote() -> None:
    text = (
        "Hello, I wanted to follow up on our discussion about the project timeline and deliverables.\n\n"
        "On March 10, 2026, John Smith wrote:\n"
        "> Previous message content here\n"
        "> that should be removed"
    )
    result = strip_quoted_text(text)
    assert "Hello, I wanted to follow up" in result
    assert "John Smith wrote" not in result


def test_strip_quoted_text_angle_brackets() -> None:
    text = (
        "Thanks for the update, I'll review it today and get back to you with my thoughts on the proposal.\n\n"
        "> Some quoted line one\n"
        "> Some quoted line two\n"
        "> Some quoted line three\n"
        "> Some quoted line four"
    )
    result = strip_quoted_text(text)
    assert "I'll review it today" in result
    assert "> Some quoted line" not in result


def test_strip_quoted_text_forwarded() -> None:
    text = (
        "FYI see below, this is relevant to the integration work we discussed last week.\n\n"
        "---------- Forwarded message ----------\n"
        "From: someone@test.com\n"
        "Original content here"
    )
    result = strip_quoted_text(text)
    assert "FYI see below" in result
    assert "Forwarded message" not in result


def test_strip_quoted_text_short_result_keeps_original() -> None:
    text = "Hi\n\nOn March 10, 2026, John wrote:\n> stuff"
    result = strip_quoted_text(text)
    # Result "Hi" is < 50 chars, so original is kept
    assert result == text


def test_strip_quoted_text_no_quotes() -> None:
    text = "This is a normal email with no quoted text at all, just regular content."
    assert strip_quoted_text(text) == text


# ── Item 2: Queue selection produces varied sender types ──────────


def test_queue_selection_varied_sender_types(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path, num_reply_pairs=20)
    client = _make_client(monkeypatch, db_path)

    with patch("app.api.review_queue_routes.generate_draft",
               side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
        response = client.get("/review-queue/next?batch_size=10")

    assert response.status_code == 200
    data = response.json()
    items = data["items"]
    assert len(items) > 0

    # Check that we get more than one type of sender
    from app.core.sender import classify_sender
    sender_types = set()
    for item in items:
        st = classify_sender(item["inbound_author"])
        sender_types.add(st)
    # With our test data we should get at least 2 different sender types
    assert len(sender_types) >= 2, f"Only got sender types: {sender_types}"


# ── Item 9: Stats dashboard ───────────────────────────────────────


def test_stats_page_loads(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/stats")
    assert response.status_code == 200
    assert "BaherOS" in response.text
    assert "Corpus Health" in response.text


def test_stats_data_returns_json(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/stats/data")
    assert response.status_code == 200
    data = response.json()
    assert "corpus" in data
    assert "model" in data
    assert "senders" in data
    assert data["corpus"]["total_reply_pairs"] >= 1


# ── Item 6: Draft comparison ──────────────────────────────────────


def test_draft_compare_returns_both_drafts(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)

    with patch("app.api.routes.generate_draft",
               side_effect=lambda req, **kw: _mock_generate(req.inbound_message)):
        response = client.post(
            "/draft/compare",
            json={"inbound_text": "Hello, can you send me the latest report?", "sender": "test@example.com"},
        )

    assert response.status_code == 200
    data = response.json()
    assert "retrieval_draft" in data
    assert "baseline_draft" in data
    assert "retrieval_confidence" in data
    assert "exemplar_count" in data
    assert len(data["retrieval_draft"]) > 0
    assert len(data["baseline_draft"]) > 0
