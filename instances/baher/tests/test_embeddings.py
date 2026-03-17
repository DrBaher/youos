"""Tests for the embeddings module — serialize/deserialize, cosine similarity, hybrid scoring."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.embeddings import (
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from app.retrieval.service import (
    RetrievalConfig,
    RetrievalRequest,
    RetrievalService,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


def _has_mlx() -> bool:
    try:
        import mlx.core  # noqa: F401
        return True
    except ImportError:
        return False


# ── Serialize / Deserialize roundtrip ──────────────────────────────────


def test_serialize_deserialize_roundtrip() -> None:
    original = [0.1, 0.2, -0.3, 0.0, 1.0, -1.0]
    blob = serialize_embedding(original)
    restored = deserialize_embedding(blob)
    assert len(restored) == len(original)
    for a, b in zip(original, restored):
        assert abs(a - b) < 1e-6


def test_serialize_empty_vector() -> None:
    blob = serialize_embedding([])
    restored = deserialize_embedding(blob)
    assert restored == []


def test_serialize_single_element() -> None:
    blob = serialize_embedding([42.0])
    restored = deserialize_embedding(blob)
    assert len(restored) == 1
    assert abs(restored[0] - 42.0) < 1e-6


# ── Cosine similarity ─────────────────────────────────────────────────


def test_cosine_similarity_identical_vectors() -> None:
    v = [1.0, 2.0, 3.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal_vectors() -> None:
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_opposite_vectors() -> None:
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-6


def test_cosine_similarity_known_value() -> None:
    a = [1.0, 2.0, 3.0]
    b = [4.0, 5.0, 6.0]
    dot = 1 * 4 + 2 * 5 + 3 * 6  # 32
    norm_a = math.sqrt(1 + 4 + 9)  # sqrt(14)
    norm_b = math.sqrt(16 + 25 + 36)  # sqrt(77)
    expected = dot / (norm_a * norm_b)
    assert abs(cosine_similarity(a, b) - expected) < 1e-6


def test_cosine_similarity_zero_vector() -> None:
    a = [0.0, 0.0, 0.0]
    b = [1.0, 2.0, 3.0]
    assert cosine_similarity(a, b) == 0.0


# ── get_embedding dimension (mocked) ──────────────────────────────────


@pytest.mark.skipif(
    not _has_mlx(),
    reason="mlx not installed",
)
def test_get_embedding_returns_correct_dimension() -> None:
    """Mock the MLX model to verify get_embedding returns normalized vector."""
    import mlx.core as mx

    mock_dim = 128

    mock_model = MagicMock()
    mock_tokenizer = MagicMock()
    mock_tokenizer.encode.return_value = [1, 2, 3]

    fake_hidden = mx.ones((1, 3, mock_dim))
    mock_model.model.return_value = fake_hidden

    with patch("app.core.embeddings._load_model", return_value=(mock_model, mock_tokenizer)):
        from app.core.embeddings import get_embedding
        result = get_embedding("test text")

    assert len(result) == mock_dim
    # Verify normalized (L2 norm ~= 1.0)
    norm = math.sqrt(sum(x * x for x in result))
    assert abs(norm - 1.0) < 1e-4


# ── Hybrid scoring in retrieval ────────────────────────────────────────


def test_hybrid_scoring_when_embeddings_available(tmp_path: Path) -> None:
    """When enough embeddings exist, semantic_search_enabled should be True."""
    db_path = tmp_path / "baheros.db"
    _seed_db_with_embeddings(db_path, embed_fraction=1.0)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    # Mock get_embedding for query embedding
    fake_emb = [0.1] * 64
    with patch("app.retrieval.service.get_embedding", return_value=fake_emb):
        response = service.retrieve(
            RetrievalRequest(query="trim the intro", scope="reply_pairs")
        )

    assert response.semantic_search_enabled is True
    assert response.reply_pairs


def test_hybrid_scoring_disabled_when_no_embeddings(tmp_path: Path) -> None:
    """When no embeddings exist, semantic_search_enabled should be False."""
    db_path = tmp_path / "baheros.db"
    _seed_db_with_embeddings(db_path, embed_fraction=0.0)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(query="trim the intro", scope="reply_pairs")
    )

    assert response.semantic_search_enabled is False


def test_hybrid_scoring_disabled_below_threshold(tmp_path: Path) -> None:
    """Semantic search disabled when fewer than semantic_min_coverage rows have embeddings."""
    db_path = tmp_path / "baheros.db"
    # Only 1 out of 12 reply pairs has embedding = 8.3% < 10% threshold
    _seed_db_with_embeddings(db_path, embed_fraction=0.05)

    service = RetrievalService.from_database_url(
        database_url=f"sqlite:///{db_path}",
        configs_dir=ROOT_DIR / "configs",
    )

    response = service.retrieve(
        RetrievalRequest(query="trim the intro", scope="reply_pairs")
    )

    assert response.semantic_search_enabled is False


def test_existing_retrieval_tests_still_pass(tmp_path: Path) -> None:
    """Verify that basic FTS5 retrieval still works without embeddings."""
    db_path = tmp_path / "baheros.db"
    _seed_plain_db(db_path)

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
    assert response.reply_pairs[0].reply_text == (
        "Makes sense. I will tighten the intro and keep the examples."
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _seed_plain_db(db_path: Path) -> None:
    """Seed a DB without embeddings (same as test_retrieval.py)."""
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        _insert_base_data(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_db_with_embeddings(db_path: Path, *, embed_fraction: float) -> None:
    """Seed a DB and add embeddings to a fraction of reply_pairs."""
    schema_sql = (ROOT_DIR / "docs" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        # Ensure embedding columns exist
        for table in ("chunks", "reply_pairs"):
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "embedding" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding BLOB")
        _insert_base_data(conn)
        # Add more reply pairs for coverage testing
        for i in range(10):
            conn.execute(
                """
                INSERT INTO reply_pairs (
                    source_type, source_id, thread_id,
                    inbound_text, reply_text, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "gmail_thread",
                    f"pair-extra-{i}",
                    f"thread-extra-{i}",
                    f"Question about topic {i}",
                    f"Answer about topic {i}",
                    json.dumps({"account_email": "drbaher@gmail.com"}),
                ),
            )

        # Embed a fraction of reply_pairs
        all_ids = [
            row[0] for row in conn.execute("SELECT id FROM reply_pairs").fetchall()
        ]
        n_to_embed = max(int(len(all_ids) * embed_fraction), 0)
        fake_emb = serialize_embedding([0.1] * 64)
        for row_id in all_ids[:n_to_embed]:
            conn.execute(
                "UPDATE reply_pairs SET embedding = ? WHERE id = ?",
                (fake_emb, row_id),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_base_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO documents (
            source_type, source_id, title, author, external_uri,
            thread_id, created_at, updated_at, content, metadata_json, ingestion_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "gmail_thread", "m1", "Draft review",
            "Alice <alice@example.com>", None, "thread-1",
            "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z",
            "Please trim the intro. Keep the examples.",
            json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
            "seed-1",
        ),
    )
    gmail_doc_id = conn.execute(
        "SELECT id FROM documents WHERE source_id = 'm1'"
    ).fetchone()[0]

    conn.execute(
        """
        INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (gmail_doc_id, 0, "Please trim the intro. Keep the examples.", 7, 43,
         json.dumps({"chunk_role": "message_body"})),
    )
    conn.execute(
        """
        INSERT INTO reply_pairs (
            source_type, source_id, document_id, thread_id,
            inbound_text, reply_text, inbound_author, reply_author,
            paired_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "gmail_thread", "pair-1", gmail_doc_id, "thread-1",
            "Please trim the intro.\n\nKeep the examples.",
            "Makes sense. I will tighten the intro and keep the examples.",
            "Alice <alice@example.com>", "Baher <drbaher@gmail.com>",
            "2026-03-01T09:40:00Z",
            json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_gmail"}),
        ),
    )

    conn.execute(
        """
        INSERT INTO documents (
            source_type, source_id, title, author, external_uri,
            thread_id, created_at, updated_at, content, metadata_json, ingestion_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "google_doc", "doc-1", "Drafting Playbook",
            "Baher <drbaher@gmail.com>",
            "https://docs.google.com/document/d/doc-1/edit", None,
            "2026-03-02T10:00:00Z", "2026-03-04T12:00:00Z",
            "When revising drafts, trim the intro first and keep examples that clarify the point.",
            json.dumps({"account_email": "drbaher@gmail.com", "source": "gog_docs"}),
            "seed-2",
        ),
    )
    google_doc_id = conn.execute(
        "SELECT id FROM documents WHERE source_id = 'doc-1'"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO chunks (document_id, chunk_index, content, token_count, char_count, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (google_doc_id, 0,
         "When revising drafts, trim the intro first and keep examples that clarify the point.",
         13, 84, json.dumps({"chunk_role": "document_text"})),
    )
    conn.execute(
        """
        INSERT INTO reply_pairs (
            source_type, source_id, document_id, thread_id,
            inbound_text, reply_text, inbound_author, reply_author,
            paired_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "gmail_thread", "pair-2", gmail_doc_id, "thread-2",
            "Can you send a status review?",
            "Yes. I will send a short status review today.",
            "Sam <sam@example.com>", "Baher <baher@medicus.ai>",
            "2026-02-01T11:00:00Z",
            json.dumps({"account_email": "baher@medicus.ai", "source": "gog_gmail"}),
        ),
    )
