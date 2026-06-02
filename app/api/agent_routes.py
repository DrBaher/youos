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
_TEMPLATE_DIR = Path(__file__).resolve().parents[1].parent / "templates"
_TEMPLATE = _TEMPLATE_DIR / "triage.html"
_RULES_TEMPLATE = _TEMPLATE_DIR / "rules.html"
_DIGESTS_TEMPLATE = _TEMPLATE_DIR / "digests.html"


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
    # max_length bounds the stored/sent body so a multi-MB POST can't bloat the
    # row or be pushed as an oversized email (mirrors the b132 inbound caps).
    amended_draft: str = Field(min_length=1, max_length=50_000)


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

    instruction: str | None = Field(
        default=None, max_length=4_000,
        description="Free-form steer, e.g. 'shorter; decline the meeting'",
    )
    tone_hint: str | None = Field(default=None, max_length=200)
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
        # Machine re-draft (not a human edit) — tagged so feedback capture
        # doesn't mine it as a gold correction pair.
        store.mark_amended(db_url, row_id, amended_draft=new_draft, amended_by="machine")
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


@router.get("/api/agent/precision")
def triage_precision(
    request: Request,
    account: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    history_limit: int = Query(30, ge=1, le=365),
) -> dict:
    """Draft-decision precision/recall on real mail (autonomy Phase A2).

    Returns the live metric computed now (from the user's verdicts on queued
    rows) plus the recorded nightly history so the trend is visible. The live
    block is recomputed on demand; the history comes from the nightly
    snapshots in ``triage_precision_history``.
    """
    from app.evaluation.real_mail_eval import evaluate_real_mail, precision_history

    return {
        "live": evaluate_real_mail(_db_url(request), account=account, days=days),
        "history": precision_history(_db_url(request), account=account, limit=history_limit),
    }


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


class RuleBody(BaseModel):
    """One filter→action rule. ``match`` keys are ANDed — sender / domain /
    intent / cold_outreach / subject_contains / body_contains / to_contains /
    cc_contains / subject_regex / body_regex / has_attachment / known_contact /
    older_than_days / newer_than_days; ``action`` is one of skip/decline/prepend/
    hold (draft-shaping), label/archive/star/mark_read/mark_important/
    mark_unimportant (mailbox routing), or forward (outbound — gated by the send
    frontier + agent.actions.allow_forward); ``value`` is the label name (label),
    the prepend text (prepend), or the destination email (forward). See
    ``app.agent.rules.MATCH_KEYS``."""

    match: dict[str, object] = Field(min_length=1)
    action: str
    value: object | None = None


@router.get("/api/agent/rules")
def get_rules(request: Request) -> dict:
    """List the configured filter→action rules (validated), each with its
    index — the handle for PUT/DELETE."""
    from app.agent.rules import load_rules

    rules = load_rules()
    return {"rules": [{"index": i, **r} for i, r in enumerate(rules)]}


@router.post("/api/agent/rules/validate")
def validate_rule_endpoint(body: RuleBody) -> dict:
    """Dry-validate a rule without saving (for the builder UI / NL preview)."""
    from app.agent.rules import validate_rule

    ok, err = validate_rule(body.model_dump())
    return {"ok": ok, "error": err}


class RuleTextBody(BaseModel):
    """A plain-English rule description to parse into structured form."""

    text: str = Field(max_length=4_000)


@router.post("/api/agent/rules/parse")
def parse_rule_text_endpoint(body: RuleTextBody) -> dict:
    """Turn a natural-language description into a structured rule via the warm
    local model. NEVER saves — returns ``{ok, rule, error}`` for the builder to
    pre-fill so the user confirms (and can edit) before hitting Save."""
    from app.agent.nl_rule import parse_rule_text

    return parse_rule_text(body.text)


@router.post("/api/agent/rules")
def add_rule(body: RuleBody) -> dict:
    """Append a new rule. Validates, then persists the whole list to config."""
    from app.agent.rules import load_rules, save_rules, validate_rule

    rule = body.model_dump()
    ok, err = validate_rule(rule)
    if not ok:
        raise HTTPException(400, err)
    rules = load_rules() + [rule]
    saved = save_rules(rules)
    return {"ok": True, "index": len(saved) - 1, "rules": saved}


@router.put("/api/agent/rules/{index}")
def update_rule(index: int, body: RuleBody) -> dict:
    """Replace the rule at ``index``."""
    from app.agent.rules import load_rules, save_rules, validate_rule

    rules = load_rules()
    if index < 0 or index >= len(rules):
        raise HTTPException(404, f"no rule at index {index} (have {len(rules)})")
    rule = body.model_dump()
    ok, err = validate_rule(rule)
    if not ok:
        raise HTTPException(400, err)
    rules[index] = rule
    return {"ok": True, "rules": save_rules(rules)}


