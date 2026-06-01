"""Tests for decoupling the embedding model from the drafting base (b177).

The drafting base migrated to Qwen3-4B (b174). Embeddings were resolving to
``model.base``, which silently moved query vectors into a different (and
differently-dimensioned) space than the ~11.7k stored vectors built with
Qwen2.5-1.5B — breaking semantic retrieval. These tests pin the decoupling:

- the embedding model is independent of ``model.base``;
- it defaults to the stable 1.5B the existing index uses;
- the indexer re-embeds rows tagged with a different model id (self-heal),
  skips rows already tagged with the current id, and the migration adds the
  ``embedding_model_id`` column idempotently.

All model calls are mocked — these are hermetic and never load real weights.
"""

from __future__ import annotations

import sqlite3

import pytest

import app.core.config as config_mod
from app.core.config import (
    DEFAULT_EMBEDDING_MODEL,
    get_base_model,
    get_embedding_model,
)
from app.core.embeddings import get_embedding_model_id


@pytest.fixture
def _config_file(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a temp youos_config.yaml and clear caches."""
    cfg = tmp_path / "youos_config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    yield cfg
    config_mod.load_config.cache_clear()


# -- Decoupling: embedding model independent of model.base ──────────────────


def test_default_embedding_model_is_dedicated_multilingual_embedder():
    # b180: the default embedder is now a dedicated multilingual retrieval model
    # (not the causal 1.5B). The whole point of b177 still holds — it is NOT the
    # 4B drafting base, and not any *-Instruct generative id.
    assert DEFAULT_EMBEDDING_MODEL == "intfloat/multilingual-e5-small"
    assert "instruct" not in DEFAULT_EMBEDDING_MODEL.lower()
    assert "qwen3" not in DEFAULT_EMBEDDING_MODEL.lower()


def test_embedding_model_defaults_when_unset(_config_file):
    _config_file.write_text("user:\n  name: Test\n", encoding="utf-8")
    assert get_embedding_model() == DEFAULT_EMBEDDING_MODEL


def test_embedding_model_independent_of_base(_config_file):
    """base=Qwen3-4B, embedding_model unset → embedder stays the default embedder."""
    _config_file.write_text(
        "model:\n  base: mlx-community/Qwen3-4B-Instruct-2507-4bit\n",
        encoding="utf-8",
    )
    assert get_base_model() == "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    # Embedding model must NOT follow the base.
    assert get_embedding_model() == DEFAULT_EMBEDDING_MODEL
    assert get_embedding_model() != get_base_model()
    assert get_embedding_model_id() == DEFAULT_EMBEDDING_MODEL


def test_embedding_model_explicit_override(_config_file):
    """base=Qwen3-4B + embedding_model=X → X (and still not the base)."""
    _config_file.write_text(
        "model:\n"
        "  base: mlx-community/Qwen3-4B-Instruct-2507-4bit\n"
        "  embedding_model: some/Custom-Embedder\n",
        encoding="utf-8",
    )
    assert get_embedding_model() == "some/Custom-Embedder"
    assert get_embedding_model_id() == "some/Custom-Embedder"
    assert get_embedding_model() != get_base_model()


def test_legacy_embeddings_model_id_takes_precedence(_config_file):
    """The legacy embeddings.model_id key still wins for backward compat."""
    _config_file.write_text(
        "model:\n"
        "  base: mlx-community/Qwen3-4B-Instruct-2507-4bit\n"
        "  embedding_model: some/Custom-Embedder\n"
        "embeddings:\n"
        "  model_id: legacy/override\n",
        encoding="utf-8",
    )
    assert get_embedding_model_id() == "legacy/override"


def test_changing_base_does_not_change_embedder(_config_file):
    """Regression: swapping model.base must not move the embedding model id."""
    _config_file.write_text("model:\n  base: foo/Base-A\n", encoding="utf-8")
    config_mod.load_config.cache_clear()
    first = get_embedding_model_id()

    _config_file.write_text("model:\n  base: bar/Base-B\n", encoding="utf-8")
    config_mod.load_config.cache_clear()
    second = get_embedding_model_id()

    assert first == second == DEFAULT_EMBEDDING_MODEL


# -- Indexer: migration + stale-row selection ──────────────────────────────


def _make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT)"
    )
    conn.commit()
    return conn


def test_migration_adds_column_idempotently(tmp_path):
    from scripts.index_embeddings import _ensure_embedding_columns

    db = tmp_path / "t.db"
    conn = _make_db(db)

    _ensure_embedding_columns(conn)
    for table in ("chunks", "reply_pairs"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        assert "embedding" in cols
        assert "embedding_model_id" in cols

    # Second run must not raise (duplicate-column) and must leave schema intact.
    _ensure_embedding_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "embedding_model_id" in cols
    conn.close()


def test_migration_noop_when_tables_absent(tmp_path):
    """Fresh instance pre-ingest: no chunks/reply_pairs tables → no error."""
    from scripts.index_embeddings import _ensure_embedding_columns

    conn = sqlite3.connect(tmp_path / "empty.db")
    _ensure_embedding_columns(conn)  # must not raise
    conn.close()


def test_pending_selects_stale_and_null_skips_matching(tmp_path):
    """A row tagged with a different model id is selected for re-embed; a row
    tagged with the current id is skipped; a NULL row is selected."""
    from scripts.index_embeddings import _count_pending, _ensure_embedding_columns, _pending_where

    db = tmp_path / "t.db"
    conn = _make_db(db)
    _ensure_embedding_columns(conn)

    current = "Qwen/Qwen2.5-1.5B-Instruct"
    # id=1: matching current → skip
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'a', ?, ?)",
        (b"\x00\x00\x00\x01", current),
    )
    # id=2: stale (old model) → re-embed
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (2, 'b', ?, ?)",
        (b"\x00\x00\x00\x01", "Qwen/Qwen3-4B-Instruct-2507"),
    )
    # id=3: NULL embedding → embed
    conn.execute("INSERT INTO chunks (id, content) VALUES (3, 'c')")
    # id=4: legacy (embedded, NULL model id) → trusted, skip
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (4, 'd', ?, NULL)",
        (b"\x00\x00\x00\x01",),
    )
    conn.commit()

    where, params = _pending_where(current, reindex=False)
    ids = {r[0] for r in conn.execute(f"SELECT id FROM chunks WHERE {where}", params).fetchall()}
    assert ids == {2, 3}, f"expected stale(2)+null(3), got {ids}"
    assert _count_pending(conn, "chunks", current, reindex=False) == 2

    # --reindex selects everything.
    assert _count_pending(conn, "chunks", current, reindex=True) == 4
    conn.close()


def test_index_table_reembeds_stale_with_mocked_model(tmp_path, monkeypatch):
    """End-to-end (mocked embedder): stale rows get re-embedded and re-tagged
    with the current model id; matching rows are left untouched."""
    import scripts.index_embeddings as idx

    db = tmp_path / "t.db"
    conn = _make_db(db)
    idx._ensure_embedding_columns(conn)

    current = "Qwen/Qwen2.5-1.5B-Instruct"
    monkeypatch.setattr(idx, "get_embedding_model_id", lambda: current)
    # Deterministic 4-dim vector; never loads real weights.
    monkeypatch.setattr(idx, "get_embedding", lambda text, **kw: (0.1, 0.2, 0.3, 0.4))
    monkeypatch.setattr(idx, "serialize_embedding", lambda emb: b"NEWVEC")

    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'keep', ?, ?)",
        (b"OLDKEEP", current),
    )
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (2, 'stale', ?, ?)",
        (b"OLDSTALE", "Qwen/Qwen3-4B-Instruct-2507"),
    )
    conn.commit()

    processed = idx._index_table(conn, "chunks", reindex=False)
    assert processed == 1  # only the stale row

    row1 = conn.execute("SELECT embedding, embedding_model_id FROM chunks WHERE id=1").fetchone()
    assert row1[0] == b"OLDKEEP"  # untouched
    assert row1[1] == current

    row2 = conn.execute("SELECT embedding, embedding_model_id FROM chunks WHERE id=2").fetchone()
    assert row2[0] == b"NEWVEC"  # re-embedded
    assert row2[1] == current  # re-tagged with current model
    conn.close()


def test_embed_failure_does_not_clobber_valid_embedding(tmp_path, monkeypatch):
    """A transient embed failure on a row that already has a valid embedding
    must NOT overwrite it with an empty blob (the b177 data-loss footgun)."""
    import scripts.index_embeddings as idx

    db = tmp_path / "t.db"
    conn = _make_db(db)
    idx._ensure_embedding_columns(conn)

    current = "Qwen/Qwen2.5-1.5B-Instruct"
    monkeypatch.setattr(idx, "get_embedding_model_id", lambda: current)

    calls = {"n": 0}

    def _emb(text: str, **kw):
        # First call is the up-front warmup probe (must succeed so the run
        # proceeds); subsequent per-row calls raise.
        calls["n"] += 1
        if calls["n"] == 1:
            return (1.0, 0.0)
        raise RuntimeError("boom")

    monkeypatch.setattr(idx, "get_embedding", _emb)
    monkeypatch.setattr(idx, "serialize_embedding", lambda emb: b"V")

    # A stale row that DOES have a valid (old-model) embedding.
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'x', ?, ?)",
        (b"GOODVEC1536", "Qwen/Qwen3-4B-Instruct-2507"),
    )
    conn.commit()

    idx._index_table(conn, "chunks", reindex=False)

    row = conn.execute("SELECT embedding FROM chunks WHERE id=1").fetchone()
    # The good (old) vector is preserved — NOT replaced with an empty blob.
    assert row[0] == b"GOODVEC1536"


def test_abort_when_model_cannot_load(tmp_path, monkeypatch):
    """If the embedding model can't even warm up, the run aborts loudly before
    writing any row — never silently empties the index."""
    import scripts.index_embeddings as idx

    db = tmp_path / "t.db"
    conn = _make_db(db)
    idx._ensure_embedding_columns(conn)
    monkeypatch.setattr(idx, "get_embedding_model_id", lambda: "x/y")

    def _boom(text: str, **kw):
        raise RuntimeError("no weights")

    monkeypatch.setattr(idx, "get_embedding", _boom)

    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'x', ?, NULL)",
        (b"GOODVEC",),
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="failed to load"):
        idx._index_table(conn, "chunks", reindex=True)

    # Nothing was clobbered.
    row = conn.execute("SELECT embedding FROM chunks WHERE id=1").fetchone()
    assert row[0] == b"GOODVEC"


def test_reindex_processes_all_rows_with_cursor(tmp_path, monkeypatch):
    """--reindex re-embeds every row exactly once (id-cursor pagination), even
    though its WHERE clause is always-true."""
    import scripts.index_embeddings as idx

    db = tmp_path / "t.db"
    conn = _make_db(db)
    idx._ensure_embedding_columns(conn)

    current = "Qwen/Qwen2.5-1.5B-Instruct"
    monkeypatch.setattr(idx, "get_embedding_model_id", lambda: current)
    monkeypatch.setattr(idx, "BATCH_SIZE", 2)  # force multiple batches
    calls: list[str] = []

    def _emb(text: str, **kw):
        calls.append(text)
        return (1.0, 0.0)

    monkeypatch.setattr(idx, "get_embedding", _emb)
    monkeypatch.setattr(idx, "serialize_embedding", lambda emb: b"V")

    for i in range(1, 6):  # 5 rows, all already current → reindex must redo all
        conn.execute(
            "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (?, ?, ?, ?)",
            (i, f"row{i}", b"OLD", current),
        )
    conn.commit()

    processed = idx._index_table(conn, "chunks", reindex=True)
    assert processed == 5
    # 5 per-row embeds + 1 up-front warmup probe = 6; the point is each row is
    # embedded exactly once (no infinite re-querying of the always-true WHERE).
    assert len(calls) == 6
    assert calls[0] == "warmup"
    assert sorted(calls[1:]) == [f"row{i}" for i in range(1, 6)]
    assert all(
        r[0] == b"V"
        for r in conn.execute("SELECT embedding FROM chunks").fetchall()
    )
    conn.close()
