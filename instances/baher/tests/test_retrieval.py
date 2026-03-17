import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app
from app.retrieval.service import (
    RetrievalRequest,
    RetrievalService,
    detect_mode,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


def test_retrieval_service_returns_reply_pairs_and_authored_docs(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(
            query="trim the intro and keep the examples",
            source_types=("gmail_thread", "google_doc"),
            account_emails=("drbaher@gmail.com",),
        )
    )

    assert response.retrieval_method == "fts5_bm25"
    assert response.semantic_search_enabled is False
    assert response.reply_pairs
    assert response.reply_pairs[0].reply_text == "Makes sense. I will tighten the intro and keep the examples."
    assert response.chunks
    chunk_source_types = {c.source_type for c in response.chunks}
    assert "google_doc" in chunk_source_types
    google_doc_chunk = [c for c in response.chunks if c.source_type == "google_doc"][0]
    assert google_doc_chunk.title == "Drafting Playbook"


def test_retrieval_service_honors_scope_and_account_filters(tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(
            query="status review",
            scope="reply_pairs",
            account_emails=("baher@medicus.ai",),
        )
    )

    assert not response.documents
    assert not response.chunks
    assert len(response.reply_pairs) == 1
    assert response.reply_pairs[0].account_email == "baher@medicus.ai"


def test_retrieval_api_route_returns_grouped_results(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    monkeypatch.setenv("BAHEROS_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.post(
        "/retrieval/lookup",
        json={
            "query": "draft intro examples",
            "scope": "all",
            "source_types": ["gmail_thread", "google_doc"],
            "account_emails": ["drbaher@gmail.com"],
            "top_k_chunks": 2,
            "top_k_reply_pairs": 2,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["retrieval_method"] == "fts5_bm25"
    assert payload["semantic_search_enabled"] is False
    assert "detected_mode" in payload
    assert len(payload["chunks"]) <= 2
    assert len(payload["reply_pairs"]) <= 2
    assert payload["reply_pairs"][0]["reply_text"] == (
        "Makes sense. I will tighten the intro and keep the examples."
    )


def test_fts5_retrieval_ranks_by_bm25(tmp_path: Path) -> None:
    """FTS5 search returns results ranked by BM25 relevance."""
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(query="intro examples trim", scope="reply_pairs")
    )

    assert response.retrieval_method == "fts5_bm25"
    assert response.reply_pairs
    # The best match should be the one about trimming the intro
    assert "intro" in response.reply_pairs[0].inbound_text.lower()


def test_fts5_chunks_search(tmp_path: Path) -> None:
    """FTS5 search works for chunks."""
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(query="revising drafts examples", scope="documents")
    )

    assert response.retrieval_method == "fts5_bm25"
    assert response.chunks
    chunk_contents = [c.content.lower() for c in response.chunks]
    assert any("drafts" in c for c in chunk_contents)


def test_detected_mode_in_response(tmp_path: Path) -> None:
    """Mode detection result is included in the retrieval response."""
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(query="pricing proposal for the client")
    )
    assert response.detected_mode == "work"

    response2 = service.retrieve(
        RetrievalRequest(query="dinner with family this weekend")
    )
    assert response2.detected_mode == "personal"


def test_mode_detection_work() -> None:
    assert detect_mode("Send the pricing proposal to the client") == "work"
    assert detect_mode("API integration timeline for vendor") == "work"


def test_mode_detection_personal() -> None:
    assert detect_mode("dinner with friends this weekend") == "personal"
    assert detect_mode("family vacation plans for the kids") == "personal"


def test_mode_detection_unknown() -> None:
    # Very short queries with no signals return unknown
    assert detect_mode("hi") == "unknown"
    assert detect_mode("ok thanks") == "unknown"
    # Longer queries without strong signals default to work
    assert detect_mode("check the latest update") == "work"


def test_account_boost_increases_score(tmp_path: Path) -> None:
    """When account_emails filter is set, matching results get a score boost."""
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    # Query that matches both reply pairs
    response_with_account = service.retrieve(
        RetrievalRequest(
            query="status review intro trim",
            scope="reply_pairs",
            account_emails=("baher@medicus.ai",),
        )
    )
    response_without_account = service.retrieve(
        RetrievalRequest(
            query="status review intro trim",
            scope="reply_pairs",
        )
    )

    # The medicus reply pair should have a higher metadata_score with the account filter
    medicus_with = [
        rp for rp in response_with_account.reply_pairs
        if rp.account_email == "baher@medicus.ai"
    ]
    medicus_without = [
        rp for rp in response_without_account.reply_pairs
        if rp.account_email == "baher@medicus.ai"
    ]
    if medicus_with and medicus_without:
        assert medicus_with[0].metadata_score > medicus_without[0].metadata_score


def test_retrieval_config_defaults_yaml(tmp_path: Path) -> None:
    """Config is loaded from configs/retrieval/defaults.yaml when present."""
    db_path = tmp_path / "baheros.db"
    _seed_retrieval_db(db_path)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    # Values reflect current autoresearch-tuned config — update if config changes
    assert service.config.top_k_reply_pairs >= 3
    assert service.config.top_k_documents >= 1
    assert service.config.top_k_chunks >= 1
    assert service.config.recency_boost_days >= 30
    assert 0.0 <= service.config.recency_boost_weight <= 0.5
    assert 0.0 <= service.config.account_boost_weight <= 0.4


def _seed_retrieval_db(db_path: Path) -> None:
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema_sql)
        connection.execute(
            """
            INSERT INTO documents (
                source_type,
                source_id,
                title,
                author,
                external_uri,
                thread_id,
                created_at,
                updated_at,
                content,
                metadata_json,
                ingestion_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread",
                "m1",
                "Draft review",
                "Alice <alice@example.com>",
                None,
                "thread-1",
                "2026-03-01T09:00:00Z",
                "2026-03-01T09:00:00Z",
                "Please trim the intro. Keep the examples.",
                json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
                "seed-1",
            ),
        )
        gmail_document_id = connection.execute(
            "SELECT id FROM documents WHERE source_type = 'gmail_thread' AND source_id = 'm1'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                gmail_document_id,
                0,
                "Please trim the intro. Keep the examples.",
                7,
                43,
                json.dumps({"chunk_role": "message_body"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO reply_pairs (
                source_type,
                source_id,
                document_id,
                thread_id,
                inbound_text,
                reply_text,
                inbound_author,
                reply_author,
                paired_at,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread",
                "pair-1",
                gmail_document_id,
                "thread-1",
                "Please trim the intro.\n\nKeep the examples.",
                "Makes sense. I will tighten the intro and keep the examples.",
                "Alice <alice@example.com>",
                "Baher <drbaher@gmail.com>",
                "2026-03-01T09:40:00Z",
                json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO documents (
                source_type,
                source_id,
                title,
                author,
                external_uri,
                thread_id,
                created_at,
                updated_at,
                content,
                metadata_json,
                ingestion_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "google_doc",
                "doc-1",
                "Drafting Playbook",
                "Baher <drbaher@gmail.com>",
                "https://docs.google.com/document/d/doc-1/edit",
                None,
                "2026-03-02T10:00:00Z",
                "2026-03-04T12:00:00Z",
                "When revising drafts, trim the intro first and keep examples that clarify the point.",
                json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_docs"}),
                "seed-2",
            ),
        )
        google_doc_id = connection.execute(
            "SELECT id FROM documents WHERE source_type = 'google_doc' AND source_id = 'doc-1'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                google_doc_id,
                0,
                "When revising drafts, trim the intro first and keep examples that clarify the point.",
                13,
                84,
                json.dumps({"chunk_role": "document_text"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO reply_pairs (
                source_type,
                source_id,
                document_id,
                thread_id,
                inbound_text,
                reply_text,
                inbound_author,
                reply_author,
                paired_at,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail_thread",
                "pair-2",
                gmail_document_id,
                "thread-2",
                "Can you send a status review?",
                "Yes. I will send a short status review today.",
                "Sam <sam@example.com>",
                "Baher <baher@medicus.ai>",
                "2026-02-01T11:00:00Z",
                json.dumps({"account_email": "baher@medicus.ai", "source": "gog_gmail"}),
            ),
        )
        connection.commit()
    finally:
        connection.close()
