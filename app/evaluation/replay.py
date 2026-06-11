"""Inbox-replay backtest (b234): drafts vs the user's REAL replies, at scale.

The reply-pair corpus is ground truth nobody is using for end-to-end QA: for
every historical inbound we know exactly what the user actually sent. This
module replays those inbounds through the CURRENT production drafting pipeline
— with the answer held out — and diffs each draft against the real reply, so
draft-quality issues show up as measured, ranked classes instead of anecdotes.

Leakage control: an identical inbound would retrieve its own stored pair as
the top exemplar (handing the model the answer), so each case excludes its own
``reply_pair_id`` AND its whole ``thread_id`` from retrieval (same-thread
documents/chunks contain the real reply verbatim). See
``RetrievalRequest.exclude_*``.

Per-case signals:

* ``voice_match`` sub-scores (lexical / length / style, optional semantic)
  against the real reply — the "is this me?" axis.
* ``similarity`` (hybrid difflib/token) — the would-I-have-to-rewrite axis
  (1 - similarity ≈ the edit distance outcome_capture measures on live sends).
* language match draft↔reply (the inbound's language is the model's cue; the
  user's actual reply is the ground truth for which language was right).
* ``verify_draft`` blocking issues + ungrounded status assertions (b229) —
  the fabrication axis, now measured against cases instead of live accidents.
* stance heuristics: question answered, did the draft commit to something the
  real reply didn't (over-commitment).

Read-only over reply_pairs; generation runs deterministic + local-only so a
re-run is reproducible and free of cloud cost.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("youos.replay")

# Floors that keep junk cases out of the sample: an inbound under 40 chars has
# no signal to draft from; a reply under 20 chars ("ok thanks") scores noise.
_MIN_INBOUND_CHARS = 40
_MIN_REPLY_CHARS = 20
# A noreply/bot counterparty isn't a drafting case.
_AUTOMATION_AUTHOR = (
    "noreply", "no-reply", "donotreply", "notifications@", "mailer-daemon",
    "calendar-notification", "drive-shares", "docs.google.com",
)


@dataclass
class ReplayCase:
    reply_pair_id: int
    thread_id: str | None
    inbound_text: str
    real_reply: str
    inbound_author: str | None
    paired_at: str | None


@dataclass
class ReplayResult:
    case: ReplayCase
    draft: str | None
    model_used: str | None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _hard_skipped_by_triage(r: sqlite3.Row) -> bool:
    """Would today's needs-reply classifier hard-skip this inbound? (b236)

    The first live run scored receipts/scan notifications the production
    pipeline would never draft for anymore — the backtest should only measure
    mail that would actually reach drafting today. Built without headers
    (reply_pairs doesn't store them), so header-based hard skips can't fire;
    sender/subject/body-based ones (automation domains, noreply boxes,
    calendar subjects, service patterns) carry the load.
    """
    try:
        import json as _json

        from app.agent.inbox_fetch import InboxMessage
        from app.agent.needs_reply import classify
        from app.core.sender import extract_email

        meta = {}
        try:
            meta = _json.loads(r["metadata_json"] or "{}")
        except Exception:
            meta = {}
        author = r["inbound_author"] or ""
        msg = InboxMessage(
            message_id=f"replay-{r['id']}",
            thread_id=r["thread_id"] or f"replay-t-{r['id']}",
            account=str(meta.get("account_email") or "replay@example.invalid"),
            sender=author,
            sender_email=extract_email(author) or author,
            subject=str(meta.get("subject") or ""),
            body=r["inbound_text"] or "",
            headers={},
        )
        v = classify(msg)
        return v.score == 0.0 and not v.needs_reply and not v.surface_for_review
    except Exception:
        return False  # never let the filter kill sampling


def sample_pairs(
    database_url: str,
    *,
    n: int = 80,
    newest_first: bool = True,
    triage_filter: bool = True,
) -> list[ReplayCase]:
    """The N most recent usable reply pairs (real human exchange, non-trivial).

    Excludes quality-demoted pairs (b235 corpus cleanup) and, when
    ``triage_filter`` is on, inbounds today's classifier would hard-skip.
    """
    path = database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, thread_id, inbound_text, reply_text, inbound_author, paired_at, metadata_json
            FROM reply_pairs
            WHERE length(inbound_text) >= ? AND length(reply_text) >= ?
              AND COALESCE(quality_score, 1.0) > 0
            ORDER BY paired_at DESC, id DESC
            LIMIT ?
            """,
            (_MIN_INBOUND_CHARS, _MIN_REPLY_CHARS, n * 4),
        ).fetchall()
    finally:
        conn.close()

    cases: list[ReplayCase] = []
    for r in rows:
        author = (r["inbound_author"] or "").lower()
        if any(tok in author for tok in _AUTOMATION_AUTHOR):
            continue
        if triage_filter and _hard_skipped_by_triage(r):
            continue
        cases.append(
            ReplayCase(
                reply_pair_id=int(r["id"]),
                thread_id=r["thread_id"],
                inbound_text=r["inbound_text"],
                real_reply=r["reply_text"],
                inbound_author=r["inbound_author"],
                paired_at=r["paired_at"],
            )
        )
        if len(cases) >= n:
            break
    if not newest_first:
        cases.reverse()
    return cases


