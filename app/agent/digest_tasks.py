"""Scheduled email-digest tasks — collect → summarize → deliver one email.

Distinct from ``app.agent.digest`` (which reports what the *agent* did). A
digest TASK runs a Gmail query, summarizes the matching messages together with
the warm local model, and SENDS one digest email (optionally archiving the
collected messages afterward). Because it sends mail it crosses the never-send
frontier, so the send is gated exactly like the forward action:

* ``agent.digests.enabled`` — master switch (default **false**),
* ``agent.send.enabled`` — the shared send frontier (default false),
* ``agent.outbound_kill_switch`` — blocks all outbound when on.

A real send needs all three open; otherwise the run records ``blocked`` and
nothing is sent. A dry-run/preview builds the digest body and returns it without
sending or claiming the period.

Config shape (a dict so the master flag and the specs coexist)::

    agent:
      digests:
        enabled: false
        items:
          - name: Newsletters
            query: "label:Newsletters newer_than:7d"
            schedule: daily        # daily | weekly
            hour: 7                # local-tz hour at/after which it may run
            deliver_to: ""         # empty = your own inbox
            then_archive: false
            max_messages: 50

At-most-once per period: each run claims ``(name, account, period_key)`` via a
UNIQUE index (the same cross-process claim that fixed the forward double-send),
so a digest is sent at most once per day/week even with overlapping sweeps.
"""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 50
_VALID_SCHEDULES = ("daily", "weekly")


@dataclass
class DigestSpec:
    name: str
    query: str
    schedule: str = "daily"
    hour: int = 7
    deliver_to: str = ""      # empty → deliver to the account's own inbox
    then_archive: bool = False
    max_messages: int = _MAX_MESSAGES
    enabled: bool = True


def validate_digest(raw: Any) -> tuple[bool, str]:
    """Validate one digest spec. Returns (ok, error)."""
    from app.agent.rules import _looks_like_email

    if not isinstance(raw, dict):
        return False, "digest must be an object"
    if not str(raw.get("name") or "").strip():
        return False, "digest needs a non-empty 'name'"
    if not str(raw.get("query") or "").strip():
        return False, "digest needs a non-empty Gmail 'query'"
    sched = str(raw.get("schedule") or "daily").strip().lower()
    if sched not in _VALID_SCHEDULES:
        return False, f"schedule must be one of {list(_VALID_SCHEDULES)}"
    dest = str(raw.get("deliver_to") or "").strip()
    if dest and not _looks_like_email(dest):
        return False, "deliver_to must be a valid email address (or empty for your own inbox)"
    try:
        hour = int(raw.get("hour", 7))
    except (TypeError, ValueError):
        return False, "hour must be an integer 0-23"
    if not 0 <= hour <= 23:
        return False, "hour must be between 0 and 23"
    try:
        if int(raw.get("max_messages", _MAX_MESSAGES)) <= 0:
            return False, "max_messages must be a positive integer"
    except (TypeError, ValueError):
        return False, "max_messages must be a positive integer"
    return True, ""


def _normalize_digest(raw: Any) -> DigestSpec | None:
    ok, _ = validate_digest(raw)
    if not ok:
        return None
    return DigestSpec(
        name=str(raw["name"]).strip(),
        query=str(raw["query"]).strip(),
        schedule=str(raw.get("schedule") or "daily").strip().lower(),
        hour=int(raw.get("hour", 7)),
        deliver_to=str(raw.get("deliver_to") or "").strip(),
        then_archive=bool(raw.get("then_archive", False)),
        max_messages=int(raw.get("max_messages", _MAX_MESSAGES)),
        enabled=bool(raw.get("enabled", True)),
    )


