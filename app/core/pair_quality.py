"""Reply-pair quality: detect "replies" that aren't replies (b235).

The 2026-06-11 inbox-replay backtest found ~18% of sampled reply pairs had a
"reply" that was just the user's signature block, or a bare forward marker
("FYI." + signature). The thread extractor pairs every self-authored message
with the inbounds before it, so a forward or an empty send becomes a training
pair whose lesson is "a good reply is a signature" — poisoning fine-tuning,
retrieval exemplars, and any ground-truth evaluation at once.

`signature_only_reply` is the single detector all three layers share:

* ingestion (skip the pair at extraction time),
* the corpus cleanup script (demote existing rows to ``quality_score = 0``),
* eval sampling (exclude from backtest ground truth).

Conservative by design: a terse-but-real reply ("Signed papers." + signature)
is NOT junk — only pure courtesy/forward tokens with no content are.
"""

from __future__ import annotations

import re

# Courtesy/forward tokens that carry no drafting signal on their own.
_COURTESY_ONLY = re.compile(
    r"^\s*(fyi|fyi\.|noted|thanks|thank you|thx|ok|okay|done|\+1|please see below|see below)\s*[.!]*\s*$",
    re.IGNORECASE,
)

# Signature context: a name occurrence counts as the signature start only when
# followed closely by title/contact furniture — so a body sentence that merely
# mentions the user's name can't truncate the reply.
_SIG_CONTEXT = re.compile(
    r"(ceo|cto|coo|cfo|founder|geschäftsführer|managing director|w:|e:|m:|t:|tel|phone|mobile|linkedin|www\.|https?://|@)",
    re.IGNORECASE,
)

_MIN_CONTENT_CHARS = 8


def _display_names(*authors: str | None) -> list[str]:
    """Plausible signature seeds from author strings ("Baher Al Hakim <b@x>")."""
    names: list[str] = []
    for a in authors:
        if not a:
            continue
        display = a.split("<", 1)[0].strip().strip('"')
        if display and "@" not in display and len(display) >= 4:
            names.append(display)
    return names


def reply_content(reply_text: str | None, *, author_names: list[str] | None = None) -> str:
    """The reply minus its trailing signature block (best effort).

    Cuts at the first occurrence of an author display name that is followed
    within ~80 chars by signature furniture (title / contact links). Falls back
    to the full text when no confident signature start is found.
    """
    text = (reply_text or "").strip()
    if not text:
        return ""
    for name in author_names or []:
        idx = text.lower().find(name.lower())
        if idx < 0:
            continue
        tail = text[idx + len(name): idx + len(name) + 80]
        if _SIG_CONTEXT.search(tail):
            return text[:idx].strip()
    return text


def signature_only_reply(
    reply_text: str | None,
    *,
    reply_author: str | None = None,
    user_names: list[str] | None = None,
) -> bool:
    """True when the stored reply is signature/forward furniture, not a reply."""
    names = _display_names(reply_author) + list(user_names or [])
    content = reply_content(reply_text, author_names=names)
    if len(content) < _MIN_CONTENT_CHARS:
        return True
    return bool(_COURTESY_ONLY.match(content))
