"""Tests for score-weighted exemplar ordering."""

from app.generation.service import _confidence_label, _format_exemplars
from app.retrieval.service import RetrievalMatch


def _make_match(score: float, inbound: str = "test inbound", reply: str = "test reply") -> RetrievalMatch:
    return RetrievalMatch(
        result_type="reply_pair",
        score=score,
        lexical_score=score,
        metadata_score=0.0,
        source_type="email",
        source_id="test",
        account_email=None,
        title=None,
        author=None,
        external_uri=None,
        thread_id=None,
        created_at=None,
        updated_at=None,
        inbound_text=inbound,
        reply_text=reply,
    )


def test_exemplars_sorted_by_score_desc():
    pairs = [_make_match(3.0, "low", "low reply"), _make_match(8.0, "high", "high reply"), _make_match(5.0, "mid", "mid reply")]
    result = _format_exemplars(pairs)
    # EXAMPLE 1 should be highest score
    idx_high = result.index("high reply")
    idx_mid = result.index("mid reply")
    idx_low = result.index("low reply")
    assert idx_high < idx_mid < idx_low


def test_exemplars_drop_below_threshold():
    pairs = [_make_match(0.1, "too low", "bad reply"), _make_match(5.0, "good", "good reply")]
    result = _format_exemplars(pairs)
    assert "too low" not in result
    assert "good reply" in result


def test_exemplars_all_below_threshold():
    pairs = [_make_match(0.1), _make_match(0.05)]
    result = _format_exemplars(pairs)
    assert result == "(no exemplars found)"


def test_exemplars_max_cap():
    pairs = [_make_match(float(i), f"inbound {i}", f"reply {i}") for i in range(10, 0, -1)]
    result = _format_exemplars(pairs)
    assert result.count("[EXAMPLE") == 5


def test_confidence_labels():
    assert _confidence_label(0.8) == "high"
    assert _confidence_label(0.7) == "high"
    assert _confidence_label(0.5) == "medium"
    assert _confidence_label(0.4) == "medium"
    assert _confidence_label(0.3) == "low"
    assert _confidence_label(0.0) == "low"


def test_exemplars_have_confidence_annotation():
    pairs = [_make_match(8.0)]
    result = _format_exemplars(pairs)
    assert "confidence: high" in result
