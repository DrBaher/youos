"""The agent-action framework — rule-driven mailbox routing.

Beyond drafting: when a sweep fetches a message, ``agent.rules`` can ROUTE it —
apply a Gmail label, archive it (route out of the inbox), or star it. Same
guardrail shape as the send frontier:

* opt-in: ``agent.actions.enabled`` (default **false**),
* dry-run by default: ``agent.actions.dry_run`` (records intent, touches nothing),
* a daily cap: ``agent.actions.daily_cap``,
* a full ledger (``agent_actions``) so every action is accountable and
  **reversible** — undo re-adds INBOX / removes the label / unstars.

Idempotent across sweeps: an action already *applied* to a message is never
re-applied; an already-*logged* dry-run isn't re-logged (but never blocks a
later live apply).
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import Any

logger = logging.getLogger(__name__)


def _action_to_labels(action: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Map a rule action to (labels_to_add, labels_to_remove)."""
    t = action.get("type")
    if t == "label" and action.get("value"):
        return ([action["value"]], [])
    if t == "archive":
        return ([], ["INBOX"])
    if t == "star":
        return (["STARRED"], [])
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
        # live
        if _has_status(database_url, message.message_id, action, ("applied",)):
            results.append({"action": action, "status": "skipped_done"})
            continue
        if remaining <= 0:
            results.append({"action": action, "status": "skipped_cap"})
            continue
        try:
            if action.get("type") == "label" and action.get("value"):
                gmail_write.ensure_label(account=account, name=action["value"])
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
    if row["status"] != "applied":
        return {"ok": False, "http_status": 409, "detail": f"action status is {row['status']!r}; only applied actions can be undone"}

    from app.agent.store import _connect
    from app.ingestion import gmail_write

    action = {"type": row["action_type"], "value": row["action_value"]}
    add, remove = _reverse_labels(action)
    try:
        gmail_write.modify_message_labels(
            account=row["account"], message_id=row["message_id"], add=add, remove=remove,
        )
    except Exception as exc:
        return {"ok": False, "http_status": 502, "detail": f"undo failed: {exc}"}
    with closing(_connect(database_url)) as conn:
        conn.execute(
            "UPDATE agent_actions SET status = 'undone', undone_at = CURRENT_TIMESTAMP WHERE id = ?",
            (action_id,),
        )
        conn.commit()
    return {"ok": True, "detail": f"undone (reversed add={add} remove={remove})"}
