"""Triage orchestrator: fetch unread → filter → draft what needs a reply.

Phase 1 (this module): one-shot run, in-process, no persistence. The CLI
prints what would happen; a follow-up PR adds the ``agent_pending_drafts``
table and the ``/triage`` page. No auto-send, ever, in any phase that
ships from this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.inbox_fetch import InboxMessage, fetch_unread
from app.agent.needs_reply import NeedsReplyVerdict, SenderHistory, classify_many

logger = logging.getLogger(__name__)


@dataclass
class TriageDraft:
    message: InboxMessage
    verdict: NeedsReplyVerdict
    draft: str | None = None
    model_used: str | None = None
    repairs: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TriageResult:
    fetched: int
    kept: int
    drafts: list[TriageDraft]
    skipped: list[tuple[InboxMessage, NeedsReplyVerdict]]
    surfaced: list[tuple[InboxMessage, NeedsReplyVerdict]] = field(default_factory=list)
    persisted: int = 0      # rows actually inserted (idempotent: 0 on repeat runs)


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
        daily_cap = int(_cfg.get("daily_draft_cap") or 0)
        strict_local = bool(_cfg.get("strict_local") or False)
    except Exception:
        skip_senders, daily_cap, strict_local = [], 0, False

    classified = classify_many(
        messages, history=history, threshold=threshold, skip_senders=skip_senders,
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
        try:
            resp = generate_draft(
                DraftRequest(
                    inbound_message=msg.body,
                    sender=msg.sender or msg.sender_email or None,
                    subject=msg.subject,
                    account_email=account,
                    thread_id=msg.thread_id,
                    standing_instructions=standing_instructions,
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
                )
            )
            cap_remaining -= 1  # ζ: one less slot for this UTC day
        except Exception as exc:
            logger.warning("triage draft generation failed for %s: %s", msg.message_id, exc)
            drafts.append(
                TriageDraft(
                    message=msg, verdict=verdict, error=f"{type(exc).__name__}: {exc}"
                )
            )

    persisted = 0
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
                standing_instructions_snapshot=standing_instructions,
            )
            if row_id is not None:
                persisted += 1

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

    # ε: append one row per sweep — always written, regardless of persist=.
    # The audit log records *attempts*, not outcomes; --dry-run still leaves
    # a trace of what was swept (with persisted=0).
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz2

        _finished_at_iso = _dt.now(_tz2.utc).isoformat()
        _duration_ms = int((_time.monotonic() - _t0) * 1000)
        from app.agent.store import log_sweep

        log_sweep(
            database_url,
            account=account,
            trigger=trigger,
            window=window,
            threshold=threshold,
            fetched=len(messages),
            kept=kept_count,
            surfaced=len(surfaced),
            persisted=persisted,
            errors=errors_list,
            standing_instructions_snapshot=standing_instructions,
            started_at=_started_at_iso,
            finished_at=_finished_at_iso,
            duration_ms=_duration_ms,
            auto_promoted_senders=auto_promoted,
        )
    except Exception as exc:
        # Audit-log failure must not propagate — the agent loop has higher
        # priorities than its own observability.
        logger.warning("triage audit log failed: %s", exc)

    return TriageResult(
        fetched=len(messages),
        kept=kept_count,
        drafts=drafts,
        skipped=skipped,
        surfaced=surfaced,
        persisted=persisted,
    )


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
