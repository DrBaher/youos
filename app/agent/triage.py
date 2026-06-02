"""Triage orchestrator: fetch unread → filter → draft what needs a reply.

Phase 1 (this module): one-shot run, in-process, no persistence. The CLI
prints what would happen; a follow-up PR adds the ``agent_pending_drafts``
table and the ``/triage`` page. No auto-send, ever, in any phase that
ships from this module.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.escalation import assess_stakes
from app.agent.inbox_fetch import InboxMessage, fetch_unread
from app.agent.needs_reply import NeedsReplyVerdict, SenderHistory, classify_many
from app.generation.service import DEFAULT_QUALITY_FLOOR

logger = logging.getLogger(__name__)

try:
    import fcntl  # POSIX-only; cross-process sweep locking degrades without it
except ImportError:  # pragma: no cover — non-Unix
    fcntl = None  # type: ignore[assignment]

# Per-account sweep serialization. A scheduled tick and a manual / API triage
# can fire for the same account at once (e.g. the user clicks "Run triage" just
# as the notification lands). Without serialization both fetch the same unread
# set and each consume the daily_draft_cap budget independently, so the cap —
# the runaway-loop guardrail — can be exceeded ~2x; the message_id UNIQUE
# constraint stops duplicate ROWS but not duplicate model spend. A non-blocking
# per-account lock makes the second caller a no-op: the in-flight sweep already
# covers the inbox, and skipping is cheaper than blocking an HTTP request for a
# full sweep. The threading.Lock serializes within THIS process; a per-account
# fcntl.flock lockfile (below) extends that ACROSS processes, so the `youos
# triage` CLI racing the daemon scheduler can't each read the same daily-cap
# count and overshoot it ~2x. Both halves are non-blocking — the second caller
# skips. Locks/fds live for the process; the dict only grows by distinct account.
_sweep_locks: dict[str, threading.Lock] = {}
_sweep_locks_guard = threading.Lock()


def _sweep_lockfile(account: str) -> Path | None:
    """Path to the per-account cross-process lockfile, or None when the var dir
    is unavailable (then we degrade to intra-process locking only)."""
    try:
        from app.core.settings import get_var_dir

        var = get_var_dir()
        var.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", account) or "default"
        return var / f".sweep-{safe}.lock"
    except Exception:
        return None


class _SweepLock:
    """Per-account sweep lock serializing both within this process (a
    ``threading.Lock``) and across processes (an advisory ``fcntl.flock`` on a
    per-account lockfile). ``acquire`` is non-blocking: a busy account returns
    False so the caller skips rather than blocking a worker."""

    def __init__(self, account: str) -> None:
        self._account = account
        with _sweep_locks_guard:
            lk = _sweep_locks.get(account)
            if lk is None:
                lk = threading.Lock()
                _sweep_locks[account] = lk
        self._tlock = lk
        self._fd: int | None = None

    def acquire(self) -> bool:
        if not self._tlock.acquire(blocking=False):
            return False
        path = None if fcntl is None else _sweep_lockfile(self._account)
        if path is None:
            return True  # no cross-process layer available → intra-process only
        fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another PROCESS holds it (BlockingIOError ⊂ OSError) or flock
            # failed — release the in-process lock and report busy.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._tlock.release()
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._tlock.release()


def _account_lock(account: str) -> _SweepLock:
    return _SweepLock(account)


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
    # Optional "what changed" catch-up summary for long threads.
    thread_summary: str | None = None
    # 0–1 per-draft quality (voice + structure); what auto-push gates on.
    quality_score: float | None = None
    # A matched ``hold`` rule (agent.rules): draft it, but never auto-act —
    # excluded from auto-push/auto-send so a human always finishes-and-sends.
    hold: bool = False


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
    # Autonomous auto-send outcomes (opt-in, shadow by default). Each entry:
    # {"id", "action": "sent"|"shadow"|"held"|"error", "reason"?}.
    auto_sent: list[dict[str, Any]] = field(default_factory=list)
    # Mailbox-routing outcomes (label/archive/star; opt-in, dry-run default).
    # Each entry: {"message_id", "action": {...}, "status": "applied"|"dry_run"|...}.
    mailbox_actions: list[dict[str, Any]] = field(default_factory=list)


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
        # Per-draft quality floor — auto-push requires the DRAFT to be good,
        # not just the email to deserve a reply. Drafts with no quality score
        # (scoring failed) are treated as below the floor (safe default). The
        # default reuses DEFAULT_QUALITY_FLOOR so the auto-push gate and the
        # b188 abstain threshold share one policy number.
        "quality_floor": _f("quality_floor", DEFAULT_QUALITY_FLOOR),
        "min_pairs": _i("known_sender_min_pairs", 3),
        "daily_push_cap": _i("daily_push_cap", 5),
        "whitelist": _parse_autopush_whitelist(ap.get("whitelist")),
    }


def _adjudication_config() -> dict[str, Any]:
    """Read ``agent.adjudication.*`` config. Off by default; needs the warm
    model server. ``high`` is the upper edge of the band we adjudicate."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    adj = (a.get("adjudication") or {}) if isinstance(a, dict) else {}
    if not isinstance(adj, dict):
        adj = {}
    try:
        high = float(adj.get("high", 0.8))
    except (TypeError, ValueError):
        high = 0.8
    return {
        "enabled": bool(adj.get("enabled", False)),
        "high": max(0.6, min(1.0, high)),
    }


