"""Tests for the dedicated (sentence/embedding) embedder path (b180).

b177 decoupled the embedding model id from the drafting base and added per-row
``embedding_model_id`` tagging + a self-heal/``--reindex`` indexer. b180 adds a
DEDICATED-embedder backend: when the configured embedding model is a proper
retrieval encoder (default ``intfloat/multilingual-e5-small``, a multilingual
XLM-RoBERTa model loaded via ``mlx_embeddings``), embeddings are produced by that
model (mean-pooled + L2-normalized by the model, E5 ``query:``/``passage:``
prefixes applied) instead of mean-pooling a causal LM.

These tests are hermetic: the model load + encode are monkeypatched, so no
weights are ever downloaded.
"""

from __future__ import annotations

import sqlite3

import pytest

import app.core.config as config_mod
import app.core.embeddings as emb
from app.core.config import DEFAULT_EMBEDDING_MODEL, get_base_model, get_embedding_model
from app.core.embeddings import (
    _apply_prefix,
    _is_dedicated_embedder,
    _is_e5,
    get_embedding,
    get_embedding_model_id,
)


@pytest.fixture
def _config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "youos_config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    config_mod.load_config.cache_clear()
    yield cfg
    config_mod.load_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_embed_singletons(monkeypatch):
    """Each test starts with no loaded model and a clean cache."""
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_tokenizer", None)
    monkeypatch.setattr(emb, "_backend", None)
    emb.clear_embedding_cache()
    yield
    emb.clear_embedding_cache()


# -- Default + decoupling ───────────────────────────────────────────────────


def test_default_is_dedicated_multilingual_embedder():
    assert DEFAULT_EMBEDDING_MODEL == "intfloat/multilingual-e5-small"
    assert _is_dedicated_embedder(DEFAULT_EMBEDDING_MODEL)
    assert _is_e5(DEFAULT_EMBEDDING_MODEL)


def test_embedder_resolves_to_configured_model_independent_of_base(_config_file):
    """The embedder id is the configured dedicated model regardless of model.base."""
    _config_file.write_text(
        "model:\n"
        "  base: mlx-community/Qwen3-4B-Instruct-2507-4bit\n"
        "  embedding_model: intfloat/multilingual-e5-small\n",
        encoding="utf-8",
    )
    assert get_embedding_model() == "intfloat/multilingual-e5-small"
    assert get_embedding_model_id() == "intfloat/multilingual-e5-small"
    assert get_embedding_model_id() != get_base_model()


def test_default_used_when_embedding_model_unset(_config_file):
    _config_file.write_text("model:\n  base: foo/Base\n", encoding="utf-8")
    assert get_embedding_model_id() == DEFAULT_EMBEDDING_MODEL


# -- Routing: dedicated vs causal fallback ──────────────────────────────────


@pytest.mark.parametrize(
    "model_id",
    [
        "intfloat/multilingual-e5-small",
        "intfloat/multilingual-e5-base",
        "mlx-community/multilingual-e5-small-mlx",
        "BAAI/bge-m3",
        "Snowflake/snowflake-arctic-embed-m",
        "mixedbread-ai/mxbai-embed-large",
        "nomic-ai/nomic-embed-text-v1.5",
        "sentence-transformers/all-MiniLM-L6-v2",
    ],
)
def test_dedicated_embedders_route_to_dedicated(model_id):
    assert _is_dedicated_embedder(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "Qwen/Qwen2.5-1.5B-Instruct",
        "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        "Qwen/Qwen3-4B-Instruct-2507",
        "meta-llama/Llama-3.1-8B-Instruct",
        "",
    ],
)
def test_causal_ids_fall_through_to_fallback(model_id):
    assert _is_dedicated_embedder(model_id) is False


# -- E5 prefix / pooling handling ───────────────────────────────────────────


def test_e5_prefixes_applied_per_kind():
    mid = "intfloat/multilingual-e5-small"
    assert _apply_prefix("hallo welt", mid, kind="query") == "query: hallo welt"
    assert _apply_prefix("hallo welt", mid, kind="passage") == "passage: hallo welt"


def test_non_e5_dedicated_embedder_gets_no_prefix():
    assert _apply_prefix("hallo welt", "BAAI/bge-m3", kind="query") == "hallo welt"
    assert _apply_prefix("hallo welt", "BAAI/bge-m3", kind="passage") == "hallo welt"


def test_dedicated_path_selected_and_encodes_via_model(monkeypatch, _config_file):
    """For an E5 id, the dedicated backend is used: the prefixed text is encoded
    through the mlx_embeddings model and its text_embeds returned (384-d here)."""
    _config_file.write_text(
        "model:\n  embedding_model: intfloat/multilingual-e5-small\n", encoding="utf-8"
    )

    captured = {}

    class _Row:
        # Mimics an mx.array row: supports .tolist().
        def __init__(self, vals):
            self._vals = list(vals)

        def tolist(self):
            return list(self._vals)

    class _FakeOut:
        # text_embeds[0] is the single-row embedding (mean-pooled + normalized).
        text_embeds = [_Row(0.01 * i for i in range(384))]

    class _FakeModel:
        def __call__(self, input_ids, attention_mask=None):
            return _FakeOut()

    class _FakeTok:
        def batch_encode_plus(self, texts, **kw):
            captured["texts"] = texts
            return {"input_ids": [[1, 2, 3]], "attention_mask": [[1, 1, 1]]}

    def _fake_load():
        emb._backend = "dedicated"
        return _FakeModel(), _FakeTok()

    monkeypatch.setattr(emb, "_load_model", _fake_load)

    vec = get_embedding("rechnung bezahlen", kind="query")

    # Dedicated path used: E5 query prefix applied, dim recorded = 384.
    assert captured["texts"] == ["query: rechnung bezahlen"]
    assert len(vec) == 384

    # A passage embed uses the passage prefix and shares the model output dim.
    emb.clear_embedding_cache()
    vec_p = get_embedding("rechnung bezahlen", kind="passage")
    assert captured["texts"] == ["passage: rechnung bezahlen"]
    assert len(vec_p) == 384


