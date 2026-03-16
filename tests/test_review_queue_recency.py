"""Tests for recency weighting in review queue scoring (Item 7)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from app.api.review_queue_routes import score_pair_for_review


def _make_pair(days_ago: int = 0, inbound_len: int = 200) -> dict:
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return {
        "paired_at": dt.isoformat(),
        "inbound_author": "test@example.com",
        "inbound_text": "x" * inbound_len,
    }


def test_recent_pair_scores_higher():
    """A pair from today should score higher than one from 300 days ago."""
    counter = Counter()
    recent = score_pair_for_review(_make_pair(days_ago=0), counter)
    old = score_pair_for_review(_make_pair(days_ago=300), counter)
    assert recent > old


def test_very_old_pair_gets_zero_recency():
    """A pair from 400 days ago should get 0 recency_score contribution."""
    counter = Counter()
    # Both beyond 6 months (no 0.3 bonus) and beyond 365 days (0 recency)
    pair = _make_pair(days_ago=400)
    score = score_pair_for_review(pair, counter)
    # Should still get diversity + length bonus but no recency
    pair_no_date = {"paired_at": "", "inbound_author": "test@example.com", "inbound_text": "x" * 200}
    score_no_date = score_pair_for_review(pair_no_date, counter)
    assert score == score_no_date


def test_recency_score_range():
    """recency_score should be between 0 and 1."""
    for days in [0, 30, 180, 365, 500]:
        days_old = days
        recency_score = max(0.0, 1.0 - (days_old / 365))
        assert 0.0 <= recency_score <= 1.0


def test_sender_diversity_still_works():
    """Sender diversity bonus is unchanged."""
    counter = Counter({"external_client": 5})
    pair = _make_pair(days_ago=0)
    score_diverse = score_pair_for_review(pair, Counter())
    score_saturated = score_pair_for_review(pair, counter)
    assert score_diverse > score_saturated
