from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_account_for_sender, get_base_model, get_model_fallback, get_user_name, get_user_names
from app.core.sender import classify_sender, extract_domain, first_name_from_display_name
from app.core.text_utils import strip_quoted_text
from app.db.bootstrap import resolve_sqlite_path
from app.retrieval.service import (
    RetrievalMatch,
    RetrievalRequest,
    RetrievalResponse,
    retrieve_context,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DraftRequest:
    inbound_message: str
    mode: str | None = None
    audience_hint: str | None = None
    top_k_reply_pairs: int = 5
    top_k_chunks: int = 3
    account_email: str | None = None
    use_local_model: bool = True
    tone_hint: str | None = None
    sender: str | None = None
    intent_hint: str | None = None
    thread_id: str | None = None
    use_adapter: bool = True


@dataclass(slots=True)
class DraftResponse:
    draft: str
    detected_mode: str
    precedent_used: list[dict[str, Any]]
    retrieval_method: str
    confidence: str
    confidence_reason: str
    model_used: str
    sender_profile: dict[str, Any] | None = None
    suggested_subject: str | None = None
    token_estimate: int | None = None
    empty_output_retried: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_prompts(configs_dir: Path) -> dict[str, str]:
    path = configs_dir / "prompts.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_persona(configs_dir: Path) -> dict[str, Any]:
    path = configs_dir / "persona.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_signature_patterns() -> list[re.Pattern[str]]:
    """Build signature-stripping patterns dynamically from config user names."""
    patterns: list[re.Pattern[str]] = []
    for name in get_user_names():
        if name.strip():
            patterns.append(re.compile(rf"^{re.escape(name)}", re.MULTILINE))
    # Standard signature delimiters
    patterns.extend(
        [
            re.compile(r"^-- $", re.MULTILINE),
            re.compile(r"^--$", re.MULTILINE),
            re.compile(r"^Best,\s*$", re.MULTILINE),
            re.compile(r"^Cheers,\s*$", re.MULTILINE),
            re.compile(r"^Regards,\s*$", re.MULTILINE),
            re.compile(r"^Kind regards,\s*$", re.MULTILINE),
            re.compile(r"^Thanks,\s*$", re.MULTILINE),
            re.compile(r"^Sent from my iPhone", re.MULTILINE),
        ]
    )
    return patterns


# Lazily built on first use so config is loaded at call time, not import time.
_signature_patterns: list[re.Pattern[str]] | None = None


def _get_signature_patterns() -> list[re.Pattern[str]]:
    global _signature_patterns
    if _signature_patterns is None:
        _signature_patterns = _build_signature_patterns()
    return _signature_patterns


def strip_signature(text: str) -> str:
    """Strip signature from reply text for use as exemplar."""
    earliest_pos = len(text)
    found = False
    for pattern in _get_signature_patterns():
        match = pattern.search(text)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()
            found = True
    if found:
        return text[:earliest_pos].rstrip()
    return text


def _score_confidence(
    reply_pairs: list[RetrievalMatch],
    score_stats: dict[str, float] | None = None,
) -> tuple[str, str]:
    if not reply_pairs:
        return "low", "no strong matches in retrieved precedent"

    top_score = max(rp.score for rp in reply_pairs)

    # Use relative thresholds when score stats are available
    if score_stats and score_stats.get("mean") is not None and score_stats.get("stddev") is not None:
        mean = score_stats["mean"]
        stddev = score_stats["stddev"]
        if top_score > mean + stddev:
            return "high", f"top score {top_score:.1f} exceeds mean+1σ ({mean:.1f}+{stddev:.1f})"
        if top_score > mean:
            return "medium", f"top score {top_score:.1f} above mean ({mean:.1f})"
        return "low", f"top score {top_score:.1f} below mean ({mean:.1f})"

    # Fallback to absolute thresholds (empty corpus or no stats)
    high_count = sum(1 for rp in reply_pairs if rp.score > 8.0)
    medium_count = sum(1 for rp in reply_pairs if rp.score > 6.0)
    if high_count >= 3:
        return "high", f"{high_count} strong reply pair matches found"
    if medium_count >= 1:
        return "medium", f"{medium_count} moderate reply pair matches found"
    return "low", "no strong matches in retrieved precedent"


def _has_thread_context(text: str) -> bool:
    """Return True if inbound_text contains multiple 'From:' blocks (multi-message thread)."""
    return text.count("From:") >= 2


def _extract_thread_parts(text: str) -> tuple[str, list[dict[str, str]]]:
    """Extract the most recent message and thread history from a multi-message thread.

    Returns (active_inbound, history) where history is a list of
    {"sender": ..., "text": ...} dicts for prior exchanges.
    """
    # Split on "From:" blocks
    parts = re.split(r"(?=^From:\s)", text, flags=re.MULTILINE)
    if len(parts) < 2:
        return text, []

    active_inbound = parts[0].strip()
    if not active_inbound and len(parts) > 1:
        active_inbound = parts[1].strip()
        parts = parts[2:]
    else:
        parts = parts[1:]

    history: list[dict[str, str]] = []
    for part in parts[:4]:  # Last 4 messages max, will take 2 exchanges
        lines = part.strip().split("\n", 1)
        sender_line = lines[0] if lines else ""
        body = lines[1].strip() if len(lines) > 1 else ""
        sender = sender_line.replace("From:", "").strip()[:80]
        history.append({"sender": sender, "text": body[:200]})

    return active_inbound, history


def _format_thread_context(active_inbound: str, history: list[dict[str, str]]) -> str:
    """Format thread history into a prompt section."""
    if not history:
        return active_inbound

    parts = ["[THREAD HISTORY — last 2 exchanges]"]
    for entry in history[:4]:
        parts.append(f"Previous: {entry['sender']} wrote: {entry['text']}")
    parts.append("---")
    parts.append("[CURRENT MESSAGE]")
    parts.append(active_inbound)
    return "\n".join(parts)


def _lookup_prior_reply_to_sender(sender: str, database_url: str) -> str | None:
    """Find the most recent prior reply the user sent to this exact sender."""
    db_path = resolve_sqlite_path(database_url)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT reply_text FROM reply_pairs
            WHERE inbound_author LIKE ?
            ORDER BY paired_at DESC LIMIT 1
            """,
            (f"%{sender}%",),
        ).fetchone()
        if row and row[0]:
            return row[0][:200]
        return None
    except Exception:
        return None
    finally:
        conn.close()


def _confidence_label(score: float) -> str:
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def _deduplicate_by_thread(reply_pairs: list[RetrievalMatch]) -> list[RetrievalMatch]:
    """Keep only the highest-score pair per thread_id. None thread_ids are treated as unique."""
    seen_threads: dict[str, RetrievalMatch] = {}
    result: list[RetrievalMatch] = []
    for rp in reply_pairs:
        if rp.thread_id is None:
            result.append(rp)
        elif rp.thread_id not in seen_threads:
            seen_threads[rp.thread_id] = rp
            result.append(rp)
        elif rp.score > seen_threads[rp.thread_id].score:
            # Replace with higher score
            result = [r for r in result if r is not seen_threads[rp.thread_id]]
            seen_threads[rp.thread_id] = rp
            result.append(rp)
    return result


def _format_exemplars(reply_pairs: list[RetrievalMatch], *, max_exemplars: int = 5) -> str:
    if not reply_pairs:
        return "(no exemplars found)"
    # Deduplicate by thread_id
    reply_pairs = _deduplicate_by_thread(reply_pairs)
    # Sort by score descending
    sorted_pairs = sorted(reply_pairs, key=lambda rp: rp.score, reverse=True)
    # Drop exemplars with score < 0.2
    sorted_pairs = [rp for rp in sorted_pairs if rp.score >= 0.2]
    if not sorted_pairs:
        return "(no exemplars found)"

    parts: list[str] = ["The following are examples of how you have replied to similar emails:"]
    for i, rp in enumerate(sorted_pairs[:max_exemplars], 1):
        inbound = (rp.inbound_text or "")[:400]
        reply = strip_signature(rp.reply_text or "")[:600]
        # Normalize score to 0-1 range for confidence label (scores are typically 0-10+)
        norm_score = min(rp.score / 10.0, 1.0) if rp.score > 0 else 0
        conf = _confidence_label(norm_score)
        parts.append(f"[EXAMPLE {i} — confidence: {conf}]\nInbound: {inbound}\nYour reply: {reply}\n---")
    return "\n\n".join(parts)


def _precedent_summary(match: RetrievalMatch) -> dict[str, Any]:
    return {
        "source_id": match.source_id,
        "title": match.title,
        "snippet": match.snippet,
        "score": match.score,
    }


_TONE_INSTRUCTIONS: dict[str, str] = {
    "shorter": "Be more concise. Aim for half the word count.",
    "more_formal": "Use a more formal, professional tone.",
    "more_detail": "Add more detail and context to your reply.",
}


def lookup_sender_profile(email: str, database_url: str) -> dict[str, Any] | None:
    """Look up a sender profile from the database."""
    db_path = resolve_sqlite_path(database_url)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Check if sender_profiles table exists
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sender_profiles'").fetchone()
        if not exists:
            return None
        row = conn.execute("SELECT * FROM sender_profiles WHERE email = ?", (email.lower(),)).fetchone()
        if not row:
            return None
        profile_dict = {
            "email": row["email"],
            "display_name": row["display_name"],
            "domain": row["domain"],
            "company": row["company"],
            "sender_type": row["sender_type"],
            "relationship_note": row["relationship_note"],
            "reply_count": row["reply_count"],
            "avg_reply_words": row["avg_reply_words"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "topics": json.loads(row["topics_json"]) if row["topics_json"] else [],
        }
        # Include avg_response_hours if available
        try:
            profile_dict["avg_response_hours"] = row["avg_response_hours"]
        except (IndexError, KeyError):
            pass
        return profile_dict
    finally:
        conn.close()


_GREETING_WORDS = frozenset({
    "hi", "hello", "hey", "dear", "good", "morning", "afternoon", "evening",
    "hope", "greetings", "howdy", "salutations",
})

# Lines that are pure social filler — skip entirely for subject extraction
_FILLER_PATTERNS = re.compile(
    r"^(hi|hello|hey|dear|good morning|good afternoon|good evening|"
    r"i hope (you are|you're|this finds you|all is)|"
    r"hope you (are|'re)|"
    r"thank you (for|in advance)|"
    r"thanks for|"
    r"warm regards|"
    r"kind regards|"
    r"best regards|"
    r"i am writing|"
    r"i'm writing)",
    re.IGNORECASE,
)

# Action/topic keywords that signal a meaningful sentence
_TOPIC_KEYWORDS = re.compile(
    r"\b(payment|invoice|outstanding|follow.?up|meeting|schedule|proposal|"
    r"update|confirm|approve|review|request|issue|problem|question|"
    r"deadline|project|contract|agreement|feedback|report|delivery|"
    r"order|account|subscription|renewal|support|urgent|asap|required|"
    r"please|kindly|need|want|would like)\b",
    re.IGNORECASE,
)


def _subject_fallback(inbound_text: str) -> str | None:
    """Rule-based subject line fallback.

    1. If inbound has 'Subject:' header: strip 'Re:' prefixes, return 'Re: <subject>'
    2. Find first substantive sentence (contains a topic keyword, not a filler line)
    3. Truncate to ~60 chars, capitalize cleanly
    4. If nothing found: return None (let caller decide)
    """
    # 1. Check for Subject: header in first 5 lines
    for line in inbound_text.split("\n")[:5]:
        stripped = line.strip()
        if stripped.lower().startswith("subject:"):
            subj = stripped[len("subject:"):].strip()
            while subj.lower().startswith("re:"):
                subj = subj[3:].strip()
            if subj and len(subj) >= 3:
                return f"Re: {subj}"

    # 2. Find first substantive sentence — prefer topic keywords, strip "I am/I'm" openers
    lines = [line.strip() for line in inbound_text.split("\n") if line.strip()]

    def _clean_subject(text: str) -> str | None:
        # Strip leading "I am/I'm/We are/We're" to get to the point
        text = re.sub(r"^(I am|I'm|We are|We're|I would like to|I'd like to)\s+", "", text, flags=re.IGNORECASE)
        sentence = re.split(r"[.!?]", text)[0].strip()[:65].rstrip(" ,;:")
        if len(sentence) >= 8:
            return sentence[0].upper() + sentence[1:]
        return None

    # First pass: lines with topic keywords
    for line in lines:
        if _FILLER_PATTERNS.match(line) or len(line) < 15:
            continue
        if _TOPIC_KEYWORDS.search(line):
            result = _clean_subject(line)
            if result:
                return result

    # Second pass: any non-filler line
    for line in lines:
        if _FILLER_PATTERNS.match(line) or len(line) < 15:
            continue
        result = _clean_subject(line)
        if result:
            return result

    return None


def generate_subject(inbound_text: str, draft: str, database_url: str, configs_dir: Path) -> str | None:
    """Generate a subject line for the draft reply."""
    # Try rule-based fallback first
    fallback = _subject_fallback(inbound_text)
    if fallback is not None:
        return fallback

    # Only call Claude CLI if fallback returned None and model fallback != 'none'
    model_fallback = get_model_fallback()
    if model_fallback == "none":
        return None

    try:
        prompt = (
            "Generate a concise email subject line (under 60 chars) for this reply.\n\n"
            f"Inbound:\n{inbound_text[:500]}\n\nDraft reply:\n{draft[:500]}\n\n"
            "Output ONLY the subject line, nothing else."
        )
        result = _call_claude_cli(prompt)
        # Clean up: remove quotes, "Subject:" prefix
        result = result.strip().strip('"').strip("'")
        if result.lower().startswith("subject:"):
            result = result[len("subject:") :].strip()
        return result[:80] if result else None
    except Exception:
        return None


def _format_sender_context(profile: dict[str, Any]) -> str:
    """Format sender profile into a prompt context block."""
    user_name = get_user_name()
    topics = ", ".join(profile.get("topics", [])) or "none recorded"
    result = (
        f"[SENDER CONTEXT]\n"
        f"Sender: {profile.get('display_name') or 'Unknown'} <{profile['email']}>\n"
        f"Company: {profile.get('company') or 'Unknown'}\n"
        f"Type: {profile.get('sender_type') or 'unknown'}\n"
        f"Relationship: {profile.get('relationship_note') or 'no note'}\n"
        f"History: {user_name} has replied {profile.get('reply_count', 0)} times to this sender. "
        f"Avg reply length: {profile.get('avg_reply_words') or 'N/A'} words.\n"
        f"Topics discussed: {topics}"
    )
    avg_response_hours = profile.get("avg_response_hours")
    if avg_response_hours is not None:
        result += f"\nTypical reply time to this sender: ~{int(avg_response_hours)}h"
    return result


def _resolve_greeting(persona: dict[str, Any], sender_type: str | None, first_name: str | None = None) -> str:
    """Resolve greeting from persona config: mode greeting > greeting_patterns > default."""
    greeting = ""
    modes = persona.get("modes", {})
    greeting_patterns = persona.get("greeting_patterns", {})
    if sender_type and sender_type in modes and "greeting" in modes[sender_type]:
        greeting = modes[sender_type]["greeting"]
    elif sender_type and sender_type in greeting_patterns:
        greeting = greeting_patterns[sender_type]
    elif "default" in greeting_patterns:
        greeting = greeting_patterns["default"]
    if greeting and "{name}" in greeting:
        greeting = greeting.replace("{name}", first_name or "").replace("  ", " ")
    return greeting


def _resolve_closing(persona: dict[str, Any], sender_type: str | None) -> str:
    """Resolve closing from persona config: mode closing > closing_patterns > default."""
    closing = ""
    modes = persona.get("modes", {})
    closing_patterns = persona.get("closing_patterns", {})
    if sender_type and sender_type in modes and "closing" in modes[sender_type]:
        closing = modes[sender_type]["closing"]
    elif sender_type and sender_type in closing_patterns:
        closing = closing_patterns[sender_type]
    elif "default" in closing_patterns:
        closing = closing_patterns["default"]
    return closing


def assemble_prompt(
    *,
    inbound_message: str,
    reply_pairs: list[RetrievalMatch],
    persona: dict[str, Any],
    prompts: dict[str, str],
    detected_mode: str | None = None,
    audience_hint: str | None = None,
    tone_hint: str | None = None,
    sender_context: str | None = None,
    language_hint: str | None = None,
    intent_hint: str | None = None,
    sender_type: str | None = None,
    first_name: str | None = None,
) -> str:
    style = persona.get("style", {})
    voice = style.get("voice", "direct, clear, pragmatic")
    avg_words = style.get("avg_reply_words")
    constraints = style.get("constraints", [])
    # Prefer intent-specific avg_words if available
    if intent_hint and intent_hint != "general":
        intent_avg = style.get("intent_avg_words", {})
        if isinstance(intent_avg, dict) and intent_hint in intent_avg:
            avg_words = intent_avg[intent_hint]
    system = prompts.get(
        "system_prompt",
        "You are YouOS, a local-first email copilot.",
    )

    exemplars_text = _format_exemplars(reply_pairs)
    n = len(reply_pairs)

    # Build persona constraints block
    persona_lines: list[str] = [f"Persona: {voice}."]
    if avg_words:
        persona_lines.append(f"Target reply length: ~{avg_words} words.")
    for c in constraints:
        persona_lines.append(f"- {c}")

    # Style-driven constraints from persona analysis
    bullet_pct = style.get("bullet_point_pct")
    if bullet_pct is not None and bullet_pct > 0.4:
        persona_lines.append("- prefer bullet points for multi-item responses")
    directness = style.get("directness_score")
    if directness is not None and directness > 0.8:
        persona_lines.append("- be direct, avoid hedging")
    avg_para = style.get("avg_paragraphs")
    if avg_para is not None and avg_para > 2:
        persona_lines.append("- use clear paragraph breaks")

    persona_block = "\n".join(persona_lines)

    # Build optional context lines
    context_lines: list[str] = []
    if detected_mode:
        context_lines.append(f"Detected mode: {detected_mode}")
    if audience_hint:
        context_lines.append(f"Audience: {audience_hint}")
    if intent_hint and intent_hint != "general":
        context_lines.append(f"Email intent: {intent_hint}")
    if _has_thread_context(inbound_message):
        context_lines.append("Note: This inbound message contains a multi-message thread. Consider the full conversation context when drafting your reply.")
    context_block = ""
    if context_lines:
        context_block = "\n" + "\n".join(context_lines) + "\n"

    # Build tone instruction
    tone_instruction = ""
    if tone_hint and tone_hint in _TONE_INSTRUCTIONS:
        tone_instruction = f"\n{_TONE_INSTRUCTIONS[tone_hint]}\n"

    sender_block = ""
    if sender_context:
        sender_block = f"\n{sender_context}\n"

    language_block = ""
    if language_hint and language_hint != "en":
        language_block = f"\n[LANGUAGE: {language_hint}] Reply in the same language as the inbound message.\n"

    result = (
        f"[SYSTEM]\n"
        f"{system.strip()}\n"
        f"{persona_block}\n"
        f"{context_block}"
        f"{sender_block}"
        f"{language_block}"
        f"\n"
        f"[EXEMPLARS — {n} similar past replies]\n"
        f"{exemplars_text}\n"
        f"\n"
        f"[TASK]\n"
        f"Draft a reply to the following inbound message in your style.\n"
        f"Use the exemplars above as style and tone guidance.\n"
        f"Do not copy them verbatim — use them as reference only.\n"
        f"Output the draft reply text only. No preamble, no explanation.\n"
        f"{tone_instruction}"
    )

    # Append length guidance if avg_reply_words is set
    if avg_words:
        p25 = style.get("avg_reply_words_p25")
        p75 = style.get("avg_reply_words_p75")
        if p25 is not None and p75 is not None:
            result += f"\nTarget length: ~{avg_words} words (typical range: {p25}\u2013{p75}). Be concise.\n"
        else:
            result += f"\nTarget length: ~{avg_words} words. Be concise.\n"

    # Greeting/closing injection
    greeting = _resolve_greeting(persona, sender_type, first_name)
    closing = _resolve_closing(persona, sender_type)
    if greeting and closing:
        result += f"\nBegin your reply with: {greeting}\nEnd your reply with: {closing}\n"

    result += f"\n[INBOUND MESSAGE]\n{inbound_message}"
    return result


PROMPT_TOKEN_BUDGET: int = 2000


def _estimate_tokens(text: str) -> int:
    """Estimate token count using a simple word-count * 1.4 approximation."""
    return int(len(text.split()) * 1.4)


ADAPTER_PATH = Path(__file__).resolve().parents[2] / "models" / "adapters" / "latest"


def _get_base_model_id() -> str:
    return get_base_model()


def _adapter_available() -> bool:
    return (ADAPTER_PATH / "adapters.safetensors").exists()


def _compute_max_tokens(avg_reply_words: int | None, *, persona: dict[str, Any] | None = None, intent: str | None = None) -> int:
    """Compute max_tokens as a rough upper bound from avg_reply_words.

    Priority: mode-specific > intent-specific > global > default 300.
    """
    effective_words = avg_reply_words
    if persona:
        # 1. Mode-specific avg_reply_words (highest priority)
        mode_words = persona.get("_active_mode_config", {}).get("avg_reply_words")
        if mode_words is not None:
            effective_words = mode_words
        # 2. Intent-specific (only if mode didn't override)
        elif intent and intent != "general":
            intent_avg = persona.get("style", {}).get("intent_avg_words", {})
            if isinstance(intent_avg, dict) and intent in intent_avg:
                effective_words = intent_avg[intent]
        # 3. Global fallback
        if effective_words is None:
            effective_words = persona.get("style", {}).get("avg_reply_words")
    if effective_words is None:
        return 300
    return max(100, min(500, effective_words * 5))


def _strip_mlx_output(raw: str) -> str:
    """Extract just the generated text from mlx_lm generate output.

    mlx_lm wraps output like:
        ==========
        <generated text>
        ==========
        Prompt: N tokens, X tokens-per-sec
        Generation: N tokens, X tokens-per-sec
        Peak memory: X GB
    """
    # Split on the separator lines
    parts = re.split(r"={5,}", raw)
    # The generated text is the second segment (index 1) if separators exist
    if len(parts) >= 2:
        return parts[1].strip()
    # No separators — strip stats lines from the end as fallback
    lines = raw.strip().splitlines()
    clean = [
        line for line in lines
        if not re.match(r"^(Prompt:|Generation:|Peak memory:|Fetching)", line)
    ]
    return "\n".join(clean).strip()


def _call_local_model(prompt: str, *, max_tokens: int = 300, use_adapter: bool = True) -> str:
    cmd = [
        "mlx_lm",
        "generate",
        "--model",
        _get_base_model_id(),
    ]
    if use_adapter:
        cmd.extend(["--adapter-path", str(ADAPTER_PATH)])
    cmd.extend(
        [
            "--prompt",
            prompt,
            "--max-tokens",
            str(max_tokens),
        ]
    )
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mlx_lm generate failed (exit {result.returncode}): {result.stderr.strip()}")
    return _strip_mlx_output(result.stdout)


def _generate_via_ollama(prompt: str, model: str = "mistral", base_url: str = "http://localhost:11434", *, num_predict: int = 400) -> str:
    """Generate via Ollama HTTP API."""
    import urllib.request

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.7, "num_predict": num_predict}}).encode()
    req = urllib.request.Request(f"{base_url}/api/generate", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("response", "").strip()
    except Exception as exc:
        raise RuntimeError(f"Ollama generation failed: {exc}") from exc


def _call_claude_cli(prompt: str, *, max_tokens: int = 300) -> str:
    cmd = ["claude", "--print"]
    if max_tokens:
        cmd.extend(["--max-tokens", str(max_tokens)])
    cmd.append(prompt)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {stderr}")
    return result.stdout.strip()


def generate_draft(
    request: DraftRequest,
    *,
    database_url: str,
    configs_dir: Path,
) -> DraftResponse:
    # Strip quoted text from inbound before processing
    clean_inbound = strip_quoted_text(request.inbound_message)

    from app.core.text_utils import detect_language

    detected_lang = detect_language(clean_inbound)

    # Handle thread context for ongoing threads
    inbound_for_prompt = clean_inbound
    if _has_thread_context(clean_inbound):
        active_inbound, history = _extract_thread_parts(clean_inbound)
        inbound_for_prompt = _format_thread_context(active_inbound, history)

    # Look up prior reply to this sender for additional context
    user_name = get_user_name()
    if request.sender:
        prior_reply = _lookup_prior_reply_to_sender(request.sender, database_url)
        if prior_reply and _has_thread_context(clean_inbound):
            inbound_for_prompt += f"\n\n[PRIOR REPLY TO THIS SENDER]\n{user_name} previously wrote: {prior_reply}"

    # Infer account email from sender if not explicitly provided
    if request.account_email:
        account_emails = (request.account_email,)
    elif request.sender:
        inferred = get_account_for_sender(request.sender)
        account_emails = (inferred,) if inferred else ()
    else:
        account_emails = ()
    sender_type_hint = None
    sender_domain_hint = None
    if request.sender:
        sender_type_hint = classify_sender(request.sender)
        sender_domain_hint = extract_domain(request.sender)

    # Classify intent (multi-intent support)
    from app.core.intent import classify_intents_multi

    if request.intent_hint:
        detected_intent = request.intent_hint
        intent_hint_2 = None
    else:
        intents = classify_intents_multi(clean_inbound)
        detected_intent = intents[0]
        intent_hint_2 = intents[1] if len(intents) > 1 else None

    retrieval_response: RetrievalResponse = retrieve_context(
        RetrievalRequest(
            query=clean_inbound,
            scope="all",
            account_emails=account_emails,
            top_k_reply_pairs=request.top_k_reply_pairs,
            top_k_chunks=request.top_k_chunks,
            sender_type_hint=sender_type_hint,
            sender_domain_hint=sender_domain_hint,
            language_hint=detected_lang,
            intent_hint=detected_intent,
            intent_hint_2=intent_hint_2,
            thread_id=request.thread_id,
        ),
        database_url=database_url,
        configs_dir=configs_dir,
    )

    detected_mode = request.mode or retrieval_response.detected_mode
    reply_pairs = retrieval_response.reply_pairs
    # Build score stats dict from retrieval response
    score_stats = None
    if retrieval_response.mean_score is not None:
        score_stats = {
            "max": retrieval_response.max_score,
            "mean": retrieval_response.mean_score,
            "stddev": retrieval_response.score_stddev,
        }
    confidence, confidence_reason = _score_confidence(reply_pairs, score_stats=score_stats)

    prompts = _load_prompts(configs_dir)
    persona = _load_persona(configs_dir)

    # Per-sender-type persona mode override
    _sender_type = sender_type_hint
    modes = persona.get("modes", {})
    if _sender_type and _sender_type in modes:
        mode_config = modes[_sender_type]
        persona["_active_mode_config"] = mode_config
        # Merge mode values into persona style, but never override custom_constraints
        style = persona.setdefault("style", {})
        for key in ("voice", "avg_reply_words", "greeting", "closing"):
            if key in mode_config:
                style[key] = mode_config[key]
    else:
        persona["_active_mode_config"] = {}

    # Look up sender profile if sender provided
    sender_profile = None
    sender_context = None
    first_name = None
    if request.sender:
        sender_profile = lookup_sender_profile(request.sender, database_url)
        if sender_profile:
            sender_context = _format_sender_context(sender_profile)
            first_name = first_name_from_display_name(sender_profile.get("display_name"))

    prompt = assemble_prompt(
        inbound_message=inbound_for_prompt,
        reply_pairs=reply_pairs,
        persona=persona,
        prompts=prompts,
        detected_mode=detected_mode,
        audience_hint=request.audience_hint,
        tone_hint=request.tone_hint,
        sender_context=sender_context,
        language_hint=detected_lang,
        intent_hint=detected_intent,
        sender_type=sender_type_hint,
        first_name=first_name,
    )

    # Token budget check — trim exemplars if prompt is too long
    token_estimate = _estimate_tokens(prompt)
    if token_estimate > PROMPT_TOKEN_BUDGET and reply_pairs:
        # Remove lowest-scoring exemplars (already sorted desc) from the end
        trimmed_pairs = list(reply_pairs)
        removed = 0
        while trimmed_pairs and _estimate_tokens(prompt) > PROMPT_TOKEN_BUDGET:
            trimmed_pairs.pop()
            removed += 1
            prompt = assemble_prompt(
                inbound_message=inbound_for_prompt,
                reply_pairs=trimmed_pairs,
                persona=persona,
                prompts=prompts,
                detected_mode=detected_mode,
                audience_hint=request.audience_hint,
                tone_hint=request.tone_hint,
                sender_context=sender_context,
                language_hint=detected_lang,
                intent_hint=detected_intent,
                sender_type=sender_type_hint,
                first_name=first_name,
            )
        if removed:
            logger.info("Prompt truncated: removed %d exemplars to fit token budget", removed)
            reply_pairs = trimmed_pairs
        token_estimate = _estimate_tokens(prompt)

    precedent_used = [_precedent_summary(rp) for rp in reply_pairs]

    # Compute length-aware max_tokens (intent-specific if available)
    avg_reply_words = persona.get("style", {}).get("avg_reply_words")
    # Use intent-specific avg if available
    intent_avg = persona.get("style", {}).get("intent_avg_words", {})
    if isinstance(intent_avg, dict) and detected_intent in intent_avg:
        avg_reply_words = intent_avg[detected_intent]
    max_tokens = _compute_max_tokens(avg_reply_words, persona=persona, intent=detected_intent)

    fallback_model = get_model_fallback()
    try:
        if request.use_local_model and _adapter_available():
            draft = _call_local_model(prompt, max_tokens=max_tokens, use_adapter=request.use_adapter)
            model_used = "qwen2.5-1.5b-lora"
        elif fallback_model == "ollama":
            from app.core.config import get_ollama_config

            ollama_cfg = get_ollama_config()
            ollama_model = ollama_cfg.get("model", "mistral")
            ollama_url = ollama_cfg.get("base_url", "http://localhost:11434")
            draft = _generate_via_ollama(prompt, model=ollama_model, base_url=ollama_url, num_predict=max_tokens)
            model_used = f"ollama:{ollama_model}"
        elif fallback_model == "claude":
            draft = _call_claude_cli(prompt, max_tokens=max_tokens)
            model_used = "claude"
        else:
            draft = _call_claude_cli(prompt, max_tokens=max_tokens)
            model_used = fallback_model
    except Exception as exc:
        draft = f"[draft generation failed: {exc}]"
        model_used = "error"

    # Retry on empty or signature-only local model output
    empty_output_retried = False
    _draft_stripped = strip_signature(draft).strip()
    non_ws = len(draft.replace(" ", "").replace("\n", "").replace("\t", ""))
    _looks_like_only_signature = len(_draft_stripped) < 15 and non_ws > 0
    if (non_ws < 10 or _looks_like_only_signature) and model_used not in ("error", "claude"):
        logger.warning("Local model returned empty output, falling back to Claude")
        if fallback_model != "none":
            try:
                draft = _call_claude_cli(prompt, max_tokens=max_tokens)
                model_used = "claude"
                empty_output_retried = True
            except Exception as fallback_exc:
                raise ValueError("Draft generation returned empty output") from fallback_exc
        else:
            raise ValueError("Draft generation returned empty output")

        # Increment counter in pipeline log
        try:
            log_path = Path(__file__).resolve().parents[1] / "var" / "pipeline_last_run.json"
            if log_path.exists():
                log_data = json.loads(log_path.read_text(encoding="utf-8"))
                log_data["local_model_empty_retries"] = log_data.get("local_model_empty_retries", 0) + 1
                log_path.write_text(json.dumps(log_data, indent=2))
        except Exception:
            pass

    # Generate subject line
    suggested_subject = generate_subject(request.inbound_message, draft, database_url, configs_dir)

    return DraftResponse(
        draft=draft,
        detected_mode=detected_mode,
        precedent_used=precedent_used,
        retrieval_method=retrieval_response.retrieval_method,
        confidence=confidence,
        confidence_reason=confidence_reason,
        model_used=model_used,
        sender_profile=sender_profile,
        suggested_subject=suggested_subject,
        token_estimate=token_estimate,
        empty_output_retried=empty_output_retried,
    )
