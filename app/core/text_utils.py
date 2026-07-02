"""Utilities for cleaning email text."""

from __future__ import annotations

import re

# Patterns that indicate start of quoted reply history
_QUOTE_BOUNDARY_PATTERNS = [
    # "On [date], [name] wrote:" (Gmail/Outlook)
    re.compile(r"^On .{10,80} wrote:\s*$", re.MULTILINE),
    # "From: ..." followed by "Sent: ..." (Outlook style)
    re.compile(r"^From:\s+.+\nSent:\s+", re.MULTILINE),
    # Lines starting with "> " (traditional quote markers) — 3+ consecutive
    re.compile(r"(?:^> .+\n){3,}", re.MULTILINE),
    # "---------- Forwarded message ----------"
    re.compile(r"^-{5,}\s*Forwarded message\s*-{5,}", re.MULTILINE),
    # Outlook separator line
    re.compile(r"^_{20,}\s*$", re.MULTILINE),
    # "-----Original Message-----"
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.MULTILINE | re.IGNORECASE),
]


def decode_html_entities(text: str) -> str:
    """Decode HTML entities like &amp; &lt; &gt; &quot; in email text."""
    import html

    return html.unescape(text)


# Double-encoded UTF-8 ("mojibake"): UTF-8 bytes decoded as cp1252/Latin-1 and
# re-served, so "bestätige" arrives as "bestÃ¤tige" and umlauts
# mangle similarly. A live inbox audit found ~6.5% of inbound bodies affected —
# concentrated on German senders, the exact mail where voice-match matters most.
# The fix is to re-encode with the misapplied codec and decode as UTF-8; mail
# systems mostly use cp1252 (its 0x80-0x9F glyphs — smart quotes, dashes, the
# sharp-s continuation — are why "ß" and smart-quote mojibake differ from
# Latin-1), so cp1252 is tried first, Latin-1 second.
#
# The diagnostic signature is a UTF-8 lead byte (U+00C2-U+00F4) followed by
# either a continuation-range char (U+0080-U+00BF, the Latin-1 family) or one of
# the cp1252 0x80-0x9F glyphs (the smart-quote / sharp-s family). This virtually
# never occurs in well-formed text, so it gates the repair tightly.
_CP1252_HIGH = '€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ'
_MOJIBAKE_HINT_RE = re.compile("[\u00c2-\u00f4][\u0080-\u00bf" + _CP1252_HIGH + "]")


def repair_mojibake(text: str) -> str:
    """Repair double-encoded UTF-8 in ``text``; return it unchanged if it isn't
    unambiguously mojibake.

    Guards against corrupting valid text:
    * only strings carrying the mojibake signature are touched;
    * ``encode(codec)`` raises on any genuine char the codec can't represent
      (real Arabic, emoji, a lone euro sign in otherwise-clean text) — left
      as-is;
    * a candidate is accepted only if it introduces no U+FFFD and strictly
      reduces the mojibake signature count.
    """
    if not text or not _MOJIBAKE_HINT_RE.search(text):
        return text
    before = len(_MOJIBAKE_HINT_RE.findall(text))
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if "\ufffd" in fixed:
            continue
        if len(_MOJIBAKE_HINT_RE.findall(fixed)) < before:
            return fixed
    return text


def strip_quoted_text(text: str) -> str:
    """Remove quoted reply history from email body, keeping only the new content.

    Truncates at the first detected quote boundary.
    If the result is too short (< 50 chars), returns the original text.
    """
    if not text:
        return text

    earliest_pos = len(text)
    for pattern in _QUOTE_BOUNDARY_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()

    if earliest_pos < len(text):
        stripped = text[:earliest_pos].rstrip()
        if len(stripped) >= 50:
            return stripped

    return text


# Conservative sign-off / signature start markers. A line that *is* one of
# these (optionally with a trailing name on the same/next lines) marks the end
# of the message's substantive content.
_SIGNATURE_START = re.compile(
    r"^\s*(?:--\s*$|"
    r"(?:best|best regards|regards|kind regards|warm regards|cheers|thanks|"
    r"thank you|sincerely|yours|talk soon|sent from my )\b.{0,40}$)",
    re.IGNORECASE,
)


# Section markers the draft prompt uses as structure. Attacker email text that
# starts a line with one of these would otherwise inject a competing instruction
# block, so forged ones are defanged before untrusted text is embedded.
_PROMPT_SECTION_MARKER = re.compile(
    r"(?im)^([ \t]*)\[(?=(?:SYSTEM|TASK|EXEMPLARS|INBOUND MESSAGE|GROUNDING|"
    r"STYLE ANCHOR|LANGUAGE|SENDER|PRIOR REPLY)\b)"
)


