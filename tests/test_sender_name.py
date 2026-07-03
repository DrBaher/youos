"""Tests for sender first name extraction (Item 2)."""

from app.core.sender import first_name_from_display_name


def test_simple_name():
    assert first_name_from_display_name("Sarah Mitchell") == "Sarah"


def test_single_name():
    assert first_name_from_display_name("Sarah") == "Sarah"


def test_dr_title():
    assert first_name_from_display_name("Dr. Baher") == "Baher"


def test_prof_title():
    assert first_name_from_display_name("Prof. Smith") == "Smith"


def test_mr_title():
    assert first_name_from_display_name("Mr. Jones") == "Jones"


def test_mrs_title():
    assert first_name_from_display_name("Mrs. Clark") == "Clark"


def test_ms_title():
    assert first_name_from_display_name("Ms. Lee") == "Lee"


def test_sir_title():
    assert first_name_from_display_name("Sir Richard") == "Richard"


def test_email_address():
    assert first_name_from_display_name("sarah.mitchell@company.com") == "Sarah"


def test_email_underscore():
    assert first_name_from_display_name("john_doe@example.com") == "John"


def test_email_hyphen():
    assert first_name_from_display_name("mary-jane@example.com") == "Mary"


def test_none():
    assert first_name_from_display_name(None) is None


def test_empty():
    assert first_name_from_display_name("") is None


def test_whitespace():
    assert first_name_from_display_name("   ") is None


def test_lowercase_capitalized():
    assert first_name_from_display_name("sarah") == "Sarah"


def test_single_char_is_rejected():
    # b290: a 1-char "name" is not a plausible greeting ("Hi A," is worse than
    # "Hi,") — the extractor returns None so the greeting drops the name.
    assert first_name_from_display_name("A") is None


def test_org_handles_and_acronyms_rejected():
    # b290: org handles / mailbox strings / acronyms must not become greetings.
    f = first_name_from_display_name
    assert f("BK-Firmenkunden (RLBNOE)") is None
    assert f("Coworking_Vienna") is None
    assert f('"R+P - Mag. Christoph Rumpler"') is None


def test_quotes_stripped_before_parse():
    # b290: surrounding quotes must not leak into the name ('Hannah"').
    assert first_name_from_display_name('"Täubl, Hannah"') == "Hannah"


def test_hyphenated_given_name_preserved():
    f = first_name_from_display_name
    assert f("Jean-Pierre Dubois") == "Jean-Pierre"
    assert f("Anne-Marie") == "Anne-Marie"


def test_first_name_handles_last_comma_first():
    """Outlook 'Last, First' → the given name is after the comma (live bug:
    'Feichtner, Franz' greeted 'Hi Feichtner'). 'First Last, Suffix' unaffected."""
    from app.core.sender import first_name_from_display_name as f
    assert f("Feichtner, Franz") == "Franz"
    assert f("Rickert, Dale") == "Dale"
    assert f("Franz Feichtner, PhD") == "Franz"   # suffix, not surname-first
    assert f("Sarah Mitchell") == "Sarah"          # no comma unaffected
