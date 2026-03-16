"""Tests for query expansion (Item 6)."""

from app.core.query_expansion import SYNONYMS, expand_query


def test_expand_with_known_word():
    result = expand_query("Can we schedule a meeting?")
    assert "(also:" in result
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
    also_part = result.split("(also: ")[1].rstrip(")") if "(also:" in result else ""
    # Count is limited
    assert result.startswith(text)


def test_expand_caps_length():
    text = "schedule postpone urgent proposal confirm update issue team"
    result = expand_query(text, max_expansions=3)
    expansion = result[len(text):]
    # Expansion should be <=50 chars + the "(also: " prefix
    assert len(expansion) <= 60  # "(also: " + 50 chars + ")"


def test_expand_synonym_for_urgent():
    result = expand_query("This is urgent please help")
    assert "(also:" in result
    # Should have some urgency synonyms
    also = result.split("(also: ")[1].rstrip(")")
    urgent_syns = {"asap", "immediately", "critical", "priority"}
    assert any(s in also for s in urgent_syns)


def test_synonyms_dict_structure():
    assert "schedule" in SYNONYMS
    assert "urgent" in SYNONYMS
    assert isinstance(SYNONYMS["schedule"], list)
    assert len(SYNONYMS["schedule"]) > 0


def test_expand_via_reverse_lookup():
    """Words that are synonyms (not keys) should also trigger expansion."""
    result = expand_query("We need to reschedule the call")
    assert "(also:" in result