def load_digests() -> list[DigestSpec]:
    """Read + normalise ``agent.digests.items``. Returns [] on any problem
    (malformed config must never break the sweep)."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        a = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        d = a.get("digests") if isinstance(a, dict) else None
        raw = d.get("items") if isinstance(d, dict) else None
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[DigestSpec] = []
    for r in raw:
        spec = _normalize_digest(r)
        if spec is not None:
            out.append(spec)
    return out


def _digest_config() -> dict[str, Any]:
    """Gates for sending a digest: the digest master switch + the shared send
    frontier. Every gate defaults to the safe (no-send) value."""
    from app.agent.send import _send_config
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    d = (a.get("digests") or {}) if isinstance(a, dict) else {}
    enabled = bool(d.get("enabled", False)) if isinstance(d, dict) else False
    send = _send_config()
    return {"enabled": enabled, "send_enabled": bool(send["enabled"]), "kill_switch": bool(send["kill_switch"])}


def _user_tz():
    from zoneinfo import ZoneInfo

    from app.core.config import load_config

    try:
        cfg = load_config() or {}
        name = ((cfg.get("user") or {}).get("timezone")) or "UTC"
        return ZoneInfo(str(name))
    except Exception:
        return ZoneInfo("UTC")


def _period_key(schedule: str, now: datetime) -> str:
    """The bucket a run belongs to — the claim key for at-most-once. Daily →
    local date; weekly → ISO year-week."""
    if schedule == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return now.strftime("%Y-%m-%d")


def _fetch_for_digest(account: str, query: str, limit: int) -> list[dict[str, str]]:
    """Run a Gmail query and return lightweight message dicts
    ``{id, from, subject, date}`` (one ``gog gmail messages search`` call —
    verified to return those fields)."""
    import json
    import subprocess

    cmd = [
        "gog", "gmail", "messages", "search", query,
        "--account", account, "--json", "--results-only", "--no-input",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gog search exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
    data = json.loads(result.stdout or "[]")
    msgs = data if isinstance(data, list) else (data.get("messages") or data.get("results") or [])
    items: list[dict[str, str]] = []
    for m in msgs[: max(0, int(limit))]:
        mid = m.get("id") or m.get("messageId")
        if not mid:
            continue
        items.append({
            "id": str(mid),
            "from": str(m.get("from") or m.get("sender") or ""),
            "subject": str(m.get("subject") or "(no subject)"),
            "date": str(m.get("date") or ""),
        })
    return items


def build_digest_body(items: list[dict[str, str]], *, complete_fn=None) -> str:
    """Summarize the collected messages into a digest body. Uses the warm local
    model when available; always falls back to a plain itemised list so a digest
    is never empty."""
    listing = "\n".join(f"- From {it['from']} | {it['subject']} | {it['date']}" for it in items)
    header = f"YouOS digest — {len(items)} message(s)\n\n"

    if complete_fn is None:
        from app.core import model_server

        if model_server.is_enabled():
            def complete_fn(p: str) -> str:
                return model_server.complete(p, max_tokens=500, temperature=0.2)

    if complete_fn is not None:
        prompt = (
            "Summarize the following emails into a brief, skimmable digest. Group "
            "similar items, call out anything that looks important or time-sensitive, "
            "and keep it concise. Do not invent details not present below.\n\n"
            f"{listing}\n\nDigest:"
        )
        try:
            out = (complete_fn(prompt) or "").strip()
            if out:
                return f"{header}{out}\n\n— items —\n{listing}"
        except Exception as exc:
            logger.info("digest summarization failed, using plain list: %s", exc)
    return header + listing


def _claim_period(database_url: str, name: str, account: str, period_key: str) -> int | None:
    """Atomically claim a digest period by inserting its 'sending' row. Returns
    the row id, or None if this (digest, account, period) was already claimed —
    the UNIQUE index makes this the cross-process at-most-once point."""
    import sqlite3

    from app.agent.store import _connect

    try:
        with closing(_connect(database_url)) as conn:
            cur = conn.execute(
                "INSERT INTO agent_digest_runs (name, account, period_key, status) VALUES (?, ?, ?, 'sending')",
                (name, account, period_key),
            )
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def _period_done(database_url: str, name: str, account: str, period_key: str) -> bool:
    """True if this (digest, account, period) already has a live or sent run, so
    we should not re-run it. A cheap read used to short-circuit before fetching/
    summarizing; the _claim_period INSERT remains the authoritative at-most-once
    point. Only 'sending'/'sent' rows count — a prior blocked/empty/error run
    left no row, so the period stays re-runnable."""
    from app.agent.store import _connect

    with closing(_connect(database_url)) as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_digest_runs WHERE name = ? AND account = ? AND period_key = ? "
            "AND status IN ('sending', 'sent') LIMIT 1",
            (name, account, period_key),
        ).fetchone()
    return row is not None


def _update_run(database_url: str, run_id: int | None, status: str, *,
                message_count: int | None = None, sent_message_id: str | None = None,
                detail: str | None = None) -> None:
    if run_id is None:
        return
    from app.agent.store import _connect

    sets = ["status = ?"]
    params: list[Any] = [status]
    if message_count is not None:
        sets.append("message_count = ?")
        params.append(message_count)
    if sent_message_id is not None:
        sets.append("sent_message_id = ?")
        params.append(sent_message_id)
    if detail is not None:
        sets.append("detail = ?")
        params.append(detail[:500])
    params.append(run_id)
    with closing(_connect(database_url)) as conn:
        conn.execute(f"UPDATE agent_digest_runs SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()


def run_digest(database_url: str, account: str, spec: DigestSpec, *,
               now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Run one digest: fetch → summarize → (gated) send → optionally archive.

    ``dry_run`` builds and returns the body WITHOUT claiming the period or
    sending (a safe preview). A real run claims the period first (at-most-once),
    then sends only if the digest + send-frontier gates are all open."""
    from app.ingestion import gmail_write

    cfg = _digest_config()
    if not cfg["enabled"]:
        return {"status": "disabled", "name": spec.name}

    # Self-heal the ledger table so an API-triggered run on a schema-stale
    # instance can't crash on a missing agent_digest_runs (mirrors run_triage).
    try:
        from app.db.bootstrap import ensure_agent_schema

        ensure_agent_schema(database_url)
    except Exception as exc:
        logger.info("digest schema self-heal skipped: %s", exc)

    to = spec.deliver_to or account
    subject = f"YouOS digest: {spec.name}"

    # Preview: fetch + build, but DON'T claim the period or send.
    if dry_run:
        try:
            items = _fetch_for_digest(account, spec.query, spec.max_messages)
        except Exception as exc:
            return {"status": "error", "name": spec.name, "detail": f"fetch failed: {exc}"}
        body = build_digest_body(items) if items else "(no matching messages)"
        return {"status": "preview", "name": spec.name, "to": to,
                "count": len(items), "subject": subject, "body": body}

    # Real run. Fetch + gates are checked BEFORE claiming the period, so a
    # blocked / empty / transient-fetch-error run does NOT permanently consume
    # the period — it stays re-claimable next tick (once the gate opens or
    # matching mail arrives). The period is claimed only immediately before the
    # actual send, and THAT claim is the cross-process at-most-once point.
    period = _period_key(spec.schedule, now or datetime.now(_user_tz()))
    # Cheap pre-check (avoids re-fetching/summarizing every tick once sent).
    if _period_done(database_url, spec.name, account, period):
        return {"status": "skipped_done", "name": spec.name, "period": period}

    try:
        items = _fetch_for_digest(account, spec.query, spec.max_messages)
    except Exception as exc:
        logger.info("digest %r fetch failed (will retry next tick): %s", spec.name, exc)
        return {"status": "error", "name": spec.name, "period": period, "detail": f"fetch failed: {exc}"}

    if not items:
        return {"status": "empty", "name": spec.name, "period": period}

    # Gate the send (digest master is already on; check the send frontier).
    if cfg["kill_switch"]:
        reason = "outbound kill-switch is on"
    elif not cfg["send_enabled"]:
        reason = "agent.send.enabled is false"
    else:
        reason = None
    if reason is not None:
        return {"status": "blocked", "name": spec.name, "period": period,
                "count": len(items), "detail": reason}

    # Gates open + matching mail present → claim the period, then send.
    run_id = _claim_period(database_url, spec.name, account, period)
    if run_id is None:
        return {"status": "skipped_done", "name": spec.name, "period": period}
    body = build_digest_body(items)
    try:
        res = gmail_write.send_email(account=account, to=to, subject=subject, body=body)
    except Exception as exc:
        _update_run(database_url, run_id, "error", message_count=len(items), detail=f"send failed: {exc}")
        return {"status": "error", "name": spec.name, "period": period, "detail": str(exc)}

    archived = 0
    if spec.then_archive:
        for it in items:
            try:
                gmail_write.modify_message_labels(account=account, message_id=it["id"], add=[], remove=["INBOX"])
                archived += 1
            except Exception as exc:
                logger.info("digest archive of %s failed: %s", it["id"], exc)

    _update_run(database_url, run_id, "sent", message_count=len(items),
                sent_message_id=res.message_id, detail=f"sent to {to}; archived {archived}")
    return {"status": "sent", "name": spec.name, "period": period, "to": to,
            "count": len(items), "sent_message_id": res.message_id, "archived": archived}