def neutralize_prompt_markers(text: str) -> str:
    """Defang attacker-forged prompt section markers (``[TASK]``, ``[SYSTEM]``…)
    at the start of a line in untrusted text, so an inbound message can't inject
    a competing instruction block. Inserts a space after the bracket so the line
    is no longer a structural marker but stays readable. No-op for normal text."""
    if not text or "[" not in text:
        return text
    return _PROMPT_SECTION_MARKER.sub(r"\1[ ", text)


def extract_new_content(text: str) -> str:
    """Return only the new (non-quoted) content of an email body.

    Like :func:`strip_quoted_text` but WITHOUT the 50-char fallback: a trivial
    reply on a long thread ("thanks", "will do") returns just that short new
    content, not the whole quoted history. This matters for needs-reply
    scoring, where the quoted block almost always contains a stray '?' or
    imperative that would otherwise inflate a no-op acknowledgement to a
    drafted reply. Falls back to the full text only when no quote boundary is
    found at all.
    """
    if not text:
        return text
    earliest_pos = len(text)
    for pattern in _QUOTE_BOUNDARY_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()
    if earliest_pos < len(text):
        return text[:earliest_pos].rstrip()
    return text


_PHONE_LABEL_RE = re.compile(
    r"^\s*(?:phone|mobile|cell|tel|telephone|telefon|fon|mob|m|t)\s*[:.\-—]?\s*",
    re.IGNORECASE,
)
_PHONE_NUMBER_RE = re.compile(r"^\+?\d[\d\s().\-/]{5,}\d$")


def strip_phone_lines(text: str) -> str:
    """Remove lines that are essentially just a phone number (optionally with a
    ``Phone:``/``Mobile:``/``m:`` label).

    A generated reply body should never carry a phone number: the model either
    hallucinated one (live bug: a draft to Jürgen invented a wrong mobile) or
    duplicated the user's — and the real, correct number is appended via the
    Gmail signature at push time. Conservative: only drops a line whose entire
    content (after an optional label) is a phone-shaped token with ≥7 digits, so
    it can't eat a sentence that merely mentions a number."""
    if not text:
        return text
    out: list[str] = []
    for line in text.splitlines():
        candidate = _PHONE_LABEL_RE.sub("", line).strip()
        digits = sum(c.isdigit() for c in candidate)
        if candidate and digits >= 7 and _PHONE_NUMBER_RE.match(candidate):
            continue  # drop the phone line
        out.append(line)
    return "\n".join(out).strip()


def strip_signature(text: str) -> str:
    """Best-effort removal of a trailing signature/sign-off block.

    Scans the last few lines for a sign-off marker (``--``, ``Best,``,
    ``Regards``, ``Sent from my …``) and cuts from there. Conservative: only
    fires in the tail of the message and keeps the original if cutting would
    leave nothing, so it can't eat the substance of a short reply.
    """
    if not text:
        return text
    lines = text.splitlines()
    # Only look in the last 8 lines so a "Best regards," that opens a sentence
    # mid-body isn't mistaken for the sign-off.
    start = max(0, len(lines) - 8)
    for i in range(start, len(lines)):
        if _SIGNATURE_START.match(lines[i]):
            head = "\n".join(lines[:i]).rstrip()
            return head if head else text
    return text


