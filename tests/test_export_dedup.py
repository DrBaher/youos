"""Tests for training export deduplication (Item 6)."""

from __future__ import annotations

from scripts.export_feedback_jsonl import deduplicate_pairs


def test_dedup_removes_near_duplicates():
    """Near-duplicate inbound texts should be deduplicated."""
    pairs = [
        ("2024-01-01", "Hello, can you help me with the project?", "Sure!", 4.0),
        ("2024-01-02", "Hello, can you help me with the project?", "Of course!", 3.0),
    ]
    result, removed = deduplicate_pairs(pairs, threshold=0.95)
    assert removed == 1
    assert len(result) == 1
    # Should keep the higher quality one
    assert result[0][3] == 4.0


def test_dedup_keeps_different_texts():
    """Different inbound texts should not be deduplicated."""
    pairs = [
        ("2024-01-01", "Can you review the budget proposal?", "Sure!", 4.0),
        ("2024-01-02", "What time is the team meeting tomorrow?", "At 3pm.", 3.0),
    ]
    result, removed = deduplicate_pairs(pairs, threshold=0.95)
    assert removed == 0
    assert len(result) == 2


def test_dedup_empty_list():
    """Empty list should return empty."""
    result, removed = deduplicate_pairs([], threshold=0.95)
    assert removed == 0
    assert result == []


def test_dedup_single_item():
    """Single item should return as-is."""
    pairs = [("2024-01-01", "Hello", "Hi", 4.0)]
    result, removed = deduplicate_pairs(pairs, threshold=0.95)
    assert removed == 0
    assert len(result) == 1


def test_dedup_keeps_higher_quality():
    """When tied on similarity, keep higher quality score."""
    pairs = [
        ("2024-01-01", "exact same text here", "reply1", 3.0),
        ("2024-01-02", "exact same text here", "reply2", 5.0),
    ]
    result, removed = deduplicate_pairs(pairs, threshold=0.95)
    assert removed == 1
    assert result[0][3] == 5.0


def test_dedup_keeps_first_when_quality_tied():
    """When quality is tied, keep the earlier one (more recent stays by position)."""
    pairs = [
        ("2024-01-01", "exact same text", "reply1", 4.0),
        ("2024-01-02", "exact same text", "reply2", 4.0),
    ]
    result, removed = deduplicate_pairs(pairs, threshold=0.95)
    assert removed == 1
    assert len(result) == 1