def _stance_signals(draft: str, real_reply: str, inbound: str) -> dict[str, Any]:
    """Cheap, deterministic stance comparisons. Heuristic by design — they
    rank cases for human reading, they don't pass/fail anything."""
    import re

    d, r = draft.lower(), real_reply.lower()
    commit_pat = re.compile(
        r"\b(i'll|i will|we'll|we will|i can|we can|happy to|confirmed|"
        r"let's|i'd be glad|count me in|yes,)\b"
    )
    decline_pat = re.compile(
        r"\b(unfortunately|can't|cannot|won't be able|not able to|decline|"
        r"no longer|not interested|pass on)\b"
    )
    return {
        "inbound_has_question": "?" in inbound[-400:],
        "draft_commits": bool(commit_pat.search(d)),
        "reply_commits": bool(commit_pat.search(r)),
        "draft_declines": bool(decline_pat.search(d)),
        "reply_declines": bool(decline_pat.search(r)),
    }


def evaluate_case(
    case: ReplayCase,
    draft: str,
    *,
    embed_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """All per-case metrics for one (draft, real reply) comparison."""
    from app.core.diff import hybrid_similarity
    from app.core.text_utils import detect_language
    from app.evaluation.voice_match import voice_match_score
    from app.generation.verify import verify_draft

    vm = voice_match_score(draft, case.real_reply, embed_fn=embed_fn)
    vr = verify_draft(draft, inbound=case.inbound_text)
    lang_reply = detect_language(case.real_reply)
    lang_draft = detect_language(draft)
    sim = hybrid_similarity(draft, case.real_reply)

    return {
        **{k: vm.get(k) for k in ("voice_match", "lexical_overlap", "length_ratio", "style_similarity", "semantic_similarity")},
        "similarity": round(sim, 3),
        "rewrite_distance": round(1.0 - sim, 3),
        "lang_draft": lang_draft,
        "lang_reply": lang_reply,
        "lang_match": lang_draft == lang_reply,
        "verify_blocking": vr.blocking,
        "status_claims": vr.status_claims,
        "draft_words": len(draft.split()),
        "reply_words": len(case.real_reply.split()),
        **_stance_signals(draft, case.real_reply, case.inbound_text),
    }


def run_replay(
    cases: list[ReplayCase],
    *,
    database_url: str,
    configs_dir: Any,
    embed_fn: Callable[[str], Any] | None = None,
    progress: Callable[[int, int], None] | None = None,
    draft_fn: Callable[[ReplayCase], tuple[str | None, str | None]] | None = None,
) -> list[ReplayResult]:
    """Generate a draft per case (holdout-excluded) and score it.

    ``draft_fn`` overrides generation for tests: returns (draft, model_used).
    """
    results: list[ReplayResult] = []
    for i, case in enumerate(cases):
        if progress:
            progress(i + 1, len(cases))
        draft: str | None = None
        model_used: str | None = None
        error: str | None = None
        if draft_fn is not None:
            draft, model_used = draft_fn(case)
        else:
            try:
                from app.generation.service import EVAL_SEED, DraftRequest, generate_draft

                resp = generate_draft(
                    DraftRequest(
                        inbound_message=case.inbound_text,
                        sender=case.inbound_author,
                        deterministic=True,
                        seed=EVAL_SEED,
                        no_cloud_fallback=True,
                        use_exemplar_cache=False,
                        exclude_reply_pair_ids=(case.reply_pair_id,),
                        exclude_thread_ids=(case.thread_id,) if case.thread_id else (),
                    ),
                    database_url=database_url,
                    configs_dir=configs_dir,
                )
                draft = resp.draft
                model_used = resp.model_used
            except Exception as exc:  # noqa: BLE001 — one bad case must not kill the run
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("replay case %s failed: %s", case.reply_pair_id, error)

        result = ReplayResult(case=case, draft=draft, model_used=model_used, error=error)
        if draft and not error:
            try:
                result.metrics = evaluate_case(case, draft, embed_fn=embed_fn)
            except Exception as exc:  # noqa: BLE001
                result.error = f"metrics failed: {type(exc).__name__}: {exc}"
        results.append(result)
    return results


def aggregate(results: list[ReplayResult]) -> dict[str, Any]:
    """Roll per-case metrics into the issue classes worth fixing."""
    scored = [r for r in results if r.metrics and not r.error]
    n = len(scored)

    def _avg(key: str) -> float | None:
        vals = [r.metrics[key] for r in scored if isinstance(r.metrics.get(key), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    lang_mismatch = [r for r in scored if not r.metrics.get("lang_match")]
    fabricating = [r for r in scored if r.metrics.get("status_claims")]
    blocking = [r for r in scored if r.metrics.get("verify_blocking")]
    over_commit = [
        r for r in scored
        if r.metrics.get("draft_commits") and not r.metrics.get("reply_commits")
    ]
    missed_decline = [
        r for r in scored
        if r.metrics.get("reply_declines") and not r.metrics.get("draft_declines")
    ]
    too_long = [
        r for r in scored
        if r.metrics.get("reply_words") and r.metrics["draft_words"] > 2 * r.metrics["reply_words"]
    ]
    worst = sorted(scored, key=lambda r: r.metrics.get("voice_match") or 0.0)[:10]

    def _ids(rs: list[ReplayResult]) -> list[int]:
        return [r.case.reply_pair_id for r in rs]

    return {
        "cases": len(results),
        "scored": n,
        "errors": len([r for r in results if r.error]),
        "avg_voice_match": _avg("voice_match"),
        "avg_similarity": _avg("similarity"),
        "avg_rewrite_distance": _avg("rewrite_distance"),
        "avg_length_ratio": _avg("length_ratio"),
        "issues": {
            "language_mismatch": {"count": len(lang_mismatch), "pair_ids": _ids(lang_mismatch)},
            "ungrounded_status_claims": {"count": len(fabricating), "pair_ids": _ids(fabricating)},
            "verify_blocking": {"count": len(blocking), "pair_ids": _ids(blocking)},
            "over_commitment": {"count": len(over_commit), "pair_ids": _ids(over_commit)},
            "missed_decline": {"count": len(missed_decline), "pair_ids": _ids(missed_decline)},
            "draft_2x_longer_than_reply": {"count": len(too_long), "pair_ids": _ids(too_long)},
        },
        "worst_pair_ids": _ids(worst),
    }


def to_report(results: list[ReplayResult], summary: dict[str, Any]) -> dict[str, Any]:
    """JSON-serializable full report (summary + per-case rows, texts truncated)."""

    def _snip(s: str | None, lim: int = 500) -> str:
        t = (s or "").strip()
        return t if len(t) <= lim else t[:lim].rstrip() + "…"

    return {
        "summary": summary,
        "cases": [
            {
                "reply_pair_id": r.case.reply_pair_id,
                "paired_at": r.case.paired_at,
                "inbound_author": r.case.inbound_author,
                "inbound": _snip(r.case.inbound_text, 400),
                "real_reply": _snip(r.case.real_reply),
                "draft": _snip(r.draft),
                "model_used": r.model_used,
                "error": r.error,
                **{f"m_{k}": v for k, v in (r.metrics or {}).items()},
            }
            for r in results
        ],
    }
