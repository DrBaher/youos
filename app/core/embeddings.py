"""Local semantic embeddings for YouOS retrieval.

Two interchangeable backends, selected purely by the configured embedding model
id (``app.core.config.get_embedding_model`` / the legacy ``embeddings.model_id``
override — see ``get_embedding_model_id``):

1. **Dedicated embedder** (b180, preferred): a proper sentence/embedding model
   (e.g. ``intfloat/multilingual-e5-small`` / its ``mlx-community/*-mlx``
   conversion) loaded via ``mlx_embeddings``. These are bidirectional encoders
   trained for retrieval: correct mean-pooling + L2-normalization are applied by
   the model itself (``outputs.text_embeds``). E5-family ids additionally get the
   required ``query:`` / ``passage:`` prefixes. Multilingual (DE/EN) by design.

2. **Causal-LM fallback** (the pre-b180 path): mean-pools the hidden states of a
   causal LM (Qwen2.5-1.5B) loaded via ``mlx_lm``. Heavier and not purpose-built
   for retrieval, but keeps working with the existing index and on hosts where
   ``mlx_embeddings`` is not installed.

Routing is by model id (``_is_dedicated_embedder``): dedicated-embedder families
take backend #1; Qwen / *-Instruct causal ids take backend #2. Per-row
``embedding_model_id`` tagging (b177) means swapping the embedder marks old rows
stale so ``scripts/index_embeddings.py --reindex`` re-embeds them — and retrieval
only compares vectors produced by the *same* model id, so a dim change (the 1.5B
is 1536-d, e5-small is 384-d) is safe across the swap.

Embeddings are optional at runtime; the system falls back to FTS5-only retrieval
if neither backend can load.
"""

from __future__ import annotations

import functools
import math
import struct
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Module-level singletons for lazy model loading. ``_backend`` records which
# path produced the loaded model so encode/decode stay consistent within a run.
_model = None
_tokenizer = None
_backend: str | None = None  # "dedicated" | "causal"
_loaded_model_id: str | None = None  # which id the singleton actually loaded (b255)

# Embedding-model id families that are DEDICATED retrieval encoders (backend #1).
# Matched case-insensitively as substrings of the (lowercased) model id. These
# are bidirectional sentence/embedding models — never causal LMs.
_DEDICATED_EMBEDDER_MARKERS = (
    "e5",  # intfloat/multilingual-e5-* (and friends)
    "bge",  # BAAI/bge-* (bge-m3, bge-small, ...)
    "gte",  # Alibaba-NLP/gte-*
    "arctic-embed",  # Snowflake/snowflake-arctic-embed-*
    "snowflake-arctic",
    "mxbai-embed",  # mixedbread-ai/mxbai-embed-*
    "nomic-embed",  # nomic-ai/nomic-embed-*
    "minilm",  # sentence-transformers/all-MiniLM-*, multilingual-MiniLM
    "modernbert-embed",
    "sentence-transformers/",
    "-embed",  # generic "*-embed*" naming
    "embed-",
)


def get_embedding_model_id() -> str:
    """Identifier of the model used to produce embeddings.

    DECOUPLED from the drafting base as of b177. The drafting base migrated to
    Qwen3-4B (b174); embeddings must NOT follow it. Resolution:

    1. ``embeddings.model_id`` config key (legacy explicit override), then
    2. ``model.embedding_model`` / ``DEFAULT_EMBEDDING_MODEL`` via
       ``app.core.config.get_embedding_model`` (b180 default: a dedicated
       multilingual embedder).

    The recorded id is stored per-row so a future swap can detect and re-embed
    stale rows. It NEVER resolves to ``model.base``.
    """
    from app.core.config import get_embedding_model, load_config

    cfg = load_config() or {}
    emb_cfg = cfg.get("embeddings", {}) if isinstance(cfg, dict) else {}
    override = emb_cfg.get("model_id") if isinstance(emb_cfg, dict) else None
    if isinstance(override, str) and override.strip():
        return override.strip()
    return get_embedding_model(cfg)


def _is_dedicated_embedder(model_id: str) -> bool:
    """True if *model_id* names a dedicated sentence/embedding model (backend #1).

    Routing is conservative: an id is treated as a dedicated embedder only when
    it matches a known embedder family marker AND does not look like an
    instruction-tuned causal LM. Anything else (Qwen*, *-Instruct, unknown
    generative ids) falls through to the causal-LM mean-pooling path so the
    legacy behaviour and the existing 1.5B index keep working untouched.
    """
    mid = (model_id or "").lower()
    if not mid:
        return False
    # An "-instruct" / generative Qwen id is the drafting/causal family, never a
    # dedicated embedder — even if some substring coincidentally matched. The
    # exception is an explicit embedding build (e.g. "qwen3-embedding"): the
    # "embed" marker below still wins for those.
    if ("instruct" in mid or "qwen2.5" in mid or "qwen3-" in mid) and "embed" not in mid:
        return False
    return any(marker in mid for marker in _DEDICATED_EMBEDDER_MARKERS)