def _maybe_adjudicate(
    classified: list[tuple[InboxMessage, NeedsReplyVerdict]],
    *,
    threshold: float,
) -> list[tuple[InboxMessage, NeedsReplyVerdict]]:
    """LLM veto on borderline would-be drafts. For each message that passed
    the heuristic with a score just over the threshold (and isn't a VIP), ask
    the warm model whether it's a broadcast; if so, demote it to
    surface-for-review. Only ever demotes — never promotes a rejected message.

    No-ops (returns the input unchanged) when the flag is off or the model is
    unavailable, so the heuristic stands."""
    cfg = _adjudication_config()
    if not cfg["enabled"]:
        return classified
    from app.core import model_server

    if not model_server.is_enabled():
        return classified
    from app.agent.adjudicate import adjudicate

    high = cfg["high"]
    out: list[tuple[InboxMessage, NeedsReplyVerdict]] = []
    for msg, verdict in classified:
        if (
            verdict.needs_reply
            and not verdict.vip
            and threshold <= verdict.score < high
        ):
            res = adjudicate(
                subject=msg.subject,
                sender=msg.sender or msg.sender_email,
                body=msg.body,
            )
            if res is not None and res.is_broadcast:
                logger.info(
                    "adjudication vetoed draft for %s (broadcast, score %.2f)",
                    msg.message_id, verdict.score,
                )
                out.append((
                    msg,
                    NeedsReplyVerdict(
                        needs_reply=False,
                        score=verdict.score,
                        reasons=verdict.reasons + ["adjudicated broadcast (LLM veto)"],
                        cold_outreach=verdict.cold_outreach,
                        surface_for_review=True,
                        vip=verdict.vip,
                    ),
                ))
                continue
        out.append((msg, verdict))
    return out


def _maybe_calibrate(
    classified: list[tuple[InboxMessage, NeedsReplyVerdict]],
) -> list[tuple[InboxMessage, NeedsReplyVerdict]]:
    """Attach the calibrated probability to each verdict when a calibrator is
    available. No-op (returns the input unchanged) when none is fitted yet —
    so a fresh instance with no decided rows just keeps the raw heuristic."""
    try:
        from app.agent.calibration import load_calibrator

        cal = load_calibrator()
    except Exception:
        cal = None
    if cal is None:
        return classified
    out: list[tuple[InboxMessage, NeedsReplyVerdict]] = []
    for msg, verdict in classified:
        try:
            p = round(cal.probability(verdict.score), 3)
            verdict.calibrated_score = p
            verdict.reasons = verdict.reasons + [f"calibrated P={p:.2f}"]
        except Exception:
            pass
        out.append((msg, verdict))
    return out


