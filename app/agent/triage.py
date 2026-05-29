"""Triage orchestrator: fetch unread → filter → draft what needs a reply.

Phase 1 (this module): one-shot run, in-process, no persistence. The CLI
prints what would happen; a follow-up PR adds the ``agent_pending_drafts``
table and the ``/triage`` page. No auto-send, ever, in any phase that
ships from this module.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from app.agent.inbox_fetch import InboxMessage, fetch_unread
from app.agent.needs_reply import NeedsReplyVerdict, SenderHistory, classify_many

logger = logging.getLogger(__name__)

# Per-account sweep serialization. A scheduled tick and a manual / API triage
# can fire for the same account at once (e.g. the user clicks "Run triage" just
# as the notification lands). Without serialization both fetch the same unread
# set and each consume the daily_draft_cap budget independently, so the cap —
# the runaway-loop guardrail — can be exceeded ~2x; the message_id UNIQUE
# constraint stops duplicate ROWS but not duplicate model spend. A non-blocking
# per-account lock makes the second caller a no-op: the in-flight sweep already
# covers the inbox, and skipping is cheaper than blocking an HTTP request for a
# full sweep. These locks live for the process; the dict only ever grows by the
# number of distinct accounts.
_sweep_locks: dict[str, threading.Lock] = {}
_sweep_locks_guard = threading.Lock()


def _account_lock(account: str) -> threading.Lock:
    with _sweep_locks_guard:
        lk = _sweep_locks.get(account)
        if lk is None:
            lk = threading.Lock()
            _sweep_locks[account] = lk
        return lk


def _empty_sweep_accum() -> dict[str, Any]:
    """Default accumulators so a sweep that fails before producing anything
    still has well-formed values for the audit row + result."""
    return {
        "messages": [], "drafts": [], "skipped": [], "surfaced": [],
        "persisted": 0, "kept_count": 0, "errors_list": [], "auto_promoted": [],
    }


def _log_sweep_safe(
    database_url: str,
    *,
    account: str,
    trigger: str,
    window: str,
    threshold: float,
    accum: dict[str, Any],
    fatal_error: str | None,
    standing_instructions: str | None,
    started_at: str,
    t0: float,
) -> None:
    """Append exactly one ``agent_audit`` row for the sweep — on success,
    partial success, or fatal error. This is what makes a totally-failed sweep
    (expired gog auth, network down) VISIBLE: previously a sweep that raised
    before the end never logged, so the observability success-rate stayed green
    while the agent was dead. Never raises (audit fidelity < agent uptime)."""
    import time as _time
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    try:
        from app.agent.store import log_sweep

        errors = list(accum.get("errors_list") or [])
        if fatal_error:
            errors.append(fatal_error)
        log_sweep(
            database_url,
            account=account,
            trigger=trigger,
            window=window,
            threshold=threshold,
            fetched=len(accum.get("messages") or []),
            kept=int(accum.get("kept_count") or 0),
            surfaced=len(accum.get("surfaced") or []),
            persisted=int(accum.get("persisted") or 0),
            errors=errors,
            standing_instructions_snapshot=standing_instructions,
            started_at=started_at,
            finished_at=_dt.now(_tz.utc).isoformat(),
            duration_ms=int((_time.monotonic() - t0) * 1000),
            auto_promoted_senders=accum.get("auto_promoted") or [],
        )
    except Exception as exc:
        logger.warning("triage audit log failed: %s", exc)


@dataclass
class TriageDraft:
    message: InboxMessage
    verdict: NeedsReplyVerdict
    draft: str | None = None
    model_used: str | None = None
    repairs: list[str] = field(default_factory=list)
    error: str | None = None
    # The effective standing instructions used for THIS draft (global +
    # any matched per-sender/intent rules). Persisted so the operator can
    # see why a draft took a stance.
    standing_instructions_snapshot: str | None = None


@dataclass
class TriageResult:
    fetched: int
    kept: int
    drafts: list[TriageDraft]
    skipped: list[tuple[InboxMessage, NeedsReplyVerdict]]
    surfaced: list[tuple[InboxMessage, NeedsReplyVerdict]] = field(default_factory=list)
    persisted: int = 0      # rows actually inserted (idempotent: 0 on repeat runs)
    # Tiered auto-push outcomes (opt-in). Each entry:
    # {"id", "action": "pushed"|"would_push"|"skipped_cap"|"error", "gmail_draft_id"?}.
    auto_pushed: list[dict[str, Any]] = field(default_factory=list)


# --- tiered auto-push (opt-in; stays inside the never-send boundary) --------


def _auto_push_config() -> dict[str, Any]:
    """Read ``agent.auto_push.*`` config with safe, conservative defaults.

    Auto-push creates a Gmail DRAFT (never sends) for high-confidence replies to
    known, whitelisted senders. Every guardrail defaults to the safe value:
    disabled, dry-run on, an empty whitelist (which means nothing is pushed),
    a high confidence floor, and a low daily cap."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    ap = (a.get("auto_push") or {}) if isinstance(a, dict) else {}
    if not isinstance(ap, dict):
        ap = {}

    def _f(key, default):
        try:
            return float(ap.get(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key, default):
        try:
            return int(ap.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(ap.get("enabled", False)),
        "dry_run": bool(ap.get("dry_run", True)),
        "confidence_floor": _f("confidence_floor", 0.85),
        "min_pairs": _i("known_sender_min_pairs", 3),
        "daily_push_cap": _i("daily_push_cap", 5),
        "whitelist": _parse_autopush_whitelist(ap.get("whitelist")),
    }


def _parse_autopush_whitelist(raw: Any) -> list[str]:
    """Normalise the whitelist (comma/newline string or list) to lowercase entries."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = [s.strip() for s in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        return []
    return [s.lower() for s in items if s]


def _sender_in_whitelist(sender_email: str | None, whitelist: list[str]) -> bool:
    """True if the sender matches a whitelist entry (exact email or @domain)."""
    if not sender_email or not whitelist:
        return False
    email = sender_email.lower()
    for entry in whitelist:
        if entry.startswith("@"):
            if email.endswith(entry):
                return True
        elif email == entry:
            return True
    return False


def _maybe_auto_push(
    *,
    database_url: str,
    account: str,
    history: SenderHistory | None,
    candidates: list[tuple[int, TriageDraft]],
) -> list[dict[str, Any]]:
    """Auto-create Gmail drafts for the qualifying freshly-persisted rows.

    A row qualifies only if ALL hold: auto-push enabled; a non-empty whitelist
    matches the sender; the draft generated cleanly and isn't cold-outreach;
    the needs-reply score ≥ confidence_floor; prior reply pairs with the sender
    ≥ min_pairs; and the per-day cap isn't exhausted. In dry-run (the default)
    it only logs what it WOULD push. Never sends — only writes Gmail Drafts via
    the same idempotent path the manual route uses. Failure-isolated.
    """
    cfg = _auto_push_config()
    if not cfg["enabled"] or not candidates:
        return []
    whitelist = cfg["whitelist"]
    if not whitelist:
        # Enabled but no whitelist → nothing is pushed (safety). Surface once.
        logger.info("auto-push enabled but agent.auto_push.whitelist is empty — nothing auto-pushed")
        return []

    floor = cfg["confidence_floor"]
    min_pairs = cfg["min_pairs"]
    dry_run = cfg["dry_run"]
    cap = cfg["daily_push_cap"]
    if cap <= 0:
        # cap == 0 disables auto-push entirely.
        return []

    from app.agent.push import push_pending_row
    from app.agent.store import count_pushed_today

    remaining: int = max(0, cap - count_pushed_today(database_url, account=account))

    results: list[dict[str, Any]] = []
    for row_id, d in candidates:
        if d.draft is None or d.error:
            continue
        if d.verdict.cold_outreach:
            continue
        if d.verdict.score < floor:
            continue
        sender_email = d.message.sender_email
        if not _sender_in_whitelist(sender_email, whitelist):
            continue
        if history is not None and history.count_for(sender_email) < min_pairs:
            continue

        if dry_run:
            logger.info(
                "auto-push (dry-run): WOULD push row %s — score %.2f, sender %s",
                row_id, d.verdict.score, sender_email,
            )
            results.append({"id": row_id, "action": "would_push"})
            continue
        if remaining <= 0:
            results.append({"id": row_id, "action": "skipped_cap"})
            continue
        try:
            outcome = push_pending_row(database_url, row_id)
        except Exception as exc:  # never let auto-push break the sweep
            logger.warning("auto-push failed for row %s: %s", row_id, exc)
            results.append({"id": row_id, "action": "error", "detail": str(exc)})
            continue
        if outcome.ok:
            remaining -= 1
            results.append({"id": row_id, "action": "pushed", "gmail_draft_id": outcome.gmail_draft_id})
            logger.info("auto-pushed row %s to Gmail (draft %s)", row_id, outcome.gmail_draft_id)
        else:
            results.append({"id": row_id, "action": "error", "detail": outcome.detail})
    return results


def run_triage(
    *,
    account: str,
    window: str = "7d",
    limit: int = 50,
    threshold: float = 0.6,
    database_url: str | None = None,
    configs_dir: Any = None,
    backend: str | None = None,
    persist: bool = True,
    standing_instructions: str | None = None,
    trigger: str = "manual",  # ε: tagged in agent_audit so /triage shows
                              # who started which sweep (scheduled vs manual vs api)
) -> TriageResult:
    """Fetch unread, filter, generate drafts for the survivors, persist to
    ``agent_pending_drafts``.

    ``database_url`` and ``configs_dir`` default to the active instance via
    ``get_settings()``. ``persist=False`` runs purely in-memory (dry-run
    mode used by the CLI's ``--dry-run`` flag).
    """
    # Resolve settings only if the caller didn't pass overrides. Lets tests
    # drop in mocks without touching the global settings cache.
    if database_url is None or configs_dir is None:
        from app.core.settings import get_settings

        settings = get_settings()
        database_url = database_url or settings.database_url
        configs_dir = configs_dir or settings.configs_dir

    # ε: bracket the whole sweep so we can log a single agent_audit row at
    # the end — start time, duration, counts, any per-message errors.
    import time as _time
    from datetime import datetime
    from datetime import timezone as _tz

    _started_at_iso = datetime.now(_tz.utc).isoformat()
    _t0 = _time.monotonic()

    # δ: if the caller didn't pass standing instructions, fall back to the
    # ``agent.standing_instructions`` config value so a /triage user that
    # invokes run_triage manually (or via the API trigger) still gets the
    # current standing instructions applied. ``None`` means "don't inject."
    if standing_instructions is None:
        try:
            from app.agent.scheduler import get_agent_config

            standing_instructions = get_agent_config().get("standing_instructions") or None
        except Exception:
            standing_instructions = None

    # #6: serialize sweeps per account. If a sweep for this account is already
    # running (a scheduled tick overlapping a manual/API run), skip — the
    # in-flight sweep already covers the inbox, and running a second pass would
    # only burn model time and risk exceeding the daily cap.
    lock = _account_lock(account)
    if not lock.acquire(blocking=False):
        logger.info(
            "triage: a sweep for %s is already in progress — skipping this %s run",
            account, trigger,
        )
        return TriageResult(fetched=0, kept=0, drafts=[], skipped=[], surfaced=[], persisted=0)

    accum = _empty_sweep_accum()
    fatal_error: str | None = None
    try:
        accum = _run_sweep(
            account=account, window=window, limit=limit, threshold=threshold,
            database_url=database_url, configs_dir=configs_dir, backend=backend,
            persist=persist, standing_instructions=standing_instructions,
        )
    except Exception as exc:
        # #4: a sweep that raises (auth expiry, network blip, DB locked) must
        # not vanish — record it so the finally-block audit row captures it,
        # then re-raise so the scheduler counts the failure and can alert.
        fatal_error = f"{type(exc).__name__}: {exc}"
        logger.warning("triage sweep failed for %s (%s): %s", account, trigger, exc)
        raise
    finally:
        lock.release()
        _log_sweep_safe(
            database_url, account=account, trigger=trigger, window=window,
            threshold=threshold, accum=accum, fatal_error=fatal_error,
            standing_instructions=standing_instructions,
            started_at=_started_at_iso, t0=_t0,
        )

    return TriageResult(
        fetched=len(accum["messages"]),
        kept=accum["kept_count"],
        drafts=accum["drafts"],
        skipped=accum["skipped"],
        surfaced=accum["surfaced"],
        persisted=accum["persisted"],
        auto_pushed=accum.get("auto_pushed") or [],
    )


def _run_sweep(
    *,
    account: str,
    window: str,
    limit: int,
    threshold: float,
    database_url: str,
    configs_dir: Any,
    backend: str | None,
    persist: bool,
    standing_instructions: str | None,
) -> dict[str, Any]:
    """The sweep body: label-sync → fetch → classify → cap → draft → persist →
    auto-promote. Returns the accumulators ``run_triage`` needs for its audit
    row and result. A raise here propagates to ``run_triage``, whose finally
    block logs the failure."""
    # b57: Sync Gmail-label dismissals BEFORE fetching unread, so any rows
    # the user labelled YouOS/skip from their phone get dismissed in this
    # sweep (and so the next-step skip_senders / counters reflect them).
    # Failure-isolated: a label-sync error here logs and continues.
    try:
        from app.agent.gmail_label_sync import sync_gmail_label_dismissals

        _label_result = sync_gmail_label_dismissals(
            account=account, database_url=database_url,
        )
        if _label_result.dismissed:
            logger.info(
                "gmail-label sync: dismissed %d row(s) from labelled threads before triage",
                len(_label_result.dismissed),
            )
    except Exception as exc:
        logger.warning("gmail-label sync failed: %s", exc)

    # 1) Fetch unread inbox threads.
    messages = fetch_unread(account, window=window, limit=limit, backend=backend)

    # 2) Score + filter. Sender-history uses the active instance's DB so a
    # repeat-correspondent gets the prior-pairs boost.
    history = SenderHistory.from_database_url(database_url)

    # ζ: read safety guardrails (skip-list, daily cap, strict-local) from
    # config. Pull them once per sweep so the values are stable across all
    # messages in the same triage even if the config is being edited.
    try:
        from app.agent.scheduler import get_agent_config

        _cfg = get_agent_config()
        skip_senders = _cfg.get("skip_senders") or []
        vip_senders = _cfg.get("vip_senders") or []
        daily_cap = int(_cfg.get("daily_draft_cap") or 0)
        strict_local = bool(_cfg.get("strict_local") or False)
    except Exception:
        skip_senders, vip_senders, daily_cap, strict_local = [], [], 0, False

    # Structured per-sender/intent rules (decline recruiters, CC partner for
    # client X, propose slots for meetings, skip cold outreach). Loaded once
    # per sweep. Empty when unconfigured → no behavior change.
    from app.agent.rules import apply_rules, load_rules, rules_need_intent

    rules = load_rules()
    _rules_need_intent = rules_need_intent(rules)

    classified = classify_many(
        messages, history=history, threshold=threshold,
        skip_senders=skip_senders, vip_senders=vip_senders,
    )

    # ζ: daily cap — count already-persisted rows for this account today, then
    # cap how many MORE we'll write this sweep. 0 disables. Hit-cap drafts are
    # still classified (and audit-logged in their counts), just not persisted
    # or generated to avoid burning model time on dropped output.
    cap_remaining: int | float
    if persist and daily_cap > 0:
        from app.agent.store import count_persisted_today

        already = count_persisted_today(database_url, account=account)
        cap_remaining = max(0, daily_cap - already)
    else:
        cap_remaining = float("inf")

    # 3) Draft the survivors via the same generation pipeline /feedback uses.
    from app.generation.service import DraftRequest, generate_draft

    drafts: list[TriageDraft] = []
    skipped: list[tuple[InboxMessage, NeedsReplyVerdict]] = []
    surfaced: list[tuple[InboxMessage, NeedsReplyVerdict]] = []
    cap_hit_count = 0
    for msg, verdict in classified:
        if not verdict.needs_reply:
            skipped.append((msg, verdict))
            if verdict.surface_for_review:
                surfaced.append((msg, verdict))
            continue
        # ζ: daily cap — stop generating *and persisting* new drafts once the
        # account's UTC-day quota is exhausted. Record as a skip with a clear
        # reason so the operator can see why a sweep ended quiet.
        if cap_remaining <= 0:
            cap_hit_count += 1
            verdict_capped = NeedsReplyVerdict(
                needs_reply=False, score=verdict.score,
                reasons=verdict.reasons + [f"daily cap reached ({daily_cap})"],
                cold_outreach=verdict.cold_outreach,
                surface_for_review=False,
            )
            skipped.append((msg, verdict_capped))
            continue

        # Per-message standing-instruction rules. Compute the effective
        # instructions (global + matched rules); a 'skip' rule drops the
        # message from drafting entirely.
        effective_instructions = standing_instructions
        if rules:
            _intents: list[str] | None = None
            if _rules_need_intent:
                try:
                    from app.core.intent import classify_intents_multi

                    _intents = classify_intents_multi(msg.body)
                except Exception:
                    _intents = None
            from app.core.sender import extract_domain

            _rr = apply_rules(
                rules,
                sender_email=msg.sender_email,
                domain=extract_domain(msg.sender or msg.sender_email or ""),
                intents=_intents,
                cold_outreach=verdict.cold_outreach,
                base_instructions=standing_instructions,
            )
            if _rr["skip"]:
                verdict_ruleskip = NeedsReplyVerdict(
                    needs_reply=False, score=verdict.score,
                    reasons=verdict.reasons + ["skipped by agent.rules"],
                    cold_outreach=verdict.cold_outreach,
                    surface_for_review=False,
                )
                skipped.append((msg, verdict_ruleskip))
                continue
            effective_instructions = _rr["instructions"]

        try:
            resp = generate_draft(
                DraftRequest(
                    inbound_message=msg.body,
                    sender=msg.sender or msg.sender_email or None,
                    subject=msg.subject,
                    account_email=account,
                    thread_id=msg.thread_id,
                    # Conversation history so the drafter doesn't answer the
                    # wrong question in a multi-turn thread (populated by
                    # fetch_unread from the real thread).
                    thread_history=msg.thread_history or None,
                    standing_instructions=effective_instructions,
                    # ζ: refuse cloud fallback during background triage when
                    # strict-local is on. The generation pipeline reads this
                    # via DraftRequest; cold-start before the LoRA is trained
                    # is still served by the model server, but a hard local
                    # failure won't silently go to Claude.
                    strict_local=strict_local,
                ),
                database_url=database_url,
                configs_dir=configs_dir,
            )
            drafts.append(
                TriageDraft(
                    message=msg,
                    verdict=verdict,
                    draft=resp.draft,
                    model_used=resp.model_used,
                    repairs=list(getattr(resp, "repairs", []) or []),
                    standing_instructions_snapshot=effective_instructions,
                )
            )
            cap_remaining -= 1  # ζ: one less slot for this UTC day
        except Exception as exc:
            logger.warning("triage draft generation failed for %s: %s", msg.message_id, exc)
            drafts.append(
                TriageDraft(
                    message=msg, verdict=verdict, error=f"{type(exc).__name__}: {exc}",
                    standing_instructions_snapshot=effective_instructions,
                )
            )

    persisted = 0
    auto_push_candidates: list[tuple[int, TriageDraft]] = []
    if persist:
        from app.agent.store import upsert_pending

        # Tier 1: real drafts (needs_reply=True, generated successfully or not).
        for d in drafts:
            row_id = upsert_pending(
                database_url=database_url,
                message_id=d.message.message_id,
                thread_id=d.message.thread_id,
                account=d.message.account,
                sender=d.message.sender,
                sender_email=d.message.sender_email,
                subject=d.message.subject,
                body=d.message.body,
                received_at=d.message.received_at,
                needs_reply_score=d.verdict.score,
                reasons=d.verdict.reasons,
                cold_outreach=d.verdict.cold_outreach,
                tier="draft",
                draft=d.draft,
                draft_model=d.model_used,
                draft_repairs=d.repairs,
                standing_instructions_snapshot=d.standing_instructions_snapshot,
            )
            if row_id is not None:
                persisted += 1
                # Only freshly-persisted draft rows are auto-push candidates
                # (row_id is None on the idempotent repeat — already handled).
                auto_push_candidates.append((row_id, d))

        # Tier 2: borderline cases (no draft generated; the UI shows them
        # collapsed so the user can act manually).
        for msg, verdict in surfaced:
            row_id = upsert_pending(
                database_url=database_url,
                message_id=msg.message_id,
                thread_id=msg.thread_id,
                account=msg.account,
                sender=msg.sender,
                sender_email=msg.sender_email,
                subject=msg.subject,
                body=msg.body,
                received_at=msg.received_at,
                needs_reply_score=verdict.score,
                reasons=verdict.reasons,
                cold_outreach=verdict.cold_outreach,
                tier="surface",
                draft=None,
                draft_model=None,
                draft_repairs=[],
                standing_instructions_snapshot=standing_instructions,
            )
            if row_id is not None:
                persisted += 1

    kept_count = sum(1 for d in drafts if d.error is None)
    errors_list = [d.error for d in drafts if d.error]

    # b44 / b52: Auto-promote senders dismissed as 'noise' ≥3 times to
    # agent.skip_senders. Opt-in (off by default). Now runs *before* log_sweep
    # so the audit row can capture which senders were auto-added in this
    # sweep (b52 surfaces these in /triage Recent activity). Failure-isolated:
    # an exception here returns [] and the sweep still gets logged.
    auto_promoted: list[str] = []
    try:
        auto_promoted = _maybe_auto_promote_skip_senders(
            database_url=database_url, account=account,
        )
    except Exception as exc:
        logger.warning("auto-promote skip_senders failed: %s", exc)

    # Tiered auto-push: opt-in, dry-run by default, whitelist-gated. Stays
    # inside the never-send boundary (writes Gmail Drafts only). Failure-isolated.
    auto_pushed: list[dict[str, Any]] = []
    try:
        auto_pushed = _maybe_auto_push(
            database_url=database_url, account=account,
            history=history, candidates=auto_push_candidates,
        )
    except Exception as exc:
        logger.warning("auto-push step failed: %s", exc)

    return {
        "messages": messages,
        "drafts": drafts,
        "skipped": skipped,
        "surfaced": surfaced,
        "persisted": persisted,
        "kept_count": kept_count,
        "errors_list": errors_list,
        "auto_promoted": auto_promoted,
        "auto_pushed": auto_pushed,
    }


# Threshold for auto-promotion. Higher than the UI's min_count=2 because
# auto-action without click should require stronger signal than a
# user-confirmed promotion.
_AUTO_PROMOTE_MIN_COUNT = 3
_AUTO_PROMOTE_WINDOW_DAYS = 30


def _maybe_auto_promote_skip_senders(*, database_url: str, account: str) -> list[str]:
    """If ``agent.auto_promote_skip_senders`` is enabled, promote any sender
    dismissed as 'noise' ≥3 times in the last 30d to ``agent.skip_senders``.

    Returns the list of newly-promoted senders (empty if disabled, or none
    qualified, or all candidates were already on the list). Errors are
    swallowed at the caller; the agent loop has higher priorities than its
    own self-tuning.
    """
    from app.agent.store import noise_dismissal_candidates
    from app.core.feature_flags import get_flag, set_flag

    if not bool(get_flag("agent.auto_promote_skip_senders")):
        return []

    candidates = noise_dismissal_candidates(
        database_url,
        account=account,
        days=_AUTO_PROMOTE_WINDOW_DAYS,
        min_count=_AUTO_PROMOTE_MIN_COUNT,
    )
    if not candidates:
        return []

    # Read current value, append any new entries, write back. Mirrors the
    # /api/agent/skip_senders/promote route's logic — keep these in sync.
    current = get_flag("agent.skip_senders") or ""
    sep = "\n" if "\n" in current else ", "
    existing = {
        s.strip().lower()
        for s in current.replace("\n", ",").split(",")
        if s.strip()
    }
    added: list[str] = []
    new_value = current
    for c in candidates:
        s = (c.get("sender_email") or "").strip().lower()
        if not s or s in existing:
            continue
        existing.add(s)
        added.append(s)
        new_value = (new_value + sep + s) if new_value else s

    if not added:
        return []

    try:
        set_flag("agent.skip_senders", new_value)
    except (KeyError, ValueError) as exc:
        logger.warning("auto-promote: set_flag failed (%s)", exc)
        return []

    logger.info(
        "auto-promoted %d sender(s) to agent.skip_senders for account=%s: %s",
        len(added), account, ", ".join(added),
    )
    return added
