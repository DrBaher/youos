"""Precision/recall of the draft decision, measured on REAL mail.

The fixture harness (``triage_eval.py``) scores the classifier against
hand-labeled cases. This one scores it against the **user's own verdicts** on
the queue — the only ground truth that reflects the live inbox.

How a decided row becomes a label:

* What the agent *predicted* is its **tier**: ``draft`` = "this needs a reply, I
  drafted it" (predicted positive); ``surface`` = "borderline, I surfaced it for
  review but didn't draft" (predicted negative / abstain).
* What was *true* is the user's verdict:
    - ``sent`` / ``amended`` → the draft was used → the message **deserved** a
      reply (truth positive).
    - dismissed ``noise`` / ``wrong_sender`` → the agent shouldn't have engaged
      (truth negative — these are the false positives we care about).
    - dismissed ``wrong_content`` → it *did* deserve a reply, the draft was just
      wrong (truth positive for the needs-reply decision; draft quality is a
      separate axis covered by the quality gate).
    - dismissed ``already_handled`` / ``other`` / no reason, or still ``pending``
      → can't label the *needs-reply* decision confidently → **excluded**.

So: TP = drafted & deserved; FP = drafted & shouldn't-have; FN = surfaced &
deserved (should have drafted); TN = surfaced & shouldn't-have.

Read-only over ``agent_pending_drafts``. ``record_snapshot`` appends one row to
``triage_precision_history`` so the false-positive rate is trackable over time.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any

_POSITIVE_DISMISSALS = frozenset({"wrong_content"})
_NEGATIVE_DISMISSALS = frozenset({"noise", "wrong_sender"})


def _truth_label(status: str | None, dismissal_reason: str | None) -> str | None:
    """Map a decided row to ``"pos"`` (deserved a reply), ``"neg"`` (didn't), or
    ``None`` (can't tell — excluded from the metric)."""
    if status in ("sent", "amended"):
        return "pos"
    if status == "dismissed":
        if dismissal_reason in _NEGATIVE_DISMISSALS:
            return "neg"
        if dismissal_reason in _POSITIVE_DISMISSALS:
            return "pos"
        return None  # already_handled / other / no reason → ambiguous
    return None  # pending / unknown


def evaluate_real_mail(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Compute precision/recall of the draft decision from decided queue rows.

    Returns ``{precision, recall, f1, confusion: {tp,fp,tn,fn}, sample_size,
    excluded, fp_by_reason, fp_senders}``. ``sample_size`` is the number of
    *labelable* rows; ``excluded`` counts decided rows we couldn't label.
    """
    from app.agent.store import _connect

    where = "date(created_at) >= date('now', ?)"
    params: list[Any] = [f"-{int(days)} days"]
    if account:
        where += " AND account = ?"
        params.append(account)

    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"SELECT tier, status, dismissal_reason, sender_email "
            f"FROM agent_pending_drafts WHERE {where}",
            params,
        ).fetchall()

    tp = fp = fn = tn = excluded = 0
    fp_by_reason: dict[str, int] = {}
    fp_senders: dict[str, int] = {}
    for r in rows:
        truth = _truth_label(r["status"], r["dismissal_reason"])
        if truth is None:
            excluded += 1
            continue
        predicted_pos = (r["tier"] == "draft")
        if predicted_pos and truth == "pos":
            tp += 1
        elif predicted_pos and truth == "neg":
            fp += 1
            reason = r["dismissal_reason"] or "no_reason"
            fp_by_reason[reason] = fp_by_reason.get(reason, 0) + 1
            sender = (r["sender_email"] or "unknown").lower()
            fp_senders[sender] = fp_senders.get(sender, 0) + 1
        elif (not predicted_pos) and truth == "pos":
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision and recall and (precision + recall):
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = None

    return {
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "sample_size": tp + fp + fn + tn,
        "excluded": excluded,
        "window_days": int(days),
        "account": account,
        # Sorted, top few — so the operator sees what's driving false positives.
        "fp_by_reason": dict(sorted(fp_by_reason.items(), key=lambda kv: -kv[1])),
        "fp_senders": dict(sorted(fp_senders.items(), key=lambda kv: -kv[1])[:10]),
    }


def record_snapshot(
    database_url: str,
    result: dict[str, Any],
    *,
    account: str | None = None,
    days: int = 30,
) -> int | None:
    """Append a precision snapshot to ``triage_precision_history``. Returns the
    new row id. A snapshot with no labelable rows is still recorded (with NULL
    metrics) so a gap in the time series is visible rather than silent."""
    from app.agent.store import _connect

    c = result.get("confusion", {})
    with closing(_connect(database_url)) as conn:
        cur = conn.execute(
            """
            INSERT INTO triage_precision_history (
                account, window_days, precision, recall, f1,
                tp, fp, fn, tn, sample_size, excluded
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account if account is not None else result.get("account"),
                int(days),
                result.get("precision"),
                result.get("recall"),
                result.get("f1"),
                int(c.get("tp", 0)),
                int(c.get("fp", 0)),
                int(c.get("fn", 0)),
                int(c.get("tn", 0)),
                int(result.get("sample_size", 0)),
                int(result.get("excluded", 0)),
            ),
        )
        conn.commit()
        return cur.lastrowid


def precision_history(
    database_url: str,
    *,
    account: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Most-recent-first precision snapshots for charting / trend display."""
    from app.agent.store import _connect

    where = ""
    params: list[Any] = []
    if account:
        where = "WHERE account = ?"
        params.append(account)
    params.append(int(limit))
    with closing(_connect(database_url)) as conn:
        rows = conn.execute(
            f"SELECT computed_at, window_days, precision, recall, f1, "
            f"tp, fp, fn, tn, sample_size, excluded "
            f"FROM triage_precision_history {where} "
            f"ORDER BY computed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def run_and_record(
    database_url: str,
    *,
    account: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Compute and persist in one call (the nightly entrypoint)."""
    result = evaluate_real_mail(database_url, account=account, days=days)
    record_snapshot(database_url, result, account=account, days=days)
    return result
