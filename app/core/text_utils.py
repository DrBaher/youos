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