def run_due_digests(database_url: str, account: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Run every enabled digest that is due now (local hour ≥ its hour and not
    yet run this period). Called from the scheduler each tick; the period claim
    makes repeated ticks idempotent. No-op unless ``agent.digests.enabled``."""
    cfg = _digest_config()
    if not cfg["enabled"]:
        return []
    specs = load_digests()
    if not specs:
        return []
    now_local = now or datetime.now(_user_tz())
    out: list[dict[str, Any]] = []
    for spec in specs:
        if not spec.enabled:
            continue
        if now_local.hour < spec.hour:
            continue  # not yet the digest hour today
        try:
            out.append(run_digest(database_url, account, spec, now=now_local))
        except Exception as exc:  # never let one digest break the loop
            logger.warning("digest %r failed: %s", spec.name, exc)
            out.append({"status": "error", "name": spec.name, "detail": str(exc)})
    sent = sum(1 for r in out if r.get("status") == "sent")
    if out:
        logger.info("digests: %d run, %d sent", len(out), sent)
    return out


def list_digest_runs(database_url: str, *, account: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    from app.agent.store import _connect

    where, params = "", []
    if account:
        where = "WHERE account = ?"
        params.append(account)
    params.append(int(limit))
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"SELECT id, name, account, period_key, status, message_count, sent_message_id, detail, created_at "
            f"FROM agent_digest_runs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
