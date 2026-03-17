"""Tests for features ported from YouOS."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.core.text_utils import detect_language
from app.generation.service import (
    DraftResponse,
    _generate_via_ollama,
    assemble_prompt,
    generate_subject,
)
from app.main import create_app

ROOT_DIR = Path(__file__).resolve().parents[1]


def _seed_db(db_path: Path, *, num_reply_pairs: int = 5) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.execute(
            "INSERT INTO documents (source_type, source_id, title, author, content, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("gmail_thread", "test-doc-1", "Re: Test", "Baher", "content", '{}'),
        )
        doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for i in range(num_reply_pairs):
            conn.execute(
                "INSERT INTO reply_pairs (source_type, source_id, document_id, inbound_text, "
                "reply_text, inbound_author, paired_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("gmail_reply", f"rp-{i}", doc_id,
                 f"Test inbound email {i} with enough content to pass filters easily.",
                 f"Reply {i} text here.", f"sender{i}@test.com", "2025-12-15T10:00:00", "{}"),
            )
        # Create draft_history table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS draft_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_text TEXT NOT NULL,
                sender TEXT,
                generated_draft TEXT NOT NULL,
                final_reply TEXT,
                edit_distance_pct REAL,
                confidence TEXT,
                model_used TEXT,
                retrieval_method TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO draft_history "
            "(inbound_text, sender, generated_draft, confidence, model_used) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test inbound", "test@example.com", "Test draft", "high", "claude"),
        )
        conn.commit()
    finally:
        conn.close()


def _make_client(monkeypatch, db_path: Path) -> TestClient:
    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    return TestClient(create_app())


# ── Feature 1: Empty state flash fix ──


def test_main_content_hidden_by_default(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert response.status_code == 200
    assert 'id="mainContent" style="display:none;"' in response.text
    assert 'id="emptyState"' in response.text


def test_api_config_returns_corpus_ready(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "corpus_ready" in data
    assert data["corpus_ready"] is True
    assert data["display_name"] == "BaherOS"


def test_api_config_empty_db(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    # Create empty DB with schema but no data
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)
    conn.close()
    client = _make_client(monkeypatch, db_path)
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["corpus_ready"] is False


# ── Feature 2: Streaming generation ──


def test_stream_route_exists(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    # Just check that the route accepts POST (it will try to stream)
    with patch("app.api.stream_routes.generate_draft") as mock_gen:
        mock_gen.return_value = DraftResponse(
            draft="Test", detected_mode="work", precedent_used=[],
            retrieval_method="mock", confidence="high",
            confidence_reason="mock", model_used="mock",
        )
        response = client.post(
            "/draft/stream",
            json={"inbound_text": "Hello, can you help me with the project timeline?"},
        )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")


def test_feedback_html_has_streaming_js(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert "/draft/stream" in response.text
    assert "streaming-cursor" in response.text


# ── Feature 3: Subject line generation ──


def test_generate_subject_from_header() -> None:
    text = "Subject: Proposal review\n\nHi, can you review this?"
    result = generate_subject(
        text, "Sure, I'll review it.", "sqlite:///fake.db", ROOT_DIR / "configs",
    )
    assert result is not None
    assert "Re:" in result


def test_draft_response_has_suggested_subject() -> None:
    resp = DraftResponse(
        draft="test", detected_mode="work", precedent_used=[],
        retrieval_method="test", confidence="high",
        confidence_reason="test", model_used="test",
        suggested_subject="Re: Test subject",
    )
    assert resp.suggested_subject == "Re: Test subject"
    d = resp.to_dict()
    assert d["suggested_subject"] == "Re: Test subject"


def test_feedback_html_has_subject_ui(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert "suggestedSubject" in response.text
    assert "suggested_subject" in response.text


# ── Feature 4: Draft history ──


def test_history_route_returns_items(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/history")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1
    assert data["items"][0]["generated_draft"] == "Test draft"


def test_history_route_empty_table(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)
    conn.close()
    client = _make_client(monkeypatch, db_path)
    response = client.get("/history")
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_history_route_no_table(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    # Create DB without draft_history table
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE dummy (id INTEGER)")
    conn.close()
    client = _make_client(monkeypatch, db_path)
    response = client.get("/history")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_feedback_html_has_history_tab(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert 'data-tab="history"' in response.text
    assert 'id="tab-history"' in response.text
    assert "historyList" in response.text


# ── Feature 5: Keyboard shortcuts ──


def test_keyboard_shortcuts_in_html(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    html = response.text
    assert "rq-keyhints" in html
    assert "kbOverlay" in html
    assert "isReviewQueueActive" in html
    assert "ArrowRight" in html
    assert "ArrowLeft" in html


# ── Feature 6: Multi-language detection ──


def test_detect_language_english() -> None:
    assert detect_language("Hello, how are you doing today?") == "en"


def test_detect_language_german() -> None:
    text = "Sehr geehrter Herr, ich möchte bitte die Unterlagen haben. Mit freundlichen Grüßen"
    result = detect_language(text)
    assert result == "de"


def test_detect_language_french() -> None:
    text = ("Bonjour monsieur, merci pour votre message. "
            "Nous vous répondrons dans les meilleurs délais.")
    result = detect_language(text)
    assert result == "fr"


def test_detect_language_arabic() -> None:
    text = "مرحبا، كيف حالك؟ أريد أن أعرف المزيد عن المشروع"
    result = detect_language(text)
    assert result == "ar"


def test_detect_language_spanish() -> None:
    text = "Estimado señor, gracias por favor de enviar los documentos."
    result = detect_language(text)
    assert result == "es"


def test_detect_language_empty() -> None:
    assert detect_language("") == "en"


def test_language_hint_in_prompt() -> None:
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=[],
        persona={},
        prompts={},
        language_hint="de",
    )
    assert "[LANGUAGE: de]" in prompt


def test_language_hint_english_not_shown() -> None:
    prompt = assemble_prompt(
        inbound_message="Hello",
        reply_pairs=[],
        persona={},
        prompts={},
        language_hint="en",
    )
    assert "[LANGUAGE:" not in prompt


def test_language_badge_in_review_queue(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert "rqLangBadge" in response.text
    assert "lang-badge" in response.text


# ── Feature 7: Ollama backend ──


def test_generate_via_ollama_function_exists() -> None:
    """Verify _generate_via_ollama is importable."""
    assert callable(_generate_via_ollama)


# ── Feature 8: Embedding indexer ──


def test_nightly_pipeline_has_embedding_step() -> None:
    import scripts.nightly_pipeline as pipeline
    assert hasattr(pipeline, "step_index_embeddings")
    result = pipeline.step_index_embeddings.__doc__
    assert "embedding" in result.lower()


# ── Feature 9: Incremental ingestion ──


def test_nightly_pipeline_incremental_helpers() -> None:
    import scripts.nightly_pipeline as pipeline
    assert callable(pipeline._get_last_ingest_at)
    assert callable(pipeline._set_last_ingest_at)


def test_last_ingest_at_roundtrip(tmp_path: Path) -> None:
    import scripts.nightly_pipeline as pipeline
    original_db = pipeline.DEFAULT_DB
    try:
        pipeline.DEFAULT_DB = tmp_path / "baheros.db"
        conn = sqlite3.connect(pipeline.DEFAULT_DB)
        conn.close()

        assert pipeline._get_last_ingest_at("test@example.com") is None
        pipeline._set_last_ingest_at("test@example.com", "2026-03-15T00:00:00Z")
        assert pipeline._get_last_ingest_at("test@example.com") == "2026-03-15T00:00:00Z"
    finally:
        pipeline.DEFAULT_DB = original_db


# ── Feature 10: Corpus deduplication ──


def test_dedup_script_importable() -> None:
    from scripts.deduplicate_corpus import (
        deduplicate,
        find_duplicate_documents,
        find_duplicate_reply_pairs,
    )
    assert callable(deduplicate)
    assert callable(find_duplicate_reply_pairs)
    assert callable(find_duplicate_documents)


def test_dedup_no_duplicates(tmp_path: Path) -> None:
    from scripts.deduplicate_corpus import find_duplicate_reply_pairs
    db_path = tmp_path / "test.db"
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text, metadata_json) "
        "VALUES ('gmail', 'id1', 'hello', 'hi', '{}')"
    )
    conn.commit()
    dupes = find_duplicate_reply_pairs(conn)
    conn.close()
    assert len(dupes) == 0


def test_dedup_finds_duplicates(tmp_path: Path) -> None:
    from scripts.deduplicate_corpus import find_duplicate_reply_pairs
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_type TEXT, source_id TEXT, thread_id TEXT, inbound_text TEXT, "
        "reply_text TEXT, metadata_json TEXT DEFAULT '{}')"
    )
    # Two rows with same source_type/source_id
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text) "
        "VALUES ('g','id1','a','b')",
    )
    conn.execute(
        "INSERT INTO reply_pairs (source_type, source_id, inbound_text, reply_text) "
        "VALUES ('g','id1','a','b')",
    )
    conn.commit()
    dupes = find_duplicate_reply_pairs(conn)
    conn.close()
    assert len(dupes) == 1


def test_nightly_pipeline_has_dedup_step() -> None:
    import scripts.nightly_pipeline as pipeline
    assert hasattr(pipeline, "step_deduplicate")


# ── Feature 11: Model set/show CLI ──


def test_cli_importable() -> None:
    from app.cli import app, model_app, model_set, model_show
    assert app is not None
    assert model_app is not None
    assert callable(model_set)
    assert callable(model_show)


# ── Feature 12: About page ──


def test_about_page_loads(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/about")
    assert response.status_code == 200
    assert "BaherOS" in response.text
    assert "YouOS" not in response.text


def test_about_page_has_key_sections(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/about")
    html = response.text
    assert "What is BaherOS" in html
    assert "Privacy model" in html
    assert "Autoresearch" in html
    assert "Tech stack" in html
    assert "FAQ" in html


def test_about_page_uses_periwinkle_not_teal(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/about")
    assert "#c8c8ff" in response.text
    assert "#00c4a7" not in response.text


# ── Feature 13: Onboarding tour ──


def test_onboarding_tour_in_html(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    html = response.text
    assert "tourOverlay" in html
    assert "baher_tour_done" in html
    assert "youos_tour_done" not in html
    assert "showTourLink" in html


def test_tour_has_six_steps(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    # Count tour steps
    assert "Your email. Your model." in response.text
    assert "Privacy by design" in response.text
    assert "It learns from you" in response.text
    assert "Autoresearch" in response.text
    assert "Draft Reply tab" in response.text
    assert "Start with the Review Queue" in response.text


# ── Cross-cutting: BaherOS branding ──


def test_no_youos_branding_in_feedback(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert "YouOS" not in response.text


def test_periwinkle_accent_in_feedback(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_db(db_path)
    client = _make_client(monkeypatch, db_path)
    response = client.get("/feedback")
    assert "#c8c8ff" in response.text
    assert "#00c4a7" not in response.text


# ── Bootstrap migration ──


def test_bootstrap_creates_draft_history(tmp_path: Path) -> None:
    from scripts.bootstrap_db import _migrate_draft_history
    db_path = tmp_path / "baheros.db"
    conn = sqlite3.connect(db_path)
    conn.close()
    created = _migrate_draft_history(db_path)
    assert created is True
    # Second call should be no-op
    created2 = _migrate_draft_history(db_path)
    assert created2 is False
