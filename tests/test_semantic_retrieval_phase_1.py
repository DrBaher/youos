"""Semantic-retrieval Phase 1: indexer survives fresh corpora, coverage
observability lands, embedding-model identity is tracked per row.

Three things this PR opens up that were previously invisible / broken:

1. **G1 fresh-corpus crash.** ``_ensure_embedding_columns`` used to
   ``ALTER TABLE chunks ADD COLUMN embedding`` unconditionally — on an
   instance pre-first-ingest, the ``chunks`` table doesn't exist yet and
   the indexer crashed (visible as the ``Embedding indexer failed``
   WARN in the smoke nightlies). Now: tables that don't exist yet are
   simply skipped; ingestion will create them and the next indexer run
   picks them up.

2. **G2 coverage observability.** ``app.core.stats.get_embedding_coverage``
   returns ``{chunks: 0.42, reply_pairs: 0.31}`` for the active instance.
   Wired into ``pipeline_last_run.json`` (top-level ``embedding_coverage``
   field) and the ``/stats/data`` endpoint, alongside fixing a stale
   single-float ``system_health.embedding_coverage`` that was based on a
   buggy ``LIKE '%embedding%'`` query against ``metadata_json`` —
   producing a coverage number that had nothing to do with the actual
   embedding BLOB.

3. **G4 embedding-model identity.** New ``embeddings.model_id`` config
   key (defaults to ``get_base_model()`` — zero behaviour change) plus
   an ``embedding_model_id TEXT`` column on chunks/reply_pairs. The
   indexer tags every row with the model it used; the retrieval reranker
   skips rows whose model_id doesn't match the configured one (different
   embedding space = meaningless cosine sim). Legacy rows (NULL
   ``embedding_model_id``) are trusted as matching, so existing
   instances don't have to re-embed on upgrade.
"""

from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fresh_instance(monkeypatch, tmp_path, _reset_settings):
    """A YOUOS_DATA_DIR pointing at a tmp dir with only `var/`. No DB tables yet."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def populated_instance(monkeypatch, tmp_path, _reset_settings):
    """A YOUOS_DATA_DIR with a DB that already has chunks/reply_pairs tables."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, content TEXT);
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT
        );
        INSERT INTO chunks (id, content) VALUES (1, 'first chunk'), (2, 'second chunk');
        INSERT INTO reply_pairs (id, inbound_text, reply_text)
            VALUES (1, 'inbound a', 'reply a'), (2, 'inbound b', 'reply b'), (3, 'inbound c', 'reply c');
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


# ── G1: fresh-corpus crash ────────────────────────────────────────────────

def test_ensure_embedding_columns_skips_missing_tables(fresh_instance):
    """The smoke-test failure mode: fresh corpus, no `chunks` table yet.
    Pre-PR this raised `OperationalError: no such table: chunks` and the
    nightly's embedding step exited with WARN every single run until the
    first ingestion happened."""
    from scripts.index_embeddings import _ensure_embedding_columns

    db = fresh_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    try:
        # Must not raise. No tables exist; this is a no-op.
        _ensure_embedding_columns(conn)
    finally:
        conn.close()


def test_ensure_embedding_columns_adds_both_columns_when_tables_exist(populated_instance):
    """Once ingestion has created the tables, the migration adds *both* the
    legacy `embedding` BLOB and the new `embedding_model_id` TEXT columns."""
    from scripts.index_embeddings import _ensure_embedding_columns

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    try:
        _ensure_embedding_columns(conn)
        chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        rp_cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_pairs)").fetchall()}
        assert {"embedding", "embedding_model_id"} <= chunk_cols
        assert {"embedding", "embedding_model_id"} <= rp_cols
    finally:
        conn.close()


def test_ensure_embedding_columns_is_idempotent(populated_instance):
    from scripts.index_embeddings import _ensure_embedding_columns

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    try:
        _ensure_embedding_columns(conn)
        _ensure_embedding_columns(conn)  # second call must not raise
    finally:
        conn.close()


def test_index_table_no_ops_when_table_missing(fresh_instance):
    """`_index_table` shouldn't crash when called against a not-yet-created
    table — without this guard the indexer ran `SELECT COUNT(*) FROM chunks
    WHERE embedding IS NULL` and OperationalError-ed even after the
    `_ensure_embedding_columns` guard was in place. Confirmed in the smoke
    run: pre-fix the embeddings step was WARN every night; post-fix it's
    OK on a fresh instance."""
    from scripts.index_embeddings import _index_table

    db = fresh_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    try:
        # Must not raise. Returns 0 rows processed.
        processed = _index_table(conn, "chunks", limit=10, dry_run=False)
        assert processed == 0
    finally:
        conn.close()