def _is_e5(model_id: str) -> bool:
    """True for E5-family ids, which require ``query:`` / ``passage:`` prefixes."""
    mid = (model_id or "").lower()
    return "e5" in mid


def _apply_prefix(text: str, model_id: str, *, kind: str) -> str:
    """Apply the model-specific input prefix for *kind* ("query" | "passage").

    Only E5 requires this ("query: " / "passage: ", per the model card — needed
    even for non-English text, or retrieval quality degrades). Other dedicated
    encoders (bge-m3, arctic, mxbai, gte, MiniLM) take raw text and do their own
    pooling, so they are returned unchanged.
    """
    if _is_e5(model_id):
        prefix = "query: " if kind == "query" else "passage: "
        return f"{prefix}{text}"
    return text


def _load_model():
    """Lazy-load the configured embedding model + tokenizer.

    Routes by model id: a dedicated embedder loads via ``mlx_embeddings``; any
    other (causal) id loads via ``mlx_lm`` and is mean-pooled by hand. Sets the
    module-level ``_backend`` so the encode path knows how to read the model.
    """
    global _model, _tokenizer, _backend, _loaded_model_id
    model_id = get_embedding_model_id()
    if _model is not None:
        if _loaded_model_id == model_id:
            return _model, _tokenizer
        # embeddings.model_id changed at runtime (b255): the singleton would
        # otherwise keep serving vectors from the OLD model's space (and the
        # lru cache would return them) while stored rows are validated against
        # the NEW id — drop both and reload.
        _model = None
        _tokenizer = None
        _backend = None
        _get_embedding_cached.cache_clear()

    dedicated = _is_dedicated_embedder(model_id)

    if dedicated:
        try:
            import mlx.core as mx  # noqa: F401
            from mlx_embeddings.utils import load as _emb_load
        except ImportError as exc:
            raise RuntimeError(
                "mlx_embeddings is required for the dedicated embedding model "
                f"{model_id!r}. Install with: pip install mlx-embeddings"
            ) from exc
        try:
            _model, _tokenizer = _emb_load(model_id)
            _backend = "dedicated"
            _loaded_model_id = model_id
        except Exception as exc:
            _model = None
            _tokenizer = None
            _backend = None
            raise RuntimeError(
                f"Failed to load dedicated embedding model {model_id!r}: {exc}. "
                "Refusing to produce embeddings in an undefined space."
            ) from exc
        return _model, _tokenizer

    # Causal-LM fallback (pre-b180 path): mean-pool a generative model's hidden
    # states via mlx_lm.
    try:
        import mlx.core as mx  # noqa: F401
        from mlx_lm import load
    except ImportError as exc:
        raise RuntimeError("mlx_lm is required for embedding generation. Install with: pip install mlx-lm") from exc

    try:
        _model, _tokenizer = load(model_id)
        _backend = "causal"
        _loaded_model_id = model_id
    except Exception as exc:
        # Fail loud rather than silently writing garbage / a wrong-space index.
        _model = None
        _tokenizer = None
        _backend = None
        raise RuntimeError(
            f"Failed to load embedding model {model_id!r}: {exc}. "
            "Refusing to produce embeddings in an undefined space."
        ) from exc
    return _model, _tokenizer


def _embed_dedicated(text: str, model_id: str, *, kind: str) -> tuple[float, ...]:
    """Encode *text* with a dedicated embedder (mlx_embeddings backend).

    The model applies the correct pooling (mean) + L2-normalization itself and
    exposes the result as ``outputs.text_embeds``. We only add the model-specific
    input prefix (E5) and pass the attention mask through.
    """
    model, tokenizer = _load_model()
    prefixed = _apply_prefix(text, model_id, kind=kind)

    inputs = tokenizer.batch_encode_plus(
        [prefixed],
        return_tensors="mlx",
        padding=True,
        truncation=True,
        max_length=512,
    )
    outputs = model(inputs["input_ids"], attention_mask=inputs["attention_mask"])
    emb = outputs.text_embeds[0]  # already mean-pooled + normalized
    return tuple(emb.tolist())


def _embed_causal(text: str) -> tuple[float, ...]:
    """Encode *text* by mean-pooling a causal LM's hidden states (fallback)."""
    import mlx.core as mx

    model, tokenizer = _load_model()
    tokens = tokenizer.encode(text, return_tensors=None)
    if not tokens:
        dim = model.model.embed_tokens.weight.shape[1]
        return tuple([0.0] * dim)

    input_ids = mx.array([tokens])
    hidden = model.model(input_ids)  # (1, seq_len, hidden_dim)
    embedding = mx.mean(hidden, axis=1).squeeze(0)  # (hidden_dim,)
    norm = mx.sqrt(mx.sum(embedding * embedding))
    norm = mx.maximum(norm, mx.array(1e-12))
    embedding = embedding / norm
    return tuple(embedding.tolist())


@functools.lru_cache(maxsize=512)
def _get_embedding_cached(text: str, kind: str, model_id: str) -> tuple[float, ...]:
    """Cached embed of *text* for the given *kind* ("query" | "passage").

    The kind is part of the cache key because E5 produces different vectors for
    ``query:`` vs ``passage:`` prefixes; the model id is too (b255) so a
    runtime model swap can never serve a vector from the old space.
    """
    _load_model()
    if _backend == "dedicated":
        return _embed_dedicated(text, model_id, kind=kind)
    return _embed_causal(text)


