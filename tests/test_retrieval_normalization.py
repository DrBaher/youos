"""Lexical-score normalization so metadata/semantic can actually reorder."""

from __future__ import annotations

from app.retrieval.service import RetrievalMatch, _normalize_pool


def _m(*, lexical, metadata, source_id):
    # score = lexical + metadata (the production combine, no quality/subject mult here).
    return RetrievalMatch(
        result_type="reply_pair", score=round(lexical + metadata, 4),
        lexical_score=lexical, metadata_score=metadata,
        source_type="reply_pair", source_id=source_id, account_email=None,
        title=None, author=None, external_uri=None, thread_id=None,
        created_at=None, updated_at=None,
    )


def test_normalization_lets_metadata_outrank_raw_lexical():
    # Raw: A(12) > B(10.5) > C(2). But B carries a big metadata boost.
    a = _m(lexical=12.0, metadata=0.0, source_id="A")
    b = _m(lexical=10.0, metadata=0.5, source_id="B")
    c = _m(lexical=2.0, metadata=0.0, source_id="C")
    matches = [a, b, c]

    _normalize_pool(matches)
    # lex_norm: A=1.0, B=0.8, C=0.0 → combined A=1.0, B=1.3, C=0.0.
    by_id = {m.source_id: m.score for m in matches}
    assert by_id["B"] > by_id["A"] > by_id["C"], by_id
    # On the raw scale the boost (0.5) could never overcome a 2.0 lexical gap;
    # normalized, it reorders the top result.


def test_normalization_preserves_quality_subject_multiplier():
    # score carries a 2x quality/subject multiplier over (lexical+metadata).
    m = _m(lexical=8.0, metadata=0.0, source_id="X")
    m.score = round((8.0 + 0.0) * 2.0, 4)  # mult = 2.0
    other = _m(lexical=4.0, metadata=0.0, source_id="Y")
    _normalize_pool([m, other])
    # lex_norm(X)=1.0; combined = (1.0 + 0.0) * 2.0 = 2.0 (multiplier kept).
    assert m.score == 2.0


def test_normalization_noop_for_single_match():
    m = _m(lexical=5.0, metadata=0.1, source_id="Z")
    before = m.score
    _normalize_pool([m])
    assert m.score == before
