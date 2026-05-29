"""The agent-action framework — rule-driven mailbox routing.

Beyond drafting: when a sweep fetches a message, ``agent.rules`` can ROUTE it —
apply a Gmail label, archive it (route out of the inbox), star it, mark it
read, or move it in/out of the Important tab. Every action is a reversible
Gmail label add/remove. Same guardrail shape as the send frontier:

* opt-in: ``agent.actions.enabled`` (default **false**),
* dry-run by default: ``agent.actions.dry_run`` (records intent, touches nothing),
* a daily cap: ``agent.actions.daily_cap``,
* a full ledger (``agent_actions``) so every action is accountable and
  **reversible** — undo re-adds INBOX / removes the label / unstars.

Idempotent across sweeps: an action already *applied* to a message is never
re-applied; an already-*logged* dry-run isn't re-logged (but never blocks a
later live apply).

The OUTBOUND ``forward`` action is handled separately (``apply_outbound_actions``)
because it SENDS mail: it is gated behind the send frontier (``agent.send.enabled``
+ outbound kill-switch) PLUS a dedicated ``agent.actions.allow_forward`` opt-in,
is at-most-once (claimed before send; errors are not auto-retried), and is
IRREVERSIBLE — ``undo_action`` refuses it.
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import Any

logger = logging.getLogger(__name__)


def _action_to_labels(action: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Map a rule action to (labels_to_add, labels_to_remove). Every action is a
    reversible label mutation so undo (``_reverse_labels``) is just the swap."""
    t = action.get("type")
    if t == "label" and action.get("value"):
        return ([action["value"]], [])
    if t == "archive":
        return ([], ["INBOX"])
    if t == "star":
        return (["STARRED"], [])
    if t == "mark_read":
        return ([], ["UNREAD"])
    if t == "mark_important":
        return (["IMPORTANT"], [])
    if t == "mark_unimportant":
        return ([], ["IMPORTANT"])
    return ([], [])


def _reverse_labels(action: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The inverse of ``_action_to_labels`` — for undo."""
    add, remove = _action_to_labels(action)
    return (remove, add)


def _actions_config() -> dict[str, Any]:
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    ac = (a.get("actions") or {}) if isinstance(a, dict) else {}
    if not isinstance(ac, dict):
        ac = {}
    try:
        cap = int(ac.get("daily_cap", 50))
    except (TypeError, ValueError):
        cap = 50
    return {
        "enabled": bool(ac.get("enabled", False)),
        "dry_run": bool(ac.get("dry_run", True)),
        "daily_cap": max(0, cap),
    }


def _forward_config() -> dict[str, Any]:
    """Gates for the outbound 'forward' action. Forwarding SENDS mail, so it
    needs the routing framework on AND the send frontier open AND a dedicated
    opt-in — every gate defaults to the safe (no-forward) value."""
    from app.agent.send import _send_config
    from app.core.config import load_config

    base = _actions_config()  # enabled / dry_run / daily_cap
    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    ac = (a.get("actions") or {}) if isinstance(a, dict) else {}
    allow = bool(ac.get("allow_forward", False)) if isinstance(ac, dict) else False
    send = _send_config()  # {enabled, kill_switch}
    return {
        **base,
        "allow_forward": allow,
        "send_enabled": bool(send.get("enabled")),
        "kill_switch": bool(send.get("kill_switch")),
    }


def _record(database_url, *, account, message, action, status, detail=None) -> int | None:
    from app.agent.store import _connect

    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions "
            "(account, message_id, thread_id, sender_email, subject, action_type, action_value, status, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account, message.message_id, getattr(message, "thread_id", None),
                getattr(message, "sender_email", None), (getattr(message, "subject", "") or "")[:200],
                action.get("type"), action.get("value"), status, detail,
            ),
        )
        conn.commit()
        return cur.lastrowid


def _claim_forward(database_url, *, account, message, action) -> int | None:
    """Atomically claim a forward by inserting its 'forwarding' row. Returns the
    new row id, or None if a concurrent claim/apply/error already holds this
    (message, destination) — the loser must NOT send. The partial UNIQUE index
    ``idx_agent_actions_forward_claim`` makes this the cross-process
    serialization point for at-most-once forwarding (a plain check-then-insert
    would let two separate processes both pass the dedup read and double-send)."""
    import sqlite3

    try:
        return _record(database_url, account=account, message=message, action=action,
                       status="forwarding", detail=f"forwarding to {action.get('value')}")
    except sqlite3.IntegrityError:
        return None


def _update_status(database_url, row_id: int | None, status: str, detail: str | None = None) -> None:
    """Flip an existing ledger row's status (used by the forward claim→apply
    transition so a crash mid-send can't cause a re-forward)."""
    if row_id is None:
        return
    from app.agent.store import _connect

    with closing(_connect(database_url)) as conn:
        conn.execute("UPDATE agent_actions SET status = ?, detail = ? WHERE id = ?", (status, detail, row_id))
        conn.commit()


def _has_status(database_url, message_id, action, statuses: tuple[str, ...]) -> bool:
    from app.agent.store import _connect

    val = action.get("value")
    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_actions WHERE message_id = ? AND action_type = ? "
            "AND (action_value IS ? OR action_value = ?) "
            f"AND status IN ({','.join('?' * len(statuses))}) LIMIT 1",
            (message_id, action.get("type"), val, val, *statuses),
        ).fetchone()
    return row is not None