def detect_language(text: str) -> str:
    """Detect language of text. Returns ISO 639-1 code (e.g. 'en', 'de', 'ar').

    Simple heuristic based on character scripts and common words.
    """
    if not text:
        return "en"

    # Check for Arabic script (Unicode range \u0600-\u06FF)
    arabic_chars = sum(1 for c in text if "\u0600" <= c <= "\u06ff")
    if arabic_chars > len(text) * 0.1:
        return "ar"

    # Check for common German words. (b183) The original short list missed
    # everyday business openers like "Hallo … ich wollte einmal … nachhorchen,
    # ob es schon Neuigkeiten gibt" — only ``ich`` matched, so a German inbound
    # scored 1 < 2 and fell through to "en", which then DROPPED the
    # reply-in-the-sender's-language instruction (the live German→English draft
    # regression). The list is broadened with high-frequency function words and
    # greetings/closers so a normal short German email clears the threshold,
    # while keeping discriminative tokens (umlauts, ß, "geehrte") that don't
    # collide with English.
    lower = text.lower()
    german_words = [
        "der", "die", "das", "und", "ist", "nicht", "sie", "ich", "ein",
        "eine", "wir", "sehr", "geehrter", "geehrte", "mit", "freundlichen",
        "grüßen", "bitte", "können", "hallo", "wollte", "einmal", "ob", "es",
        "schon", "gibt", "haben", "wäre", "wären", "würde", "möchte", "möchten",
        "danke", "vielen", "dank", "gerne", "nächste", "woche", "nachhorchen",
        "neuigkeiten", "auch", "noch", "über", "für", "war", "wird", "werden",
        "guten", "tag", "liebe", "lieber", "viele", "herzliche", "ihnen",
    ]
    german_hits = sum(1 for w in german_words if re.search(r"\b" + re.escape(w) + r"\b", lower))

    # Check for common French words. (b184) Broadened with high-frequency
    # function words / pronouns / closers so a SHORT informal French reply
    # ("Oui, j'ai vu la proposition…") clears the threshold instead of falling
    # through to "en". Tokens shared with es/it (la, le, un, une, que) stay in
    # the list but are non-discriminative by themselves — the diacritic signal
    # below breaks ties toward the right Romance language.
    french_words = [
        "vous", "nous", "est", "les", "une", "pour", "dans", "avec", "sur",
        "que", "qui", "sont", "cette", "mais", "bonjour", "merci", "monsieur",
        "madame", "je", "votre", "vos", "nos", "très", "bien", "était",
        "serait", "voulais", "savoir", "nouvelles", "prochaine", "semaine",
        "cordialement", "salutations", "oui", "non", "ai", "vu", "la", "le",
        "pas", "un", "instant", "moi", "toi", "fait", "suis", "alors", "donc",
        "voici", "voilà", "bonsoir", "ceci", "cela", "tout", "rien", "proposition",
    ]
    french_hits = sum(1 for w in french_words if re.search(r"\b" + re.escape(w) + r"\b", lower))

    # Check for common Spanish words. (b184) The original list was too thin and
    # carried no character signal: the short live-demo Spanish draft
    # "Sí, puedo conectarme esta semana. Envíame el horario que te funciona."
    # matched only ``semana`` (1 < 2) and fell through to "en", which then
    # surfaced a CORRECT Spanish draft as a false language mismatch in verify.
    # Broadened with everyday function words / pronouns / openers/closers.
    spanish_words = [
        "usted", "nosotros", "para", "como", "pero", "hola", "gracias",
        "señor", "señora", "estimado", "estimada", "por", "favor", "también",
        "quería", "saber", "próxima", "semana", "saludos", "muchas", "buenos",
        "días", "atentamente", "sí", "esta", "este", "esto", "el", "la",
        "los", "las", "te", "envíame", "puedo", "saludo", "cordial",
        "una", "con", "funciona", "horario", "conectarme", "soy", "estoy",
        "tengo", "quiero", "claro", "vale", "ahora", "mañana",
    ]
    spanish_hits = sum(1 for w in spanish_words if re.search(r"\b" + re.escape(w) + r"\b", lower))

    # Check for common Italian words. (b184)
    italian_words = [
        "sono", "anche", "perché", "grazie", "ciao", "buongiorno", "prego",
        "settimana", "prossima", "cordiali", "saluti", "gentile", "egregio",
        "vorrei", "sapere", "novità", "questo", "questa",
        "molto", "posso", "devo",
    ]
    italian_hits = sum(1 for w in italian_words if re.search(r"\b" + re.escape(w) + r"\b", lower))

    # Check for common Portuguese words. (b184)
    portuguese_words = [
        "você", "obrigado", "obrigada", "olá", "bom", "dia", "próxima",
        "atenciosamente", "cumprimentos", "gostaria", "novidades",
        "não", "uma", "muito", "está", "estou", "sim", "então",
    ]
    portuguese_hits = sum(
        1 for w in portuguese_words if re.search(r"\b" + re.escape(w) + r"\b", lower)
    )

    scores = {
        "de": german_hits,
        "fr": french_hits,
        "es": spanish_hits,
        "it": italian_hits,
        "pt": portuguese_hits,
    }

    # (b184) High-precision character signals. Certain glyphs/diacritics are
    # near-unambiguous for a single language and are very rare in English even
    # in loanwords ("café", "naïve", "résumé" carry an accented vowel but never
    # ñ/¿/¡/ß). When present, nudge that language's score so a SHORT string with
    # a strong marker classifies correctly even below the word-count threshold,
    # while incidental accented English loanwords get no boost and stay "en".
    if "ñ" in lower or "¿" in text or "¡" in text:
        scores["es"] += 2
    if "ß" in lower:
        scores["de"] += 2
    # German-only umlauts on top of at least one function-word hit.
    if german_hits and any(ch in lower for ch in ("ä", "ö", "ü")):
        scores["de"] += 1

    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best

    return "en"


# ISO 639-1 → human-readable language name for prompt instructions (b183).
# Used to render an explicit "Reply in German." directive when detection is
# confident. Unknown codes fall back to the generic mirror instruction.
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",  # (b184) detector can now classify Italian
    "pt": "Portuguese",  # (b184) detector can now classify Portuguese
    "ar": "Arabic",
}


def language_name(code: str | None) -> str | None:
    """Return the English language name for an ISO 639-1 code, or None."""
    if not code:
        return None
    return _LANGUAGE_NAMES.get(code.strip().lower())