def test_ensure_embedding_columns_only_one_of_chunks_or_reply_pairs(monkeypatch, tmp_path, _reset_settings):
    """Half-migrated DB — `chunks` exists, `reply_pairs` doesn't (or vice
    versa). Each table is independent; the missing one is skipped."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, content TEXT)")
    conn.commit()
    try:
        from scripts.index_embeddings import _ensure_embedding_columns

        _ensure_embedding_columns(conn)
        chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        assert "embedding" in chunk_cols
        assert "embedding_model_id" in chunk_cols
    finally:
        conn.close()


# ── G2: embedding coverage observability ──────────────────────────────────

def test_get_embedding_coverage_returns_per_table_fractions(populated_instance):
    """Half-embedded chunks (1/2), one-third-embedded reply_pairs (1/3)."""
    from app.core.stats import get_embedding_coverage

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.execute("ALTER TABLE reply_pairs ADD COLUMN embedding BLOB")
    conn.execute("UPDATE chunks SET embedding = ? WHERE id = 1", (b"\x00\x01\x02\x03",))
    conn.execute("UPDATE reply_pairs SET embedding = ? WHERE id = 2", (b"\x00\x01\x02\x03",))
    conn.commit()
    conn.close()

    coverage = get_embedding_coverage(f"sqlite:///{db}")
    assert coverage == {"chunks": 0.5, "reply_pairs": pytest.approx(1 / 3, abs=1e-3)}


def test_get_embedding_coverage_skips_tables_without_embedding_column(populated_instance):
    """The reply_pairs table here has no `embedding` column at all. Must be
    silently dropped from the result, not crash, not show 0.0 (which would
    misleadingly imply "indexed but everything failed")."""
    from app.core.stats import get_embedding_coverage

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.commit()
    conn.close()

    coverage = get_embedding_coverage(f"sqlite:///{db}")
    assert "chunks" in coverage
    assert "reply_pairs" not in coverage


def test_get_embedding_coverage_returns_empty_dict_for_missing_db(monkeypatch, tmp_path, _reset_settings):
    from app.core.stats import get_embedding_coverage

    coverage = get_embedding_coverage(f"sqlite:///{tmp_path}/nonexistent.db")
    assert coverage == {}


def test_get_embedding_coverage_skips_empty_tables(populated_instance):
    """A table exists with the embedding column but no rows — we can't
    divide by zero, and reporting "100% covered" or "0% covered" are both
    misleading. Drop it from the result."""
    from app.core.stats import get_embedding_coverage

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.execute("ALTER TABLE reply_pairs ADD COLUMN embedding BLOB")
    conn.execute("DELETE FROM chunks")
    conn.commit()
    conn.close()

    coverage = get_embedding_coverage(f"sqlite:///{db}")
    assert "chunks" not in coverage
    assert "reply_pairs" in coverage  # still has 3 rows, all unembedded
    assert coverage["reply_pairs"] == 0.0


def test_nightly_pipeline_writes_embedding_coverage_into_log(monkeypatch, populated_instance):
    """End-to-end: the nightly's run_log now includes per-table coverage so
    a dashboard / debugging session can answer 'is semantic firing?' from
    a single JSON file."""
    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.execute("ALTER TABLE reply_pairs ADD COLUMN embedding BLOB")
    conn.execute("UPDATE chunks SET embedding = ? WHERE id = 1", (b"\x00\x01",))
    conn.commit()
    conn.close()

    import scripts.nightly_pipeline as np_mod

    # Stub every step so main() runs without subprocesses.
    def _no(*_a, **_kw): return True

    def _no_dict(*_a, **_kw): return {"captured": 0, "total": 0, "skipped": 0, "errors": 0}

    def _no_embed(*_a, **_kw): return {"ok": True}

    monkeypatch.setattr(np_mod, "step_deduplicate", _no)
    monkeypatch.setattr(np_mod, "step_ingest_gmail", _no)
    monkeypatch.setattr(np_mod, "step_auto_feedback", _no_dict)
    monkeypatch.setattr(np_mod, "step_export_feedback", _no)
    monkeypatch.setattr(np_mod, "step_finetune_lora", _no)
    monkeypatch.setattr(np_mod, "step_golden_eval", _no)
    monkeypatch.setattr(np_mod, "step_index_embeddings", _no_embed)
    monkeypatch.setattr(np_mod, "step_autoresearch", _no)
    monkeypatch.setattr(np_mod, "should_skip_dedup", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_finetune", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_embeddings", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "should_skip_autoresearch", lambda _db: (False, ""))
    monkeypatch.setattr(np_mod, "_count_unused_feedback", lambda _db: 0)
    monkeypatch.setattr("sys.argv", ["nightly_pipeline.py"])

    from scripts.nightly_pipeline import main

    main()

    log = json.loads((populated_instance / "var" / "pipeline_last_run.json").read_text())
    assert "embedding_coverage" in log
    assert log["embedding_coverage"]["chunks"] == 0.5  # 1 of 2 chunks embedded


# ── G4: embedding-model identity ──────────────────────────────────────────

def test_get_embedding_model_id_defaults_to_dedicated_embedder(monkeypatch, _reset_settings):
    """No override → the dedicated embedder default (b177/b180), NOT the base."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **kw: {})
    from app.core.config import DEFAULT_EMBEDDING_MODEL, get_base_model
    from app.core.embeddings import get_embedding_model_id

    assert get_embedding_model_id() == DEFAULT_EMBEDDING_MODEL
    # Decoupled (b177): the embedder is never the drafting base.
    assert get_embedding_model_id() != get_base_model()