def count_actions_today(database_url, *, account: str | None = None) -> int:
    """Applied (real) actions today (UTC) — what the daily cap counts."""
    from app.agent.store import _connect

    sql = "SELECT COUNT(*) FROM agent_actions WHERE status = 'applied' AND date(created_at) = date('now')"
    params: list[Any] = []
    if account:
        sql += " AND account = ?"
        params.append(account)
    with closing(_connect(database_url)) as conn:
        r = conn.execute(sql, params).fetchone()
    return int(r[0]) if r else 0


def apply_mailbox_actions(
    database_url: str,
    account: str,
    message: Any,
    actions: list[dict[str, Any]],
    *,
    remaining: int | float = float("inf"),
    known_labels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Apply the routing ``actions`` to ``message`` (or log them in dry-run).

    Gated by ``agent.actions.enabled``. ``remaining`` is the daily-cap budget
    left (real applies only). Returns one result dict per action with
    ``status`` ∈ {applied, dry_run, skipped_done, skipped_cap, error}."""
    cfg = _actions_config()
    if not cfg["enabled"] or not actions:
        return []
    from app.ingestion import gmail_write

    dry = cfg["dry_run"]
    results: list[dict[str, Any]] = []
    for action in actions:
        add, remove = _action_to_labels(action)
        if not add and not remove:
            continue
        if dry:
            if _has_status(database_url, message.message_id, action, ("applied", "dry_run")):
                results.append({"action": action, "status": "skipped_done"})
                continue
            _record(database_url, account=account, message=message, action=action,
                    status="dry_run", detail=f"would add={add} remove={remove}")
            results.append({"action": action, "status": "dry_run"})
            continue
        # live — also treat a deliberately UNDONE (or in-flight 'undoing') action
        # as "done" so the next sweep doesn't silently re-apply what the user
        # just undid. A re-apply after undo must be an explicit action, never
        # the default sweep behaviour.
        if _has_status(database_url, message.message_id, action, ("applied", "undone", "undoing")):
            results.append({"action": action, "status": "skipped_done"})
            continue
        if remaining <= 0:
            results.append({"action": action, "status": "skipped_cap"})
            continue
        try:
            if action.get("type") == "label" and action.get("value"):
                gmail_write.ensure_label(account=account, name=action["value"], known=known_labels)
            gmail_write.modify_message_labels(
                account=account, message_id=message.message_id, add=add, remove=remove,
            )
            _record(database_url, account=account, message=message, action=action,
                    status="applied", detail=f"add={add} remove={remove}")
            results.append({"action": action, "status": "applied"})
            remaining -= 1
        except Exception as exc:  # never let a routing failure break the sweep
            logger.warning("mailbox action %s on %s failed: %s", action, message.message_id, exc)
            _record(database_url, account=account, message=message, action=action,
                    status="error", detail=str(exc)[:300])
            results.append({"action": action, "status": "error", "detail": str(exc)})
    return results


def apply_outbound_actions(
    database_url: str,
    account: str,
    message: Any,
    actions: list[dict[str, Any]],
    *,
    remaining: int | float = float("inf"),
) -> list[dict[str, Any]]:
    """Execute outbound 'forward' actions (or log them in dry-run / blocked).

    Forwarding SENDS mail and is IRREVERSIBLE, so this is the most-gated action:
      * ``agent.actions.enabled`` must be on (the routing framework),
      * dry-run records intent and sends NOTHING,
      * a real forward additionally requires ``agent.actions.allow_forward`` AND
        ``agent.send.enabled`` AND the outbound kill-switch OFF — any closed gate
        records ``blocked`` and sends nothing,
      * AT-MOST-ONCE: a message+destination already applied/error/forwarding is
        never retried (better to miss a forward than double-send), and the row is
        claimed as ``forwarding`` BEFORE the send so a crash can't re-forward.

    Returns one result per action with status in {applied, dry_run, blocked,
    skipped_done, skipped_cap, error}."""
    cfg = _forward_config()
    if not cfg["enabled"] or not actions:
        return []
    from app.ingestion import gmail_write

    dry = cfg["dry_run"]
    results: list[dict[str, Any]] = []
    for action in actions:
        if action.get("type") != "forward":
            continue
        dest = str(action.get("value") or "").strip()
        if not dest:
            continue
        # At-most-once: never re-forward a message+dest already sent, errored, or
        # in-flight. An errored forward is NOT auto-retried (it may have partially
        # sent) — surfaced in the ledger for the user to handle manually.
        if _has_status(database_url, message.message_id, action, ("applied", "error", "forwarding")):
            results.append({"action": action, "status": "skipped_done"})
            continue
        if dry:
            if _has_status(database_url, message.message_id, action, ("dry_run",)):
                results.append({"action": action, "status": "skipped_done"})
                continue
            _record(database_url, account=account, message=message, action=action,
                    status="dry_run", detail=f"would forward to {dest}")
            results.append({"action": action, "status": "dry_run"})
            continue
        # LIVE — every send gate must be open, else record 'blocked' (no send).
        if cfg["kill_switch"]:
            reason = "outbound kill-switch is on"
        elif not cfg["send_enabled"]:
            reason = "agent.send.enabled is false"
        elif not cfg["allow_forward"]:
            reason = "agent.actions.allow_forward is false"
        else:
            reason = None
        if reason is not None:
            _record(database_url, account=account, message=message, action=action,
                    status="blocked", detail=reason)
            results.append({"action": action, "status": "blocked", "detail": reason})
            continue
        if remaining <= 0:
            results.append({"action": action, "status": "skipped_cap"})
            continue
        # Atomically claim the row as 'forwarding' BEFORE sending. The claim is
        # DB-enforced (partial UNIQUE index), so it serializes across PROCESSES:
        # if a concurrent sweep already claimed/sent this (message, dest), our
        # insert loses (returns None) and we must NOT send. And if we crash
        # between the gog call and recording the result, the surviving
        # 'forwarding' row blocks a re-forward next sweep. Both → at-most-once.
        row_id = _claim_forward(database_url, account=account, message=message, action=action)
        if row_id is None:
            results.append({"action": action, "status": "skipped_done"})
            continue
        try:
            res = gmail_write.forward_message(account=account, message_id=message.message_id, to=dest)
        except Exception as exc:
            logger.warning("forward of %s to %s failed: %s", message.message_id, dest, exc)
            _update_status(database_url, row_id, "error", str(exc)[:300])
            results.append({"action": action, "status": "error", "detail": str(exc)})
            continue
        _update_status(database_url, row_id, "applied", f"forwarded to {dest} (sent id={res.message_id})")
        results.append({"action": action, "status": "applied"})
        remaining -= 1
    return results


# --- accountability / undo -------------------------------------------------


def list_actions(database_url: str, *, account: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from app.agent.store import _connect

    where = ""
    params: list[Any] = []
    if account:
        where = "WHERE account = ?"
        params.append(account)
    params.append(int(limit))
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"SELECT id, account, message_id, sender_email, subject, action_type, action_value, "
            f"status, detail, created_at, undone_at FROM agent_actions {where} "
            f"ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_action(database_url: str, action_id: int) -> dict[str, Any] | None:
    from app.agent.store import _connect

    with closing(_connect(database_url)) as conn:
        row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    return dict(row) if row else None


def undo_action(database_url: str, action_id: int) -> dict[str, Any]:
    """Reverse a previously *applied* action (re-add INBOX / remove the label /
    unstar). Returns ``{ok, detail}``. Only 'applied' actions can be undone."""
    row = get_action(database_url, action_id)
    if not row:
        return {"ok": False, "http_status": 404, "detail": "action not found"}
    if row["action_type"] == "forward":
        # Outbound sends are irreversible — there is no un-forward.
        return {"ok": False, "http_status": 409,
                "detail": "a forward cannot be undone — the email was already sent"}
    if row["status"] != "applied":
        return {"ok": False, "http_status": 409, "detail": f"action status is {row['status']!r}; only applied actions can be undone"}

    from app.agent.store import _connect
    from app.ingestion import gmail_write

    # Atomically claim the row (applied -> undoing) so a retried/concurrent undo
    # can't double-run the gog modify. Only the caller that flips it proceeds.
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            "UPDATE agent_actions SET status = 'undoing' WHERE id = ? AND status = 'applied'",
            (action_id,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "http_status": 409, "detail": "undo already in progress or not applicable"}

    action = {"type": row["action_type"], "value": row["action_value"]}
    add, remove = _reverse_labels(action)
    try:
        gmail_write.modify_message_labels(
            account=row["account"], message_id=row["message_id"], add=add, remove=remove,
        )
    except Exception as exc:
        # Roll the claim back so the user can retry the undo.
        with closing(_connect(database_url)) as conn:
            conn.execute("UPDATE agent_actions SET status = 'applied' WHERE id = ? AND status = 'undoing'", (action_id,))
            conn.commit()
        return {"ok": False, "http_status": 502, "detail": f"undo failed: {exc}"}
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_actions SET status = 'undone', undone_at = CURRENT_TIMESTAMP WHERE id = ?",
            (action_id,),
        )
        conn.commit()
    return {"ok": True, "detail": f"undone (reversed add={add} remove={remove})"}
