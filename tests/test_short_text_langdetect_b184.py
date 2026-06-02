"""b184 — detect_language must recall SHORT non-English text.

b183 fixed language-mirroring (drafts reply in the inbound's language) but a
live 4-language battery exposed a detector weakness: short Spanish/French
classified as "en". Concretely the correct Spanish draft

    "Sí, puedo conectarme esta semana. Envíame el horario que te funciona."

matched only ``semana`` (1 < 2 marker words) and fell through to "en", which
then surfaced a CORRECT Spanish draft as a FALSE language mismatch in
verify.py and prevented _language_instruction() from naming the language.

Root cause: thin es/fr/it/pt marker lists + no character/diacritic signal, so
short strings sat below the ">= 2 marker words" threshold. The fix (detector
only) broadens the marker sets and adds high-precision character signals
(ñ/¿/¡ → es, ß → de, umlauts → de) that nudge a language above the threshold.

These tests pin the regression target, the FR/DE/EN short controls, an
English-loanword over-correction guard, and a no-regression check of every
b183 detect_language case.
"""

from __future__ import annotations

import pytest

from app.core.text_utils import detect_language, language_name

# The exact short Spanish draft that regressed to "en" in the live battery.
DEMO_SPANISH = "Sí, puedo conectarme esta semana. Envíame el horario que te funciona."


# --- the b184 regression -----------------------------------------------------

def test_short_spanish_regression_target():
    """THE bug: this exact string must classify as Spanish, not English."""
    assert detect_language(DEMO_SPANISH) == "es"


def test_short_french():
    assert (
        detect_language("Oui, j'ai vu la proposition. Pas un fit pour l'instant.")
        == "fr"
    )


def test_short_german():
    assert (
        detect_language("Hallo, ja gerne. Schick mir bitte die Uhrzeit für nächste Woche.")
        == "de"
    )


def test_short_english_control_not_misdetected():
    """A short English reply must stay English (no false positive)."""
    assert detect_language("Yes, I can connect this week. Send me a time that works.") == "en"


# --- diacritic / character signals -------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "¿Nos vemos el martes?",            # ¿ + ñ-free but inverted question mark
        "Mañana te confirmo el horario.",   # ñ
        "¡Perfecto! Te escribo el lunes.",  # ¡
    ],
)
def test_spanish_character_signals(text):
    assert detect_language(text) == "es"


def test_german_eszett_signal():
    # ß is near-unambiguous for German even in a very short string.
    assert detect_language("Alles klar, die Straße kenne ich gut.") == "de"


# --- over-correction guard ---------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Thanks, I grabbed a coffee at the café before the call.",
        "It was a rather naïve plan but it worked out fine.",
        "Please send over your résumé when you get a chance.",
    ],
)
def test_english_loanword_stays_english(text):
    """An incidental accented loanword (café/naïve/résumé) must NOT flip the
    classification — those carry an accented vowel but never ñ/¿/¡/ß, and a
    single accented token doesn't outscore the English default."""
    assert detect_language(text) == "en"


# --- language naming for the newly-detectable Romance languages --------------

def test_language_name_italian_portuguese():
    assert language_name("it") == "Italian"
    assert language_name("pt") == "Portuguese"


# --- no regression of the b183 detect_language cases -------------------------

def test_b183_demo_german_still_detected():
    assert (
        detect_language(
            "Hallo Baher, ich wollte einmal freundlichst nachhorchen, "
            "ob es schon Neuigkeiten gibt."
        )
        == "de"
    )


def test_b183_formal_german_still_detected():
    assert (
        detect_language(
            "Sehr geehrter Herr Müller,\n\n"
            "ich möchte Sie gerne zu einem Gespräch über die geplante "
            "Zusammenarbeit einladen. Wären Sie nächste Woche verfügbar?\n\n"
            "Mit freundlichen Grüßen,\nAnna Schmidt"
        )
        == "de"
    )


def test_b183_french_still_detected():
    assert (
        detect_language("Bonjour Baher, je voulais savoir si vous avez des nouvelles.")
        == "fr"
    )


def test_b183_english_not_misdetected():
    assert (
        detect_language("Hi Baher, just checking in to see if there is any news yet.")
        == "en"
    )
