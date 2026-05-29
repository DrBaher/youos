"""Daily-digest generator for the agent loop.

A YouOS-local way to get *remote* visibility into what the agent has been doing
while you're away. Reads the audit log + pending queue + dismissal stats and
formats them into a human-readable summary that can be:

  * printed to stdout (``youos agent digest``)
  * piped to ``mail``/``sendmail`` via cron for an actual email
  * served as JSON (``--format json``) for whatever post-processor you want

Designed for the case where ``/triage`` isn't reachable (no Tailscale, on a
plane, etc.) but Gmail-on-phone is — the digest gives you the agent's
behavior at a glance without needing the full UI.

The data sources are all already in the DB (audit log, pending drafts,
dismissal stats from b39/b42/b52). No new tables — pure formatting layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

DigestFormat = Literal["text", "html", "json", "chat"]


@dataclass
class DigestData:
    """The raw payload behind a digest — what gets formatted into text/html/json.

    Single struct so ``build_digest`` can be tested without parsing the output.
    """

    account: str
    days: int
    generated_at: str
    sweeps: int
    sweeps_successful: int
    fetched: int
    hard_skipped: int
    drafted: int
    surfaced: int
    persisted: int
    pending_count: int           # rows still status='pending' (await user action)
    pushed_count: int            # rows status='sent' with a gmail_draft_id
    dismissed_count: int
    dismissal_rate: float
    dismissal_by_reason: dict[str, int]
    auto_promoted: list[str]     # cumulative across the window
    top_noise_senders: list[dict[str, Any]]  # [{sender_email, count}, ...]
    triage_url: str | None       # configured Tailscale URL if any
    # b59: chat-format and orchestrators need a short row preview without
    # the formatter re-querying the DB. Up to 5 pending rows, each with
    # just the fields a chat bubble surfaces. Keeps the formatter pure.
    pending_preview: list[dict[str, Any]]
    # Follow-up open loops (defaults keep older callers/tests constructing
    # DigestData by position working).
    owed_count: int = 0
    awaiting_count: int = 0
    owed_preview: list[dict[str, Any]] = field(default_factory=list)
    awaiting_preview: list[dict[str, Any]] = field(default_factory=list)


def build_digest(
    *,
    database_url: str,
    account: str,
    days: int = 1,
) -> DigestData:
    """Pull the data behind a digest for ``account`` over the last ``days``.

    Uses the existing store helpers — no new SQL paths so any DB-schema
    change shows up in one place. Errors propagate; the caller decides
    whether to wrap.
    """
    from app.agent.store import (
        dismissal_stats,
        list_pending,
        list_recent_sweeps,
        noise_dismissal_candidates,
        sweep_aggregate,
    )

    sweep_agg = sweep_aggregate(database_url, account=account, days=days)
    dism = dismissal_stats(database_url, account=account, days=days)
    sweeps = list_recent_sweeps(database_url, account=account, limit=200)

    # Auto-promotions across the window — union from each audit row in scope.
    cutoff = _utcnow().timestamp() - days * 86400
    auto_promoted: list[str] = []
    for s in sweeps:
        try:
            t = datetime.fromisoformat(s.get("started_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            t = 0.0
        if t >= cutoff:
            for sender in (s.get("auto_promoted") or []):
                if sender and sender not in auto_promoted:
                    auto_promoted.append(sender)

    # Pending-vs-pushed split from the queue itself (not from audit, which
    # records sweep-level totals not lifecycle outcomes).
    pending_rows = list_pending(database_url, account=account, status="pending", limit=500)
    sent_rows = list_pending(database_url, account=account, status="sent", limit=500)
    pushed_count = sum(1 for r in sent_rows if r.get("gmail_draft_id"))
    # b59: top-5 pending rows captured here so the chat formatter doesn't
    # need to re-query (which would also fight test isolation — the
    # formatter ran against get_settings().database_url not the caller's).
    pending_preview = [
        {
            "id": r["id"],
            "tier": r.get("tier"),
            "needs_reply_score": r.get("needs_reply_score") or 0.0,
            "sender": r.get("sender") or r.get("sender_email"),
            "sender_email": r.get("sender_email"),
            "subject": (r.get("subject") or "")[:80],
            "thread_summary": r.get("thread_summary"),
        }
        for r in pending_rows[:5]
    ]

    # Top noise senders — pull candidates at min_count=1 so even single
    # noise dismissals show up; the digest is informational.
    top = noise_dismissal_candidates(
        database_url, account=account, days=days, min_count=1,
    )[:5]

    # Construct the Tailscale-aware triage URL if configured. Falls back to None.
    triage_url = _resolve_triage_url()

    # Follow-up open loops (owed inbound + awaiting reply). Failure-isolated so a
    # follow-up query problem can't break the whole digest.
    owed: list[dict[str, Any]] = []
    awaiting: list[dict[str, Any]] = []
    try:
        from app.agent.followups import build_followups

        fu = build_followups(database_url, account=account)
        owed = fu["owed"]
        awaiting = fu["awaiting"]
    except Exception:
        owed, awaiting = [], []

    return DigestData(
        account=account,
        days=days,
        generated_at=_utcnow().isoformat(),
        sweeps=sweep_agg["sweeps"],
        sweeps_successful=sweep_agg["successful"],
        fetched=sweep_agg["fetched"],
        hard_skipped=sweep_agg["hard_skipped"],
        drafted=max(sweep_agg["persisted"] - sweep_agg["surfaced"], 0),
        surfaced=sweep_agg["surfaced"],
        persisted=sweep_agg["persisted"],
        pending_count=len(pending_rows),
        pushed_count=pushed_count,
        dismissed_count=dism["dismissed"],
        dismissal_rate=dism["dismissal_rate"],
        dismissal_by_reason={k: v for k, v in dism["by_reason"].items() if v},
        auto_promoted=auto_promoted,
        top_noise_senders=top,
        triage_url=triage_url,
        pending_preview=pending_preview,
        owed_count=len(owed),
        awaiting_count=len(awaiting),
        owed_preview=owed[:5],
        awaiting_preview=awaiting[:5],
    )


def format_digest(data: DigestData, *, fmt: DigestFormat = "text") -> str:
    """Render ``DigestData`` into one of the supported output formats."""
    if fmt == "json":
        return json.dumps(_data_to_dict(data), indent=2, default=str)
    if fmt == "html":
        return _format_html(data)
    if fmt == "chat":
        return _format_chat(data)
    return _format_text(data)


def summary_line(d: DigestData) -> str:
    """One-line headline suitable for a chat bubble / push notification.

    Designed for orchestrators (Hermes / OpenClaw / Telegram bot) that
    want a single-line first message — they can request the structured
    JSON afterwards if the user drills in. Always ≤120 chars.
    """
    span = "today" if d.days == 1 else f"last {d.days}d"
    headline = (
        f"YouOS ({span}): {d.pending_count} pending · "
        f"{d.pushed_count} pushed · {d.dismissed_count} dismissed "
        f"({d.sweeps} sweeps)"
    )
    return headline[:120]


# --- formatters ------------------------------------------------------------


def _format_text(d: DigestData) -> str:
    lines: list[str] = []
    span = "today" if d.days == 1 else f"last {d.days} days"
    lines.append(f"YouOS — Agent digest for {d.account} ({span})")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Sweeps:      {d.sweeps} ({d.sweeps_successful} successful)")
    lines.append(f"Fetched:     {d.fetched}")
    lines.append(f"Hard-skipped:{d.hard_skipped} (newsletters / automation / CI)")
    lines.append(f"Drafted:     {d.drafted}")
    lines.append(f"Surfaced:    {d.surfaced} (borderline, not auto-drafted)")
    lines.append("")
    lines.append(f"Pending review: {d.pending_count}")
    lines.append(f"Pushed to Gmail Drafts: {d.pushed_count}")
    lines.append(f"Dismissed: {d.dismissed_count} ({d.dismissal_rate:.0%} of persisted)")
    if d.dismissal_by_reason:
        lines.append("  by reason:")
        for k, v in d.dismissal_by_reason.items():
            lines.append(f"    {k}: {v}")
    lines.append("")
    if d.owed_count or d.awaiting_count:
        lines.append("Follow-ups:")
        if d.owed_count:
            lines.append(f"  Owed a reply ({d.owed_count}):")
            for r in d.owed_preview:
                lines.append(
                    f"    • #{r['id']} {(r.get('subject') or '(no subject)')[:50]}  ←  "
                    f"{r.get('sender') or r.get('sender_email')}  ({r['age_days']}d)"
                )
        if d.awaiting_count:
            lines.append(f"  Awaiting their reply ({d.awaiting_count}):")
            for r in d.awaiting_preview:
                lines.append(
                    f"    • #{r['id']} {(r.get('subject') or '(no subject)')[:50]}  →  "
                    f"{r.get('sender') or r.get('sender_email')}  ({r['age_days']}d)"
                )
        lines.append("")
    if d.auto_promoted:
        lines.append(f"Auto-promoted to skip_senders ({len(d.auto_promoted)}):")
        for s in d.auto_promoted:
            lines.append(f"  • {s}")
        lines.append("")
    if d.top_noise_senders:
        lines.append("Top dismissed-as-noise senders:")
        for entry in d.top_noise_senders:
            lines.append(f"  • {entry['sender_email']}  ({entry['count']}×)")
        lines.append("")
    if d.triage_url:
        lines.append(f"Review the queue: {d.triage_url}/triage")
    else:
        lines.append("Review the queue at /triage (configure tailscale.hostname for a remote URL)")
    return "\n".join(lines) + "\n"


def _format_chat(d: DigestData) -> str:
    """Compact summary suitable for a Telegram / Slack / WhatsApp bubble.

    First line is the ``summary_line`` headline. Following lines list the
    top pending drafts with their ids so an orchestrator can render
    inline "Push" / "Dismiss" actions targeting those ids. Keeps under
    ~1500 chars (well under Telegram's 4096 limit, comfortable in WhatsApp).

    Designed for the user's vision of "Hermes/OpenClaw calls YouOS,
    summarises in Telegram" — the orchestrator paraphrases the headline,
    then the user can ask "push #12" and the orchestrator hits
    ``POST /api/agent/pending/12/push_to_gmail``. Reads from
    ``d.pending_preview`` (populated by ``build_digest``) so the
    formatter is pure — no DB re-query at render time.
    """
    lines: list[str] = [summary_line(d)]

    if d.pending_preview:
        lines.append("")
        lines.append("Top pending:")
        for r in d.pending_preview:
            tier = r.get("tier", "?")
            score = r.get("needs_reply_score") or 0.0
            sender = (r.get("sender") or r.get("sender_email") or "(unknown)")
            subject = (r.get("subject") or "(no subject)")[:60]
            # Row id = orchestrator's action handle:
            # POST /api/agent/pending/<id>/{push_to_gmail,dismiss,save_as_feedback_pair}
            lines.append(f"  #{r['id']} [{tier} {score:.2f}] {subject}  ←  {sender[:40]}")

    if d.owed_count or d.awaiting_count:
        bits = []
        if d.owed_count:
            bits.append(f"{d.owed_count} awaiting your reply")
        if d.awaiting_count:
            bits.append(f"{d.awaiting_count} awaiting theirs")
        lines.append("")
        lines.append("Follow-ups: " + ", ".join(bits))

    if d.auto_promoted:
        lines.append("")
        more = "..." if len(d.auto_promoted) > 3 else ""
        lines.append(f"Auto-skipped {len(d.auto_promoted)} sender(s): {', '.join(d.auto_promoted[:3])}{more}")

    if d.triage_url:
        lines.append("")
        lines.append(f"Review: {d.triage_url}/triage")

    return "\n".join(lines)


def _format_html(d: DigestData) -> str:
    span = "today" if d.days == 1 else f"last {d.days} days"
    triage_link = (
        f'<p><a href="{d.triage_url}/triage">Review the queue</a></p>'
        if d.triage_url else
        '<p>Review the queue at <code>/triage</code> (configure <code>tailscale.hostname</code> for a remote URL)</p>'
    )
    by_reason_html = ""
    if d.dismissal_by_reason:
        items = "".join(f"<li><code>{k}</code>: {v}</li>" for k, v in d.dismissal_by_reason.items())
        by_reason_html = f"<p>By reason:</p><ul>{items}</ul>"
    auto_promoted_html = ""
    if d.auto_promoted:
        items = "".join(f"<li><code>{s}</code></li>" for s in d.auto_promoted)
        auto_promoted_html = f"<p><strong>Auto-promoted to skip_senders</strong> ({len(d.auto_promoted)}):</p><ul>{items}</ul>"
    top_noise_html = ""
    if d.top_noise_senders:
        items = "".join(
            f"<li><code>{e['sender_email']}</code> ({e['count']}×)</li>"
            for e in d.top_noise_senders
        )
        top_noise_html = f"<p>Top dismissed-as-noise senders:</p><ul>{items}</ul>"
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;margin:24px auto;line-height:1.5">
<h2>YouOS — Agent digest for {d.account}</h2>
<p style="color:#666">{span}</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td>Sweeps</td><td>{d.sweeps} ({d.sweeps_successful} successful)</td></tr>
  <tr><td>Fetched</td><td>{d.fetched}</td></tr>
  <tr><td>Hard-skipped</td><td>{d.hard_skipped}</td></tr>
  <tr><td>Drafted</td><td>{d.drafted}</td></tr>
  <tr><td>Surfaced</td><td>{d.surfaced}</td></tr>
  <tr><td>Pending review</td><td>{d.pending_count}</td></tr>
  <tr><td>Pushed to Gmail Drafts</td><td>{d.pushed_count}</td></tr>
  <tr><td>Dismissed</td><td>{d.dismissed_count} ({d.dismissal_rate:.0%})</td></tr>
</table>
{by_reason_html}
{auto_promoted_html}
{top_noise_html}
{triage_link}
</body></html>"""


def _data_to_dict(d: DigestData) -> dict[str, Any]:
    return {
        # Chat-friendly one-liner — orchestrators read this first to emit a
        # single-bubble summary, then drill into the structured fields if
        # the user asks for more.
        "summary": summary_line(d),
        "account": d.account,
        "days": d.days,
        "generated_at": d.generated_at,
        "sweeps": d.sweeps,
        "sweeps_successful": d.sweeps_successful,
        "fetched": d.fetched,
        "hard_skipped": d.hard_skipped,
        "drafted": d.drafted,
        "surfaced": d.surfaced,
        "persisted": d.persisted,
        "pending_count": d.pending_count,
        "pushed_count": d.pushed_count,
        "dismissed_count": d.dismissed_count,
        "dismissal_rate": d.dismissal_rate,
        "dismissal_by_reason": d.dismissal_by_reason,
        "auto_promoted": d.auto_promoted,
        "top_noise_senders": d.top_noise_senders,
        "triage_url": d.triage_url,
        # b59: chat / orchestrator surface — top-5 pending rows with
        # action handles. Mirrors what `_format_chat` would render.
        "pending_preview": d.pending_preview,
        # Follow-up open loops.
        "owed_count": d.owed_count,
        "awaiting_count": d.awaiting_count,
        "owed_preview": d.owed_preview,
        "awaiting_preview": d.awaiting_preview,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_triage_url() -> str | None:
    """Build the user-visible URL where /triage lives — Tailscale form if
    configured, otherwise None (the caller falls back to docs guidance)."""
    try:
        from app.core.config import get_server_port, get_tailscale_hostname, load_config

        cfg = load_config() or {}
        host = (cfg.get("server") or {}).get("host") or "127.0.0.1"
        port = get_server_port(cfg)
        ts = get_tailscale_hostname(cfg)
        if ts:
            return f"http://{ts}:{port}"
        # Exposed non-loopback bind — return the configured host directly.
        if host not in ("127.0.0.1", "localhost", ""):
            return f"http://{host}:{port}"
    except Exception:
        pass
    return None
