"""Simple text similarity utilities for auto-feedback capture."""

from __future__ import annotations

from difflib import SequenceMatcher


def similarity_ratio(a: str, b: str) -> float:
    """Returns 0.0 (completely different) to 1.0 (identical)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_meaningfully_different(draft: str, actual: str, threshold: float = 0.80) -> bool:
    """True if draft and actual differ enough to be a useful training pair."""
    return similarity_ratio(draft, actual) < threshold