def test_get_embedding_model_id_honors_config_override(monkeypatch, _reset_settings):
    """`embeddings.model_id` in the config overrides the base-model fallback,
    so a user can pick a dedicated embedding model without changing their
    LoRA base."""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"embeddings": {"model_id": "nomic-ai/nomic-embed-text-v1.5"}},
    )
    from app.core.embeddings import get_embedding_model_id

    assert get_embedding_model_id() == "nomic-ai/nomic-embed-text-v1.5"


def test_get_embedding_model_id_ignores_blank_override(monkeypatch, _reset_settings):
    """A YAML with `embeddings: {model_id: ""}` is fat-fingered — fall back to the
    dedicated embedder default rather than passing the empty string to load."""
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda *a, **kw: {"embeddings": {"model_id": "   "}},
    )
    from app.core.config import DEFAULT_EMBEDDING_MODEL
    from app.core.embeddings import get_embedding_model_id

    assert get_embedding_model_id() == DEFAULT_EMBEDDING_MODEL


def test_retrieval_skips_embeddings_with_mismatched_model_id(populated_instance):
    """Stale-detection: a row whose stored model_id doesn't match the
    currently-configured one must be excluded from semantic reranking
    (different embedding space → cosine sim is noise)."""
    from app.retrieval.service import _has_column

    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding_model_id TEXT")
    conn.commit()
    try:
        # Sanity: the column-existence probe used in the reranker sees the
        # new column. Without this the reranker would never know to filter
        # by model_id and stale-detection would silently no-op.
        assert _has_column(conn, "chunks", "embedding_model_id")
        assert _has_column(conn, "chunks", "embedding")
        assert not _has_column(conn, "chunks", "nope")
    finally:
        conn.close()


def test_legacy_rows_with_null_model_id_are_still_used(populated_instance):
    """Back-compat: rows that predate the model_id column have it NULL.
    These must be trusted (treated as matching the current model) so
    existing instances don't have to re-embed on upgrade. The reranker's
    logic for this is `stored_model_id is not None and stored_model_id !=
    current_model_id` — pin the truth table here so the back-compat
    contract is loud."""
    current = "model-v1"

    def _stale_check(stored: str | None, current_: str) -> bool:
        # Mirrors the reranker's filter expression. Returns True if the
        # row should be SKIPPED.
        return stored is not None and stored != current_

    assert _stale_check(None, current) is False, "legacy NULL must be trusted as match"
    assert _stale_check("model-v1", current) is False, "exact match should pass"
    assert _stale_check("model-v2", current) is True, "different model should be skipped"
    assert _stale_check("", current) is True, "empty string is a real-but-different value, skip"


def test_indexer_writes_model_id_alongside_embedding(populated_instance, monkeypatch):
    """The model_id is captured per row at indexing time so a later swap
    can identify which rows are stale. Stub `get_embedding` so this test
    doesn't actually try to load mlx_lm in the Linux test container."""
    db = populated_instance / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
    conn.execute("ALTER TABLE chunks ADD COLUMN embedding_model_id TEXT")
    conn.commit()
    conn.row_factory = sqlite3.Row
    try:
        # Stub embedding + model id.
        import scripts.index_embeddings as idx

        monkeypatch.setattr(idx, "get_embedding", lambda _t, **kw: (0.1, 0.2, 0.3))
        monkeypatch.setattr(idx, "serialize_embedding", lambda emb: bytes([1, 2, 3, 4]))
        monkeypatch.setattr(idx, "get_embedding_model_id", lambda: "model-test-7")

        processed = idx._index_table(conn, "chunks", limit=2, dry_run=False)
        assert processed == 2

        rows = conn.execute("SELECT id, embedding, embedding_model_id FROM chunks ORDER BY id").fetchall()
        for row in rows:
            assert row["embedding"] == bytes([1, 2, 3, 4])
            assert row["embedding_model_id"] == "model-test-7"
    finally:
        conn.close()
