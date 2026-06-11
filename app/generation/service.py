from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import (
    cloud_escalation_enabled,
    get_account_for_sender,
    get_base_model,
    get_model_fallback,
    get_persona_style_anchor,
    get_user_name,
    get_user_names,
)
from app.core.config import (
    model_label as _model_label,
)
from app.core.sender import classify_sender, extract_domain, first_name_from_display_name
from app.core.settings import get_adapter_path
from app.core.text_utils import neutralize_prompt_markers, strip_quoted_text
from app.db.bootstrap import connect as _connect
from app.db.bootstrap import resolve_sqlite_path
from app.retrieval.service import (
    RetrievalMatch,
    RetrievalRequest,
    RetrievalResponse,
    retrieve_context,
)

logger = logging.getLogger(__name__)

_EXEMPLAR_CACHE_TTL_SECONDS = 30 * 60
_exemplar_cache: dict[tuple[str, str], dict[str, Any]] = {}


def clear_exemplar_cache(*, database_url: str | None = None) -> None:
    _exemplar_cache.clear()
    if database_url:
        try:
            db_path = resolve_sqlite_path(database_url)
            conn = _connect(db_path)
            try:
                conn.execute("DELETE FROM exemplar_cache")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to clear persistent exemplar cache", exc_info=True)


def _cache_key(intent_hint: str | None, sender_type: str | None) -> tuple[str, str]:
    return ((intent_hint or "general").strip().lower(), (sender_type or "unknown").strip().lower())


def _get_cached_exemplar_ids(intent_hint: str | None, sender_type: str | None, *, database_url: str | None = None) -> tuple[list[str], bool, str]:
    key = _cache_key(intent_hint, sender_type)
    key_str = f"{key[0]}::{key[1]}"

    # 1) In-memory fast path
    entry = _exemplar_cache.get(key)
    if entry:
        if time.time() - float(entry.get("ts", 0.0)) <= _EXEMPLAR_CACHE_TTL_SECONDS:
            ids = [str(x) for x in entry.get("ids", []) if x]
            logger.info("Exemplar cache HIT(mem) key=%s size=%d", key_str, len(ids))
            return ids, True, key_str
        _exemplar_cache.pop(key, None)

    # 2) Persistent fallback
    if database_url:
        try:
            db_path = resolve_sqlite_path(database_url)
            conn = _connect(db_path)
            try:
                row = conn.execute(
                    "SELECT source_ids_json, strftime('%s', updated_at) FROM exemplar_cache WHERE cache_key = ?",
                    (key_str,),
                ).fetchone()
                if row:
                    source_ids_json, updated_epoch = row
                    updated_epoch = int(updated_epoch or 0)
                    if updated_epoch and (time.time() - updated_epoch) <= _EXEMPLAR_CACHE_TTL_SECONDS:
                        ids = [str(x) for x in json.loads(source_ids_json or "[]") if x]
                        _exemplar_cache[key] = {"ts": time.time(), "ids": ids[:10]}
                        logger.info("Exemplar cache HIT(db) key=%s size=%d", key_str, len(ids))
                        return ids, True, key_str
                    conn.execute("DELETE FROM exemplar_cache WHERE cache_key = ?", (key_str,))
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Exemplar cache DB read failed for key=%s", key_str, exc_info=True)

    logger.info("Exemplar cache MISS key=%s", key_str)
    return [], False, key_str


