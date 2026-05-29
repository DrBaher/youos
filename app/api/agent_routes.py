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


@router.get("/api/agent/pending/{row_id}")
def get_agent_pending(row_id: int, request: Request) -> dict:
    """Fetch a single pending row by id.

    The retry-safety procedure in AGENT_OPERATIONS.md tells orchestrators to
    GET this after a timed-out push_to_gmail to check whether ``gmail_draft_id``
    landed before retrying. It also lets an orchestrator cheaply confirm an
    action took effect without re-listing the whole queue.
    """
    row = store.get(_db_url(request), row_id)
    if not row:
        raise HTTPException(404, "pending row not found")
    return row


# --- state transitions -------------------------------------------------------


class AmendBody(BaseModel):
    amended_draft: str = Field(min_length=1)


@router.post("/api/agent/pending/{row_id}/amend")
def amend(row_id: int, body: AmendBody, request: Request) -> dict:
    if not store.mark_amended(_db_url(request), row_id, amended_draft=body.amended_draft):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


class RegenerateBody(BaseModel):
    """Re-draft a pending row in the user's voice with extra steering.

    Unlike ``amend`` (which takes verbatim replacement text), this re-runs the
    full generation pipeline — persona, exemplars, LoRA — so an orchestrator
    can say "make it shorter and decline the meeting" and get a draft that
    still sounds like the user, instead of having to write the reply itself.
    """

    instruction: str | None = Field(default=None, description="Free-form steer, e.g. 'shorter; decline the meeting'")
    tone_hint: str | None = Field(default=None)
    mode: str | None = Field(default=None, pattern="^(internal|client|personal)$")
    persist: bool = Field(default=True, description="Store as amended_draft; False = preview only")


@router.post("/api/agent/pending/{row_id}/regenerate")
def regenerate(row_id: int, body: RegenerateBody, request: Request) -> dict:
    """Re-generate the reply for a pending row, optionally steered by a
    free-form instruction, and (by default) store it as ``amended_draft``.

    The reply is produced by the same in-voice generation pipeline the rest of
    YouOS uses — never sends. Pass ``persist: false`` to preview without
    overwriting the queued draft.
    """
    db_url = _db_url(request)
    row = store.get(db_url, row_id)
    if not row:
        raise HTTPException(404, "pending row not found")
    inbound = row.get("body") or ""
    if not inbound.strip():
        raise HTTPException(400, "row has no inbound body to regenerate from")

    from app.generation.service import DraftRequest, generate_draft

    settings = request.app.state.settings
    try:
        resp = generate_draft(
            DraftRequest(
                inbound_message=inbound,
                sender=row.get("sender") or row.get("sender_email"),
                subject=row.get("subject"),
                account_email=row.get("account"),
                thread_id=row.get("thread_id"),
                standing_instructions=(body.instruction or None),
                tone_hint=body.tone_hint,
                mode=body.mode,
            ),
            database_url=settings.database_url,
            configs_dir=settings.configs_dir,
        )
    except Exception as exc:
        raise HTTPException(502, f"regeneration failed: {exc}") from exc

    new_draft = resp.draft or ""
    persisted = bool(body.persist and new_draft.strip())
    if persisted:
        store.mark_amended(db_url, row_id, amended_draft=new_draft)
    return {
        "ok": True,
        "draft": new_draft,
        "model_used": resp.model_used,
        "persisted": persisted,
        "row": store.get(db_url, row_id),
    }


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


@router.get("/api/agent/skip_sender_candidates")
def skip_sender_candidates(
    request: Request,
    account: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    min_count: int = Query(2, ge=1, le=20),
) -> dict:
    """Senders the user has repeatedly dismissed as 'noise' — closing the
    feedback loop. Each entry already has count + most-recent subject; the
    /triage UI lets the user promote any subset to ``agent.skip_senders``."""
    return {
        "candidates": store.noise_dismissal_candidates(
            _db_url(request), account=account, days=days, min_count=min_count,
        ),
        "min_count": min_count,
        "window_days": days,
    }


class PromoteSkipSendersBody(BaseModel):
    """Bulk-append senders to ``agent.skip_senders``.

    Preserves the existing separator (comma or newline) so the user's
    chosen formatting in /settings stays intact. Idempotent — senders
    already on the list are skipped (counted under ``already_present``).
    """

    senders: list[str] = Field(min_length=1)