def get_embedding(text: str, *, kind: str = "query") -> tuple[float, ...]:
    """Generate a normalized embedding for *text*.

    *kind* ("query" | "passage") only affects dedicated E5 embedders (input
    prefix); it is ignored by the causal-LM fallback and by non-E5 encoders.
    Indexing passes ``kind="passage"`` for corpus rows; retrieval uses the
    default ``"query"``.

    Returns a tuple (hashable) for lru_cache compatibility.
    """
    return _get_embedding_cached(text, kind, get_embedding_model_id())


def get_embedding_cache_info() -> dict[str, int]:
    """Return cache stats for get_embedding."""
    info = _get_embedding_cached.cache_info()
    return {"hits": info.hits, "misses": info.misses, "size": info.currsize}


def clear_embedding_cache() -> None:
    """Clear the embedding LRU cache."""
    _get_embedding_cached.cache_clear()


def get_embedding_batch(texts: list[str], *, kind: str = "query") -> list[tuple[float, ...]]:
    """Generate embeddings for a batch of texts.

    Dedicated embedders are encoded in a single batched forward pass (correct
    per-row mean pooling via the attention mask); the causal-LM fallback batches
    through MLX with manual mean pooling, falling back to sequential per-item
    processing if batching fails.
    """
    if not texts:
        return []

    # Check cache first — return immediately if all cached.
    results: list[tuple[float, ...] | None] = [None] * len(texts)
    uncached_indices: list[int] = []
    for i, t in enumerate(texts):
        try:
            results[i] = get_embedding(t, kind=kind)
        except Exception:
            uncached_indices.append(i)

    if not uncached_indices:
        return [r for r in results if r is not None]

    model_id = get_embedding_model_id()
    try:
        import mlx.core as mx  # noqa: F401

        _load_model()
        uncached_texts = [texts[i] for i in uncached_indices]

        if _backend == "dedicated":
            embs = _embed_dedicated_batch(uncached_texts, model_id, kind=kind)
            for j, orig_idx in enumerate(uncached_indices):
                results[orig_idx] = embs[j]
        else:
            results = _causal_batch_fill(results, uncached_indices, uncached_texts)
    except Exception:
        for i in uncached_indices:
            results[i] = get_embedding(texts[i], kind=kind)

    return [r if r is not None else get_embedding(texts[i], kind=kind) for i, r in enumerate(results)]


def _embed_dedicated_batch(texts: list[str], model_id: str, *, kind: str) -> list[tuple[float, ...]]:
    """Batched encode for a dedicated embedder (single forward pass)."""
    model, tokenizer = _load_model()
    prefixed = [_apply_prefix(t, model_id, kind=kind) for t in texts]
    inputs = tokenizer.batch_encode_plus(
        prefixed,
        return_tensors="mlx",
        padding=True,
        truncation=True,
        max_length=512,
    )
    outputs = model(inputs["input_ids"], attention_mask=inputs["attention_mask"])
    return [tuple(row.tolist()) for row in outputs.text_embeds]


def _causal_batch_fill(
    results: list[tuple[float, ...] | None],
    uncached_indices: list[int],
    uncached_texts: list[str],
) -> list[tuple[float, ...] | None]:
    """Fill *results* for the causal-LM fallback via a single batched forward."""
    import mlx.core as mx

    model, tokenizer = _load_model()
    token_lists = [tokenizer.encode(t, return_tensors=None) for t in uncached_texts]
    max_len = max((len(tl) for tl in token_lists), default=1)
    padded = [tl + [tokenizer.pad_token_id or 0] * (max_len - len(tl)) for tl in token_lists]
    input_ids = mx.array(padded)  # (batch, seq_len)
    hidden = model.model(input_ids)  # (batch, seq_len, hidden_dim)

    for j, orig_idx in enumerate(uncached_indices):
        tl_len = len(token_lists[j])
        if tl_len == 0:
            dim = model.model.embed_tokens.weight.shape[1]
            emb_tuple = tuple([0.0] * dim)
        else:
            emb = mx.mean(hidden[j, :tl_len, :], axis=0)
            norm = mx.sqrt(mx.sum(emb * emb))
            norm = mx.maximum(norm, mx.array(1e-12))
            emb = emb / norm
            emb_tuple = tuple(emb.tolist())
        results[orig_idx] = emb_tuple
    return results


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    if len(a) != len(b):
        raise ValueError(f"Dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-12 or norm_b < 1e-12:
        return 0.0
    return dot / (norm_a * norm_b)


def serialize_embedding(emb: list[float]) -> bytes:
    """Serialize embedding to bytes (float32 array) for SQLite BLOB storage."""
    return struct.pack(f"<{len(emb)}f", *emb)


def deserialize_embedding(blob: bytes) -> list[float]:
    """Deserialize embedding from SQLite BLOB back to float list."""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))