def _extract_facts_enabled() -> bool:
    """Whether the sweep harvests facts from drafted inbound mail into memory.
    Off by default — it's an autonomous write to the user's memory table."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    ef = (a.get("extract_facts") or {}) if isinstance(a, dict) else {}
    return bool(ef.get("enabled", False)) if isinstance(ef, dict) else False


def _maybe_extract_facts(msg: InboxMessage, database_url: str | None) -> None:
    """Extract concrete facts the sender stated and save them to memory, so
    replies are grounded rather than invented. No-op unless
    ``agent.extract_facts.enabled``; rule-based only (high precision, no model);
    failure-isolated."""
    if not database_url or not _extract_facts_enabled():
        return
    try:
        from app.core.facts_extractor import extract_and_save
        from app.db.bootstrap import resolve_sqlite_path

        db_path = resolve_sqlite_path(database_url)
        saved = extract_and_save(
            msg.body or "",
            db_path,
            sender_email=msg.sender_email,
            use_llm=False,
            # Inbound is attacker-controlled: extract contact facts (sender-keyed)
            # but never a user_pref, which would become a global standing
            # instruction injected into every draft.
            allow_user_pref=False,
        )
        if saved:
            logger.info("extracted %d fact(s) from %s", len(saved), msg.message_id)
    except Exception as exc:
        logger.info("fact extraction skipped for %s: %s", msg.message_id, exc)


def _calendar_config() -> dict[str, Any]:
    """Read ``agent.calendar.*`` config + the user's timezone. Safe defaults."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    cal = (a.get("calendar") or {}) if isinstance(a, dict) else {}
    if not isinstance(cal, dict):
        cal = {}
    user = (cfg.get("user") or {}) if isinstance(cfg, dict) else {}
    tz = (user.get("timezone") if isinstance(user, dict) else None) or "UTC"

    def _i(key, default):
        try:
            return int(cal.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(cal.get("enabled", False)),
        "tz": str(tz),
        "business_days": _i("business_days", 5),
        "work_start_hour": _i("work_start_hour", 9),
        "work_end_hour": _i("work_end_hour", 17),
        "slot_minutes": _i("slot_minutes", 30),
        "max_slots": _i("max_slots", 3),
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
    quality_floor = cfg["quality_floor"]
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
        # A 'hold' rule means a human must finish-and-send — never auto-act.
        # (Blocking auto-push also blocks auto-send: only pushed drafts are
        # eligible for auto-send.)
        if getattr(d, "hold", False):
            logger.info("auto-push: row %s held by an agent.rules hold rule", row_id)
            continue
        if d.verdict.cold_outreach:
            continue
        if d.verdict.score < floor:
            continue
        # The draft itself must be good enough — not just the email worth a
        # reply. No quality score (scoring failed) → treat as below floor.
        if d.quality_score is None or d.quality_score < quality_floor:
            logger.info(
                "auto-push: row %s held — draft quality %s < floor %.2f",
                row_id, d.quality_score, quality_floor,
            )
            continue
        # High-stakes mail (money / legal / firm commitments) is held for human
        # review even when it would otherwise auto-push — the escalation policy
        # never lets these through without a person deciding.
        if assess_stakes(d.message.subject, d.message.body) == "high":
            logger.info("auto-push: row %s held — high-stakes content", row_id)
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


def _auto_send_config() -> dict[str, Any]:
    """Read ``agent.auto_send.*``. Off by default; SHADOW mode by default even
    when enabled, so turning it on soaks (log-only) until you opt into 'live'."""
    from app.core.config import load_config

    cfg = load_config() or {}
    a = (cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    s = (a.get("auto_send") or {}) if isinstance(a, dict) else {}
    if not isinstance(s, dict):
        s = {}

    def _i(key, default):
        try:
            return int(s.get(key, default))
        except (TypeError, ValueError):
            return default

    mode = str(s.get("mode", "shadow")).strip().lower()
    if mode not in ("shadow", "live"):
        mode = "shadow"
    return {
        "enabled": bool(s.get("enabled", False)),
        "mode": mode,
        # Clamp to >=1 so the undo window is ALWAYS non-zero — a 0 would let
        # auto-send fire on a draft created earlier in the same sweep.
        "delay_minutes": max(1, _i("delay_minutes", 60)),
        "min_recipient_trust": max(0, _i("min_recipient_trust", 3)),
        "max_per_sweep": max(1, _i("max_per_sweep", 5)),
        "daily_send_cap": max(0, _i("daily_send_cap", 5)),
    }


def _maybe_auto_send(*, database_url: str | None, account: str) -> list[dict[str, Any]]:
    """Autonomous auto-send pass — the top rung of the policy ladder.

    For drafts that have sat past the undo/delay window, re-applies the
    escalation decision (``auto_act`` only) and a per-recipient trust gate,
    then sends via the hard-gated send path. ``mode='shadow'`` (the default)
    records a soak-only send without touching Gmail. No-op unless
    ``agent.auto_send.enabled``. Never sends a draft created in this same sweep
    (the delay window guarantees that)."""
    if not database_url:
        return []
    cfg = _auto_send_config()
    if not cfg["enabled"]:
        return []

    from app.agent import store
    from app.agent.escalation import assess_stakes, decide_action, escalation_config
    from app.agent.send import send_pending_row

    # Reaper: free any rows stranded in 'sending' by a crashed prior run so they
    # become eligible again (safe — a truly-sent row is 'sent', not 'sending').
    try:
        reaped = store.reap_stale_sending(database_url)
        if reaped:
            logger.info("auto-send: reaped %d stale 'sending' row(s)", reaped)
    except Exception as exc:
        logger.info("auto-send reaper skipped: %s", exc)

    # Daily send cap (UTC) — a blast-radius bound mirroring the auto-push cap:
    # 0 (or less) DISABLES auto-send entirely (matches daily_push_cap semantics),
    # never "unlimited".
    if cfg["daily_send_cap"] <= 0:
        return []
    esc = escalation_config()
    shadow = cfg["mode"] != "live"
    remaining: int | float = cfg["daily_send_cap"] - store.count_sent_today(database_url, account=account)
    due = store.due_for_auto_send(
        database_url, account=account,
        delay_minutes=cfg["delay_minutes"], limit=cfg["max_per_sweep"],
    )
    results: list[dict[str, Any]] = []
    for row in due:
        row_id = row["id"]
        if remaining <= 0:
            results.append({"id": row_id, "action": "held", "reason": "daily send cap reached"})
            continue
        decision = decide_action(
            quality_score=row.get("quality_score"),
            needs_reply_score=row.get("needs_reply_score") or 0.0,
            # Prefer the calibrated probability when one was persisted (Phase
            # A2); decide_action falls back to the raw score when it's None.
            calibrated_score=row.get("calibrated_score"),
            subject=row.get("subject"),
            body=row.get("body"),
            auto_act_floor=float(esc["auto_act_floor"]),
            confidence_floor=float(esc["confidence_floor"]),
            high_stakes_blocks=bool(esc["high_stakes_blocks"]),
        )
        if decision.action != "auto_act":
            results.append({"id": row_id, "action": "held", "reason": decision.action})
            continue
        # Stakes guard on the DRAFT too — escalation scans the inbound, but a
        # draft can itself state money/legal/commitment the inbound didn't.
        # Scan the body that will ACTUALLY be sent (push uses amended_draft or
        # draft), so an edited/regenerated draft that invents a price is caught.
        body_to_send = row.get("amended_draft") or row.get("draft")
        if esc["high_stakes_blocks"] and assess_stakes(row.get("subject"), body_to_send) == "high":
            results.append({"id": row_id, "action": "held", "reason": "high-stakes draft content"})
            continue
        trust = store.recipient_trust(database_url, row.get("sender_email"), account=account)
        if trust < cfg["min_recipient_trust"]:
            results.append({
                "id": row_id, "action": "held",
                "reason": f"recipient trust {trust} < {cfg['min_recipient_trust']}",
            })
            continue
        outcome = send_pending_row(database_url, row_id, shadow=shadow)
        if outcome.ok:
            action = "shadow" if (shadow or outcome.shadow) else "sent"
            if action == "sent":
                remaining -= 1  # only a real send consumes the daily cap
            results.append({"id": row_id, "action": action, "message_id": outcome.sent_message_id})
            logger.info("auto-send (%s) row %s", action, row_id)
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

    # Self-heal the agent tables before sweeping. A sweep against a stale
    # instance DB (server not restarted after a schema change, or started with
    # an instance-relative path so bootstrap couldn't find schema.sql) would
    # otherwise fail at the persist step EVERY tick with zero drafts — invisibly.
    # This is idempotent + cheap; failure-isolated so a migration hiccup can't
    # itself kill the sweep (the sweep would then surface the real DB error).
    try:
        from app.db.bootstrap import ensure_agent_schema

        ensure_agent_schema(database_url)
    except Exception as exc:
        logger.warning("ensure_agent_schema failed (continuing): %s", exc)

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
    if not lock.acquire():
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
        auto_sent=accum.get("auto_sent") or [],
        mailbox_actions=accum.get("mailbox_actions") or [],
    )


def _maybe_apply_mailbox_actions(
    database_url: str | None, account: str, messages: list[InboxMessage],
) -> list[dict[str, Any]]:
    """Apply agent.rules mailbox routing (label/archive/star/mark_read/
    mark_important/mark_unimportant) to every fetched message. No-op unless
    ``agent.actions.enabled`` and at least one mailbox-routing rule exists.
    Enforces the daily cap across the sweep; dry-run records intent."""
    if not database_url or not messages:
        return []
    from app.agent import actions as act
    from app.agent.rules import MAILBOX_ACTIONS, evaluate_mailbox_actions, load_rules

    cfg = act._actions_config()
    if not cfg["enabled"]:
        return []
    # 0 (or less) DISABLES routing — consistent with daily_push_cap /
    # daily_send_cap (never "unlimited").
    if cfg["daily_cap"] <= 0:
        return []
    rules = load_rules()
    # Test against the single source of truth (rules.MAILBOX_ACTIONS) so a new
    # action type can't silently no-op here for a rule set that uses only it.
    if not any(r["action"] in MAILBOX_ACTIONS for r in rules):
        return []

    from app.agent.inbox_fetch import message_age_days
    from app.core.sender import extract_domain

    # Sender history → the ``known_contact`` predicate (do I have prior reply
    # pairs with this sender?). Built once for the sweep; queries are cached.
    history = SenderHistory.from_database_url(database_url)

    remaining: int | float = cfg["daily_cap"] - act.count_actions_today(database_url, account=account)
    # Per-sweep label cache: fetch existing labels at most ONCE (only when a live
    # label apply could happen), instead of a `labels list` subprocess per
    # matched message. None in dry-run (no creates) keeps it cheap.
    known_labels: set[str] | None = None
    if not cfg["dry_run"]:
        try:
            from app.ingestion import gmail_write

            known_labels = gmail_write.list_labels(account=account)
        except Exception as exc:
            logger.info("label cache fetch skipped: %s", exc)
    results: list[dict[str, Any]] = []
    for msg in messages:
        acts = evaluate_mailbox_actions(
            rules,
            sender_email=msg.sender_email,
            domain=extract_domain(msg.sender or msg.sender_email or ""),
            subject=msg.subject,
            body=msg.body,
            to=msg.headers.get("to"),
            cc=msg.headers.get("cc"),
            has_attachment=msg.has_attachment,
            age_days=message_age_days(msg.received_at),
            known_contact=history.count_for(msg.sender_email) > 0,
        )
        if not acts:
            continue
        res = act.apply_mailbox_actions(
            database_url, account, msg, acts, remaining=remaining, known_labels=known_labels,
        )
        for r in res:
            r["message_id"] = msg.message_id
            if r.get("status") == "applied":
                remaining -= 1
        results.extend(res)
    if results:
        applied = sum(1 for r in results if r.get("status") == "applied")
        logger.info("mailbox routing: %d action(s) applied, %d total recorded", applied, len(results))
    return results


def _maybe_forward(
    database_url: str | None, account: str, messages: list[InboxMessage],
) -> list[dict[str, Any]]:
    """Apply agent.rules 'forward' actions to fetched messages — the OUTBOUND
    path, kept separate from label routing because it SENDS mail. No-op unless
    ``agent.actions.enabled`` and a forward rule exists. The actual send is gated
    inside ``apply_outbound_actions`` (send frontier + allow_forward + not
    dry-run); dry-run records intent only. Shares the daily cap with routing via
    the ledger count."""
    if not database_url or not messages:
        return []
    from app.agent import actions as act
    from app.agent.inbox_fetch import message_age_days
    from app.agent.rules import OUTBOUND_ACTIONS, evaluate_outbound_actions, load_rules
    from app.core.sender import extract_domain

    cfg = act._actions_config()
    if not cfg["enabled"] or cfg["daily_cap"] <= 0:
        return []
    rules = load_rules()
    if not any(r["action"] in OUTBOUND_ACTIONS for r in rules):
        return []

    history = SenderHistory.from_database_url(database_url)
    remaining: int | float = cfg["daily_cap"] - act.count_actions_today(database_url, account=account)
    results: list[dict[str, Any]] = []
    for msg in messages:
        acts = evaluate_outbound_actions(
            rules,
            sender_email=msg.sender_email,
            domain=extract_domain(msg.sender or msg.sender_email or ""),
            subject=msg.subject,
            body=msg.body,
            to=msg.headers.get("to"),
            cc=msg.headers.get("cc"),
            has_attachment=msg.has_attachment,
            age_days=message_age_days(msg.received_at),
            known_contact=history.count_for(msg.sender_email) > 0,
        )
        if not acts:
            continue
        res = act.apply_outbound_actions(database_url, account, msg, acts, remaining=remaining)
        for r in res:
            r["message_id"] = msg.message_id
            if r.get("status") == "applied":
                remaining -= 1
        results.extend(res)
    if results:
        fwd = sum(1 for r in results if r.get("status") == "applied")
        blocked = sum(1 for r in results if r.get("status") == "blocked")
        logger.info("forward routing: %d forwarded, %d blocked, %d total recorded", fwd, blocked, len(results))
    return results


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

    # 1b) Mailbox routing (the agent-action framework): apply label / archive /
    # star / mark_read / mark_important / mark_unimportant rules to EVERY fetched
    # message — routing isn't tied to drafting. Off by default + dry-run by
    # default; failure-isolated so a routing error can't break the sweep.
    mailbox_actions: list[dict[str, Any]] = []
    try:
        mailbox_actions = _maybe_apply_mailbox_actions(database_url, account, messages)
        # A message routed to ARCHIVE shouldn't also be drafted/persisted — the
        # user's rule said "get this out of my inbox", so drop it from the draft
        # pipeline (in dry-run too, so the soak previews the real behaviour).
        _archived = {
            a["message_id"] for a in mailbox_actions
            if a.get("action", {}).get("type") == "archive" and a.get("status") in ("applied", "dry_run")
        }
        if _archived:
            messages = [m for m in messages if m.message_id not in _archived]
            logger.info("mailbox routing: %d archived message(s) excluded from drafting", len(_archived))
    except Exception as exc:
        logger.warning("mailbox-routing step failed: %s", exc)

    # 1c) Outbound forward routing — SEPARATE from label routing because it sends
    # mail. Gated inside apply_outbound_actions (send frontier + allow_forward);
    # off unless a forward rule exists. Failure-isolated. Results join the same
    # ledger/accumulator (forward never archives, so drafting is unaffected).
    try:
        forward_actions = _maybe_forward(database_url, account, messages)
        mailbox_actions.extend(forward_actions)
    except Exception as exc:
        logger.warning("forward-routing step failed: %s", exc)

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

    # Calendar: when drafting a reply to a meeting request, offer real open
    # slots. Reads free/busy per meeting-request message (gog CLI). Opt-in.
    cal_cfg = _calendar_config()
    _need_intent = _rules_need_intent or cal_cfg["enabled"]

    # Long-thread "what changed" summaries (opt-in). Read once per sweep.
    try:
        from app.core.config import load_config as _lc

        _st = (((_lc() or {}).get("agent") or {}).get("summarize_threads") or {})
        _summarize_enabled = bool(_st.get("enabled", False)) if isinstance(_st, dict) else False
        _summary_min = int(_st.get("min_messages", 4) or 4) if isinstance(_st, dict) else 4
    except Exception:
        _summarize_enabled, _summary_min = False, 4

    classified = classify_many(
        messages, history=history, threshold=threshold,
        skip_senders=skip_senders, vip_senders=vip_senders,
    )
    # Borderline LLM veto: ask the warm model to demote would-be drafts that
    # are actually broadcasts (no-op unless agent.adjudication.enabled + model
    # available). Catches newsletters the regex misses.
    classified = _maybe_adjudicate(classified, threshold=threshold)

    # Attach the calibrated P(deserved a reply) when a calibrator has been
    # fitted from the user's own past verdicts (no-op on a fresh instance with
    # no decided rows). Observability + the principled gate for an act
    # decision; never changes needs_reply.
    classified = _maybe_calibrate(classified)

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

        # Per-message standing-instruction rules + calendar slot proposals.
        # Compute the effective instructions (global + matched rules + meeting
        # slots); a 'skip' rule drops the message from drafting entirely.
        effective_instructions = standing_instructions
        _intents: list[str] | None = None
        if _need_intent:
            try:
                from app.core.intent import classify_intents_multi

                _intents = classify_intents_multi(msg.body)
            except Exception:
                _intents = None

        _hold = False
        if rules:
            from app.agent.inbox_fetch import message_age_days
            from app.core.sender import extract_domain

            # Failure-isolated like the calendar/summary/draft steps below: a
            # rule-eval error must never abort the sweep (run_triage re-raises).
            # On failure, fall back to the global instructions and no hold.
            try:
                _rr = apply_rules(
                    rules,
                    sender_email=msg.sender_email,
                    domain=extract_domain(msg.sender or msg.sender_email or ""),
                    intents=_intents,
                    cold_outreach=verdict.cold_outreach,
                    base_instructions=standing_instructions,
                    subject=msg.subject,
                    body=msg.body,
                    to=msg.headers.get("to"),
                    cc=msg.headers.get("cc"),
                    has_attachment=msg.has_attachment,
                    age_days=message_age_days(msg.received_at),
                    known_contact=history.count_for(msg.sender_email) > 0,
                )
            except Exception as exc:
                logger.warning("agent.rules evaluation failed for %s: %s", msg.message_id, exc)
                _rr = {"skip": False, "hold": False, "instructions": standing_instructions, "matched": []}
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
            # A 'hold' rule: still draft (the reply is ready) but never auto-act
            # on it — excluded from auto-push/auto-send so a human decides.
            _hold = bool(_rr.get("hold"))

        # Calendar: a meeting request → propose real open slots so the draft
        # offers concrete times. Failure-isolated; never creates events.
        if cal_cfg["enabled"] and _intents and "meeting_request" in _intents:
            try:
                from app.agent.calendar import propose_open_slots

                slots = propose_open_slots(
                    account, tz=cal_cfg["tz"], business_days=cal_cfg["business_days"],
                    work_start_hour=cal_cfg["work_start_hour"], work_end_hour=cal_cfg["work_end_hour"],
                    slot_minutes=cal_cfg["slot_minutes"], max_slots=cal_cfg["max_slots"],
                )
                if slots:
                    note = (
                        f"The sender is asking to meet. You are free at: {slots}. "
                        f"Offer 2–3 of these specific times (times are in {cal_cfg['tz']}); "
                        "do not invent any other availability."
                    )
                    effective_instructions = f"{effective_instructions}\n{note}" if effective_instructions else note
            except Exception as exc:
                logger.warning("calendar slot proposal failed: %s", exc)

        # Long-thread catch-up summary (depends only on the thread, not the
        # draft). Failure-isolated.
        _summary: str | None = None
        if _summarize_enabled and msg.thread_history and len(msg.thread_history) >= _summary_min:
            try:
                from app.agent.thread_summary import summarize_thread

                _summary = summarize_thread(
                    msg.thread_history, subject=msg.subject, min_messages=_summary_min,
                )
            except Exception as exc:
                logger.warning("thread summary failed: %s", exc)

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
                    # b188: this is the AUTONOMOUS path — no human is watching
                    # this specific draft. interactive=False arms the abstain
                    # gate so a draft whose quality_score falls below the floor
                    # is WITHHELD and the email is surfaced for review instead of
                    # presenting a weak throwaway reply as ready. (The /draft API,
                    # /feedback, CLI, etc. leave interactive=True and always draft.)
                    interactive=False,
                ),
                database_url=database_url,
                configs_dir=configs_dir,
            )
            # b188: ABSTAIN. The drafter withheld this draft because its quality
            # fell below the floor — do NOT present it as a ready reply. Route the
            # email to the existing surface-for-review tier ("needs your
            # attention") instead. The generated text/score are kept in telemetry
            # (logged inside generate_draft) but never surfaced as usable here.
            # This produces FEWER outbound-eligible drafts, never more, so the
            # never-send invariant is trivially preserved.
            if getattr(resp, "withheld", False):
                _q = getattr(resp, "quality_score", None)
                surfaced.append(
                    (
                        msg,
                        NeedsReplyVerdict(
                            needs_reply=False,
                            score=verdict.score,
                            reasons=verdict.reasons
                            + [getattr(resp, "withhold_reason", None) or "draft withheld (low quality)"],
                            cold_outreach=verdict.cold_outreach,
                            surface_for_review=True,
                            vip=getattr(verdict, "vip", False),
                            calibrated_score=getattr(verdict, "calibrated_score", None),
                        ),
                    )
                )
                logger.info(
                    "abstain: surfacing %s for review instead of drafting (quality=%s)",
                    msg.message_id, _q,
                )
                cap_remaining -= 1  # ζ: a withheld draft still consumed a slot
                _maybe_extract_facts(msg, database_url)
                continue
            drafts.append(
                TriageDraft(
                    message=msg,
                    verdict=verdict,
                    draft=resp.draft,
                    model_used=resp.model_used,
                    repairs=list(getattr(resp, "repairs", []) or []),
                    standing_instructions_snapshot=effective_instructions,
                    thread_summary=_summary,
                    quality_score=getattr(resp, "quality_score", None),
                    hold=_hold,
                )
            )
            cap_remaining -= 1  # ζ: one less slot for this UTC day
            # Fact grounding: harvest any concrete facts the sender stated in
            # this (real, drafted) message into the memory table, so this and
            # future replies are grounded instead of inventing. Flag-gated
            # (off by default; it writes to memory), failure-isolated.
            _maybe_extract_facts(msg, database_url)
        except Exception as exc:
            logger.warning("triage draft generation failed for %s: %s", msg.message_id, exc)
            drafts.append(
                TriageDraft(
                    message=msg, verdict=verdict, error=f"{type(exc).__name__}: {exc}",
                    standing_instructions_snapshot=effective_instructions,
                    thread_summary=_summary,
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
                thread_summary=d.thread_summary,
                quality_score=d.quality_score,
                calibrated_score=getattr(d.verdict, "calibrated_score", None),
                hold=getattr(d, "hold", False),
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

    # Autonomous auto-send: the send frontier. Acts on drafts that have sat
    # past the undo window, passed escalation, and reached enough recipient
    # trust. Off by default, SHADOW by default; gated by send.enabled +
    # kill-switch downstream. Never sends drafts created this same sweep.
    auto_sent: list[dict[str, Any]] = []
    try:
        auto_sent = _maybe_auto_send(database_url=database_url, account=account)
    except Exception as exc:
        logger.warning("auto-send step failed: %s", exc)

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
        "auto_sent": auto_sent,
        "mailbox_actions": mailbox_actions,
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