@router.delete("/api/agent/rules/{index}")
def delete_rule(index: int) -> dict:
    """Delete the rule at ``index``."""
    from app.agent.rules import load_rules, save_rules

    rules = load_rules()
    if index < 0 or index >= len(rules):
        raise HTTPException(404, f"no rule at index {index} (have {len(rules)})")
    removed = rules.pop(index)
    return {"ok": True, "removed": removed, "rules": save_rules(rules)}


@router.get("/api/agent/actions")
def list_mailbox_actions(
    request: Request,
    account: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """The agent-action ledger: recent label/archive/star routing actions
    (applied / dry_run / error / undone) for accountability + the undo UI."""
    from app.agent.actions import list_actions

    return {"actions": list_actions(_db_url(request), account=account, limit=limit)}


@router.post("/api/agent/actions/{action_id}/undo")
def undo_mailbox_action(action_id: int, request: Request) -> dict:
    """Reverse a previously-applied routing action (re-add INBOX / remove the
    label / unstar). Only 'applied' actions can be undone; a forward cannot."""
    from app.agent.actions import undo_action

    res = undo_action(_db_url(request), action_id)
    if not res.get("ok"):
        raise HTTPException(res.get("http_status", 500), res.get("detail", "undo failed"))
    return res


class DigestRunBody(BaseModel):
    """Manually run (or preview) a configured digest task."""

    name: str
    account: str | None = None
    dry_run: bool = True   # default to a safe preview (build body, don't send)


class DigestBody(BaseModel):
    """One digest spec for the authoring API. ``weekday`` accepts a day name or
    0-6 (Mon=0); see ``app.agent.digest_tasks.validate_digest``."""

    name: str
    query: str
    prompt: str = ""
    schedule: str = "daily"
    weekday: object | None = None
    hour: int = 7
    minute: int = 0
    destination: str = "agent"
    account: str = ""
    deliver_to: str = ""
    then_archive: bool = False
    max_messages: int = 50
    summary_model: str = "local"
    enabled: bool = True

    def to_spec_dict(self) -> dict:
        d = self.model_dump()
        if d.get("weekday") is None:
            d.pop("weekday", None)   # let the validator/default handle absence
        return d


@router.get("/api/agent/digests")
def list_digests(request: Request, account: str | None = Query(None)) -> dict:
    """The configured digest tasks (each with its index — the handle for
    PUT/DELETE) + recent run history. Read-only; never sends."""
    from app.agent.digest_tasks import list_digest_runs, load_digests

    specs = [{"index": i, **vars(s)} for i, s in enumerate(load_digests())]
    runs = list_digest_runs(_db_url(request), account=account, limit=50)
    return {"digests": specs, "runs": runs}


@router.post("/api/agent/digests/validate")
def validate_digest_endpoint(body: DigestBody) -> dict:
    """Dry-validate a digest spec without saving (for the builder UI)."""
    from app.agent.digest_tasks import validate_digest

    ok, err = validate_digest(body.to_spec_dict())
    return {"ok": ok, "error": err}


@router.post("/api/agent/digests")
def add_digest(body: DigestBody) -> dict:
    """Append a new digest. Validates, then persists the whole list to config."""
    from app.agent.digest_tasks import load_digests, save_digests, validate_digest

    spec = body.to_spec_dict()
    ok, err = validate_digest(spec)
    if not ok:
        raise HTTPException(400, err)
    existing = [vars(s) for s in load_digests()]
    saved = save_digests(existing + [spec])
    return {"ok": True, "index": len(saved) - 1, "digests": saved}


@router.put("/api/agent/digests/{index}")
def update_digest(index: int, body: DigestBody) -> dict:
    """Replace the digest at ``index``."""
    from app.agent.digest_tasks import load_digests, save_digests, validate_digest

    existing = [vars(s) for s in load_digests()]
    if index < 0 or index >= len(existing):
        raise HTTPException(404, f"no digest at index {index} (have {len(existing)})")
    spec = body.to_spec_dict()
    ok, err = validate_digest(spec)
    if not ok:
        raise HTTPException(400, err)
    existing[index] = spec
    return {"ok": True, "digests": save_digests(existing)}


@router.delete("/api/agent/digests/{index}")
def delete_digest(index: int) -> dict:
    """Delete the digest at ``index``."""
    from app.agent.digest_tasks import load_digests, save_digests

    existing = [vars(s) for s in load_digests()]
    if index < 0 or index >= len(existing):
        raise HTTPException(404, f"no digest at index {index} (have {len(existing)})")
    removed = existing.pop(index)
    return {"ok": True, "removed": removed, "digests": save_digests(existing)}


@router.post("/api/agent/digests/run")
def run_digest_now(body: DigestRunBody, request: Request) -> dict:
    """Run one configured digest by name. ``dry_run`` (default true) previews the
    digest body without sending or consuming the period; ``dry_run=false`` does a
    real run (gated by the digest + send-frontier flags, at-most-once per period)."""
    from app.agent.digest_tasks import load_digests, run_digest
    from app.core.config import get_user_emails

    spec = next((s for s in load_digests() if s.name == body.name), None)
    if spec is None:
        raise HTTPException(404, f"no digest named {body.name!r} (configure it under agent.digests.items)")

    # Prefer the caller's account; else the digest's own scoped account; else the
    # first configured account — so a preview/run targets the right inbox.
    account = body.account or spec.account
    if not account:
        emails = get_user_emails()
        if not emails:
            raise HTTPException(400, "no account configured (user.emails empty)")
        account = emails[0]
    return run_digest(_db_url(request), account, spec, dry_run=body.dry_run)


class DigestQueryTextBody(BaseModel):
    """A plain-English description of which emails a digest should include.
    ``model`` picks the translator: 'local' (default, on-device) or 'cloud'
    (a frontier model — only this short description is sent, never email)."""

    text: str = Field(max_length=4_000)
    model: str = "local"


@router.post("/api/agent/digests/parse-query")
def parse_digest_query_endpoint(body: DigestQueryTextBody) -> dict:
    """Translate a plain-English 'which emails' description into a Gmail query via
    the local or a frontier model. Returns ``{ok, query, error}`` — the builder
    fills the query field with it so the user can review/edit before saving.
    Never saves."""
    from app.agent.digest_tasks import query_from_text

    return query_from_text(body.text, model=body.model)


@router.get("/api/agent/digests/pending")
def list_pending_digests_endpoint(request: Request, account: str | None = Query(None)) -> dict:
    """Computed-but-not-yet-collected 'agent'-destination digests (status
    'ready'), each with its body — what an orchestrator pulls to deliver. After
    delivering, call POST /api/agent/digests/{id}/collected."""
    from app.agent.digest_tasks import list_pending_digests

    return {"pending": list_pending_digests(_db_url(request), account=account)}


@router.post("/api/agent/digests/{run_id}/collected")
def collect_digest_endpoint(run_id: int, request: Request) -> dict:
    """Mark a 'ready' digest as collected (the orchestrator delivered it). Only a
    'ready' run can be collected; idempotent across retries via an atomic claim."""
    from app.agent.digest_tasks import mark_collected

    res = mark_collected(_db_url(request), run_id)
    if not res.get("ok"):
        raise HTTPException(res.get("http_status", 500), res.get("detail", "collect failed"))
    return res


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
            "urgency_score": r.get("urgency_score"),  # b189: time-criticality (visibility only)
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


@router.get("/api/agent/followups")
def followups(
    request: Request,
    account: str | None = Query(None),
) -> dict:
    """Open loops the agent is tracking: inbound you owe a reply to (aging
    pending rows) and replies you're awaiting (sent rows with no newer thread
    activity). Read-only; drives the digest nudge and an orchestrator's
    "anything I'm forgetting?" answer.
    """
    from app.agent.followups import build_followups
    from app.core.config import get_user_emails

    if not account:
        emails = get_user_emails()
        account = emails[0] if emails else None
    return build_followups(_db_url(request), account=account)


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


class SendBody(BaseModel):
    """Options for sending a pushed draft. ``shadow`` runs the full path but
    records a soak-only send (never touches Gmail); ``dry_run`` exercises the
    real backend with its no-change flag."""

    shadow: bool = False
    dry_run: bool = False
    backend: str | None = None


@router.post("/api/agent/pending/{row_id}/send")
def send_pending(row_id: int, request: Request, body: SendBody | None = None) -> dict:
    """Phase B (send frontier): SEND the Gmail draft attached to this row.

    The one route that crosses the never-send boundary. It is hard-gated:
    ``agent.outbound_kill_switch`` blocks everything; a real send additionally
    requires ``agent.send.enabled`` (default false). With sending disabled you
    can still ``shadow`` send (soak: records the intent, never touches Gmail).

    The row must already be pushed to Gmail (have a ``gmail_draft_id``) — we
    send the exact draft, not a re-marshaled body. Idempotent: a re-send of an
    already-sent row returns the prior result.
    """
    from app.agent.send import send_pending_row

    b = body or SendBody()
    outcome = send_pending_row(
        _db_url(request), row_id, shadow=b.shadow, dry_run=b.dry_run, backend=b.backend,
    )
    if not outcome.ok:
        raise HTTPException(outcome.http_status or 500, outcome.detail or "send failed")
    return {
        "ok": True,
        "sent_message_id": outcome.sent_message_id,
        "shadow": outcome.shadow,
        "sent_already": outcome.sent_already,
        "detail": outcome.detail,
        "row": outcome.row,
    }


class ConfirmSendBody(BaseModel):
    """Human-confirmed send in one call (the OpenClaw 'I approve this' action).

    Optionally carries the user's final edited text (``amended_draft``) so a
    review-edit-confirm round-trip is a single request: amend → push → send.
    """

    amended_draft: str | None = Field(
        default=None, max_length=50_000,
        description="Final edited reply text; omit to send the existing draft as-is.",
    )
    backend: str | None = Field(default=None)


@router.post("/api/agent/pending/{row_id}/confirm_send")
def confirm_send(row_id: int, request: Request, body: ConfirmSendBody | None = None) -> dict:
    """One-call human-confirmed send: (optional edit) → create the Gmail draft →
    send it. The single action an orchestrator (OpenClaw) fires when the user
    approves a reply, so review→edit→confirm is one request, not three.

    Send is still hard-gated by ``agent.send.enabled`` + the kill-switch (this
    is a *human-confirmed* send, distinct from autonomous ``auto_send``). If the
    draft is created but the send fails, the Gmail draft remains and the error
    is returned so the caller can retry ``/send`` (idempotent).
    """
    from app.agent.push import push_pending_row
    from app.agent.send import _send_config, send_pending_row

    db = _db_url(request)
    b = body or ConfirmSendBody()

    # 0) Gate FIRST, before creating any Gmail draft — so a disabled send (or an
    #    armed kill-switch) doesn't leave an orphan draft behind.
    gate = _send_config()
    if gate["kill_switch"]:
        raise HTTPException(403, "outbound kill-switch is on; all sending is blocked")
    if not gate["enabled"]:
        raise HTTPException(403, "sending is disabled (set agent.send.enabled to allow confirmed sends)")

    # 1) Apply the user's final edit, if any (tagged 'user' so it counts as a
    #    real correction for the feedback loop).
    if b.amended_draft is not None and b.amended_draft.strip():
        pre = store.get(db, row_id)
        if not pre:
            raise HTTPException(404, "pending row not found")
        # A Gmail draft created BEFORE this edit (e.g. auto_push during the
        # sweep) predates it, and there's no in-place update primitive — so
        # push's idempotent fast path would SEND THE OLD, un-approved body and
        # silently drop the operator's edit. Refuse rather than send content the
        # operator never approved.
        if pre.get("gmail_draft_id"):
            raise HTTPException(
                409,
                "this row already has a Gmail draft created before your edit; the edit can't be "
                "applied in place. Dismiss and re-draft, or call confirm_send without amended_draft "
                "to send the existing draft as-is.",
            )
        if not store.mark_amended(db, row_id, amended_draft=b.amended_draft, amended_by="user"):
            raise HTTPException(404, "pending row not found")

    # 2) Materialize the Gmail draft (idempotent; uses amended_draft or draft).
    push = push_pending_row(db, row_id, backend=b.backend)
    if not push.ok:
        raise HTTPException(push.http_status or 500, push.detail or "could not create the Gmail draft to send")

    # 3) Send it (gated by agent.send.enabled + kill-switch).
    outcome = send_pending_row(db, row_id, backend=b.backend)
    if not outcome.ok:
        # The draft exists in Gmail; surface the send error so the caller can
        # retry /send without re-pushing.
        raise HTTPException(
            outcome.http_status or 500,
            f"{outcome.detail or 'send failed'} (a Gmail draft {push.gmail_draft_id} exists; retry /send)",
        )
    return {
        "ok": True,
        "gmail_draft_id": push.gmail_draft_id,
        "sent_message_id": outcome.sent_message_id,
        "sent_already": outcome.sent_already,
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


@router.get("/rules", response_class=HTMLResponse)
def rules_page() -> HTMLResponse:
    """Render the filter→action rule builder (CRUD over /api/agent/rules) plus
    the recent-routing-actions ledger. Served per-request like the other pages."""
    if not _RULES_TEMPLATE.exists():
        raise HTTPException(500, "rules template missing")
    return HTMLResponse(_RULES_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/digests", response_class=HTMLResponse)
def digests_page() -> HTMLResponse:
    """Render the digest-task builder (CRUD over /api/agent/digests) with preview
    + run history. Served per-request like the other pages."""
    if not _DIGESTS_TEMPLATE.exists():
        raise HTTPException(500, "digests template missing")
    return HTMLResponse(_DIGESTS_TEMPLATE.read_text(encoding="utf-8"))
