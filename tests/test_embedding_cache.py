"""Tests for LRU cache on get_embedding (Item 11)."""

from __future__ import annotations

from app.core.embeddings import clear_embedding_cache, get_embedding_cache_info


def test_cache_info_returns_dict():
    """get_embedding_cache_info returns a dict with hits/misses/size."""
    clear_embedding_cache()
    info = get_embedding_cache_info()
    assert "hits" in info
    assert "misses" in info
    assert "size" in info
    assert isinstance(info["hits"], int)
    assert isinstance(info["misses"], int)
    assert isinstance(info["size"], int)


def test_clear_cache():
    """clear_embedding_cache resets cache stats."""
    clear_embedding_cache()
    info = get_embedding_cache_info()
    assert info["size"] == 0
    assert info["hits"] == 0
    assert info["misses"] == 0


def test_cache_has_maxsize():
    """The embedding LRU cache has a maxsize of 512.

    The cache moved onto the internal ``_get_embedding_cached(text, kind)`` in
    b180 (kind is part of the key for E5 query/passage prefixes); ``get_embedding``
    is now a thin wrapper around it.
    """
    from app.core.embeddings import _get_embedding_cached

    assert _get_embedding_cached.cache_info().maxsize == 512
