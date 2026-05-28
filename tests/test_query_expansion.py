"""Tests for query expansion (Item 6)."""

from app.core.query_expansion import SYNONYMS, expand_query


def test_expand_with_known_word():
    result = expand_query("Can we schedule a meeting?")
    # Synonyms are appended bare (no "also:" label, which polluted FTS ranking).
    assert result.startswith("Can we schedule a meeting?")
    assert result != "Can we schedule a meeting?"
    assert "schedule" in result.lower() or "call" in result.lower() or "sync" in result.lower()


def test_expand_no_match():
    result = expand_query("Hello there, how are you?")
    assert result == "Hello there, how are you?"


def test_expand_empty():
    assert expand_query("") == ""


def test_expand_max_expansions():
    text = "schedule update issue"
    result = expand_query(text, max_expansions=2)
    # Should have at most 2 groups
    assert result.startswith(text)


def test_expand_no_also_label():
    # Regression: the "(also: ...)" label was tokenized into the FTS query so
    # the word "also" polluted BM25 ranking. It must not appear.
    result = expand_query("Can we schedule a meeting?")
    assert "also" not in result.lower()
    assert "(" not in result


def test_expand_caps_length():
    text = "schedule postpone urgent proposal confirm update issue team"
    result = expand_query(text, max_expansions=3)
    expansion = result[len(text):]
    # Expansion is a leading space + <=50 chars of synonyms.
    assert len(expansion) <= 52


def test_expand_synonym_for_urgent():
    result = expand_query("This is urgent please help")
    assert result.startswith("This is urgent please help")
    appended = result[len("This is urgent please help"):]
    urgent_syns = {"asap", "immediately", "critical", "priority"}
    assert any(s in appended for s in urgent_syns)


def test_synonyms_dict_structure():
    assert "schedule" in SYNONYMS
    assert "urgent" in SYNONYMS
    assert isinstance(SYNONYMS["schedule"], list)
    assert len(SYNONYMS["schedule"]) > 0


def test_expand_via_reverse_lookup():
    """Words that are synonyms (not keys) should also trigger expansion."""
    result = expand_query("We need to reschedule the call")
    assert result.startswith("We need to reschedule the call")
    assert result != "We need to reschedule the call"


# --- Topic-keyword extraction (QA-driven retrieval fix) --------------------
# Long email inbounds drown BM25 in pleasantries ("Hi", "Thanks", "happy to")
# and the retriever surfaces intro/template emails instead of topic-specific
# precedents. extract_topic_keywords strips that noise so BM25 sees what
# carries the topic.


def test_extract_topic_keywords_short_query_passes_through():
    """Short queries (< 25 words) are already focused — return unchanged."""
    from app.core.query_expansion import extract_topic_keywords

    q = "Q3 budget pricing enterprise tier"
    assert extract_topic_keywords(q) == q


def test_extract_topic_keywords_strips_pleasantries_from_long_inbound():
    """The Alex/Stripe inbound from the QA run: stopwords + email-template
    idioms drop, topic terms survive."""
    from app.core.query_expansion import extract_topic_keywords

    inbound = (
        "Hi Baher, We're planning Q3 budget and I wanted to check if you're still "
        "happy with current pricing, or whether we should look at moving you to the "
        "enterprise tier. Could you share a rough sense of monthly volume so I can "
        "run the numbers? Happy to jump on a 15-min call if easier. Thanks, Alex"
    )
    out = extract_topic_keywords(inbound).lower()
    # Topic terms survive
    for keep in ("q3", "budget", "pricing", "enterprise", "tier", "monthly", "volume"):
        assert keep in out, f"topic term {keep!r} was stripped"
    # Greeting / template noise is gone
    for drop in ("hi", "thanks", "happy", "would", "could"):
        assert f" {drop} " not in f" {out} ", f"stopword {drop!r} survived: {out!r}"


def test_extract_topic_keywords_never_returns_empty():
    """Defensive: a filler-only long input must NOT collapse to empty (which
    would make FTS return nothing). Fall back to the original text."""
    from app.core.query_expansion import extract_topic_keywords

    filler = " ".join(["thanks", "hi", "you", "and", "the", "is"] * 10)  # > 25 words, all stopwords
    out = extract_topic_keywords(filler)
    assert out  # not empty
    assert out == filler


def test_extract_topic_keywords_empty_input():
    from app.core.query_expansion import extract_topic_keywords

    assert extract_topic_keywords("") == ""
    assert extract_topic_keywords("   ") == "   "


def test_extract_topic_keywords_drops_extra_stopwords():
    """Caller can pass extra stopwords (e.g. the user's own first name) so
    every-inbound name noise doesn't dominate BM25 ranking."""
    from app.core.query_expansion import extract_topic_keywords

    inbound = (
        "Hi Baher, We're planning Q3 budget and I wanted to check if you're still "
        "happy with current pricing, or whether we should look at moving you to the "
        "enterprise tier. Could you share a rough sense of monthly volume?"
    )
    out = extract_topic_keywords(inbound, extra_stopwords={"baher"})
    assert "baher" not in out.lower()
    # Topic terms still survive.
    assert "Q3" in out and "budget" in out and "pricing" in out
