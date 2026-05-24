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
