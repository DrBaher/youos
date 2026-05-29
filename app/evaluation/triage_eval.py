"""Precision/recall harness for the needs-reply triage classifier.

The audit's sharpest accuracy finding: triage quality was *unobservable* — the
only runtime signal was the post-hoc dismissal rate, and false negatives (real
mail the filter buried) left no trace at all. This measures the classifier
against a labelled set so "how accurate is my triage?" finally has an answer.

A case is ``{label: bool, sender, sender_email?, subject?, body, headers?}``.
``label`` is the ground truth (does this inbound deserve a reply?). The harness
runs the real :func:`app.agent.needs_reply.classify` and reports
precision/recall/F1 + a confusion matrix at a given threshold, plus a small
threshold sweep so you can see the precision/recall trade-off.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.inbox_fetch import InboxMessage
from app.agent.needs_reply import classify


def _case_to_message(case: dict[str, Any]) -> InboxMessage:
    return InboxMessage(
        message_id=str(case.get("message_id", "case")),
        thread_id=str(case.get("thread_id", "t")),
        account=str(case.get("account", "you@example.com")),
        sender=case.get("sender", "") or "",
        sender_email=case.get("sender_email"),
        subject=case.get("subject", "") or "",
        body=case.get("body", "") or "",
        headers={str(k).lower(): str(v) for k, v in (case.get("headers") or {}).items()},
    )


def evaluate_triage(
    cases: Iterable[dict[str, Any]],
    *,
    threshold: float = 0.6,
    skip_senders: list[str] | None = None,
) -> dict[str, Any]:
    """Score the classifier against labelled cases at ``threshold``.

    Returns ``{precision, recall, f1, accuracy, confusion: {tp,fp,tn,fn},
    n, threshold, errors}`` where ``errors`` lists the misclassified cases
    (so you can eyeball what the filter gets wrong)."""
    tp = fp = tn = fn = 0
    errors: list[dict[str, Any]] = []
    cases = list(cases)
    for c in cases:
        verdict = classify(_case_to_message(c), threshold=threshold, skip_senders=skip_senders)
        pred = bool(verdict.needs_reply)
        label = bool(c["label"])
        if pred and label:
            tp += 1
        elif pred and not label:
            fp += 1
            errors.append({"kind": "false_positive", "subject": c.get("subject"),
                           "sender": c.get("sender"), "score": verdict.score})
        elif not pred and not label:
            tn += 1
        else:
            fn += 1
            errors.append({"kind": "false_negative", "subject": c.get("subject"),
                           "sender": c.get("sender"), "score": verdict.score})

    n = len(cases)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    accuracy = (tp + tn) / n if n else 0.0
    return {
        "n": n,
        "threshold": threshold,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(accuracy, 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "errors": errors,
    }


def threshold_sweep(
    cases: Iterable[dict[str, Any]],
    *,
    thresholds: Iterable[float] = (0.5, 0.55, 0.6, 0.65, 0.7, 0.75),
) -> list[dict[str, Any]]:
    """F1/precision/recall at several thresholds — surfaces the trade-off so a
    user can pick ``agent.threshold`` from data rather than guessing."""
    cases = list(cases)
    out: list[dict[str, Any]] = []
    for t in thresholds:
        r = evaluate_triage(cases, threshold=t)
        out.append({k: r[k] for k in ("threshold", "precision", "recall", "f1", "accuracy")})
    return out


def best_threshold(cases: Iterable[dict[str, Any]], **kw) -> float:
    """The threshold maximizing F1 over the sweep (ties → lower threshold,
    favoring recall — better to surface than to bury)."""
    sweep = threshold_sweep(cases, **kw)
    if not sweep:
        return 0.6
    return max(sweep, key=lambda r: (r["f1"], -r["threshold"]))["threshold"]