@router.post("/api/agent/skip_senders/promote")
def promote_skip_senders(body: PromoteSkipSendersBody) -> dict:
    """Append the given senders to ``agent.skip_senders`` via the same
    feature-flag whitelist the /settings page uses. Returns counts so the
    UI can render "added 3, already present 1" feedback."""
    from app.core.feature_flags import list_flags, set_flag

    flags = list_flags()
    current_value = ""
    for flag in flags:
        if flag.get("key") == "agent.skip_senders":
            current_value = flag.get("value") or ""
            break

    # Honour the existing separator — comma-then-newline preference matches
    # what the /triage 'also skip sender' checkbox writes.
    sep = "\n" if "\n" in current_value else ", "
    existing = {
        s.strip().lower()
        for s in current_value.replace("\n", ",").split(",")
        if s.strip()
    }

    added: list[str] = []
    already_present: list[str] = []
    for raw in body.senders:
        s = (raw or "").strip().lower()
        if not s:
            continue
        if s in existing:
            already_present.append(s)
        else:
            added.append(s)
            existing.add(s)

    if not added:
        return {
            "ok": True,
            "added": [],
            "already_present": already_present,
            "value": current_value,
        }

    # Rebuild preserving order: original entries first (preserves user's
    # manual layout), then the new ones at the tail.
    new_value = current_value
    for s in added:
        new_value = (new_value + sep + s) if new_value else s

    try:
        saved = set_flag("agent.skip_senders", new_value)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"could not update agent.skip_senders: {exc}") from exc

    return {
        "ok": True,
        "added": added,
        "already_present": already_present,
        "value": saved,
    }


@router.get("/api/agent/resolve")
def resolve(
    request: Request,
    q: str = Query(..., min_length=1, description="Substring to match against subject + sender + sender_email"),
    account: str | None = Query(None),
    status: str = Query("pending", pattern="^(pending|amended|sent|dismissed)$"),
    limit: int = Query(5, ge=1, le=50),
) -> dict:
    """Find pending rows whose subject or sender matches ``q`` — orchestrator
    NLU helper (b62).

    The orchestrator vision: when the user says "push the Q3 pricing email
    to Gmail", the orchestrator hits ``GET /api/agent/resolve?q=Q3 pricing``
    to get the matching row id, then dispatches the action. If multiple
    rows match, the orchestrator can disambiguate in the chat bubble
    ("I found two matches; which one?").

    Ranking: subject-substring match > sender-substring match. Ties broken
    by most-recent-first. Case-insensitive. Substring (LIKE %q%), not fuzzy.
    Fuzzy/embedding matching is a future feature — substring covers the
    "user mentions a real word from the subject" case which is the dominant
    pattern for short chat instructions.
    """
    from app.agent import store

    db_url = _db_url(request)
    # Pull a generous superset and rank in Python — keeps the query simple
    # and avoids depending on FTS5 here (which the agent module deliberately
    # doesn't, per the b49 sqlite-path simplicity).
    rows = store.list_pending(db_url, account=account, status=status, limit=200)
    needle = q.strip().lower()
    matches: list[dict] = []
    for r in rows:
        subj = (r.get("subject") or "").lower()
        sender = (r.get("sender") or "").lower()
        email = (r.get("sender_email") or "").lower()
        score = 0
        where = ""
        if needle in subj:
            score = 100 - subj.index(needle)  # earlier match = better
            where = "subject"
        elif needle in sender or needle in email:
            score = 50 - min((sender + email).index(needle), 50)
            where = "sender"
        else:
            continue
        matches.append({
            "id": r["id"],
            "tier": r.get("tier"),
            "subject": r.get("subject"),
            "sender": r.get("sender"),
            "sender_email": r.get("sender_email"),
            "needs_reply_score": r.get("needs_reply_score"),
            "match_field": where,
            "match_score": score,
        })

    matches.sort(key=lambda m: (m["match_score"], m["id"]), reverse=True)
    return {"q": q, "count": len(matches), "rows": matches[:limit]}