def _update_exemplar_cache(intent_hint: str | None, sender_type: str | None, source_ids: list[str], *, database_url: str | None = None) -> None:
    key = _cache_key(intent_hint, sender_type)
    key_str = f"{key[0]}::{key[1]}"
    ids = [sid for sid in source_ids[:10] if sid]
    _exemplar_cache[key] = {"ts": time.time(), "ids": ids}

    if database_url:
        try:
            db_path = resolve_sqlite_path(database_url)
            conn = _connect(db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO exemplar_cache(cache_key, source_ids_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source_ids_json=excluded.source_ids_json,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (key_str, json.dumps(ids)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Exemplar cache DB write failed for key=%s", key_str, exc_info=True)


def _apply_cached_order(reply_pairs: list[RetrievalMatch], cached_ids: list[str]) -> list[RetrievalMatch]:
    if not cached_ids or not reply_pairs:
        return reply_pairs
    rank = {sid: i for i, sid in enumerate(cached_ids)}
    cached = [rp for rp in reply_pairs if rp.source_id in rank]
    uncached = [rp for rp in reply_pairs if rp.source_id not in rank]
    cached.sort(key=lambda rp: rank.get(rp.source_id, 9999))
    return cached + uncached


def _top_exemplar_source_ids(reply_pairs: list[RetrievalMatch], limit: int = 5) -> list[str]:
    # Prefer feedback-derived quality (metadata["quality_score"]) first, then
    # relevance score. Retrieval surfaces quality_score into metadata so this
    # primary key is live in production, not just in tests.
    ranked = sorted(
        [rp for rp in reply_pairs if rp.source_id],
        key=lambda rp: ((rp.metadata or {}).get("quality_score", 1.0), rp.score),
        reverse=True,
    )
    return [rp.source_id for rp in ranked[:limit]]


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
    subject: str | None = None
    user_prompt: str | None = None
    # Caller-provided guidance threaded into the prompt as an extra persona
    # constraint. Used by the autonomous agent loop to inject standing
    # instructions ("today I'm OOO; politely decline meetings") into every
    # triage draft without changing the user's stored persona. Combines
    # additively with the auto-detected cold-outreach DECLINE_NUDGE so both
    # can apply to the same draft.
    standing_instructions: str | None = None
    # Prior turns in the same thread (oldest→newest), each
    # ``{"sender": ..., "text": ...}``. The autonomous agent populates this from
    # the fetched thread so the drafter sees conversation context instead of
    # pattern-matching on a single message and answering the wrong question.
    # Preferred over the brittle regex thread extraction when present.
    thread_history: list[dict[str, str]] | None = None
    # ζ: refuse cloud fallback for this specific draft. The agent triage
    # path sets this so background sweeps can't silently send inbound text
    # to Claude — if the local model is unavailable, generate_draft returns
    # an error placeholder rather than falling back. Interactive /feedback
    # is unaffected; this is strictly per-request.
    strict_local: bool = False
    # Eval-only: an empty local-model output must NOT shell out to the (slow,
    # possibly-unauthenticated) Claude CLI. The autoresearch/golden eval sets
    # this so a degenerate empty draft is recorded as empty/failed and the suite
    # moves on, instead of throwing 200× `claude CLI failed (exit 1)` in the
    # launchd environment where the cloud CLI isn't logged in. Distinct from
    # ``strict_local`` (an agent-safety control) so the two intents stay
    # independent; either one disables the cloud fallback. Production drafting
    # leaves this False and keeps its normal fallback behavior. (b168)
    no_cloud_fallback: bool = False
    # When False, bypass the exemplar cache so this draft reflects the CURRENT
    # retrieval config rather than a previously-cached exemplar selection. The
    # autoresearch eval sets this False — otherwise the cache pins the same
    # exemplars across every candidate and retrieval-param mutations become
    # no-ops (the eval scores identically and nothing is ever kept). Production
    # drafting leaves it True for consistency + speed.
    use_exemplar_cache: bool = True
    # Pin the generation backend regardless of config/use_local_model. Used by
    # the cross-model comparison (app/evaluation/model_compare.py) to draft the
    # same case under each engine. One of "mlx" | "ollama" | "claude" | "none";
    # None (default) = normal selection. Pinning "mlx" still requires the local
    # model to be available — otherwise it falls through the usual chain.
    backend_override: str | None = None
    # Eval-only determinism (b166). When True, decoding is forced to greedy
    # (temperature=0 / argmax) and seeded, OVERRIDING config decoding and the
    # multi-candidate temperature spread, so re-scoring the same config yields
    # the same composite. The autoresearch/golden eval sets this; production
    # drafting leaves it False and keeps the config/model-default behavior.
    deterministic: bool = False
    # Seed used when ``deterministic`` is True (defaults to EVAL_SEED). Passed to
    # the mlx_lm subprocess (--seed) and the warm model server so both paths are
    # reproducible.
    seed: int | None = None
    # b175: explicit per-request opt-in to the cloud (Claude) drafting escape
    # hatch. Set ONLY by an interactive draft endpoint; background / eval /
    # scheduled callers must leave this False (the default). Even when True,
    # cloud drafting is still gated by the config flag
    # (drafting.cloud_escalation.enabled) AND hard-blocked whenever any of
    # ``strict_local`` / ``no_cloud_fallback`` / ``deterministic`` is set, so it
    # is impossible on any background or eval path. See ``_cloud_escalation_allowed``.
    allow_cloud_escalation: bool = False
    # b188: True when a human explicitly asked to SEE this draft (the /draft API,
    # /feedback, CLI, review-queue regenerate, cross-model compare). Such callers
    # must ALWAYS get a draft string back — the user opted in to inspect it — so
    # the low-quality ABSTAIN path is suppressed for them. Defaults True so every
    # pre-existing caller keeps that always-draft behavior unchanged; ONLY the
    # autonomous triage sweep (app/agent/triage.py) passes interactive=False,
    # which is the sole path that may withhold a weak draft and instead surface
    # the email for review. See ``_should_abstain`` / ``_abstain_config``.
    interactive: bool = True
    # b194: opt-in to best-of-N multi-candidate generation for THIS request.
    # Multi-candidate fans out n drafts on a temperature spread and keeps the
    # highest draft_quality_score one — a real but modest quality lift (mostly
    # length-fit + style) that costs n× generation. We only want that cost where
    # the latency is HIDDEN: the autonomous triage sweep (drafts sit in the
    # review queue; no human waits) and the compare-models tuning harness set
    # this True. Interactive callers (/draft, /feedback, CLI, review-queue
    # regenerate, streaming) leave it False so they stay single-candidate and
    # fast — the +0.013 voice lift isn't worth doubling perceived latency. The
    # config knob ``generation.multi_candidate.n`` still controls n; this flag
    # only decides WHETHER a given request is allowed to fan out. Hard-blocked on
    # the deterministic/eval path regardless (see generate_draft).
    multi_candidate_ok: bool = False
    # Holdout exclusion (b234): forwarded to retrieval so a replayed
    # historical inbound can't retrieve its own stored answer (the exact
    # reply pair, or same-thread documents/chunks containing the real reply).
    # Used by the inbox-replay backtest; empty in production.
    exclude_reply_pair_ids: tuple[int, ...] = ()
    exclude_thread_ids: tuple[str, ...] = ()


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
    exemplar_cache_hit: bool = False
    exemplar_cache_key: str | None = None
    # Post-generation repair (see _repair_draft). length_flag is always
    # computed ("ok"/"long"/"short"/None); repairs lists any mutations applied
    # (only when the opt-in repair flags are enabled).
    length_flag: str | None = None
    repairs: list[str] = field(default_factory=list)
    # Ranked alternatives when multi-candidate generation is enabled (else []).
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # 0–1 estimate of how good THIS draft is (voice fidelity vs the user's
    # retrieved replies + structural fit, collapsed toward 0 for generic acks /
    # fallback drafts). Distinct from ``confidence`` (which is retrieval-precedent
    # strength). This is what an autonomous action should gate on — "is the draft
    # itself good enough to act on", not just "does this email deserve a reply".
    quality_score: float | None = None
    # Verify-before-accept findings (app/generation/verify.py). A blocking issue
    # (language mismatch, invented email/link) collapses quality_score so the
    # auto-push quality floor holds the draft for review. Empty when clean.
    verify_issues: list[str] = field(default_factory=list)
    # b175: cloud (Claude) drafting transparency. ``cloud_used`` is True iff this
    # draft was produced by a cloud backend that egressed the inbound off-device;
    # ``egress_notice`` carries a human-readable warning in that case (else None).
    # The UI / telemetry surface these so cloud drafting is never hidden.
    cloud_used: bool = False
    egress_notice: str | None = None
    # b188: ABSTAIN. True iff this is an AUTONOMOUS draft whose quality_score fell
    # below the abstain threshold, so it must NOT be presented or recorded as a
    # ready/usable reply — the caller (autonomous triage) routes the email to the
    # existing surface-for-review tier ("needs your attention") instead. The
    # ``draft`` text and ``quality_score`` are still populated (work is never
    # discarded silently — they're kept for telemetry / a later human look), but
    # downstream must treat the draft as withheld. ``withhold_reason`` carries a
    # human-readable explanation (e.g. "withheld: quality 0.32 < 0.50"). This is
    # impossible to set on an interactive or deterministic/eval request (see
    # ``_should_abstain``), so it never perturbs the golden eval or the /draft API.
    withheld: bool = False
    withhold_reason: str | None = None

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
    # Standard signature delimiters on their own line (newline-separated form).
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
    # Inline signature markers — the LoRA sometimes emits a run-on signature
    # ("Cheers, Baher Al Hakim CEO / Work AI w: work.example e: baher@…")
    # rather than the newline-separated form. Each pattern is signature-specific
    # to avoid eating legitimate prose: role+separator+capital, the single-letter
    # contact marker w:/e:/p:/t: followed by a URL/email/phone/domain, and the
    # "Sent from my <device>" idiom anywhere on a line.
    patterns.extend(
        [
            re.compile(
                r"\b(CEO|Founder|Co-?founder|Director|Managing Director|Head of)\s*[/|—\-]\s*[A-Z]"
            ),
            re.compile(
                r"(?:^|\s)[wepftWEPFT]:\s+(?:https?://|[\w.+-]+@[\w-]+|\+?\d|[\w-]+\.[a-z]{2,})"
            ),
            re.compile(r"Sent from my (iPhone|iPad|Android|Mac|Galaxy)", re.IGNORECASE),
        ]
    )
    return patterns


# Quote-tail artifact: "On 23. Jul 2025 at 10:17 +0200, X <a@b> wrote:" — the
# LoRA can hallucinate this prefix when trained on quote-laden replies. The
# {0,100} bound keeps the match local; DOTALL would over-eat across paragraphs.
_QUOTE_TAIL_PATTERN = re.compile(r"\bOn\s+[^\n]{0,160}\bwrote\s*:", re.IGNORECASE)


def strip_quote_tail(text: str) -> str:
    """Remove an email-quote tail like ``On <date>, <person> wrote:``.

    Truncates from the start of the match through the rest of the draft.
    Returns the trimmed text (or ``text`` unchanged if no match).
    """
    m = _QUOTE_TAIL_PATTERN.search(text)
    if m:
        return text[: m.start()].rstrip()
    return text


def decode_html_entities(text: str) -> str:
    """Decode HTML entities (``&#39;`` → ``'``, ``&amp;`` → ``&``) that leak
    through when the LoRA is trained on HTML-mangled mail bodies."""
    import html

    return html.unescape(text)


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


def _strip_trailing_user_name(text: str) -> str:
    """Strip the user's name (and any trailing words like a surname) when it
    sits at the very end of the text, after a sentence-terminating
    punctuation mark.

    QA found ``strip_signature`` correctly removes the contact-detail block
    (``CEO / Work AI w: work.example e: …``) but leaves the trailing
    ``Baher Al Hakim`` intact — it's not at line start, so the line-anchored
    patterns miss it. The LoRA then learns ``[brief content] + [name]`` and
    on short queries emits only the name+contact half.

    Pattern: ``(?<=[.,!?])\\s+<first-name>\\b[^.!?]*$`` —
    - lookbehind for `.`, `,`, `!`, `?` (so mid-sentence uses don't match)
    - the user's first name from ``get_user_names()`` (only the first name is
      configured for BaherOS; trailing surname tokens are captured by the
      ``[^.!?]*`` tail)
    - any non-sentence-ending chars to EOF (consumes ``Al Hakim``, contact
      remnants, etc.)
    """
    if not text:
        return text
    names = sorted(
        {n.strip() for n in get_user_names() if n.strip()},
        key=len, reverse=True,
    )
    for name in names:
        pattern = re.compile(
            rf"(?<=[.,!?])\s+{re.escape(name)}\b[^.!?]*$",
            re.IGNORECASE,
        )
        new_text = pattern.sub("", text).rstrip()
        if new_text != text:
            return new_text
    return text


def strip_exemplar_signature(text: str) -> str:
    """Aggressive signature strip for exemplars *fed back into the prompt*.

    Beyond ``strip_signature`` (contact details, role+separator, "Sent from
    my…"), this also drops the trailing user-name suffix. We don't want the
    LoRA to see ``[content] + [name]`` patterns in its precedents and mirror
    them — especially on short queries, where the model otherwise emits the
    name half with no content half at all (BaherOS regression QA).
    """
    return _strip_trailing_user_name(strip_signature(text))


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


def _lookup_prior_reply_to_sender(sender: str, database_url: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Find the most recent prior reply the user sent to this exact sender."""
    _own_conn = conn is None
    if _own_conn:
        db_path = resolve_sqlite_path(database_url)
        conn = _connect(db_path)
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
            return row[0][:PRIOR_REPLY_CHARS]
        return None
    except Exception:
        logger.warning("Failed to look up prior reply for sender %s", sender, exc_info=True)
        return None
    finally:
        if _own_conn:
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


def _format_exemplars(
    reply_pairs: list[RetrievalMatch],
    *,
    max_exemplars: int = 5,
    score_stats: dict[str, float] | None = None,
) -> str:
    if not reply_pairs:
        return "(no exemplars found)"
    # Deduplicate by thread_id
    reply_pairs = _deduplicate_by_thread(reply_pairs)
    # Sort by score descending
    sorted_pairs = sorted(reply_pairs, key=lambda rp: rp.score, reverse=True)
    # Drop exemplars with score below MIN_SCORE_FILTER
    from app.retrieval.service import MIN_SCORE_FILTER
    sorted_pairs = [rp for rp in sorted_pairs if rp.score >= MIN_SCORE_FILTER]
    if not sorted_pairs:
        return "(no exemplars found)"

    # D1: Use relative thresholds (mean±σ) when stats available
    if score_stats and score_stats.get("mean") is not None and score_stats.get("stddev") is not None:
        mean = score_stats["mean"]
        stddev = score_stats["stddev"]
        def _relative_conf(score: float) -> str:
            if score > mean + stddev:
                return "high"
            if score > mean:
                return "medium"
            return "low"
        conf_fn = _relative_conf
    else:
        def _abs_conf(score: float) -> str:
            norm = min(score / 10.0, 1.0) if score > 0 else 0.0
            return _confidence_label(norm)
        conf_fn = _abs_conf

    parts: list[str] = ["The following are examples of how you have replied to similar emails:"]
    for i, rp in enumerate(sorted_pairs[:max_exemplars], 1):
        inbound = (rp.inbound_text or "")[:EXEMPLAR_INBOUND_CHARS]
        reply = strip_exemplar_signature(rp.reply_text or "")[:EXEMPLAR_REPLY_CHARS]
        conf = conf_fn(rp.score)
        parts.append(f"[EXAMPLE {i} — confidence: {conf}]\nInbound: {inbound}\nYour reply: {reply}\n---")
    return "\n\n".join(parts)


def _precedent_summary(match: RetrievalMatch) -> dict[str, Any]:
    return {
        "source_id": match.source_id,
        "title": match.title,
        "snippet": match.snippet,
        "score": match.score,
        "reply_pair_id": match.reply_pair_id,
    }


_TONE_INSTRUCTIONS: dict[str, str] = {
    "shorter": "Be more concise. Aim for half the word count.",
    "more_formal": "Use a more formal, professional tone.",
    "more_detail": "Add more detail and context to your reply.",
    "warmer": "Use a warmer, friendlier, more personal tone.",
    "casual": "Use a casual, conversational tone — as you would with a close colleague.",
    "urgent": "Convey urgency clearly. Lead with the action needed and timeline.",
    "concise": "Be extremely concise. One to three sentences maximum.",
    "detailed": "Provide thorough, detailed explanations. Break down complex points.",
    "professional": "Maintain a polished, professional business tone throughout.",
}


_MEMORY_STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "been", "will", "your", "some",
    "they", "their", "what", "when", "where", "just", "like", "also", "into",
    "more", "here", "there", "about", "which", "would", "could", "should",
    "please", "thank", "regards", "hello", "dear", "hope", "doing", "well",
    "email", "reply", "send", "sent", "write", "writing",
})


def _extract_content_words(text: str) -> list[str]:
    """Extract significant words from text for project/topic matching."""
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    return [w for w in words if w not in _MEMORY_STOPWORDS]


def lookup_facts(
    *,
    sender: str | None,
    inbound_text: str,
    database_url: str,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Query memory table for facts relevant to this draft.

    Returns facts in three categories:
    - user_pref: always included (sign-off style, meeting times, etc.)
    - contact: facts about the sender (matched by email)
    - project: facts matched by key/tag appearing in the inbound text
    """
    _own_conn = conn is None
    if _own_conn:
        db_path = resolve_sqlite_path(database_url)
        conn = _connect(db_path)
        conn.row_factory = sqlite3.Row
    facts: list[dict[str, Any]] = []
    try:
        # 1. User preferences — always include
        rows = conn.execute(
            "SELECT type, key, fact FROM memory WHERE type = 'user_pref' ORDER BY updated_at DESC"
        ).fetchall()
        facts.extend({"type": r["type"], "key": r["key"], "fact": r["fact"]} for r in rows)

        # 2. Contact facts for sender email
        if sender:
            # ``sender`` is attacker-controlled (anyone can mail the user). Cap it
            # and bound the local part ({1,64}) so this email regex can't be
            # driven into O(n^2) backtracking by a long no-'@' value.
            s = sender.lower()[:1024]
            m = re.search(r"[\w.+-]{1,64}@[\w.-]+\.\w+", s)
            email = m.group(0) if m else s.strip()
            rows = conn.execute(
                "SELECT type, key, fact FROM memory WHERE type = 'contact' AND lower(key) = ?",
                (email,),
            ).fetchall()
            facts.extend({"type": r["type"], "key": r["key"], "fact": r["fact"]} for r in rows)

        # 3. Project facts matching keywords in inbound text
        project_rows = conn.execute(
            "SELECT type, key, fact, tags FROM memory WHERE type = 'project'"
        ).fetchall()
        if project_rows:
            inbound_lower = inbound_text.lower()
            for row in project_rows:
                key_lower = row["key"].lower()
                tags = json.loads(row["tags"]) if row["tags"] else []
                if key_lower in inbound_lower or any(t.lower() in inbound_lower for t in tags):
                    facts.append({"type": row["type"], "key": row["key"], "fact": row["fact"]})

    except Exception:
        logger.exception("lookup_facts failed for sender %r", sender)
    finally:
        if _own_conn:
            conn.close()
    return facts


def _format_facts_context(facts: list[dict[str, Any]]) -> str:
    """Format facts into a prompt block."""
    if not facts:
        return ""
    lines = ["[FACTS CONTEXT — facts about this sender and your preferences]"]
    for f in facts:
        t = f["type"]
        k = f["key"]
        v = f["fact"]
        if t == "user_pref":
            lines.append(f"- Your preference ({k}): {v}")
        elif t == "contact":
            lines.append(f"- About {k}: {v}")
        elif t == "project":
            lines.append(f"- Project ({k}): {v}")
    return "\n".join(lines)


# A specific fact the sender is asking us to supply: address, contact detail,
# price, availability, a date/time, a link. When the inbound asks for one of
# these, the model must not invent an answer — it should state only a grounded
# fact (from the inbound, thread, or FACTS CONTEXT) or else ask/defer. Pairs
# with verify-before-accept (b89), which catches inventions after the fact.
_FACT_REQUEST_PAT = re.compile(
    r"\b(address|email|e-mail|phone|number|contact|price|cost|quote|rate|fee|"
    r"available|availability|free|when|what time|which day|date|deadline|"
    r"link|url|website)\b",
    re.IGNORECASE,
)

_GROUNDING_RULE = (
    "This message asks for a specific detail. State only facts that appear in "
    "the inbound message, the thread, or the facts context above. If you don't "
    "have the requested detail, ask for it or say you'll follow up — never "
    "invent an address, date, price, link, or contact."
)

# Courtesy floor (b179, tightened b186). Baher's voice is direct and concise,
# but a small local model drafting a decline / cold-outreach rejection can tip
# from direct into rude ("your model is a copycat. No value-add.", "Pas un
# fit.", "No response.") — curt fragments that read as dismissive. This rule
# keeps the register warm + professional + courteous on the failure cases
# WITHOUT padding the common case: it (a) requires a complete, courteous reply
# even when the answer is no — acknowledge the sender, decline clearly, close
# politely — and (b) bans the curt/blunt/insulting forms by example, while
# (c) explicitly forbidding verbosity, flattery, and over-apology so normal
# replies stay tight. b186 strengthens (a) and adds the (b) examples after a
# live demo surfaced terse French/English declines. It is always present in the
# system turn (and the legacy assemble_prompt) so it generalizes rather than
# being a per-intent hack, and it does not name a language (so it never fights
# the b183 language-mirroring directive) or assert any fact (so it never fights
# the b179 grounding rule).
_COURTESY_RULE = (
    "Stay warm, professional, and courteous in every reply — including when you "
    "decline, disagree, or reject. A decline should still be a complete, polite "
    "message: briefly acknowledge the sender, state the no clearly, and close "
    "courteously (e.g. \"Thanks for reaching out — this isn't a fit for us right "
    "now, but I appreciate the note.\"). Never send curt, blunt, or dismissive "
    "fragments (e.g. \"Not a fit.\", \"No.\", \"No response.\") or anything that "
    "insults or belittles the sender. Achieve this without flattery, filler, or "
    "over-apologizing — keep your usual direct, concise register."
)

# Language mirroring (b183). A draft must answer in the SAME language as the
# inbound — a German email gets a German reply, not an English one. In a live
# demo this silently regressed: the b173 chat refactor kept a language block but
# left it gated on a positive non-English detection (``language_hint != "en"``),
# so whenever the cheap heuristic under-detected (short/informal German) the
# instruction vanished entirely and the small local model defaulted to English.
# The fix makes the instruction ALWAYS present (so a misdetect can't drop it)
# and names the language explicitly when detection is confident, because a bare
# "match the sender's language" line proved too weak for the local model — even
# a correctly-detected formal German inbound still drafted in English without an
# explicit "Reply in German." directive. Generic fallback for unknown/English.
_LANGUAGE_MIRROR_RULE = (
    "Always write your reply in the SAME language as the sender's message. "
    "Mirror the inbound language exactly; do not translate it to English."
)


def _language_instruction(language_hint: str | None) -> str:
    """Build the language-mirroring directive for the system turn (b183).

    Always returns a non-empty instruction so the directive can never be
    dropped. When ``language_hint`` resolves to a known non-English language the
    instruction names it explicitly ("Reply in German.") — the strong form the
    local model actually honors; otherwise the generic mirror rule is used.
    """
    from app.core.text_utils import language_name

    name = language_name(language_hint)
    if name and language_hint and language_hint.strip().lower() != "en":
        return f"Reply in {name}. {_LANGUAGE_MIRROR_RULE}"
    return _LANGUAGE_MIRROR_RULE


def _inbound_requests_fact(inbound: str) -> bool:
    """True when the inbound poses a question that asks for a concrete fact."""
    if not inbound or "?" not in inbound:
        return False
    return bool(_FACT_REQUEST_PAT.search(inbound))


def lookup_sender_profile(email: str, database_url: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Look up a sender profile from the database."""
    _own_conn = conn is None
    if _own_conn:
        db_path = resolve_sqlite_path(database_url)
        conn = _connect(db_path)
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
        if _own_conn:
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


def generate_subject(
    inbound_text: str,
    draft: str,
    database_url: str,
    configs_dir: Path,
    fallback_model: str | None = None,
) -> str | None:
    """Generate a subject line for the draft reply.

    ``fallback_model`` is the per-request fallback decision already resolved by
    the caller (``"none"`` under strict_local). It MUST be honored here — reading
    the global ``get_model_fallback()`` instead would ship the inbound body to
    the cloud Claude CLI during a strict-local unattended sweep when the local
    model is unavailable, breaking the never-egress invariant. Falls back to the
    global only when the caller didn't thread a decision.
    """
    # Try rule-based fallback first
    fallback = _subject_fallback(inbound_text)
    if fallback is not None:
        return fallback

    # Only use a model if fallback != 'none'
    model_fallback = fallback_model if fallback_model is not None else get_model_fallback()
    if model_fallback == "none":
        return None

    try:
        prompt = (
            "Generate a concise email subject line (under 60 chars) for this reply.\n\n"
            f"Inbound:\n{inbound_text[:500]}\n\nDraft reply:\n{draft[:500]}\n\n"
            "Output ONLY the subject line, nothing else."
        )
        # Prefer the local model so subject generation doesn't depend on the
        # Claude CLI (which was the silent failure source in the nightly job
        # and stalled draft generation by 120s on every benchmark case).
        if _local_model_available():
            result = _call_local_model(prompt, max_tokens=30, use_adapter=False)
        elif model_fallback == "claude":
            result = _call_claude_cli(prompt)
        else:
            # No local model and the resolved fallback isn't Claude — don't
            # silently egress to a backend the request didn't ask for; skip
            # the (optional) subject instead.
            return None
        # Clean up: remove quotes, "Subject:" prefix
        result = result.strip().strip('"').strip("'")
        if result.lower().startswith("subject:"):
            result = result[len("subject:") :].strip()
        return result[:80] if result else None
    except Exception:
        logger.warning("Subject generation failed", exc_info=True)
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


# sender_type values (app/core/sender.py) vs persona ``modes`` keys: a sender is
# "external_client" but the persona mode/pattern is conventionally "client".
# Look mail up under both so either naming resolves.
_PERSONA_TYPE_ALIASES: dict[str, str] = {"external_client": "client"}


def _persona_type_keys(sender_type: str | None) -> list[str]:
    keys = [sender_type] if sender_type else []
    alias = _PERSONA_TYPE_ALIASES.get(sender_type or "")
    if alias:
        keys.append(alias)
    return keys


def _apply_name(template: str, name: str | None) -> str:
    """Fill ``{name}`` / ``[name]`` with ``name``; if no name, collapse the
    placeholder (and its leading space) so we never emit "Hi ," with a dangling
    space before the punctuation."""
    if not template:
        return template
    out = template
    for ph in ("{name}", "[name]"):
        if ph in out:
            out = out.replace(ph, name) if name else out.replace(" " + ph, "").replace(ph, "")
    return out.replace("  ", " ")


def _signoff_name() -> str | None:
    """The user's own name for a closing's ``{name}`` (e.g. "Best, Baher")."""
    try:
        from app.core.config import load_config

        return ((load_config() or {}).get("user", {}).get("name") or "").strip() or None
    except Exception:
        return None


def _resolve_greeting(persona: dict[str, Any], sender_type: str | None, first_name: str | None = None) -> str:
    """Resolve greeting: explicit mode greeting > greeting_patterns > default >
    the flat ``greeting_style`` fallback. The flat fallback is skipped for
    ``internal`` mail — the user doesn't greet colleagues (b222). Explicitly
    configured internal greetings are still honored."""
    modes = persona.get("modes") or {}
    patterns = persona.get("greeting_patterns") or {}
    greeting = ""
    for k in _persona_type_keys(sender_type):
        m = modes.get(k)
        if isinstance(m, dict) and m.get("greeting"):
            greeting = m["greeting"]
            break
    if not greeting:
        for k in _persona_type_keys(sender_type):
            if patterns.get(k):
                greeting = patterns[k]
                break
    if not greeting and patterns.get("default"):
        greeting = patterns["default"]
    if not greeting and sender_type != "internal" and persona.get("greeting_style"):
        greeting = persona["greeting_style"]
    return _apply_name(greeting, first_name)


def _resolve_closing(persona: dict[str, Any], sender_type: str | None) -> str:
    """Resolve closing: explicit mode closing > closing_patterns > default > the
    flat ``closing_informal`` (personal) / ``closing_formal`` fallback. The flat
    fallback is skipped for ``internal`` mail (b222). ``{name}`` is filled with
    the user's own sign-off name."""
    modes = persona.get("modes") or {}
    patterns = persona.get("closing_patterns") or {}
    closing = ""
    for k in _persona_type_keys(sender_type):
        m = modes.get(k)
        if isinstance(m, dict) and m.get("closing"):
            closing = m["closing"]
            break
    if not closing:
        for k in _persona_type_keys(sender_type):
            if patterns.get(k):
                closing = patterns[k]
                break
    if not closing and patterns.get("default"):
        closing = patterns["default"]
    if not closing and sender_type != "internal":
        if sender_type == "personal" and persona.get("closing_informal"):
            closing = persona["closing_informal"]
        elif persona.get("closing_formal"):
            closing = persona["closing_formal"]
        elif persona.get("closing_informal"):
            closing = persona["closing_informal"]
    if closing and ("{name}" in closing or "[name]" in closing):
        closing = _apply_name(closing, _signoff_name())
    return closing


# --- Post-generation repair -------------------------------------------------
# The model output is returned with only an emptiness check today. This pass
# optionally repairs the draft and always annotates its length. Mutations are
# OFF by default (behavior-preserving) — flip them on per instance once their
# effect has been verified against real drafts.

_GREETING_TOKENS = (
    "hi ", "hi,", "hey", "hello", "dear", "good morning", "good afternoon", "good evening",
    # German (b231: "Liebe Amina" wasn't recognized, so the repair pass
    # prepended "Hey," on top — a double greeting on every German draft)
    "liebe ", "lieber ", "hallo", "sehr geehrte", "guten morgen", "guten tag",
    "guten abend", "servus", "moin", "grüß ",
    # French / Spanish / Italian
    "bonjour", "bonsoir", "salut ", "cher ", "chère ", "hola", "buenos días",
    "buenas tardes", "querido", "querida", "ciao", "gentile ", "caro ", "cara ",
    # Arabic
    "مرحبا", "أهلا", "اهلا", "عزيزي", "عزيزتي", "السلام عليكم",
)
_CLOSING_TOKENS = (
    "best", "cheers", "regards", "kind regards", "thanks", "thank you",
    "sincerely", "talk soon", "warmly", "all the best", "speak soon",
    # German (matched as substrings of the last 3 lines — only multi-word /
    # unambiguous forms; "lg"/"mfg" abbreviations would false-match inside
    # ordinary words)
    "mit freundlichen grüßen", "mit besten grüßen", "freundliche grüße",
    "viele grüße", "liebe grüße", "beste grüße", "herzliche grüße",
    "schöne grüße", "mit lieben grüßen",
    # French / Spanish / Italian
    "cordialement", "amicalement", "bien à vous", "à bientôt",
    "saludos", "un abrazo", "un saludo", "gracias", "cordiali saluti",
    "un saluto", "a presto", "grazie",
    # Arabic
    "مع التحية", "تحياتي", "شكرا", "مع خالص التحية",
)


def _get_repair_config() -> dict[str, bool]:
    """Read ``generation.repair`` flags.

    Defaults reflect intent: the three artifact-removal repairs (signature,
    quote tail, HTML entities) are objectively-correct cleanups and default
    True — they catch training-data leakage the model emits but the user
    never wants. ``enforce_greeting_closing`` defaults False because it
    *adds* content the model didn't produce; safer as opt-in.
    """
    from app.core.config import load_config

    cfg = load_config() or {}
    gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
    rep = gen.get("repair", {}) if isinstance(gen, dict) else {}
    rep = rep if isinstance(rep, dict) else {}
    return {
        "enforce_greeting_closing": bool(rep.get("enforce_greeting_closing", False)),
        "strip_trailing_signature": bool(rep.get("strip_trailing_signature", True)),
        "strip_quote_tail": bool(rep.get("strip_quote_tail", True)),
        "decode_html_entities": bool(rep.get("decode_html_entities", True)),
    }


def _draft_has_greeting(text: str, greeting: str) -> bool:
    first = text.lstrip().split("\n", 1)[0].strip().lower()
    if not first:
        return False
    g = greeting.strip().lower().rstrip(",")
    if g and first.startswith(g.split()[0]):
        return True
    return any(first.startswith(tok) for tok in _GREETING_TOKENS)


def _draft_has_closing(text: str, closing: str) -> bool:
    tail = "\n".join(text.rstrip().splitlines()[-3:]).lower()
    if not tail:
        return False
    c_first = closing.strip().lower().split("\n", 1)[0].split(",", 1)[0].strip()
    if c_first and c_first in tail:
        return True
    return any(tok in tail for tok in _CLOSING_TOKENS)


# b187: length CONTROL. The ok-band is the [low, high] word window a draft must
# land in to count "ok". We derive it from the persona's reply-length
# distribution when it carries percentiles (avg_reply_words_p25 / _p75 — the
# tighter, data-grounded band), otherwise from a multiplicative spread around
# the average (avg_reply_words). The SAME band is the single source of truth for
# the length flag, the candidate length-fit term, and the band-derived token
# budget — so "ok"/control/ranking never disagree.
_BAND_LOW_FACTOR = 0.6
_BAND_HIGH_FACTOR = 1.4


def _length_band(
    target_words: int | None,
    *,
    p25: int | None = None,
    p75: int | None = None,
) -> tuple[int, int] | None:
    """Derive the persona ok-band ``(low, high)`` in words.

    Prefers the persona percentiles (p25–p75) when both are present and sane —
    a tighter, data-grounded window than a flat multiple of the average.
    Otherwise falls back to ``[0.6*avg, 1.4*avg]`` around ``target_words``.
    Returns None when there's no usable target.
    """
    if p25 is not None and p75 is not None:
        try:
            lo, hi = int(p25), int(p75)
        except (TypeError, ValueError):
            lo = hi = 0
        if 0 < lo <= hi:
            return max(1, lo), hi
    if not target_words or target_words <= 0:
        return None
    lo = max(1, int(round(target_words * _BAND_LOW_FACTOR)))
    hi = max(lo, int(round(target_words * _BAND_HIGH_FACTOR)))
    return lo, hi


def _length_guidance_line(
    avg_words: int,
    *,
    p25: int | None = None,
    p75: int | None = None,
) -> str:
    """Firmer prompt length guidance (b187): an explicit target plus a soft
    UPPER bound at the ok-band's high edge.

    Replaces the vague "~N words. Be concise." with "Aim for ~N words; keep it
    under M." where M is the band's high edge — an explicit ceiling the model
    can honor, which is what actually moves drafts off the "long" tail. The
    high edge is the SAME one the token budget caps at, so prompt and budget
    agree. Deterministic given the persona, so eval reproducibility holds."""
    band = _length_band(avg_words, p25=p25, p75=p75)
    if band is None:
        return f"\nTarget length: ~{avg_words} words. Be concise.\n"
    low, high = band
    # Two-sided target: an explicit center plus BOTH edges. Naming the lower
    # edge stops the model from over-shrinking on the "be concise" cue (which,
    # on a terse model, drove drafts below the band → "short"); naming the
    # upper edge is the soft ceiling that trims the "long" tail. Phrased as a
    # range so neither edge dominates.
    return (
        f"\nTarget length: about {avg_words} words "
        f"(stay within {low}–{high} words — not shorter, not longer).\n"
    )


def _length_flag(
    text: str,
    target_words: int | None,
    *,
    p25: int | None = None,
    p75: int | None = None,
) -> str | None:
    """Non-mutating length annotation relative to the persona ok-band.

    Uses the SAME band as control/ranking (``_length_band``): below the low
    edge is "short", above the high edge is "long", inside is "ok". A draft is
    off-target iff the flag is "long" or "short"."""
    band = _length_band(target_words, p25=p25, p75=p75)
    if band is None:
        return None
    n = len(text.split())
    if n == 0:
        return None
    low, high = band
    if n > high:
        return "long"
    if n < low:
        return "short"
    return "ok"


def _repair_draft(
    draft: str,
    *,
    greeting: str,
    closing: str,
    target_words: int | None,
    config: dict[str, bool],
    p25: int | None = None,
    p75: int | None = None,
) -> tuple[str, list[str], str | None]:
    """Optionally repair a draft; always return its length flag.

    Returns ``(text, repairs_applied, length_flag)``. Placeholder/error drafts
    (``[...]``) are left untouched.
    """
    repairs: list[str] = []
    text = draft
    stripped_all = text.strip()
    if stripped_all.startswith("[") and stripped_all.endswith("]"):
        return text, repairs, None

    # Order: quote-tail first (it can sit *before* a signature in the output,
    # so stripping it first leaves the signature pass with a smaller, cleaner
    # substring to work on). HTML decode is order-independent and runs last.
    if config.get("strip_quote_tail"):
        stripped = strip_quote_tail(text)
        if stripped and stripped != text.rstrip():
            text = stripped
            repairs.append("stripped_quote_tail")

    if config.get("strip_trailing_signature"):
        # Two passes: first drop contact details / role lines (strip_signature),
        # then drop a trailing "Baher Al Hakim" suffix that strip_signature
        # misses because it isn't at line-start. Same logic as the exemplar
        # path — the LoRA emits the same trailing-name artifact on its output.
        stripped = _strip_trailing_user_name(strip_signature(text)).rstrip()
        if stripped and stripped != text.rstrip():
            text = stripped
            repairs.append("stripped_trailing_signature")

    if config.get("decode_html_entities"):
        decoded = decode_html_entities(text)
        if decoded != text:
            text = decoded
            repairs.append("decoded_html_entities")

    if config.get("enforce_greeting_closing"):
        # b231: the configured persona greeting/closing are English. Prepending
        # "Hey," to a German draft (or "Best," under "Mit freundlichen Grüßen")
        # is worse than no greeting — skip the additions for non-English drafts
        # and let the model's own in-language greeting stand.
        draft_is_english = True
        try:
            from app.core.text_utils import detect_language

            draft_is_english = detect_language(text) == "en"
        except Exception:
            pass
        if draft_is_english:
            if greeting and not _draft_has_greeting(text, greeting):
                text = f"{greeting}\n\n{text.lstrip()}"
                repairs.append("added_greeting")
            if closing and not _draft_has_closing(text, closing):
                text = f"{text.rstrip()}\n\n{closing}"
                repairs.append("added_closing")

    return text, repairs, _length_flag(text, target_words, p25=p25, p75=p75)


def _draft_logging_enabled() -> bool:
    """``generation.log_drafts`` — default True (append-only local signal log)."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
        if isinstance(gen, dict):
            return gen.get("log_drafts", True) is not False
    except Exception:
        logger.debug("Could not read generation.log_drafts", exc_info=True)
    return True


def _log_draft_event(
    database_url: str,
    *,
    inbound_text: str,
    draft: str,
    account_email: str | None,
    sender: str | None,
    sender_type: str | None,
    detected_mode: str | None,
    intent: str | None,
    confidence: str | None,
    confidence_reason: str | None,
    model_used: str | None,
    retrieval_method: str | None,
    exemplar_ids: list[str],
    length_flag: str | None,
) -> bool:
    """Append one row to ``draft_events``. Never raises — logging a draft must
    not break drafting. Returns True if a row was written.

    Self-heals the table (``CREATE TABLE IF NOT EXISTS``) so it works on an
    instance whose DB predates this table without requiring a bootstrap.
    """
    if not _draft_logging_enabled():
        return False
    try:
        db_path = resolve_sqlite_path(database_url)
        conn = _connect(db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS draft_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL,
                    account_email TEXT, sender TEXT, sender_type TEXT,
                    detected_mode TEXT, intent TEXT, confidence TEXT,
                    confidence_reason TEXT, model_used TEXT, retrieval_method TEXT,
                    exemplar_ids TEXT NOT NULL DEFAULT '[]', length_flag TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """INSERT INTO draft_events
                   (inbound_text, generated_draft, account_email, sender, sender_type,
                    detected_mode, intent, confidence, confidence_reason, model_used,
                    retrieval_method, exemplar_ids, length_flag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    inbound_text, draft, account_email, sender, sender_type, detected_mode,
                    intent, confidence, confidence_reason, model_used, retrieval_method,
                    json.dumps([str(i) for i in (exemplar_ids or [])]), length_flag,
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        logger.warning("Failed to log draft event", exc_info=True)
        return False


EOS_TOKEN = "<|im_end|>"


def _chat_stop_sequences(stop: list[str] | None = None) -> list[str]:
    """Decode-time stop sequences for the local chat model (b173).

    ``<|im_end|>`` is the ChatML end-of-turn token the adapter was trained to
    emit; stopping there is the primary fix for the run-on bracket documents.
    The remaining guards (``\n[``, ``\nSubject:``, ``\n---``) are
    belt-and-suspenders against a base-without-adapter slipping a
    document-shaped continuation through.
    """
    seqs = [EOS_TOKEN, "\n[", "\nSubject:", "\n---"]
    for x in stop or []:
        if x and x not in seqs:
            seqs.append(x)
    return seqs


def _split_chat_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
    """Return ``(system_text, user_text)`` from a ``[system, user]`` list."""
    system_text = ""
    user_text = ""
    for m in messages or []:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            system_text = content
        elif role == "user":
            user_text = content
    return system_text, user_text


def assemble_chat_messages(
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
    memory_facts: list[dict[str, Any]] | None = None,
    score_stats: dict[str, float] | None = None,
    subject: str | None = None,
    user_prompt: str | None = None,
    extra_constraint: str | None = None,
) -> list[dict[str, str]]:
    """Build ChatML ``[{system}, {user}]`` for the local chat model (b173).

    Mirrors how the fine-tuning corpus was built
    (scripts/export_feedback_jsonl.build_record: a system turn carrying the
    persona/style/grounding/language and a user turn carrying the inbound
    message). Inference now uses the SAME shape the adapter was trained on, so
    the model emits a bare reply and halts at ``<|im_end|>`` instead of
    mimicking the old ``[SYSTEM]/[EXEMPLARS]/[EXAMPLE i]/[REPLY]`` bracket
    document and never stopping.

    The persona/style/context information is identical to ``assemble_prompt``
    but expressed as plain system-message prose (no bracket markers). The
    per-exemplar ``Inbound:/Your reply:/---`` scaffold is intentionally DROPPED
    from the prompt: training had no exemplars in the messages, so omitting
    them makes inference more like training, and the adapter already encodes
    the user's voice. The untrusted inbound is the user turn (markers there are
    still neutralized so they can't inject a fake instruction block).
    """
    system_text = _assemble_system_text(
        persona=persona,
        prompts=prompts,
        detected_mode=detected_mode,
        audience_hint=audience_hint,
        tone_hint=tone_hint,
        sender_context=sender_context,
        language_hint=language_hint,
        intent_hint=intent_hint,
        sender_type=sender_type,
        first_name=first_name,
        memory_facts=memory_facts,
        user_prompt=user_prompt,
        extra_constraint=extra_constraint,
        inbound_message=inbound_message,
        include_exemplar_hint=bool(reply_pairs),
    )
    user_text = neutralize_prompt_markers(inbound_message)
    if subject:
        user_text = f"Subject: {neutralize_prompt_markers(subject)}\n\n{user_text}"
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def _assemble_system_text(
    *,
    persona: dict[str, Any],
    prompts: dict[str, str],
    detected_mode: str | None,
    audience_hint: str | None,
    tone_hint: str | None,
    sender_context: str | None,
    language_hint: str | None,
    intent_hint: str | None,
    sender_type: str | None,
    first_name: str | None,
    memory_facts: list[dict[str, Any]] | None,
    user_prompt: str | None,
    extra_constraint: str | None,
    inbound_message: str,
    include_exemplar_hint: bool,
) -> str:
    """Persona/style/grounding/task as plain prose (no bracket scaffold) for the
    ChatML system turn. Same information content as ``assemble_prompt`` minus
    the bracket markers and the per-exemplar block."""
    style = persona.get("style", {})
    voice = style.get("voice", "direct, clear, pragmatic")
    avg_words = style.get("avg_reply_words")
    constraints = style.get("constraints", [])
    if intent_hint and intent_hint != "general":
        intent_avg = style.get("intent_avg_words", {})
        if isinstance(intent_avg, dict) and intent_hint in intent_avg:
            avg_words = intent_avg[intent_hint]
    system = prompts.get(
        "system_prompt",
        "You are YouOS, a local-first email copilot.",
    )
    _suffix = prompts.get("system_prompt_suffix", "")
    if _suffix and _suffix.strip():
        system = f"{system.rstrip()}\n{_suffix.strip()}"

    persona_lines: list[str] = [f"Persona: {voice}."]
    if avg_words:
        persona_lines.append(f"Target reply length: ~{avg_words} words.")
    for c in constraints:
        persona_lines.append(f"- {c}")
    bullet_pct = style.get("bullet_point_pct")
    if bullet_pct is not None and bullet_pct > 0.4:
        persona_lines.append("- prefer bullet points for multi-item responses")
    directness = style.get("directness_score")
    if directness is not None and directness > 0.8:
        persona_lines.append("- be direct, avoid hedging")
    avg_para = style.get("avg_paragraphs")
    if avg_para is not None and avg_para > 2:
        persona_lines.append("- use clear paragraph breaks")
    if extra_constraint:
        persona_lines.append(f"- {extra_constraint}")
    persona_block = "\n".join(persona_lines)

    style_anchor_block = ""
    if sender_type:
        style_anchor = get_persona_style_anchor(sender_type)
        if style_anchor:
            style_anchor_block = f"\nStyle anchor ({sender_type}):\n{style_anchor.strip()}\n"

    context_lines: list[str] = []
    if detected_mode:
        context_lines.append(f"Detected mode: {detected_mode}")
    if audience_hint:
        context_lines.append(f"Audience: {audience_hint}")
    if intent_hint and intent_hint != "general":
        context_lines.append(f"Email intent: {intent_hint}")
    if _has_thread_context(inbound_message):
        context_lines.append(
            "Note: This inbound message contains a multi-message thread. "
            "Consider the full conversation context when drafting your reply."
        )
    context_block = ""
    if context_lines:
        context_block = "\n" + "\n".join(context_lines) + "\n"

    tone_instruction = ""
    if tone_hint:
        if tone_hint in _TONE_INSTRUCTIONS:
            tone_instruction = f"\n{_TONE_INSTRUCTIONS[tone_hint]}\n"
        else:
            tone_instruction = f"\nTone guidance: {tone_hint}\n"

    custom_instruction = ""
    if user_prompt and user_prompt.strip():
        custom_instruction = f"\nAdditional user instruction: {user_prompt.strip()}\n"

    sender_block = ""
    if sender_context:
        sender_block = f"\n{sender_context}\n"

    facts_block = ""
    if memory_facts:
        facts_block = f"\n{_format_facts_context(memory_facts)}\n"

    # Language mirroring (b183): ALWAYS present so a heuristic mis-detect can't
    # drop the directive (the live German→English regression). Names the
    # language when detection is confident.
    language_block = f"\n{_language_instruction(language_hint)}\n"

    grounding_block = ""
    if _inbound_requests_fact(inbound_message):
        grounding_block = f"\n{_GROUNDING_RULE}\n"

    courtesy_block = f"\n{_COURTESY_RULE}\n"

    style_hint = ""
    if include_exemplar_hint:
        style_hint = (
            "\nWrite in your own established voice and tone "
            "(already learned from your past replies).\n"
        )

    result = (
        f"{system.strip()}\n"
        f"{persona_block}\n"
        f"{context_block}"
        f"{style_anchor_block}"
        f"{sender_block}"
        f"{facts_block}"
        f"{language_block}"
        f"{grounding_block}"
        f"{courtesy_block}"
        f"{style_hint}"
        f"\n"
        f"Draft a reply to the inbound message below in your style.\n"
        f"Output the draft reply text only. No preamble, no explanation.\n"
        f"{tone_instruction}"
        f"{custom_instruction}"
    )
    if avg_words:
        result += _length_guidance_line(
            avg_words,
            p25=style.get("avg_reply_words_p25"),
            p75=style.get("avg_reply_words_p75"),
        )
    greeting = _resolve_greeting(persona, sender_type, first_name)
    closing = _resolve_closing(persona, sender_type)
    if greeting and closing:
        result += f"\nBegin your reply with: {greeting}\nEnd your reply with: {closing}\n"
    return result.strip()


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
    memory_facts: list[dict[str, Any]] | None = None,
    score_stats: dict[str, float] | None = None,
    subject: str | None = None,
    user_prompt: str | None = None,
    extra_constraint: str | None = None,
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
    # Optional tunable instruction appended to the system prompt. Kept separate
    # from `system_prompt` so autoresearch can A/B drafting-style instructions
    # WITHOUT clobbering the instance's persona/brand system prompt. Default
    # empty = no change. (The old `drafting_prompt` key was mutated by
    # autoresearch but read by nothing — this is the consumed replacement.)
    _suffix = prompts.get("system_prompt_suffix", "")
    if _suffix and _suffix.strip():
        system = f"{system.rstrip()}\n{_suffix.strip()}"

    exemplars_text = _format_exemplars(reply_pairs, score_stats=score_stats)
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

    # Per-draft constraint (e.g. cold-outreach decline nudge — see
    # app.core.cold_outreach.DECLINE_NUDGE). Joins the persona block as a
    # final line so the model sees it alongside the other constraints.
    if extra_constraint:
        persona_lines.append(f"- {extra_constraint}")

    persona_block = "\n".join(persona_lines)

    style_anchor_block = ""
    if sender_type:
        style_anchor = get_persona_style_anchor(sender_type)
        if style_anchor:
            style_anchor_block = f"\n[STYLE ANCHOR — {sender_type}]\n{style_anchor.strip()}\n"

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

    # Build tone instruction — known keys map to instructions, free-text injected directly
    tone_instruction = ""
    if tone_hint:
        if tone_hint in _TONE_INSTRUCTIONS:
            tone_instruction = f"\n{_TONE_INSTRUCTIONS[tone_hint]}\n"
        else:
            tone_instruction = f"\nTone guidance: {tone_hint}\n"

    custom_instruction = ""
    if user_prompt and user_prompt.strip():
        custom_instruction = f"\nAdditional user instruction: {user_prompt.strip()}\n"

    sender_block = ""
    if sender_context:
        sender_block = f"\n{sender_context}\n"

    facts_block = ""
    if memory_facts:
        facts_block = f"\n{_format_facts_context(memory_facts)}\n"

    # Language mirroring (b183): always emit, naming the language when known.
    # Mirrors the chat-path logic so the flat-text fallback (ollama/claude) gets
    # the same directive instead of silently dropping it under "en".
    language_block = f"\n[LANGUAGE] {_language_instruction(language_hint)}\n"

    # Fact-grounding guard — only when the inbound actually asks for a concrete
    # detail, so the common case keeps the unchanged prompt (minimises any
    # golden-eval drift) and the guard appears exactly where invention is a risk.
    grounding_block = ""
    if _inbound_requests_fact(inbound_message):
        grounding_block = f"\n[GROUNDING] {_GROUNDING_RULE}\n"

    courtesy_block = f"\n[COURTESY] {_COURTESY_RULE}\n"

    result = (
        f"[SYSTEM]\n"
        f"{system.strip()}\n"
        f"{persona_block}\n"
        f"{context_block}"
        f"{style_anchor_block}"
        f"{sender_block}"
        f"{facts_block}"
        f"{language_block}"
        f"{grounding_block}"
        f"{courtesy_block}"
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
        f"{custom_instruction}"
    )

    # Append length guidance if avg_reply_words is set
    if avg_words:
        result += _length_guidance_line(
            avg_words,
            p25=style.get("avg_reply_words_p25"),
            p75=style.get("avg_reply_words_p75"),
        )

    # Greeting/closing injection
    greeting = _resolve_greeting(persona, sender_type, first_name)
    closing = _resolve_closing(persona, sender_type)
    if greeting and closing:
        result += f"\nBegin your reply with: {greeting}\nEnd your reply with: {closing}\n"

    # Defang attacker-forged section markers in the untrusted inbound + subject
    # so they can't inject a competing [TASK]/[SYSTEM] instruction block.
    inbound_section = neutralize_prompt_markers(inbound_message)
    if subject:
        inbound_section = f"Subject: {neutralize_prompt_markers(subject)}\n\n{inbound_section}"
    result += f"\n[INBOUND MESSAGE]\n{inbound_section}"
    return result


PROMPT_TOKEN_BUDGET: int = 2000
EXEMPLAR_REPLY_CHARS: int = 600
EXEMPLAR_INBOUND_CHARS: int = 400
PRIOR_REPLY_CHARS: int = 200
SUBPROCESS_TIMEOUT: int = 120

# Fixed seed for eval/golden generations (b166). Any constant works; it only has
# to be stable so re-scoring the same config produces the same composite.
EVAL_SEED = 1234

# b188: shared per-draft quality floor. This is the SAME 0–1 number the
# autonomous auto-push path gates on (``agent.auto_push.quality_floor`` default —
# see app/agent/triage.py:_auto_push_config, which reuses this constant). The
# abstain gate (below) defaults its threshold to this value so "good enough to
# auto-draft" and "good enough to present as a ready reply" are one policy, not
# two drifting numbers.
DEFAULT_QUALITY_FLOOR = 0.5


def _estimate_tokens(text: str) -> int:
    """Estimate token count using a simple word-count * 1.4 approximation."""
    return int(len(text.split()) * 1.4)


def _resolve_decoding(intent: str | None, confidence: str | None) -> tuple[float | None, float | None]:
    """Resolve ``(temperature, top_p)`` from ``generation.decoding`` config.

    Returns ``(None, None)`` when nothing is configured — preserving each
    backend's current default (MLX: no ``--temp``/``--top-p``; Ollama: 0.7).
    Surfacing these (like the retrieval weights) is the precondition for the
    autoresearch loop to A/B-tune fidelity vs. variety. Supports a per-intent
    temperature override (``intent_temperature``) and a per-confidence delta
    (``high_confidence_temperature_delta`` / ``low_confidence_temperature_delta``),
    e.g. lower temperature when retrieval is high-confidence.
    """
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
        dec = gen.get("decoding", {}) if isinstance(gen, dict) else {}
        if not isinstance(dec, dict) or not dec:
            return None, None

        temp = dec.get("temperature")
        intent_temps = dec.get("intent_temperature", {})
        if intent and isinstance(intent_temps, dict) and intent in intent_temps:
            temp = intent_temps[intent]

        if temp is not None and confidence:
            delta_key = {
                "high": "high_confidence_temperature_delta",
                "low": "low_confidence_temperature_delta",
            }.get(confidence)
            if delta_key and delta_key in dec:
                try:
                    temp = max(0.0, float(temp) + float(dec[delta_key]))
                except (TypeError, ValueError):
                    pass

        top_p = dec.get("top_p")
        temp_f = float(temp) if temp is not None else None
        top_p_f = float(top_p) if top_p is not None else None
        return temp_f, top_p_f
    except Exception:
        logger.debug("Could not resolve generation.decoding", exc_info=True)
        return None, None


# --- Multi-candidate generation + ranking -----------------------------------
# Optionally generate several drafts with DIVERSE sampling and return the best
# by the per-draft quality score (draft_quality_score — voice fidelity +
# structure, with verify folded in). Off by default — it multiplies model calls
# (n× tokens; the warm server amortizes load but not generation). Hard-inert on
# the deterministic/eval/background path (see generate_draft): exactly ONE
# greedy candidate there, byte-identical to single-candidate drafting.

# Default temperature spread for n candidates when no explicit list is given.
# A spread (not a single repeated temperature + varied seed) is what makes the
# candidates actually differ; greedy temp=0 would make them identical. The
# spread is clamped to [0.3, 1.0] and is stable for a given n so the candidate
# set is reproducible at the config level.
def _diverse_temperatures(n: int) -> list[float]:
    if n <= 1:
        return [0.7]
    lo, hi = 0.3, 1.0
    step = (hi - lo) / (n - 1)
    return [round(lo + i * step, 3) for i in range(n)]


def _multi_candidate_config() -> dict[str, Any]:
    """Read ``generation.multi_candidate`` (default OFF / single candidate).

    Canonical knob is ``generation.multi_candidate.n`` (b186): the number of
    candidates to generate and rank. ``n <= 1`` (the default) is single-candidate
    drafting — exactly today's behavior and latency. ``n > 1`` fans out a diverse
    temperature spread and keeps the highest-scoring draft.

    Back-compat: the legacy ``enabled: true`` / ``temperatures: [...]`` shape
    (b172/PR-D) is still honored. When ``enabled`` is set without ``n``, ``n``
    is the length of the temperature list (default spread = 3). An explicit
    ``temperatures`` list always wins over the ``n``-derived spread.

    Returns ``{"enabled", "n", "temperatures"}``; ``enabled`` is True iff
    ``n > 1`` (so >1 candidate will actually be generated).
    """
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
        mc = gen.get("multi_candidate", {}) if isinstance(gen, dict) else {}
        mc = mc if isinstance(mc, dict) else {}

        explicit_temps = mc.get("temperatures")
        has_temps = isinstance(explicit_temps, list) and bool(explicit_temps)

        # Resolve n: explicit n wins; else legacy enabled+temperatures implies
        # len(temperatures); else legacy enabled alone implies the default 3;
        # else single-candidate (n=1).
        n_raw = mc.get("n")
        if n_raw is not None:
            n = max(1, int(n_raw))
        elif bool(mc.get("enabled", False)):
            n = len(explicit_temps) if has_temps else 3
        else:
            n = 1

        if has_temps:
            temps = [float(t) for t in explicit_temps]
        else:
            temps = _diverse_temperatures(n)

        return {"enabled": n > 1, "n": n, "temperatures": temps}
    except Exception:
        logger.debug("Could not read generation.multi_candidate", exc_info=True)
        return {"enabled": False, "n": 1, "temperatures": [0.7]}


def _is_usable_draft(text: str) -> bool:
    """Mirror the empty/signature-only check used by the fallback path."""
    non_ws = len(text.replace(" ", "").replace("\n", "").replace("\t", ""))
    if non_ws < 10:
        return False
    stripped_all = text.strip()
    if stripped_all.startswith("[") and stripped_all.endswith("]"):
        return False
    stripped = strip_signature(text).strip()
    return not (len(stripped) < 15 and non_ws > 0)


def _score_candidate(
    text: str,
    *,
    target_words: int | None,
    greeting: str,
    closing: str,
    p25: int | None = None,
    p75: int | None = None,
) -> float:
    """Deterministic candidate score (higher is better).

    Unusable (empty/placeholder/signature-only) drafts are disqualified.
    Otherwise: length-fit (peaks at the persona target) plus credit for
    honoring the persona greeting/closing when those are configured.

    b187: length-fit is band-aware — an in-band draft (``_length_flag`` "ok",
    against the SAME band used for control/ranking) earns the +0.5 bonus, and a
    draft that falls outside the band ("long"/"short") is additionally
    penalized so a well-sized candidate is preferred over an off-target one of
    equal voice/structure.
    """
    if not _is_usable_draft(text):
        return float("-inf")
    score = 0.0
    if target_words and target_words > 0:
        ratio = len(text.split()) / target_words
        score += max(0.0, 1.0 - abs(ratio - 1.0))  # 1.0 at exact match, →0 as it drifts
        flag = _length_flag(text, target_words, p25=p25, p75=p75)
        if flag == "ok":
            score += 0.5
        elif flag in ("long", "short"):
            score -= 0.5  # out-of-band penalty: prefer a same-quality in-band draft
    if greeting:
        score += 0.5 if _draft_has_greeting(text, greeting) else 0.0
    if closing:
        score += 0.5 if _draft_has_closing(text, closing) else 0.0
    return score


# Voice fidelity is the signal the product is built on, so when exemplars are
# available it dominates candidate selection — comparable in magnitude to the
# structural terms (length-fit + greeting/closing ≈ 0–2). Previously the multi-
# candidate path ranked on length+signoff alone, which is orthogonal to "sounds
# like me" and could discard the most voice-faithful draft for running long.
_VOICE_WEIGHT = 2.0


# Bare-acknowledgment phrases — a draft that's just one of these (short, no
# question, no concrete content) is low-value and must not auto-act. This is
# the mechanical cause of the live "thanks, I'll check it out" false positives.
_GENERIC_ACK_PHRASES = (
    "thanks for the update", "thank you for the update", "thanks for letting me know",
    "i'll check it out", "i will check it out", "got it, thanks", "got it thanks",
    "sounds good, thanks", "sounds good", "will do", "noted, thanks", "noted thanks",
    "great, thanks", "thanks!", "thank you!", "ok, thanks", "okay, thanks",
)


def _is_generic_ack(text: str) -> bool:
    """True if the draft is essentially a contentless acknowledgement.

    Not mere containment — a real reply may *open* with "thanks for the update"
    and then commit to something concrete. We test *dominance*: no question,
    and once the ack phrase(s) are stripped almost nothing of substance is left.
    These are the live newsletter false positives ("thanks, I'll check it out")
    and shouldn't be auto-acted."""
    t = strip_signature(text or "").strip().lower()
    if not t or "?" in t:
        return False
    matched = [p for p in _GENERIC_ACK_PHRASES if p in t]
    if not matched:
        return False
    residual = t
    for p in matched:
        residual = residual.replace(p, " ")
    # Words of real substance left after the ack phrase(s) are removed.
    content = [w for w in residual.split() if any(c.isalpha() for c in w)]
    return len(content) <= 3


def draft_quality_score(
    draft: str,
    *,
    reply_pairs: list[Any] | None,
    target_words: int | None,
    greeting: str = "",
    closing: str = "",
    model_used: str | None = None,
    empty_output_retried: bool = False,
    p25: int | None = None,
    p75: int | None = None,
) -> float:
    """A 0–1 estimate of whether THIS draft is good enough to act on.

    Blends voice fidelity (averaged ``voice_match`` against the user's top
    retrieved replies — deterministic, ~0 extra cost) with structural fit
    (length + greeting/closing, via ``_score_candidate``). Collapses to ~0 for
    unusable or generic-acknowledgement drafts, and is discounted for
    empty-output retries and non-LoRA (cloud/base) fallbacks. This is what
    auto-push / auto-send should gate on — not the needs-reply score."""
    if not _is_usable_draft(draft):
        return 0.0
    struct = _score_candidate(
        draft, target_words=target_words, greeting=greeting, closing=closing,
        p25=p25, p75=p75,
    )
    if struct == float("-inf"):
        return 0.0
    struct_norm = max(0.0, min(1.0, struct / 2.0))  # _score_candidate maxes ~2.0–2.5

    refs = [
        rp.reply_text for rp in (reply_pairs or [])[:3]
        if getattr(rp, "reply_text", None)
    ]
    voice: float | None = None
    if refs:
        from app.evaluation.voice_match import voice_match_score

        vals: list[float] = []
        for r in refs:
            try:
                vals.append(float(voice_match_score(draft, r)["voice_match"]))
            except Exception:
                continue
        if vals:
            voice = sum(vals) / len(vals)

    q = (0.6 * voice + 0.4 * struct_norm) if voice is not None else struct_norm
    if _is_generic_ack(draft):
        q = min(q, 0.15)
    if empty_output_retried:
        q *= 0.7
    if model_used and "lora" not in model_used.lower():
        q *= 0.85  # cloud/base fallback: less likely to match the user's voice
    return round(max(0.0, min(1.0, q)), 3)


def _rank_candidates(
    raw: list[tuple[str, str, float | None]],
    *,
    target_words: int | None,
    greeting: str,
    closing: str,
    exemplar_replies: list[str] | None = None,
    embed_fn: Any = None,
    p25: int | None = None,
    p75: int | None = None,
) -> list[dict[str, Any]]:
    """Score and sort candidates best-first.

    ``raw`` is a list of ``(draft, model_used, temperature)``. When
    ``exemplar_replies`` (the user's real retrieved replies) are given, each
    candidate also gets a voice-match score averaged across the top few
    exemplars — measuring how much it sounds like the user, not just whether it
    fits the persona length. Averaging across several exemplars (rather than
    matching one) avoids rewarding verbatim parroting; the deterministic
    components run at zero model cost (pass ``embed_fn`` only to add the
    semantic term). Returns dicts with draft, model_used, temperature, score,
    and ``voice_match`` (None when no exemplars), sorted descending.
    """
    refs = [r for r in (exemplar_replies or []) if r and r.strip()][:3]
    scored: list[dict[str, Any]] = []
    for draft, model_used, temperature in raw:
        base = _score_candidate(
            draft, target_words=target_words, greeting=greeting, closing=closing,
            p25=p25, p75=p75,
        )
        voice: float | None = None
        score = base
        if base != float("-inf") and refs:
            from app.evaluation.voice_match import voice_match_score

            vals: list[float] = []
            for ref in refs:
                try:
                    vals.append(float(voice_match_score(draft, ref, embed_fn=embed_fn)["voice_match"]))
                except Exception:
                    continue
            if vals:
                voice = sum(vals) / len(vals)
                score = base + _VOICE_WEIGHT * voice
        scored.append({
            "draft": draft,
            "model_used": model_used,
            "temperature": temperature,
            "score": score,
            "voice_match": round(voice, 3) if voice is not None else None,
        })
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def _rank_candidates_by_quality(
    raw: list[tuple[str, str, float | None]],
    *,
    reply_pairs: list[Any] | None,
    target_words: int | None,
    greeting: str,
    closing: str,
    p25: int | None = None,
    p75: int | None = None,
) -> list[dict[str, Any]]:
    """Rank multi-candidate drafts by the per-draft quality score (b186).

    ``raw`` is ``[(draft, model_used, temperature), ...]`` in generation order.
    Each candidate is scored with ``draft_quality_score`` — the SAME 0–1 signal
    auto-push/auto-send gates on (voice fidelity vs. the user's retrieved
    replies + structural fit, with generic-ack / cloud-fallback collapse) — so
    the candidate kept is the one most worth acting on, not merely the
    best-length-fitting. The highest score wins; ties resolve to the
    LOWEST original index (stability) because we sort by ``-quality`` and the
    enumerate index ascending, both as the sort key.

    Returns dicts (best-first) with ``draft``, ``model_used``, ``temperature``,
    ``quality_score``, and ``candidate_index`` (the original generation index of
    the kept candidate, recorded for telemetry).
    """
    scored: list[dict[str, Any]] = []
    for idx, (draft, model_used, temperature) in enumerate(raw):
        try:
            q = draft_quality_score(
                draft, reply_pairs=reply_pairs, target_words=target_words,
                greeting=greeting, closing=closing, model_used=model_used,
                p25=p25, p75=p75,
            )
        except Exception:
            logger.warning("multi-candidate: scoring one candidate failed", exc_info=True)
            q = 0.0
        scored.append({
            "draft": draft,
            "model_used": model_used,
            "temperature": temperature,
            "quality_score": q,
            "candidate_index": idx,
        })
    # Best quality first; tie → lowest original index (stable). Sorting on a
    # (-quality, index) tuple makes the tie-break explicit and order-independent.
    scored.sort(key=lambda c: (-(c["quality_score"] or 0.0), c["candidate_index"]))
    return scored


ADAPTER_PATH = get_adapter_path()


def _get_base_model_id() -> str:
    return get_base_model()


def _adapter_available() -> bool:
    return (ADAPTER_PATH / "adapters.safetensors").exists()


def _local_model_available() -> bool:
    """Local MLX generation is usable when the `mlx_lm` CLI is on PATH.

    Distinct from `_adapter_available()`: the LoRA adapter is optional —
    when absent, generation falls back to the base model rather than
    failing over to a cloud model. The review_queue's "auto" mode keeps
    its stricter "must have adapter" gate via `_adapter_available()`.
    """
    return shutil.which("mlx_lm") is not None


def _persona_adapter_available(sender_type: str | None) -> Path | None:
    """Return the path to a per-persona adapter, or None when not available.

    Phase 3 of per-persona adapters. The routing decision in
    `generate_draft` calls this with the inbound's `sender_type_hint`; a
    non-None return means the per-persona adapter is on disk and routing
    can use it instead of the global. NULL/unknown/empty sender_type
    returns None — we don't have a persona adapter for "unknown" and
    routing should fall through to the global in that case.
    """
    if not sender_type or sender_type == "unknown":
        return None
    from app.core.settings import get_persona_adapter_path

    path = get_persona_adapter_path(sender_type)
    if (path / "adapters.safetensors").exists():
        return path
    return None


def _persona_routing_enabled() -> bool:
    """True when ``personas.routing_enabled: true`` is set in the config.

    Default-off so existing installs upgrade through Phases 1+2 without
    any generation behavior change — the user opts in once Phase 2 has
    accumulated per-persona adapters they want to use.
    """
    try:
        from app.core.config import load_config

        raw = load_config() or {}
        if not isinstance(raw, dict):
            return False
        personas_cfg = raw.get("personas") or {}
        if not isinstance(personas_cfg, dict):
            return False
        return bool(personas_cfg.get("routing_enabled", False))
    except Exception:
        return False


def _max_inbound_chars() -> int:
    """Max characters of inbound text to feed the model (``generation.
    max_inbound_chars``, default 4000; 0 disables). Bounds prompt size so a
    huge email can't overflow the local model into a cloud fallback."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
        return max(0, int(gen.get("max_inbound_chars", 4000)))
    except (TypeError, ValueError, AttributeError):
        return 4000


# b187: a draft word is ~1.5 tokens for this tokenizer family (English prose,
# ChatML). The token budget caps the UPPER band edge plus headroom so a
# genuinely runaway "long" draft is bounded, while an in-band draft has ample
# room to finish naturally on the <|im_end|> stop (no mid-sentence truncation).
_TOKENS_PER_WORD = 1.5
# Multiplicative headroom over the upper-band token estimate. 2.0× the
# high-edge budget leaves a comfortable margin for greeting/closing lines and
# natural sentence completion, so an IN-band draft never truncates mid-sentence
# — the cap only bites a genuine runaway (roughly 2× the band high edge), which
# was the goal: bound the "long" tail without clipping good drafts.
_MAX_TOKENS_HEADROOM = 2.0


def _compute_max_tokens(
    avg_reply_words: int | None,
    *,
    persona: dict[str, Any] | None = None,
    intent: str | None = None,
    p25: int | None = None,
    p75: int | None = None,
) -> int:
    """Compute max_tokens from the persona length BAND (b187), not avg×5.

    Resolves the effective average (mode > intent > global) exactly as before,
    then derives the ok-band's UPPER edge (``_length_band``) and budgets
    ``high_edge_words * tokens/word * headroom``. Bounds a runaway "long" draft
    while leaving enough headroom that an in-band draft finishes naturally on the
    stop token — verified against the golden suite for clean endings.

    Priority for the effective average: mode-specific > intent-specific > global
    > default 300 tokens.
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
    band = _length_band(effective_words, p25=p25, p75=p75)
    if band is None:
        return 300
    _low, high = band
    budget = int(round(high * _TOKENS_PER_WORD * _MAX_TOKENS_HEADROOM))
    return max(100, min(500, budget))


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


def _call_local_model(
    prompt: str | list[dict[str, str]],
    *,
    max_tokens: int = 300,
    use_adapter: bool = True,
    adapter_path: Path | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
) -> str:
    """Run mlx_lm against the base model, optionally with a LoRA adapter.

    ``prompt`` is either a ChatML messages list (``[{system}, {user}]`` — the
    format the adapter was fine-tuned on, used for drafting) or a raw completion
    string (used only by the tiny subject-suggestion helper). For messages,
    generation is driven through the model's chat template — the warm
    ``/v1/chat/completions`` endpoint, or ``mlx_lm generate
    --system-prompt/--prompt`` WITHOUT ``--ignore-chat-template`` — and stopped
    at ``<|im_end|>`` (b173). This aligns inference with training: the model
    emits a bare reply and halts at end-of-turn instead of mimicking the old
    ``[SYSTEM]/[EXEMPLARS]/[REPLY]`` bracket document and running on.

    `adapter_path` overrides the global ADAPTER_PATH — used by Phase-3
    persona routing to point at `<models>/adapters/personas/<sender_type>/`
    instead of the global `<models>/adapters/latest/`. Falls back to
    ADAPTER_PATH when `use_adapter=True` and `adapter_path` is None
    (preserves the historical behavior).

    ``seed`` (b166) pins the PRNG for reproducible eval generation. When set it
    is passed to both the warm model server and the cold mlx_lm subprocess. The
    mlx_lm CLI flag is ``--seed`` (verified against mlx-lm 0.31.3); combined
    with ``temperature=0`` this gives deterministic greedy/argmax decoding.
    """
    is_chat = isinstance(prompt, list)
    stop = _chat_stop_sequences() if is_chat else None

    # Prefer the warm server (no per-draft model reload) for the common case: the
    # global adapter (or base). It serves a single adapter loaded at startup, so a
    # per-persona adapter_path, or an explicit base request (use_adapter=False
    # while an adapter is loaded), must use the per-request subprocess below.
    if adapter_path is None and use_adapter:
        from app.core import model_server

        if model_server.is_enabled() and model_server.ensure_running():
            try:
                if is_chat:
                    # Chat endpoint applies the model's chat template (matching
                    # training) and stops at <|im_end|> via ``stop`` (b173).
                    return model_server.chat_complete(
                        prompt, max_tokens=max_tokens, temperature=temperature,
                        top_p=top_p, seed=seed, stop=stop,
                    )
                return model_server.complete(
                    prompt, max_tokens=max_tokens, temperature=temperature, top_p=top_p, seed=seed
                )
            except Exception:
                logger.warning("warm model server call failed; falling back to subprocess", exc_info=True)

    cmd = [
        "mlx_lm",
        "generate",
        "--model",
        _get_base_model_id(),
    ]
    chosen_adapter = adapter_path if adapter_path is not None else (ADAPTER_PATH if use_adapter else None)
    if chosen_adapter is not None:
        cmd.extend(["--adapter-path", str(chosen_adapter)])
    if is_chat:
        # Drive the subprocess through the chat template so inference matches
        # training: --system-prompt + --prompt are rendered with the model's
        # ChatML template (we do NOT pass --ignore-chat-template), and
        # --extra-eos-token <|im_end|> stops generation at end-of-turn instead
        # of running on into a fabricated bracket document (b173).
        system_text, user_text = _split_chat_messages(prompt)
        cmd.extend(["--prompt", user_text])
        if system_text:
            cmd.extend(["--system-prompt", system_text])
        cmd.extend(["--extra-eos-token", EOS_TOKEN])
        cmd.extend(["--max-tokens", str(max_tokens)])
    else:
        cmd.extend(
            [
                "--prompt",
                prompt,
                "--max-tokens",
                str(max_tokens),
            ]
        )
    # Only pass sampling flags when configured — omitting them preserves
    # mlx_lm's own defaults (the historical behavior).
    if temperature is not None:
        cmd.extend(["--temp", str(temperature)])
    if top_p is not None:
        cmd.extend(["--top-p", str(top_p)])
    # Eval-only: pin the PRNG so re-scoring the same config is reproducible.
    if seed is not None:
        cmd.extend(["--seed", str(seed)])
    result = _run_subprocess(cmd, timeout=SUBPROCESS_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"mlx_lm generate failed (exit {result.returncode}): {result.stderr.strip()}")
    return _strip_mlx_output(result.stdout)


def _local_draft_once(
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None,
    top_p: float | None,
    request: "DraftRequest",
    sender_type_hint: str | None,
    seed: int | None = None,
) -> tuple[str, str]:
    """Produce one local-model draft, honoring the Phase-3 adapter precedence.

    Returns ``(draft, model_used)``. Factored out so both the single-draft
    path and multi-candidate generation share the exact same adapter routing.
    ``seed`` (b166) is threaded to ``_call_local_model`` for reproducible eval.
    """
    persona_adapter_path: Path | None = None
    if request.use_adapter and _persona_routing_enabled():
        persona_adapter_path = _persona_adapter_available(sender_type_hint)
    if persona_adapter_path is not None:
        draft = _call_local_model(
            prompt, max_tokens=max_tokens, adapter_path=persona_adapter_path,
            temperature=temperature, top_p=top_p, seed=seed,
        )
        # b174: derive the label from the configured base so it tracks a model
        # migration; keep the per-persona ``-<type>`` suffix.
        return draft, f"{_model_label(with_adapter=True)}-{sender_type_hint}"
    with_adapter = request.use_adapter and _adapter_available()
    draft = _call_local_model(
        prompt, max_tokens=max_tokens, use_adapter=with_adapter,
        temperature=temperature, top_p=top_p, seed=seed,
    )
    # b174: label derived from the configured base (no longer a hardcoded
    # qwen2.5 string) so it doesn't lie after a model migration.
    return draft, _model_label(with_adapter=with_adapter)


def _generate_via_ollama(
    prompt: str,
    model: str = "mistral",
    base_url: str = "http://localhost:11434",
    *,
    num_predict: int = 400,
    temperature: float | None = None,
    top_p: float | None = None,
    seed: int | None = None,
) -> str:
    """Generate via Ollama HTTP API.

    ``seed`` (b166) maps to Ollama's ``options.seed`` so the eval is
    reproducible when the local model is unavailable and Ollama is the backend.
    """
    import urllib.request

    # Preserve the historical 0.7 default when temperature isn't configured.
    options: dict[str, Any] = {"temperature": 0.7 if temperature is None else temperature, "num_predict": num_predict}
    if top_p is not None:
        options["top_p"] = top_p
    if seed is not None:
        options["seed"] = seed
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": options}).encode()
    req = urllib.request.Request(f"{base_url}/api/generate", data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("response", "").strip()
    except Exception as exc:
        raise RuntimeError(f"Ollama generation failed: {exc}") from exc


def _run_subprocess(cmd: list[str], *, timeout: int = SUBPROCESS_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with a hard timeout that kills the whole process group.

    `subprocess.run(timeout=...)` only SIGKILLs the direct child; if that child
    (e.g. the `claude` Node CLI or `mlx_lm`) spawned its own subprocesses that
    inherited the stdout/stderr pipes, the parent's read blocks waiting for EOF
    until *those* exit — so a single stalled generation can hang far past the
    timeout (observed: an 8-minute stall on a 120s timeout). Running in a new
    session and killing the process group on timeout releases the pipes.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _call_claude_cli(prompt: str, *, max_tokens: int = 300) -> str:  # noqa: ARG001
    # Note: claude CLI --print does not support --max-tokens; use -p to pass prompt
    cmd = ["claude", "--print", "-p", prompt]
    result = _run_subprocess(cmd, timeout=SUBPROCESS_TIMEOUT)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {stderr}")
    return result.stdout.strip()


# --- Cloud (Claude) drafting opt-in gate (b175) -----------------------------
# Cloud drafting EGRESSES private inbound mail off-device, so it is hard-gated.
# These helpers are the single, centralized, fail-closed decision point that the
# cloud-preference levers in generate_draft key off.

# Backends that egress the inbound off-device.
_CLOUD_BACKENDS = frozenset({"claude"})

# Human-readable notice attached to any cloud-drafted response so the UI and
# telemetry can never silently hide that the inbound left the device.
_CLOUD_EGRESS_NOTICE = (
    "This draft was generated by a cloud model (Claude); the inbound email "
    "text was sent off-device."
)


def _is_cloud_backend(name: str | None) -> bool:
    return name is not None and name in _CLOUD_BACKENDS


def _cloud_escalation_allowed(request: DraftRequest) -> bool:
    """Single, fail-closed predicate gating ALL cloud (Claude) drafting (b175).

    Cloud egress is permitted ONLY when EVERY condition holds:

      1. NONE of the background / eval guards is set —
         ``strict_local`` (agent triage sweep, b66),
         ``no_cloud_fallback`` (autoresearch/golden eval + scheduled digest,
         b168), or ``deterministic`` (eval determinism, b166). These mark every
         non-interactive path; checked FIRST so cloud is impossible there
         regardless of the flag or the opt-in.
      2. The request explicitly opted in (``allow_cloud_escalation``) — only an
         interactive draft endpoint ever sets this.
      3. The config flag ``drafting.cloud_escalation.enabled`` is truthy.

    Fail-closed: a missing flag (default config) or any error reading it
    returns False, so out-of-the-box behaviour is local-only.
    """
    # (1) Background / eval guards hard-block cloud, unconditionally + first.
    if (
        getattr(request, "strict_local", False)
        or getattr(request, "no_cloud_fallback", False)
        or getattr(request, "deterministic", False)
    ):
        return False
    # (2) Explicit per-request opt-in required.
    if not getattr(request, "allow_cloud_escalation", False):
        return False
    # (3) Config flag required; cloud_escalation_enabled() is itself fail-closed.
    try:
        return bool(cloud_escalation_enabled())
    except Exception:  # pragma: no cover - defensive, fail-closed
        logger.warning("cloud-escalation flag read failed; denying cloud", exc_info=True)
        return False


def _abstain_config() -> dict[str, Any]:
    """Read ``generation.abstain.*`` config (b188).

    The abstain feature withholds a weak AUTONOMOUS draft and routes the email to
    the surface-for-review tier instead of presenting a low-confidence reply as
    ready. Defaults:

    * ``enabled`` — True. This is the requested default-ON behavior, but it can
      only ever fire on the tightly-gated autonomous path (see
      ``_should_abstain``); it is INERT for interactive and deterministic/eval
      requests regardless of this flag, so default-ON is safe for the golden eval.
    * ``min_quality`` — the shared per-draft quality floor
      (``DEFAULT_QUALITY_FLOOR`` = the auto-push floor), so the abstain threshold
      and the auto-push gate are one policy number, not two.
    """
    from app.core.config import load_config

    cfg = load_config() or {}
    gen = cfg.get("generation", {}) if isinstance(cfg, dict) else {}
    ab = gen.get("abstain", {}) if isinstance(gen, dict) else {}
    ab = ab if isinstance(ab, dict) else {}
    try:
        min_quality = float(ab.get("min_quality", DEFAULT_QUALITY_FLOOR))
    except (TypeError, ValueError):
        min_quality = DEFAULT_QUALITY_FLOOR
    return {
        "enabled": bool(ab.get("enabled", True)),
        "min_quality": min_quality,
    }


def _should_abstain(request: DraftRequest, quality_score: float | None) -> tuple[bool, str | None]:
    """Decide whether to WITHHOLD this draft (b188). Fail-closed: never withhold
    on error.

    Abstain fires ONLY when EVERY condition holds:

      1. NOT ``deterministic`` — the eval/golden/autoresearch/nightly family must
         still score a draft for every case, so abstain is impossible there and
         the golden eval is byte-identical to before this feature.
      2. NOT ``interactive`` — a human who explicitly asked to see a draft always
         gets one; only the autonomous triage sweep sets ``interactive=False``.
      3. The config flag ``generation.abstain.enabled`` is truthy (default True).
      4. A quality_score exists AND is below ``generation.abstain.min_quality``.
         A missing score is NOT abstained on here (the autonomous auto-push gate
         already treats a missing score as below-floor and holds it from acting),
         so we don't withhold a draft merely because scoring threw.

    Returns ``(withheld, reason)``.
    """
    try:
        if getattr(request, "deterministic", False):
            return (False, None)
        if getattr(request, "interactive", True):
            return (False, None)
        cfg = _abstain_config()
        if not cfg["enabled"]:
            return (False, None)
        if quality_score is None:
            return (False, None)
        floor = cfg["min_quality"]
        if quality_score < floor:
            return (True, f"withheld: quality {quality_score:.2f} < {floor:.2f}")
        return (False, None)
    except Exception:  # pragma: no cover - defensive, fail-open (never withhold)
        logger.warning("abstain check failed; not withholding", exc_info=True)
        return (False, None)


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

    # Handle thread context for ongoing threads. Prefer caller-supplied
    # structured history (the agent fetches the real thread) — it's far more
    # reliable than parsing "From:" blocks out of a single quoted body, which
    # strip_quoted_text has usually already removed. Fall back to the regex
    # extraction for callers that paste a whole quoted thread into one message.
    inbound_for_prompt = clean_inbound
    if request.thread_history:
        history = [
            {"sender": h.get("sender", ""), "text": h.get("text", "")}
            for h in request.thread_history if h.get("text")
        ]
        if history:
            inbound_for_prompt = _format_thread_context(clean_inbound, history)
    elif _has_thread_context(clean_inbound):
        active_inbound, history = _extract_thread_parts(clean_inbound)
        inbound_for_prompt = _format_thread_context(active_inbound, history)

    # Bound the inbound fed to the model. A very long email (e.g. an
    # auto-generated meeting-notes dump) can overflow the local model's context
    # and force an empty-output fallback to the cloud; truncating the tail keeps
    # generation on-device. Generous default (only trims genuinely huge inputs);
    # the retrieval query above used the full text, so retrieval is unaffected.
    _cap = _max_inbound_chars()
    if _cap and len(inbound_for_prompt) > _cap:
        inbound_for_prompt = inbound_for_prompt[:_cap].rstrip() + "\n[… message truncated for length …]"

    # Open one shared DB connection for all metadata lookups (P1)
    db_path = resolve_sqlite_path(database_url)
    shared_conn = _connect(db_path)
    shared_conn.row_factory = sqlite3.Row
    try:

        # Look up prior reply to this sender for additional context (works for standalone emails too)
        user_name = get_user_name()
        if request.sender:
            prior_reply = _lookup_prior_reply_to_sender(request.sender, database_url, conn=shared_conn)
            if prior_reply:
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
        sender_email_hint = None
        if request.sender:
            # b190: pass database_url so an enriched sender_profiles row (built
            # from real reply history) takes precedence over the coarse
            # heuristic — gives persona-adapter routing a consistent, richer
            # sender_type across sessions.
            sender_type_hint = classify_sender(request.sender, database_url)
            sender_domain_hint = extract_domain(request.sender)
            # Exact-email hint lets retrieval boost pairs from prior direct
            # exchanges with this sender — much sharper than same-domain.
            from app.core.sender import extract_email
            sender_email_hint = extract_email(request.sender)

        # Cold-outreach detection — pushy sales outbounds get a polite-decline
        # nudge in the prompt so the LoRA doesn't auto-accept calls/demos
        # from people the user has no history with. Heuristic; logged for
        # auditing alongside repairs.
        from app.core.cold_outreach import DECLINE_NUDGE, detect_cold_outbound

        _cold_verdict = detect_cold_outbound(
            subject=request.subject,
            body=clean_inbound,
            sender_email=sender_email_hint,
        )
        cold_outbound_nudge = DECLINE_NUDGE if _cold_verdict.is_cold else None

        # Combine caller-provided standing instructions (e.g. the agent's
        # "today I'm OOO" string) with the auto-detected cold-outreach nudge.
        # Both land in the prompt's persona-constraints block via the
        # ``extra_constraint`` kwarg on ``assemble_prompt``.
        _extras = [s for s in (cold_outbound_nudge, request.standing_instructions) if s]
        extra_constraint = "\n".join(_extras) if _extras else None

        # Classify intent (multi-intent support)
        from app.core.intent import classify_intents_multi

        if request.intent_hint:
            detected_intent = request.intent_hint
            intent_hint_2 = None
        else:
            intents = classify_intents_multi(clean_inbound)
            detected_intent = intents[0]
            intent_hint_2 = intents[1] if len(intents) > 1 else None

        # E20: for very short inbound (<50 chars), fall back to sender-profile-based retrieval
        retrieval_query = clean_inbound
        if len(clean_inbound.strip()) < 50 and request.sender:
            # Augment query with sender email so retrieval finds past replies to this person
            retrieval_query = f"{clean_inbound} {request.sender}".strip()

        retrieval_response: RetrievalResponse = retrieve_context(
            RetrievalRequest(
                query=retrieval_query,
                scope="all",
                account_emails=account_emails,
                top_k_reply_pairs=request.top_k_reply_pairs,
                top_k_chunks=request.top_k_chunks,
                sender_type_hint=sender_type_hint,
                sender_domain_hint=sender_domain_hint,
                sender_email_hint=sender_email_hint,
                language_hint=detected_lang,
                intent_hint=detected_intent,
                intent_hint_2=intent_hint_2,
                thread_id=request.thread_id,
                exclude_reply_pair_ids=request.exclude_reply_pair_ids,
                exclude_thread_ids=request.exclude_thread_ids,
            ),
            database_url=database_url,
            configs_dir=configs_dir,
        )

        detected_mode = request.mode or retrieval_response.detected_mode
        reply_pairs = retrieval_response.reply_pairs

        if request.use_exemplar_cache:
            cached_ids, exemplar_cache_hit, exemplar_cache_key = _get_cached_exemplar_ids(detected_intent, sender_type_hint, database_url=database_url)
            reply_pairs = _apply_cached_order(reply_pairs, cached_ids)

            selected_ids = _top_exemplar_source_ids(reply_pairs)
            # Only persist on a cache miss or when the selection actually changed —
            # otherwise every hit triggered a redundant DB write of the same row.
            if not exemplar_cache_hit or selected_ids != cached_ids[: len(selected_ids)]:
                _update_exemplar_cache(detected_intent, sender_type_hint, selected_ids, database_url=database_url)
        else:
            # Cache bypassed (autoresearch eval): use retrieval's own ordering so
            # the current config actually determines the exemplars. No read, no
            # write — the cache stays untouched for production drafting.
            exemplar_cache_hit = False
            exemplar_cache_key = None
            selected_ids = _top_exemplar_source_ids(reply_pairs)

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
            sender_profile = lookup_sender_profile(request.sender, database_url, conn=shared_conn)
            if sender_profile:
                sender_context = _format_sender_context(sender_profile)
                first_name = first_name_from_display_name(sender_profile.get("display_name"))
            if not first_name:
                # b231: no profile row (or a profile without a display name)
                # left every greeting a bare "Hi," — fall back to the display
                # name in the sender header itself ("Nadine Nehme <n@x>").
                from email.utils import parseaddr

                display = parseaddr(request.sender)[0]
                if display:
                    first_name = first_name_from_display_name(display)

        # Look up facts (user prefs, contact facts, project context)
        memory_facts = lookup_facts(
            sender=request.sender,
            inbound_text=clean_inbound,
            database_url=database_url,
            conn=shared_conn,
        )
    finally:
        shared_conn.close()

    messages = assemble_chat_messages(
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
        memory_facts=memory_facts,
        score_stats=score_stats,
        subject=request.subject,
        user_prompt=request.user_prompt,
        extra_constraint=extra_constraint,
    )

    # Token budget: estimate from the chat messages. Exemplars are no
    # longer in the prompt (assemble_chat_messages drops them — the adapter
    # already encodes the voice), so the old exemplar-trimming knapsack is
    # obsolete; the system+user turns are what the model actually sees.
    token_estimate = _estimate_tokens(
        messages[0]["content"] + "\n" + messages[-1]["content"]
    ) if messages else 0

    # Flat-text prompt for non-chat fallback backends (ollama / claude CLI),
    # which take a single string rather than ChatML messages.
    prompt = (messages[0]["content"] + "\n\n" + messages[-1]["content"]) if messages else ""

    precedent_used = [_precedent_summary(rp) for rp in reply_pairs]

    # Compute length-aware max_tokens (intent-specific if available)
    avg_reply_words = persona.get("style", {}).get("avg_reply_words")
    # Use intent-specific avg if available
    intent_avg = persona.get("style", {}).get("intent_avg_words", {})
    if isinstance(intent_avg, dict) and detected_intent in intent_avg:
        avg_reply_words = intent_avg[detected_intent]
    # b187: persona reply-length percentiles (tighter ok-band when present —
    # see _length_band). Read once and threaded through the token budget,
    # length flag, candidate ranking, and concise-retry so all four agree.
    _band_p25 = persona.get("style", {}).get("avg_reply_words_p25")
    _band_p75 = persona.get("style", {}).get("avg_reply_words_p75")
    max_tokens = _compute_max_tokens(
        avg_reply_words, persona=persona, intent=detected_intent,
        p25=_band_p25, p75=_band_p75,
    )
    # Decoding params (default None -> each backend keeps its current default).
    temperature, top_p = _resolve_decoding(detected_intent, confidence)
    # Eval-only determinism (b166): force greedy (temp=0) + a fixed seed,
    # overriding any config decoding, so re-scoring the same config yields the
    # same composite. top_p is irrelevant under argmax, so drop it. The
    # multi-candidate temperature spread is also bypassed below — a spread is
    # inherently non-greedy and would reintroduce variance. Production drafting
    # leaves request.deterministic False and is completely unaffected.
    eval_seed: int | None = None
    if getattr(request, "deterministic", False):
        temperature = 0.0
        top_p = None
        eval_seed = request.seed if request.seed is not None else EVAL_SEED

    # Greeting/closing resolved once — reused by candidate ranking and the
    # post-generation repair pass below.
    repair_greeting = _resolve_greeting(persona, sender_type_hint, first_name)
    repair_closing = _resolve_closing(persona, sender_type_hint)

    fallback_model = get_model_fallback()
    # An explicit backend_override pins the engine for this draft (used by the
    # cross-model comparison). "mlx" routes to the local path; anything else
    # forces that fallback model and disables the local path.
    use_local = request.use_local_model
    if request.backend_override:
        use_local = request.backend_override == "mlx"
        if request.backend_override != "mlx":
            fallback_model = request.backend_override
    # ζ: strict-local — per-request refusal to fall back to cloud. The agent
    # triage path opts in via DraftRequest; interactive /feedback is unaffected.
    # Acts BELOW the backend_override (the cross-model comparison takes
    # precedence — that one explicitly wants to compare each backend).
    if getattr(request, "strict_local", False) and not request.backend_override:
        fallback_model = "none"
    # b168: eval-only no-cloud-fallback. Forces fallback_model='none' for the
    # autoresearch/golden eval so an empty local output fails soft (raises here,
    # caught per-case by run_eval_suite → empty draft) instead of shelling to the
    # unauthenticated Claude CLI 200× per run. Like strict_local, this sits below
    # backend_override (the cross-model comparison explicitly wants each backend).
    if getattr(request, "no_cloud_fallback", False) and not request.backend_override:
        fallback_model = "none"
    # b175: CENTRALIZED, FAIL-CLOSED cloud-escalation gate. It governs ONLY the
    # *new explicit cloud-preference levers*: a cloud ``backend_override`` (the
    # cross-model A/B pinning "claude") and the per-request
    # ``allow_cloud_escalation`` opt-in. It deliberately does NOT touch the
    # pre-existing config emergency fallback (``model.fallback``) on a normal
    # request: with the flag OFF and NO override/opt-in, the fallback chain is
    # byte-for-byte identical to before this feature existed (local-first, then
    # the configured fallback — already blocked on background paths by the
    # strict_local / no_cloud_fallback guards above).
    #
    # The gate fires ONLY when this draft actively PREFERS cloud — a cloud
    # backend_override OR the opt-in. In that case, unless
    # ``_cloud_escalation_allowed`` permits it (flag ON + interactive + opt-in +
    # no background/eval guard), the cloud preference is stripped. Because the
    # predicate is fail-closed and returns False for any
    # strict_local/no_cloud_fallback/deterministic request, cloud is IMPOSSIBLE
    # to *prefer* on any nightly/autoresearch/golden/triage/scheduled path even
    # if such a caller somehow set the opt-in or a cloud override.
    _wants_cloud_pref = _is_cloud_backend(request.backend_override) or getattr(
        request, "allow_cloud_escalation", False
    )
    if _wants_cloud_pref and not _cloud_escalation_allowed(request):
        # Strip the cloud preference (the override and/or its cloud fallback) so
        # this request stays local.
        if _is_cloud_backend(fallback_model):
            fallback_model = "none"
        # If a cloud backend_override was the only reason use_local was disabled,
        # restore the request's local preference so we still produce a real local
        # draft rather than a "no model available" placeholder.
        if request.backend_override and _is_cloud_backend(request.backend_override):
            use_local = request.use_local_model
            fallback_model = "none"
    candidates: list[dict[str, Any]] = []
    try:
        if use_local and _local_model_available():
            # Phase 3 routing precedence is encapsulated in _local_draft_once:
            #   1. per-persona adapter (routing on + caller wants it + trained)
            #   2. global "latest" adapter when one is trained
            #   3. base model otherwise
            # `model_used` is honest about which adapter actually ran.
            mc = _multi_candidate_config()
            # Multi-candidate is HARD-INERT on the deterministic/eval/golden path
            # (request.deterministic, set by run_golden_eval / run_eval /
            # run_autoresearch / nightly_pipeline — the same family of background
            # guards b175 cloud escalation keys off). A temperature spread is
            # inherently non-greedy and would reintroduce night-to-night variance,
            # so there we generate EXACTLY ONE greedy + seeded candidate below,
            # byte-identical to single-candidate drafting. The b166 determinism
            # tests pin this.
            #
            # b194: it is also gated on request.multi_candidate_ok — the n× cost
            # is only paid where latency is hidden (the autonomous triage sweep
            # and the compare-models tuning harness). Interactive callers leave
            # the flag False (default) and stay single-candidate / fast.
            if (
                mc["enabled"]
                and not getattr(request, "deterministic", False)
                and getattr(request, "multi_candidate_ok", False)
            ):
                # Generate n candidates with a DIVERSE temperature spread (so they
                # actually differ), then keep the one with the highest per-draft
                # quality score. We rank with draft_quality_score — the SAME 0–1
                # signal auto-push/auto-send gates on (voice fidelity vs. the
                # user's retrieved replies + structural fit, with verify-before-
                # accept folded in and generic-ack / cloud-fallback drafts
                # collapsed) — so the kept candidate is the one most worth acting
                # on, not merely the best-fitting length. Uses the warm chat path
                # via _local_draft_once.
                raw: list[tuple[str, str, float | None]] = []
                for t in mc["temperatures"]:
                    try:
                        d, mu = _local_draft_once(
                            messages, max_tokens=max_tokens, temperature=t, top_p=top_p,
                            request=request, sender_type_hint=sender_type_hint,
                        )
                        raw.append((d, mu, t))
                    except Exception:
                        logger.warning("multi-candidate: one candidate failed", exc_info=True)
                if not raw:
                    raise RuntimeError("all draft candidates failed")
                candidates = _rank_candidates_by_quality(
                    raw, reply_pairs=reply_pairs, target_words=avg_reply_words,
                    greeting=repair_greeting, closing=repair_closing,
                    p25=_band_p25, p75=_band_p75,
                )
                _winner = candidates[0]
                draft = _winner["draft"]
                model_used = _winner["model_used"]
                logger.info(
                    "multi-candidate: generated=%d kept idx=%d quality=%.3f temp=%s",
                    len(candidates), _winner["candidate_index"],
                    _winner["quality_score"] if _winner["quality_score"] is not None else -1.0,
                    _winner["temperature"],
                )
            else:
                draft, model_used = _local_draft_once(
                    messages, max_tokens=max_tokens, temperature=temperature, top_p=top_p,
                    request=request, sender_type_hint=sender_type_hint, seed=eval_seed,
                )
        elif fallback_model == "ollama":
            from app.core.config import get_ollama_config

            ollama_cfg = get_ollama_config()
            ollama_model = ollama_cfg.get("model", "mistral")
            ollama_url = ollama_cfg.get("base_url", "http://localhost:11434")
            draft = _generate_via_ollama(
                prompt, model=ollama_model, base_url=ollama_url, num_predict=max_tokens,
                temperature=temperature, top_p=top_p, seed=eval_seed,
            )
            model_used = f"ollama:{ollama_model}"
        elif fallback_model == "claude":
            draft = _call_claude_cli(prompt, max_tokens=max_tokens)
            model_used = "claude"
        elif fallback_model == "none":
            # Fallback explicitly disabled: no local model and no cloud call.
            draft = "[no model available: local model unavailable and fallback disabled]"
            model_used = "none"
        else:
            draft = _call_claude_cli(prompt, max_tokens=max_tokens)
            model_used = fallback_model
    except Exception:
        # Log the detail server-side; the draft field is user-visible, so don't
        # leak raw exception text (paths, config keys, backend CLI stderr).
        logger.exception("draft generation failed")
        draft = "[draft generation failed — see server logs]"
        model_used = "error"

    # Retry on empty or signature-only local model output
    empty_output_retried = False

    def _is_empty_draft(d: str) -> bool:
        stripped = strip_signature(d).strip()
        nws = len(d.replace(" ", "").replace("\n", "").replace("\t", ""))
        return nws < 10 or (len(stripped) < 15 and nws > 0)

    if _is_empty_draft(draft) and model_used not in ("error", "claude"):
        # First retry the LOCAL model once. Empty local output is most often a
        # cold-start / transient hiccup, and a retry keeps the reply ON-DEVICE
        # instead of bouncing private mail to the cloud. Only after a second
        # local empty do we fall back (and strict_local already forced
        # fallback_model='none' above, so it won't reach the cloud there).
        if any(tag in model_used for tag in ("qwen", "lora", "base")):
            try:
                _rd, _rm = _local_draft_once(
                    messages, max_tokens=max_tokens, temperature=temperature, top_p=top_p,
                    request=request, sender_type_hint=sender_type_hint, seed=eval_seed,
                )
                if not _is_empty_draft(_rd):
                    draft, model_used = _rd, _rm
                    logger.info("local model recovered on retry — stayed on-device")
            except Exception as exc:
                logger.info("local retry after empty output failed: %s", exc)

        if _is_empty_draft(draft):
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
            logger.warning("Failed to update pipeline log", exc_info=True)

    # Post-generation repair: always annotate length; optionally enforce the
    # persona greeting/closing and strip a trailing duplicate signature (both
    # default-off — see _get_repair_config).
    draft, repairs, length_flag = _repair_draft(
        draft,
        greeting=repair_greeting,
        closing=repair_closing,
        target_words=avg_reply_words,
        config=_get_repair_config(),
        p25=_band_p25,
        p75=_band_p75,
    )

    # b187 concise-retry (LIVE path only). If the chosen draft ran "long" (above
    # the persona band), regenerate ONCE with a stronger concise nudge + a
    # tighter token budget and keep whichever fits the band better. Bounded to a
    # single retry. HARD-GATED OFF on the deterministic/eval/background path
    # (request.deterministic — the same family of guards b186 multi-candidate and
    # b175 cloud-escalation key off) so golden/autoresearch reproducibility holds
    # and there is never a retry in eval. Only "long" is retried — forcing a
    # "short" draft longer means padding, which hurts quality. Only retried on a
    # real local draft (skip error/claude/none and the empty-output fallback).
    if (
        length_flag == "long"
        and not getattr(request, "deterministic", False)
        and use_local
        and _local_model_available()
        and any(tag in (model_used or "") for tag in ("qwen", "lora", "base"))
        and not _is_empty_draft(draft)
    ):
        try:
            band = _length_band(avg_reply_words, p25=_band_p25, p75=_band_p75)
            high = band[1] if band else None
            nudge = (
                f"\nThe previous draft was too long. Rewrite it more concisely, "
                f"under {high} words, keeping all essential content."
                if high
                else "\nBe significantly more concise; keep all essential content."
            )
            retry_messages = [dict(m) for m in messages]
            if retry_messages:
                retry_messages[0]["content"] = retry_messages[0]["content"] + nudge
            # Tighter budget: the band-derived budget without the long-tail
            # headroom (high-edge tokens), floored so it can still complete.
            tight_tokens = (
                max(80, int(round(high * _TOKENS_PER_WORD))) if high else max_tokens
            )
            _rd, _rm = _local_draft_once(
                retry_messages, max_tokens=tight_tokens,
                temperature=temperature, top_p=top_p,
                request=request, sender_type_hint=sender_type_hint, seed=eval_seed,
            )
            if not _is_empty_draft(_rd):
                _rd, _rrepairs, _rflag = _repair_draft(
                    _rd, greeting=repair_greeting, closing=repair_closing,
                    target_words=avg_reply_words, config=_get_repair_config(),
                    p25=_band_p25, p75=_band_p75,
                )
                # Keep the retry only if it fits the band better: in-band beats
                # long, and a shorter still-long draft beats a longer one.
                _keep = _rflag == "ok" or (
                    _rflag == "long" and len(_rd.split()) < len(draft.split())
                )
                if _keep:
                    draft, repairs, length_flag = _rd, _rrepairs, _rflag
                    logger.info("concise-retry: kept tighter draft (flag=%s)", _rflag)
        except Exception:
            logger.warning("concise-retry failed; keeping original draft", exc_info=True)

    # Generate subject line — thread the per-request fallback decision so
    # strict_local (fallback_model == "none") is honored here too.
    suggested_subject = generate_subject(
        request.inbound_message, draft, database_url, configs_dir, fallback_model=fallback_model
    )

    # Capture the draft event (exemplars/intent/sender_type/confidence the
    # draft was produced with) for the nightly's training signal. Fault-
    # isolated: never affects the returned draft. Skipped for forced-backend
    # (benchmark/comparison) drafts AND the deterministic eval family (golden/
    # autoresearch/replay-backtest) — neither are real user drafts, and both
    # pollute the training signal and the "drafting with" status derived from
    # draft_events.model_used (observed live: eval sweeps logged 500–1200
    # draft_events/day vs ~40 real ones).
    if request.backend_override is None and not request.deterministic:
        _log_draft_event(
            database_url,
            inbound_text=request.inbound_message,
            draft=draft,
            account_email=request.account_email,
            sender=request.sender,
            sender_type=sender_type_hint,
            detected_mode=detected_mode,
            intent=detected_intent,
            confidence=confidence,
            confidence_reason=confidence_reason,
            model_used=model_used,
            retrieval_method=retrieval_response.retrieval_method,
            exemplar_ids=selected_ids,
            length_flag=length_flag,
        )

    # Per-draft quality: how good is THIS draft (voice + structure, collapsed
    # for generic acks / fallbacks). Failure-isolated — never blocks the draft.
    try:
        _quality = draft_quality_score(
            draft, reply_pairs=reply_pairs, target_words=avg_reply_words,
            greeting=repair_greeting, closing=repair_closing,
            model_used=model_used, empty_output_retried=empty_output_retried,
            p25=_band_p25, p75=_band_p75,
        )
    except Exception:
        _quality = None

    # Verify-before-accept: deterministic safety checks (language match, no
    # invented email/link/amount). A blocking issue collapses quality_score so
    # the auto-push quality floor holds the draft for review. Failure-isolated.
    _verify_issues: list[str] = []
    try:
        from app.generation.verify import verify_draft

        _vr = verify_draft(
            draft,
            inbound=request.inbound_message,
            thread_history=request.thread_history,
            account_email=request.account_email,
            sender=request.sender,
        )
        _verify_issues = _vr.issues
        if _vr.blocking and _quality is not None:
            _quality = min(_quality, 0.1)
        # b229: an AUTONOMOUS draft asserting a completed state the thread
        # doesn't support ("payment has been received", "filed with ADGM",
        # "no further action needed") is a fabrication — the agent did nothing
        # and can't know. Collapse quality so b188 abstain surfaces the email
        # for review instead of queueing a confidently-wrong draft. Interactive
        # and deterministic/eval requests are exempt (same guards as abstain),
        # so /draft and the golden eval are unaffected.
        elif (
            _vr.status_claims
            and _quality is not None
            and not getattr(request, "interactive", True)
            and not getattr(request, "deterministic", False)
        ):
            _quality = min(_quality, 0.1)
    except Exception:
        pass

    # b188: ABSTAIN on a weak AUTONOMOUS draft. Computed AFTER verify so the
    # threshold sees the final (possibly verify-collapsed) quality. Tightly
    # gated in _should_abstain — impossible on an interactive or
    # deterministic/eval request, so this is INERT for the /draft API and the
    # golden eval. We keep the generated text + score (never discard work
    # silently); the caller treats ``withheld`` drafts as surface-for-review.
    _withheld, _withhold_reason = _should_abstain(request, _quality)
    if _withheld:
        logger.info(
            "abstain: withholding autonomous draft — %s (model=%s)",
            _withhold_reason, model_used,
        )

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
        exemplar_cache_hit=exemplar_cache_hit,
        exemplar_cache_key=exemplar_cache_key,
        length_flag=length_flag,
        repairs=repairs,
        candidates=candidates,
        quality_score=_quality,
        verify_issues=_verify_issues,
        # b175: cloud-drafting transparency. True/notice iff a cloud backend
        # actually ran (model_used == "claude"); the centralized gate above
        # guarantees that only happens on an explicit, opted-in, flag-enabled
        # interactive request — never on a background/eval path.
        cloud_used=_is_cloud_backend(model_used),
        egress_notice=(_CLOUD_EGRESS_NOTICE if _is_cloud_backend(model_used) else None),
        # b188: abstain telemetry. ``withheld``/``withhold_reason`` are non-None
        # only on the gated autonomous path above; the draft text/quality_score
        # are still populated so nothing is discarded silently.
        withheld=_withheld,
        withhold_reason=_withhold_reason,
    )
