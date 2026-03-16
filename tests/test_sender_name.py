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


def test_single_char():
    assert first_name_from_display_name("A") == "A"