@router.get("/api/agent/digest")
def digest(
    request: Request,
    account: str | None = Query(None),
    days: int = Query(1, ge=1, le=365),
) -> dict:
    """Orchestrator-facing digest endpoint (b59).

    Mirrors ``youos digest --format json`` over HTTP. Designed for
    integrations like OpenClaw / Hermes / a Telegram bot — they POST the
    user's intent ("anything important in my inbox?"), call this once,
    then paraphrase the ``summary`` field into a chat bubble. The
    structured fields are there for drill-downs ("which sender is
    auto-promoted?", "top noise dismissals?").

    See ``docs/INTEGRATIONS.md`` for the full wiring recipe.
    """
    from app.agent.digest import _data_to_dict, build_digest
    from app.core.config import get_user_emails

    if not account:
        emails = get_user_emails()
        account = emails[0] if emails else None
    if not account:
        raise HTTPException(400, "no account configured; pass ?account=...")

    data = build_digest(
        database_url=_db_url(request),
        account=account,
        days=days,
    )
    return _data_to_dict(data)


@router.get("/api/agent/observability")
def observability(
    request: Request,
    account: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """Unified observability payload for the /triage 'Agent health' card.

    Bundles three aggregates so the card needs one fetch:
      * sweep — sweep counts, success rate, total throughput, hard-skipped
      * dismissals — dismissal rate + by-reason breakdown (from b39)
      * score_histogram — bucketed needs_reply scores across persisted rows

    Also emits a small ``hints`` list with rule-based interpretations
    ("noise > 30% — consider raising the threshold or extending
    skip_senders") so the UI doesn't need to encode the thresholds itself.
    """
    db = _db_url(request)
    sweep = store.sweep_aggregate(db, account=account, days=days)
    dismissals = store.dismissal_stats(db, account=account, days=days)
    histogram = store.score_histogram(db, account=account, days=days)

    hints: list[str] = []

    # Heartbeat: an agent that silently stopped sweeping (server died, gog auth
    # expired and every sweep now fails, agent.enabled flipped off) is the
    # central trust failure for unattended running. Surface staleness loudly.
    try:
        from app.agent.scheduler import get_agent_config

        _agent_cfg = get_agent_config()
    except Exception:
        _agent_cfg = {}
    if _agent_cfg.get("enabled"):
        interval_min = int(_agent_cfg.get("interval_minutes") or 15)
        secs_since = sweep.get("seconds_since_last_sweep")
        if secs_since is None and sweep["sweeps"] == 0:
            hints.append(
                "Agent is enabled but no sweeps have been recorded — the "
                "background loop may not be running (check the server is up)."
            )
        elif secs_since is not None and secs_since > max(3 * interval_min * 60, 1800):
            mins = secs_since // 60
            hints.append(
                f"Last sweep was {mins} min ago, but the agent is set to sweep "
                f"every {interval_min} min — it looks stalled. Check "
                "/tmp/youos-serve.log and `youos doctor`."
            )

    # Filter is too generous: lots of drafts ending up dismissed as noise.
    noise = dismissals["by_reason"].get("noise", 0)
    total = dismissals["total_persisted"] or 0
    if total >= 5 and total > 0 and noise / total >= 0.30:
        hints.append(
            f"Noise dismissals = {noise}/{total} ({noise/total:.0%}). "
            "Consider raising agent.threshold or extending agent.skip_senders."
        )
    # Sweep error rate is alarming.
    if sweep["sweeps"] >= 3 and sweep["success_rate"] < 0.8:
        hints.append(
            f"Sweep success rate = {sweep['success_rate']:.0%} "
            f"({sweep['successful']}/{sweep['sweeps']}). Check the Recent activity "
            "panel for the actual errors."
        )
    # Drafting quality signal — wrong_content is a *generation* concern, not
    # a filter one, so we route it to a different remediation.
    wrong_content = dismissals["by_reason"].get("wrong_content", 0)
    if wrong_content >= 3:
        hints.append(
            f"{wrong_content} dismissals as 'wrong_content' — review queue these "
            "as feedback pairs to retrain the LoRA on them."
        )

    # Which model is actually drafting. A remote user reading this card (not the
    # per-row UI badge) otherwise can't tell when drafts silently fell back to
    # the base/cloud model — they'd keep pushing un-personalized drafts.
    drafting: dict | None = None
    try:
        from app.core.stats import get_drafting_model_status

        drafting = get_drafting_model_status(db)
        if not drafting.get("healthy", True):
            hints.append(f"Drafting: {drafting.get('label')} — {drafting.get('detail')}")
    except Exception:
        drafting = None

    return {
        "sweep": sweep,
        "dismissals": dismissals,
        "score_histogram": histogram,
        "drafting": drafting,
        "hints": hints,
    }


@router.post("/api/agent/pending/{row_id}/mark_sent")
def mark_sent(row_id: int, request: Request) -> dict:
    if not store.mark_sent(_db_url(request), row_id):
        raise HTTPException(404, "pending row not found")
    return {"ok": True, "row": store.get(_db_url(request), row_id)}


class SaveAsTrainingPairBody(BaseModel):
    """Capture the user's better version of an agent draft as a feedback pair.

    The LoRA training pipeline picks up rows from ``feedback_pairs``; this
    endpoint is the bridge from the agent's review queue into that pipeline.
    Rating defaults to 2 (the agent drafted something, the user is editing
    it because it was wrong) but can be overridden.
    """

    edited_reply: str = Field(min_length=1)
    rating: int | None = Field(default=2, ge=1, le=5)
    feedback_note: str | None = None


@router.post("/api/agent/pending/{row_id}/save_as_feedback_pair")
def save_as_feedback_pair(row_id: int, body: SaveAsTrainingPairBody, request: Request) -> dict:
    """Capture (inbound, generated_draft, edited_reply) as a feedback pair.

    Closes the dismissal-feedback loop for ``wrong_content`` dismissals
    (or any time the user has a better version): the row's inbound + the
    agent's draft + the user's correction become a training pair for the
    nightly LoRA retrain. Doesn't alter the agent row's status — the user
    can also push to Gmail, mark sent, or dismiss separately.
    """
    db_url = _db_url(request)
    row = store.get(db_url, row_id)
    if not row:
        raise HTTPException(404, "pending row not found")
    if row.get("tier") != "draft" or not row.get("draft"):
        raise HTTPException(400, "row has no draft to compare against (tier=surface, or draft is empty)")
    inbound = row.get("body") or ""
    if not inbound.strip():
        raise HTTPException(400, "row has no inbound body to use as training input")

    # Call the existing /feedback/submit handler in-process so the same
    # edit-distance / edit-category / quality-score logic runs as for the
    # interactive review queue. Importing the function (not the route)
    # avoids a second HTTP hop and shares ``request.app.state.settings``.
    from app.api.feedback_routes import SubmitBody, feedback_submit

    try:
        submit_body = SubmitBody(
            inbound_text=inbound,
            generated_draft=row.get("amended_draft") or row.get("draft") or "",
            edited_reply=body.edited_reply,
            feedback_note=body.feedback_note or f"from agent triage row {row_id}",
            rating=body.rating,
            sender=row.get("sender") or row.get("sender_email"),
        )
        payload = feedback_submit(submit_body, request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"feedback_pairs insert failed: {exc}") from exc

    # The interactive /feedback/submit response returns ``total_pairs``
    # (running count of feedback_pairs rows) but not the inserted id —
    # the autoresearch nightly pipeline keys off ``total_pairs`` to decide
    # when to retrain, so surface it back to the UI for the status line.
    return {
        "ok": True,
        "total_pairs": payload.get("total_pairs"),
        "edit_distance_pct": payload.get("edit_distance_pct"),
        "row": row,
    }


@router.post("/api/agent/pending/{row_id}/push_to_gmail")
def push_to_gmail(row_id: int, request: Request) -> dict:
    """Phase 2: create a real Gmail Drafts entry for this pending row.

    Uses the configured ``ingestion.google_backend`` (gog/gws/native). On
    success, marks the row as ``sent`` (the user finishes-and-sends from Gmail)
    and stores the Gmail draft id for traceability.

    **Idempotent.** Each backend call creates a *new* Gmail draft, so a retry
    after a timeout or two concurrent callers would otherwise leave duplicate
    drafts. The shared helper claims the row atomically (``store.begin_push``);
    a re-push of an already-pushed row returns the existing draft id with
    ``pushed_already=true`` rather than creating a second one.

    The draft text used is ``amended_draft`` if the user edited it, else the
    original ``draft`` field.
    """
    from app.agent.push import push_pending_row

    outcome = push_pending_row(_db_url(request), row_id)
    if not outcome.ok:
        raise HTTPException(outcome.http_status or 500, outcome.detail or "push failed")
    return {
        "ok": True,
        "gmail_draft_id": outcome.gmail_draft_id,
        "pushed_already": outcome.pushed_already,
        "row": outcome.row,
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
