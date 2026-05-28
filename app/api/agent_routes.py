"""Agent triage API + `/triage` page route.

REST surfaces for the autonomous-agent loop's pending drafts: list them,
amend, dismiss, mark sent, trigger a fresh triage run. The page itself
serves ``templates/triage.html``.

Never auto-sends. β.2 only shows you what's pending; β/Phase 2 adds the
``gmail.compose`` write so "Mark sent" can actually push to Gmail Drafts.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.agent import store

router = APIRouter(tags=["agent"])
_TEMPLATE = Path(__file__).resolve().parents[1].parent / "templates" / "triage.html"


def _db_url(request: Request) -> str:
    return request.app.state.settings.database_url


# --- read --------------------------------------------------------------------


@router.get("/api/agent/sweeps")
def list_agent_sweeps(
    request: Request,
    account: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    """Recent triage sweeps from the audit log — what the agent did, when,
    by which trigger, with what result + any per-message errors."""
    sweeps = store.list_recent_sweeps(_db_url(request), account=account, limit=limit)
    return {"count": len(sweeps), "sweeps": sweeps}


@router.get("/api/agent/pending")
def list_agent_pending(
    request: Request,
    account: str | None = Query(None),
    tier: str | None = Query(None, pattern="^(draft|surface)$"),
    status: str = Query("pending", pattern="^(pending|amended|sent|dismissed)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    rows = store.list_pending(
        _db_url(request),
        account=account,
        status=status,
        tier=tier if tier in ("draft", "surface") else None,
        limit=limit,
    )
    return {"count": len(rows), "rows": rows}


# --- state transitions -------------------------------------------------------


class AmendBody(BaseModel):
    amended_draft: str = Field(min_length=1)


@router.post("/api/agent/pending/{row_id}/amend")
def amend(row_id: int, body: AmendBody, request: Request) -> dict:
    if not store.mark_amended(_db_url(request), row_id, amended_draft=body.amended_draft):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


class DismissBody(BaseModel):
    """Optional dismissal reason — categorical so we can aggregate.

    The body itself is optional on the wire (legacy UI sends an empty POST);
    when present, ``reason`` must be one of ``store.DISMISSAL_REASONS``.
    """

    reason: str | None = Field(default=None)


@router.post("/api/agent/pending/{row_id}/dismiss")
def dismiss(row_id: int, request: Request, body: DismissBody | None = None) -> dict:
    reason = body.reason if body else None
    if reason is not None and reason not in store.DISMISSAL_REASONS:
        raise HTTPException(
            400,
            f"unknown dismissal reason: {reason!r} "
            f"(allowed: {', '.join(store.DISMISSAL_REASONS)})",
        )
    if not store.mark_dismissed(_db_url(request), row_id, reason=reason):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


@router.get("/api/agent/dismissal_stats")
def dismissal_stats(
    request: Request,
    account: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """Rolling-window dismissal aggregate — drives the observability surface.

    Returns total persisted vs dismissed, the dismissal rate, and a
    by-reason breakdown over the last ``days`` days.
    """
    return store.dismissal_stats(_db_url(request), account=account, days=days)


@router.post("/api/agent/pending/{row_id}/mark_sent")
def mark_sent(row_id: int, request: Request) -> dict:
    if not store.mark_sent(_db_url(request), row_id):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


@router.post("/api/agent/pending/{row_id}/push_to_gmail")
def push_to_gmail(row_id: int, request: Request) -> dict:
    """Phase 2: create a real Gmail Drafts entry for this pending row.

    Uses the configured ``ingestion.google_backend``; ``gog`` is supported,
    ``gws`` and ``native`` will raise NotImplementedError until Phase 2.2.
    On success, marks the row as ``sent`` (the user finishes-and-sends
    from Gmail) and stores the Gmail draft id for traceability.

    The draft text used is ``amended_draft`` if the user edited it, else the
    original ``draft`` field. Threading uses the inbound's ``thread_id`` so
    Gmail shows it as a draft reply on the original conversation.
    """
    db_url = _db_url(request)
    row = store.get(db_url, row_id)
    if not row:
        raise HTTPException(404, "pending row not found")
    if row.get("tier") != "draft" or not (row.get("amended_draft") or row.get("draft")):
        raise HTTPException(400, "row has no draft to push (tier=surface, or draft is empty)")
    if not row.get("sender_email"):
        raise HTTPException(400, "row has no sender_email; cannot route the reply")

    body = row.get("amended_draft") or row.get("draft") or ""
    raw_subject = row.get("subject") or ""
    # Gmail handles threading from the thread_id, but we still prepend "Re: "
    # if missing so the user sees the conventional subject in their Drafts.
    subject = raw_subject if raw_subject.lower().startswith("re:") else f"Re: {raw_subject}"

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    try:
        result = create_draft(
            account=row["account"],
            thread_id=row.get("thread_id"),
            to_email=row["sender_email"],
            subject=subject,
            body=body,
        )
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc))
    except GmailWriteError as exc:
        raise HTTPException(502, f"Gmail write failed: {exc}")

    # Persist the draft id alongside the sent timestamp.
    store.mark_sent(db_url, row_id, gmail_draft_id=result.draft_id)
    return {
        "ok": True,
        "gmail_draft_id": result.draft_id,
        "row": store.get(db_url, row_id),
    }


# --- triage trigger ----------------------------------------------------------


class TriageRunBody(BaseModel):
    account: str | None = None
    window: str = "7d"
    limit: int = 25
    threshold: float = 0.6
    backend: str | None = None


@router.post("/api/agent/triage")
def trigger_triage(body: TriageRunBody, request: Request) -> dict:
    """Run a fresh triage sweep (synchronous; UI shows a loading indicator).

    Per-account selection: caller passes ``account`` explicitly. β.2 just
    triages one inbox per request; γ adds the background scheduler that
    loops both configured accounts.
    """
    from app.agent.triage import run_triage
    from app.core.config import get_user_emails

    account = body.account
    if not account:
        emails = get_user_emails()
        if not emails:
            raise HTTPException(400, "no account configured (user.emails empty)")
        account = emails[0]

    settings = request.app.state.settings
    result = run_triage(
        account=account,
        window=body.window,
        limit=body.limit,
        threshold=body.threshold,
        database_url=settings.database_url,
        configs_dir=settings.configs_dir,
        backend=body.backend,
        trigger="api",
    )
    return {
        "account": account,
        "fetched": result.fetched,
        "kept": result.kept,
        "surfaced": len(result.surfaced),
        "persisted": result.persisted,
    }


# --- page --------------------------------------------------------------------


@router.get("/triage", response_class=HTMLResponse)
def triage_page() -> HTMLResponse:
    """Render the agent-triage page. Templates served per-request so live
    edits are visible without server restart (matches existing pages)."""
    if not _TEMPLATE.exists():
        raise HTTPException(500, "triage template missing")
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))
