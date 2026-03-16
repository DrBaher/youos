"""Tests for WhatsApp export ingestion."""
from app.ingestion.whatsapp import (
    ParsedMessage,
    build_reply_pairs,
    parse_whatsapp_export,
)

FIXTURE = """\
12/31/25, 9:41 PM - Alice: Hey, are you free tomorrow?
12/31/25, 9:42 PM - Bob: Yeah, what's up?
12/31/25, 9:43 PM - Alice: Want to grab coffee?
12/31/25, 9:45 PM - Bob: Sure, sounds good!
12/31/25, 9:50 PM - Alice: Great, see you at 10am
"""

MULTILINE_FIXTURE = """\
1/5/26, 3:00 PM - Alice: Here is a long message
that continues on the next line
and even a third line
1/5/26, 3:02 PM - Bob: Got it, thanks!
"""


def test_parse_basic():
    messages = parse_whatsapp_export(FIXTURE)
    assert len(messages) == 5
    assert messages[0].sender == "Alice"
    assert messages[0].text == "Hey, are you free tomorrow?"
    assert messages[0].timestamp == "12/31/25, 9:41 PM"
    assert messages[1].sender == "Bob"


def test_parse_multiline():
    messages = parse_whatsapp_export(MULTILINE_FIXTURE)
    assert len(messages) == 2
    assert "continues on the next line" in messages[0].text
    assert "third line" in messages[0].text
    assert messages[1].text == "Got it, thanks!"


def test_parse_empty():
    assert parse_whatsapp_export("") == []
    assert parse_whatsapp_export("\n\n\n") == []


def test_build_reply_pairs_basic():
    messages = parse_whatsapp_export(FIXTURE)
    # Bob is the user
    pairs = build_reply_pairs(messages, ("Bob",))
    assert len(pairs) == 2
    # First pair: Alice asks, Bob replies
    assert pairs[0][0].sender == "Alice"
    assert pairs[0][1].sender == "Bob"
    assert pairs[0][0].text == "Hey, are you free tomorrow?"
    assert pairs[0][1].text == "Yeah, what's up?"


def test_build_reply_pairs_case_insensitive():
    messages = parse_whatsapp_export(FIXTURE)
    pairs = build_reply_pairs(messages, ("bob",))
    assert len(pairs) == 2


def test_build_reply_pairs_no_user_match():
    messages = parse_whatsapp_export(FIXTURE)
    pairs = build_reply_pairs(messages, ("Charlie",))
    assert len(pairs) == 0


def test_build_reply_pairs_skip_consecutive_user():
    """User messages back-to-back should not form pairs."""
    text = """\
1/1/26, 10:00 AM - Bob: Hello
1/1/26, 10:01 AM - Bob: Anyone there?
1/1/26, 10:02 AM - Alice: Hi!
1/1/26, 10:03 AM - Bob: Hey Alice
"""
    messages = parse_whatsapp_export(text)
    pairs = build_reply_pairs(messages, ("Bob",))
    # Only Alice->Bob pair
    assert len(pairs) == 1
    assert pairs[0][0].sender == "Alice"
    assert pairs[0][1].sender == "Bob"