def test_causal_path_selected_for_qwen_id(monkeypatch, _config_file):
    """A Qwen *-Instruct id routes to the causal-LM mean-pooling fallback, not the
    dedicated path."""
    _config_file.write_text(
        "model:\n  embedding_model: Qwen/Qwen2.5-1.5B-Instruct\n", encoding="utf-8"
    )

    used = {"dedicated": False, "causal": False}

    def _fake_dedicated(text, model_id, *, kind):
        used["dedicated"] = True
        return (0.0,)

    def _fake_causal(text):
        used["causal"] = True
        return (0.1, 0.2, 0.3)

    # Simulate that loading resolved the causal backend.
    def _fake_load():
        emb._backend = "causal"
        return object(), object()

    monkeypatch.setattr(emb, "_load_model", _fake_load)
    monkeypatch.setattr(emb, "_embed_dedicated", _fake_dedicated)
    monkeypatch.setattr(emb, "_embed_causal", _fake_causal)

    vec = get_embedding("hello", kind="query")
    assert used["causal"] is True
    assert used["dedicated"] is False
    assert vec == (0.1, 0.2, 0.3)


def test_kind_is_part_of_cache_key(monkeypatch, _config_file):
    """query vs passage must not collide in the cache (different E5 prefixes)."""
    _config_file.write_text(
        "model:\n  embedding_model: intfloat/multilingual-e5-small\n", encoding="utf-8"
    )

    seen = []

    def _fake_dedicated(text, model_id, *, kind):
        seen.append(kind)
        return (1.0,) if kind == "query" else (2.0,)

    def _fake_load():
        emb._backend = "dedicated"
        return object(), object()

    monkeypatch.setattr(emb, "_load_model", _fake_load)
    monkeypatch.setattr(emb, "_embed_dedicated", _fake_dedicated)

    q = get_embedding("x", kind="query")
    p = get_embedding("x", kind="passage")
    assert q == (1.0,)
    assert p == (2.0,)
    assert seen == ["query", "passage"]  # both computed, no cross-kind cache hit


# -- Indexer: a different embedder id makes rows stale (b177 tagging) ────────


def _make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT, reply_text TEXT)"
    )
    conn.commit()
    return conn


def test_switching_to_dedicated_embedder_marks_old_rows_stale(tmp_path):
    """Rows tagged with the old causal-LM id are stale once the embedder is the
    new dedicated model — they are selected for re-embed; rows already tagged
    with the new id are skipped."""
    from scripts.index_embeddings import _count_pending, _ensure_embedding_columns, _pending_where

    db = tmp_path / "t.db"
    conn = _make_db(db)
    _ensure_embedding_columns(conn)

    new_id = "intfloat/multilingual-e5-small"
    old_id = "Qwen/Qwen2.5-1.5B-Instruct"

    # id=1: already on the new dedicated embedder → skip.
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'a', ?, ?)",
        (b"\x00\x00\x00\x01", new_id),
    )
    # id=2: old causal-LM vector (different model AND different dim) → re-embed.
    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (2, 'b', ?, ?)",
        (b"\x00\x00\x00\x01", old_id),
    )
    # id=3: never embedded → embed.
    conn.execute("INSERT INTO chunks (id, content) VALUES (3, 'c')")
    conn.commit()

    where, params = _pending_where(new_id, reindex=False)
    ids = {r[0] for r in conn.execute(f"SELECT id FROM chunks WHERE {where}", params).fetchall()}
    assert ids == {2, 3}
    assert _count_pending(conn, "chunks", new_id, reindex=False) == 2
    conn.close()


def test_reembed_with_dedicated_model_retags_rows(tmp_path, monkeypatch):
    """End-to-end (mocked dedicated embedder): a stale (causal-LM) row is
    re-embedded with the new dedicated model and re-tagged with its id; the
    indexer asks for a 'passage' embedding for corpus rows."""
    import scripts.index_embeddings as idx

    db = tmp_path / "t.db"
    conn = _make_db(db)
    idx._ensure_embedding_columns(conn)

    new_id = "intfloat/multilingual-e5-small"
    monkeypatch.setattr(idx, "get_embedding_model_id", lambda: new_id)

    kinds: list[str] = []

    def _emb(text, **kw):
        kinds.append(kw.get("kind", "query"))
        # 384-d dedicated-embedder vector (vs the old 1536-d causal one).
        return tuple(0.001 * i for i in range(384))

    monkeypatch.setattr(idx, "get_embedding", _emb)
    monkeypatch.setattr(idx, "serialize_embedding", lambda emb_vec: b"E5VEC")

    conn.execute(
        "INSERT INTO chunks (id, content, embedding, embedding_model_id) VALUES (1, 'stale', ?, ?)",
        (b"OLDCAUSAL", "Qwen/Qwen2.5-1.5B-Instruct"),
    )
    conn.commit()

    processed = idx._index_table(conn, "chunks", reindex=False)
    assert processed == 1

    row = conn.execute("SELECT embedding, embedding_model_id FROM chunks WHERE id=1").fetchone()
    assert row[0] == b"E5VEC"
    assert row[1] == new_id  # re-tagged with the dedicated embedder id
    # Corpus rows are embedded as passages (E5 needs the passage prefix). The
    # warmup probe also uses passage; every embed call here is a passage.
    assert kinds and all(k == "passage" for k in kinds)
    conn.close()
