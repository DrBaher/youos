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


def run_triage(
    *,
    account: str,
    window: str = "7d",
    limit: int = 50,
    threshold: float = 0.6,
    database_url: str | None = None,
    configs_dir: Any = None,
    backend: str | None = None,
) -> TriageResult:
    """Fetch unread, filter, generate drafts for the survivors.

    ``database_url`` and ``configs_dir`` default to the active instance via
    ``get_settings()``. Pass them explicitly for tests. Returns a structured
    result; no persistence (Phase 1).
    """
    # Resolve settings only if the caller didn't pass overrides. Lets tests
    # drop in mocks without touching the global settings cache.
    if database_url is None or configs_dir is None:
        from app.core.settings import get_settings

        settings = get_settings()
        database_url = database_url or settings.database_url
        configs_dir = configs_dir or settings.configs_dir

    # 1) Fetch unread inbox threads.
    messages = fetch_unread(account, window=window, limit=limit, backend=backend)

    # 2) Score + filter. Sender-history uses the active instance's DB so a
    # repeat-correspondent gets the prior-pairs boost.
    history = SenderHistory.from_database_url(database_url)
    classified = classify_many(messages, history=history, threshold=threshold)

    # 3) Draft the survivors via the same generation pipeline /feedback uses.
    from app.generation.service import DraftRequest, generate_draft

    drafts: list[TriageDraft] = []
    skipped: list[tuple[InboxMessage, NeedsReplyVerdict]] = []
    for msg, verdict in classified:
        if not verdict.needs_reply:
            skipped.append((msg, verdict))
            continue
        try:
            resp = generate_draft(
                DraftRequest(
                    inbound_message=msg.body,
                    sender=msg.sender or msg.sender_email or None,
                    subject=msg.subject,
                    account_email=account,
                    thread_id=msg.thread_id,
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
        except Exception as exc:
            logger.warning("triage draft generation failed for %s: %s", msg.message_id, exc)
            drafts.append(
                TriageDraft(
                    message=msg, verdict=verdict, error=f"{type(exc).__name__}: {exc}"
                )
            )

    return TriageResult(
        fetched=len(messages),
        kept=sum(1 for d in drafts if d.error is None),
        drafts=drafts,
        skipped=skipped,
    )
