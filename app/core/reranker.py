"""Optional cross-encoder reranking for retrieval matches."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.retrieval.service import RetrievalMatch

logger = logging.getLogger(__name__)

_cross_encoder = None
_load_attempted = False


def _lazy_load_cross_encoder():
    """Lazy-load cross-encoder model. Returns None if sentence_transformers not installed."""
    global _cross_encoder, _load_attempted
    if _load_attempted:
        return _cross_encoder
    _load_attempted = True
    try:
        from sentence_transformers import CrossEncoder

        _cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        logger.info("Cross-encoder loaded: cross-encoder/ms-marco-MiniLM-L-6-v2")
    except ImportError:
        logger.warning("sentence_transformers not installed — cross-encoder reranking disabled")
    except Exception as exc:
        logger.warning("Failed to load cross-encoder: %s", exc)
    return _cross_encoder


def rerank(query: str, matches: list[RetrievalMatch], top_n: int) -> list[RetrievalMatch]:
    """Rerank matches using cross-encoder. Falls back to original order if unavailable."""
    encoder = _lazy_load_cross_encoder()
    if encoder is None:
        return matches

    if not matches:
        return matches

    # Build query-document pairs for scoring
    pairs = []
    for match in matches:
        text = match.snippet or match.content or match.inbound_text or ""
        pairs.append((query, text))

    try:
        scores = encoder.predict(pairs)
    except Exception as exc:
        logger.warning("Cross-encoder prediction failed: %s", exc)
        return matches

    # Attach cross-encoder scores and re-sort
    scored = list(zip(matches, scores, strict=False))
    scored.sort(key=lambda x: -x[1])

    reranked = [m for m, _ in scored[:top_n]]
    # Update scores to blend with original
    for _i, (match, ce_score) in enumerate(scored[:top_n]):
        match.score = round(match.score * 0.4 + float(ce_score) * 10.0 * 0.6, 4)

    return reranked


def is_reranker_available() -> bool:
    """Check if cross-encoder reranker is available."""
    return _lazy_load_cross_encoder() is not None
