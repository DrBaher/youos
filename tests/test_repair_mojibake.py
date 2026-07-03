"""repair_mojibake — double-encoded UTF-8 (cp1252/Latin-1) repair.

Ingested email bodies sometimes arrive double-encoded ("bestÃ¤tige" for
"bestätige"); ~6.5% of a live inbox, concentrated on German mail. The helper
must fix those while never corrupting clean text (real umlauts, curly quotes,
€, Arabic, emoji).
"""

from __future__ import annotations

from app.core.text_utils import repair_mojibake as fix


def test_repairs_latin1_umlauts():
    assert fix("bestÃ¤tige deine Probeabrechnung") == "bestätige deine Probeabrechnung"
    assert fix("fÃ¼r laufende GeschÃ¤ftsbetriebe") == "für laufende Geschäftsbetriebe"


def test_repairs_cp1252_sharp_s_and_grusse():
    # "ß" mojibake goes through cp1252 (0x9F), not Latin-1.
    assert fix("Viele GrÃ¼ÃŸe") == "Viele Grüße"
    assert fix("KÃ¶nigstetter StraÃŸe") == "Königstetter Straße"


def test_repairs_smart_punctuation():
    # cp1252 0x80-0x9F glyph family (curly quote, en dash).
    assert fix("â€™x") == "’x"          # right single quote
    assert fix("aâ€“b") == "a–b"        # en dash


def test_leaves_clean_text_untouched():
    for s in [
        "Danke schön, bis Freitag!",     # real umlauts
        "Straße Grüße schön",            # real ß
        "café naïve résumé",             # real accents
        "Hi Marcus — Thursday works.",   # real em dash
        "Arabic: مرحبا",  # non-latin1 → encode fails, kept
        "Price is 5€",              # lone euro, kept
        "curly ‘q’ — dash",
        "",
    ]:
        assert fix(s) == s


def test_no_replacement_char_introduced():
    # A partial/edge string must never gain U+FFFD.
    out = fix("Grüße und schöne GrÃ¼ÃŸe")
    assert "�" not in out
