"""Query shaping for FTS retrieval — synonym expansion + topic-keyword filter.

Long email inbounds drown BM25 in pleasantries ("Hi", "Thanks", "happy to",
"could you", "looking forward"). The retriever then surfaces high-frequency
intro/template emails instead of the topic-specific precedents the inbound is
actually about. ``extract_topic_keywords`` strips that template/stopword noise
so BM25 sees the words that carry the topic ("Q3 budget pricing enterprise
tier monthly volume") — keeping the original text for the semantic re-ranker.
"""

from __future__ import annotations

# Stopwords + email-template idioms that survive a naive split() and drown the
# topic signal in BM25 over a 100-200-word inbound. Conservative: only words
# that are clearly non-topic. Frozenset for O(1) lookup.
EMAIL_STOPWORDS: frozenset[str] = frozenset({
    # Articles / conjunctions / prepositions
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "of", "to",
    "in", "on", "at", "by", "for", "with", "about", "from", "as", "into",
    "during", "before", "after", "above", "below", "between", "among", "out",
    "off", "over", "under", "again", "further",
    # Pronouns and possessives
    "i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves", "he", "him", "his",
    "himself", "she", "her", "hers", "herself", "it", "its", "itself",
    "they", "them", "their", "theirs", "themselves",
    # Be/have/do/modal verbs
    "is", "am", "are", "was", "were", "be", "being", "been",
    "have", "has", "had", "having",
    "do", "does", "did", "doing", "done",
    "will", "would", "could", "should", "might", "may", "must", "can", "shall",
    # Demonstratives / quantifiers / common adverbs
    "this", "that", "these", "those", "such",
    "all", "any", "both", "each", "every", "few", "more", "most", "other",
    "some", "many", "much", "no", "not", "nor", "so", "than", "too", "yet",
    "very", "really", "quite", "just", "only", "even", "also", "however",
    "therefore", "thus", "hence", "still", "ever", "never", "always",
    "now", "here", "there", "when", "where", "why", "how", "who",
    "what", "which", "whom", "whose",
    # Greeting / closing / pleasantry — the email-specific noise.
    "hi", "hello", "hey", "dear", "thanks", "thank", "regards", "best",
    "cheers", "sincerely", "kind", "warmly",
    # Very common email idioms that don't carry topic meaning.
    "please", "let", "know", "looking", "forward", "feel", "free", "reach",
    "happy", "able", "available", "wanted", "wanting", "want", "wants",
    "going", "go", "goes", "gone", "got", "get", "gets", "getting",
    "make", "makes", "made", "making", "take", "takes", "took", "taking",
    "say", "said", "says", "saying", "tell", "told", "telling", "ask",
    "asked", "asking", "see", "sees", "saw", "seeing", "seen",
    # Casual fillers / interjections
    "ok", "okay", "yes", "yeah", "yep", "nope", "well", "sure", "alright",
    "oh", "ah", "hm", "umm", "hmm",
})


def _normalize(word: str) -> str:
    return word.lower().strip(".,!?;:\"'()[]<>{}—–-")


def _is_topic_term(word: str, *, extra_stopwords: frozenset[str] | set[str] = frozenset()) -> bool:
    """A token survives extraction iff it's at least 2 alphanumeric chars and
    not in the email-stopword set (or the caller's extra-stopword set, used
    to drop names like the user's own first name)."""
    w = _normalize(word)
    if len(w) < 2:
        return False
    if w in EMAIL_STOPWORDS or w in extra_stopwords:
        return False
    return any(c.isalnum() for c in w)


def extract_topic_keywords(
    text: str,
    *,
    min_words_to_filter: int = 25,
    extra_stopwords: frozenset[str] | set[str] | None = None,
) -> str:
    """Return the topic-bearing tokens of ``text``, joined by spaces.

    Short inputs (< ``min_words_to_filter`` words) pass through unchanged —
    they're already focused. Long inputs (typical inbound emails) have
    stopwords + email-template idioms stripped, so BM25 ranks against the
    words that actually carry the topic. ``extra_stopwords`` lets the caller
    drop additional terms (e.g. the user's own name — every inbound has it,
    so it's pure noise for retrieval). If the filter would leave the query
    empty (defensive: a filler-only inbound), the original text is returned
    instead.
    """
    if not text or not text.strip():
        return text
    words = text.split()
    if len(words) < min_words_to_filter:
        return text
    extra = frozenset(s.lower() for s in (extra_stopwords or set()))
    kept = [w for w in words if _is_topic_term(w, extra_stopwords=extra)]
    if not kept:
        return text
    return " ".join(kept)


SYNONYMS: dict[str, list[str]] = {
    "schedule": ["meeting", "call", "sync", "calendar", "appointment"],
    "postpone": ["reschedule", "delay", "move", "push back", "shift"],
    "urgent": ["asap", "immediately", "critical", "priority", "time-sensitive"],
    "proposal": ["offer", "quote", "pitch", "suggestion"],
    "confirm": ["approve", "sign off", "green light", "authorize"],
    "update": ["status", "progress", "news", "latest"],
    "issue": ["problem", "bug", "error", "concern"],
    "team": ["colleagues", "staff", "group", "members"],
}

# Build reverse lookup: word -> synonym group key
_WORD_TO_GROUP: dict[str, str] = {}
for _key, _syns in SYNONYMS.items():
    _WORD_TO_GROUP[_key] = _key
    for _syn in _syns:
        # Only map single words (skip multi-word synonyms for tokenization)
        if " " not in _syn:
            _WORD_TO_GROUP[_syn] = _key


def expand_query(text: str, max_expansions: int = 3) -> str:
    """Expand query with synonym groups.

    Finds words in text that have synonyms and appends up to max_expansions
    synonym groups as: 'text synonym1 synonym2'. The synonyms are appended bare
    (no 'also:' label) — the label tokenized into the FTS query and the word
    'also' polluted BM25 ranking.
    Caps total expansion at 50 extra chars.
    """
    if not text:
        return text

    words = text.lower().split()
    seen_groups: set[str] = set()
    expansion_parts: list[str] = []

    for word in words:
        clean = word.strip(",.!?;:")
        group_key = _WORD_TO_GROUP.get(clean)
        if group_key and group_key not in seen_groups:
            seen_groups.add(group_key)
            # Get single-word synonyms only, exclude the original word
            syns = [s for s in SYNONYMS[group_key] if " " not in s and s != clean]
            if syns:
                expansion_parts.append(" ".join(syns[:3]))
            if len(expansion_parts) >= max_expansions:
                break

    if not expansion_parts:
        return text

    expansion = " ".join(expansion_parts)
    if len(expansion) > 50:
        expansion = expansion[:50].rsplit(" ", 1)[0]

    return f"{text} {expansion}"
