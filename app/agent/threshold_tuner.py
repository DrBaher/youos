"""Auto-tune the needs-reply threshold from real send outcomes.

The needs-reply classifier decides what lands in the triage queue: a message
scores ``>= agent.threshold`` → YouOS drafts a reply. Set too low, it drafts
for mail you never reply to (over-drafting — noise in the queue); too high, it
misses mail you'd have replied to.

We don't have to guess. ``outcome_capture`` records, for each queued draft, the
**ground truth**: did you actually send a reply on that thread (``outcome='sent'``)
or not (``outcome='no_send'``)? The *send rate* — fraction of decided drafts you
replied to — is a direct precision signal:

* send rate **low** → the queue is full of mail you don't reply to → raise the
  threshold (draft less, more selectively).
* send rate **high** → almost everything queued earned a reply; you may be
  missing borderline mail → lower the threshold (draft a little more).

This module turns that signal into a bounded, conservative threshold nudge. It
is deliberately timid: one ``step`` per run, a dead-band around the target so it
doesn't oscillate, a minimum-sample floor so a handful of outcomes can't swing
it, and hard bounds so it can never drift to a degenerate value. The nightly
applies the recommendation (``step_tune_threshold``); the Stats panel surfaces
it with a manual Apply button.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any

# A draft the user replied to within the capture window counts toward the send
# rate; an aged-out draft with no reply counts against it. Drafts still inside
# the no_send grace window (outcome not yet decided) are excluded — they're not
# evidence either way yet.
_DECIDED_OUTCOMES = ("sent", "no_send")

# Defaults. The target band is the send rate we consider "healthy": most queued
# drafts earn a real reply, but we don't demand 100% (some legitimately-drafted
# replies just never get sent). The dead-band (±tolerance) stops a send rate
# that's merely a little off-target from nudging the threshold every single run.
DEFAULT_TARGET_SEND_RATE = 0.40
DEFAULT_TOLERANCE = 0.10
DEFAULT_STEP = 0.05
DEFAULT_MIN_SAMPLES = 25
DEFAULT_BOUNDS = (0.50, 0.85)
# Outcome recency window. 14 days, not 60: the tuner must judge the threshold
# by drafts created under (or near) the CURRENT value. On baheros a 60-day
# window let ~95 pre-tune no_sends dominate for weeks after the threshold rose,
# so the tuner kept "seeing" over-drafting it had already fixed and walked
# straight to the ceiling.
DEFAULT_OUTCOME_WINDOW_DAYS = 14


@dataclass
class ThresholdRecommendation:
    """The outcome of one tuning evaluation.

    ``changed`` is True only when ``recommended`` differs from ``current`` by
    more than a rounding epsilon — the nightly / UI use it to decide whether to
    write config at all. ``reason`` is a short human string for the log and the
    Stats panel.
    """

    current: float
    recommended: float
    sent: int
    no_send: int
    send_rate: float | None
    changed: bool
    reason: str

    @property
    def samples(self) -> int:
        return self.sent + self.no_send

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": round(self.current, 4),
            "recommended": round(self.recommended, 4),
            "sent": self.sent,
            "no_send": self.no_send,
            "samples": self.samples,
            "send_rate": None if self.send_rate is None else round(self.send_rate, 4),
            "changed": self.changed,
            "reason": self.reason,
        }


def recommend_threshold(
    *,
    current: float,
    sent: int,
    no_send: int,
    target_send_rate: float = DEFAULT_TARGET_SEND_RATE,
    tolerance: float = DEFAULT_TOLERANCE,
    step: float = DEFAULT_STEP,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    bounds: tuple[float, float] = DEFAULT_BOUNDS,
) -> ThresholdRecommendation:
    """Recommend a (possibly unchanged) needs-reply threshold from send outcomes.

    Conservative by construction: returns ``current`` unchanged when there are
    fewer than ``min_samples`` decided outcomes, when the send rate is within
    ``tolerance`` of ``target_send_rate``, or when a nudge would leave the
    ``bounds`` (it clamps instead, and reports unchanged if already at the bound).
    Moves at most one ``step`` per call.
    """
    lo, hi = bounds
    sent = max(0, int(sent))
    no_send = max(0, int(no_send))
    total = sent + no_send

    def _result(recommended: float, reason: str, send_rate: float | None) -> ThresholdRecommendation:
        recommended = round(max(lo, min(hi, recommended)), 4)
        changed = abs(recommended - current) > 1e-9
        return ThresholdRecommendation(
            current=round(current, 4),
            recommended=recommended,
            sent=sent,
            no_send=no_send,
            send_rate=send_rate,
            changed=changed,
            reason=reason,
        )

    if total < min_samples:
        return _result(
            current,
            f"holding — only {total} decided outcome(s), need {min_samples} to tune",
            None if total == 0 else sent / total,
        )

    send_rate = sent / total

    if send_rate < target_send_rate - tolerance:
        # Over-drafting: the queue is mostly mail you don't reply to. Raise the
        # bar so YouOS drafts more selectively.
        proposed = current + step
        if proposed > hi + 1e-9:
            return _result(
                hi,
                f"send rate {send_rate:.0%} is low but threshold already at the {hi:.2f} ceiling",
                send_rate,
            )
        return _result(
            proposed,
            f"raising threshold: send rate {send_rate:.0%} < {target_send_rate:.0%} target "
            f"({no_send} of {total} drafts went unanswered — over-drafting)",
            send_rate,
        )

    if send_rate > target_send_rate + tolerance:
        # Under-drafting: almost everything queued earned a reply — you may be
        # missing borderline mail. Lower the bar a little.
        proposed = current - step
        if proposed < lo - 1e-9:
            return _result(
                lo,
                f"send rate {send_rate:.0%} is high but threshold already at the {lo:.2f} floor",
                send_rate,
            )
        return _result(
            proposed,
            f"lowering threshold: send rate {send_rate:.0%} > {target_send_rate:.0%} target "
            f"({sent} of {total} drafts earned a reply — room to draft more)",
            send_rate,
        )

    return _result(
        current,
        f"holding — send rate {send_rate:.0%} is within ±{tolerance:.0%} of the "
        f"{target_send_rate:.0%} target",
        send_rate,
    )


def outcome_counts(
    database_url: str,
    *,
    account: str | None = None,
    days: int = DEFAULT_OUTCOME_WINDOW_DAYS,
    since: str | None = None,
) -> tuple[int, int]:
    """``(sent, no_send)`` decided outcome counts from ``agent_pending_drafts``.

    Counts only rows reconciled by ``outcome_capture`` whose draft was created
    within ``days`` (so a stale outcome from months ago doesn't anchor the
    tuner to old behaviour) — and, when ``since`` is given (the last threshold
    change, ISO timestamp), only drafts created after it: drafts queued under a
    previous threshold are evidence about THAT threshold, not the current one.
    Returns ``(0, 0)`` if the table/columns don't exist yet.
    """
    import sqlite3

    from app.agent.store import _connect

    where = "outcome IN (?, ?) AND created_at >= datetime('now', ?)"
    params: list[Any] = [*_DECIDED_OUTCOMES, f"-{int(days)} days"]
    if since:
        where += " AND created_at >= datetime(?)"
        params.append(since)
    if account:
        where += " AND account = ?"
        params.append(account)
    try:
        with closing(_connect(database_url)) as conn:
            rows = conn.execute(
                f"SELECT outcome, COUNT(*) AS n FROM agent_pending_drafts WHERE {where} GROUP BY outcome",  # noqa: S608
                params,
            ).fetchall()
    except sqlite3.OperationalError:
        return (0, 0)
    counts = {str(r["outcome"]): int(r["n"]) for r in rows}
    return (counts.get("sent", 0), counts.get("no_send", 0))


def recommend_from_database(
    database_url: str,
    *,
    current: float,
    account: str | None = None,
    days: int = DEFAULT_OUTCOME_WINDOW_DAYS,
    since: str | None = None,
    **kwargs: Any,
) -> ThresholdRecommendation:
    """Convenience: read decided outcome counts from the DB and recommend.

    Pass ``since`` = the last threshold change (``agent.threshold_changed_at``)
    so pre-change outcomes don't keep arguing against a threshold that already
    moved. Extra keyword args pass through to :func:`recommend_threshold`
    (target, tolerance, step, min_samples, bounds)."""
    sent, no_send = outcome_counts(database_url, account=account, days=days, since=since)
    return recommend_threshold(current=current, sent=sent, no_send=no_send, **kwargs)
