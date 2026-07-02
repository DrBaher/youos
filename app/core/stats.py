"""Unified stats query layer for YouOS."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]


def _get_var_path(filename: str) -> Path:
    from app.core.settings import get_var_dir

    return get_var_dir() / filename


def _resolve_adapter_path() -> Path:
    from app.core.settings import get_adapter_path

    return get_adapter_path()


ADAPTER_PATH = _resolve_adapter_path()
AUTORESEARCH_JSONL = _get_var_path("autoresearch_runs.jsonl")
AUTORESEARCH_LOG = _get_var_path("autoresearch_log.md")


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return False


def _group_counts(conn: sqlite3.Connection, column: str, *, default: str) -> dict[str, int]:
    """COUNT(*) grouped by a draft_events column (NULLs folded to *default*)."""
    try:
        rows = conn.execute(
            f"SELECT COALESCE({column}, ?) AS k, COUNT(*) AS n FROM draft_events GROUP BY k ORDER BY n DESC",  # noqa: S608
            (default,),
        ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}
    except sqlite3.OperationalError:
        return {}


def summarize_draft_events(database_url: str) -> dict:
    """Aggregate the ``draft_events`` signal log into a draft-quality picture.

    Every generated draft is logged with the *conditions* it was produced
    under (intent, sender_type, confidence, length_flag). This summarizes them
    so the loop can see *where* drafting is weak — e.g. an intent whose drafts
    are frequently off the target length, or a cohort that draws mostly
    low-confidence retrieval. Where a draft can be matched to a real edit
    outcome, it also reports the average edit distance by condition.

    Outcome linkage (b167): the ground-truth edit signal — what the user
    *actually* sent vs. the draft — lives in ``feedback_pairs.edit_distance_pct``
    (~70k rows on a live instance, organic-backfilled from real sent mail), NOT
    in ``draft_history`` (a near-empty legacy table whose ``edit_distance_pct``
    is effectively never populated, so the old join matched 0 and the outcome
    dicts were always ``{}``). The only stable key shared by ``draft_events`` and
    ``feedback_pairs`` is ``inbound_text``: the draft *text* differs between them
    (``draft_events`` logs the produced draft; ``feedback_pairs.generated_draft``
    is the organic/full reply), so an inbound+draft join still matches ~nothing.
    We therefore join on ``inbound_text`` alone, first collapsing
    ``feedback_pairs`` to one mean edit-distance per inbound so the
    many-feedback-rows-per-inbound fan-out can't double-count a cohort. ``matched``
    is an honest coverage counter (distinct ``draft_events`` rows that found any
    outcome) so a low/zero join rate stays visible, not silently swallowed.

    Honesty filter (b185): the collapsed ``feedback_pairs`` includes ONLY rows
    that represent a real draft-vs-sent comparison — ``edit_distance_pct IS NOT
    NULL AND COALESCE(organic,0)=0 AND generated_draft <> edited_reply``. The
    ~82k "organic" backfill rows copy the sent reply into both columns with a
    hardcoded ``edit_distance_pct=0.0`` (no model draft existed), so counting
    them reads as a false "drafts are perfect" 0.0 everywhere. Excluding them
    drops ``matched`` toward zero on current data — which is the correct
    reading: there is little genuine draft-vs-sent signal yet, and the loop
    should not be told otherwise.

    The model's own drafts are never training targets; this is
    analysis/observability for the loop.
    """
    from app.db.bootstrap import resolve_sqlite_path

    empty = {
        "total": 0,
        "by_intent": {},
        "by_sender_type": {},
        "by_confidence": {},
        "by_length_flag": {},
        "by_model": {},
        "off_target_pct": None,
        "fabrication_rate": None,
        "outcome": {"matched": 0, "avg_edit_distance_by_sender_type": {}, "avg_edit_distance_by_confidence": {}},
    }

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return empty

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = _safe_count(conn, "draft_events")
        if total == 0:
            return empty

        summary = {
            "total": total,
            "by_intent": _group_counts(conn, "intent", default="unknown"),
            # b192 metric honesty: NULL sender_type means the row NEVER LOGGED a
            # sender (the pre-2026-05-28 drafting path didn't pass the author) —
            # it is NOT a classifier "unknown" verdict. classify_sender only
            # returns "unknown" for a sender string with no parseable email
            # (see classify_sender_detail). Folding NULL into "unknown" (the old
            # default) made the dashboard report ~78% scary "unknown" senders
            # when the real cause is sender-less historical rows. Bucket NULL
            # under the clearly-named "unlogged" key and reserve "unknown" for a
            # genuine classifier result. Same family as the b185 hollow-metric
            # fix: don't conflate "we never recorded it" with "we classified it".
            "by_sender_type": _group_counts(conn, "sender_type", default="unlogged"),
            "by_confidence": _group_counts(conn, "confidence", default="unknown"),
            "by_length_flag": _group_counts(conn, "length_flag", default="none"),
            # Which model actually produced each draft — the source of truth for
            # whether the LoRA adapter is really in use vs. a silent base/cloud
            # fallback. See get_drafting_model_status().
            "by_model": _group_counts(conn, "model_used", default="unknown"),
            "off_target_pct": None,
            "fabrication_rate": None,
            "outcome": {"matched": 0, "avg_edit_distance_by_sender_type": {}, "avg_edit_distance_by_confidence": {}},
        }

        # Fraction of length-annotated drafts that missed the target band.
        try:
            off, flagged = conn.execute(
                """SELECT
                       SUM(CASE WHEN length_flag IN ('long', 'short') THEN 1 ELSE 0 END),
                       SUM(CASE WHEN length_flag IS NOT NULL THEN 1 ELSE 0 END)
                   FROM draft_events"""
            ).fetchone()
            if flagged:
                summary["off_target_pct"] = round(100.0 * (off or 0) / flagged, 1)
        except sqlite3.OperationalError:
            pass

        # Fabrication rate (b286): share of REAL generated drafts that verify
        # flagged as a fabrication or hard block (invented family detail,
        # hallucinated meeting, speaker inversion, leaked scaffolding, invented
        # deadline, ungrounded status claim). Tracks whether model changes drive
        # the confidently-wrong rate down over time. Only present once the
        # verify_flags column exists (self-healed by _log_draft_event).
        if _column_exists(conn, "draft_events", "verify_flags"):
            try:
                flagged_n, total_n = conn.execute(
                    """SELECT
                           SUM(CASE WHEN verify_flags LIKE '%fabrication%'
                                      OR verify_flags LIKE '%blocking%'
                                      OR verify_flags LIKE '%status_claim%'
                                    THEN 1 ELSE 0 END),
                           COUNT(*)
                       FROM draft_events"""
                ).fetchone()
                if total_n:
                    summary["fabrication_rate"] = round(100.0 * (flagged_n or 0) / total_n, 1)
            except sqlite3.OperationalError:
                pass

        # Outcome correlation (b286 join-key fix, b185 honesty filter).
        #
        # The original join used ``inbound_text`` equality — but the two tables
        # source that text from DIFFERENT pipelines (``draft_events`` logs the
        # body handed to the drafter; ``feedback_pairs.inbound_text`` is a
        # reconstructed "\n\n---\n\n"-joined thread), so it byte-matches ~never
        # and ``matched`` sat at 0, starving autoresearch of draft-quality case
        # weights and making the loop look inert. b269 added
        # ``draft_events.thread_id`` as the stable key precisely for this, but
        # the stats join was never migrated. We now join on ``thread_id``
        # (``feedback_pairs`` has none, so we reach it via
        # ``reply_pair_id -> reply_pairs.thread_id``), and keep the old
        # inbound_text match as a COALESCE fallback for legacy rows lacking a
        # thread_id. Each side is collapsed to one mean edit-distance per key
        # first, and the LEFT JOINs are single-valued, so one draft_events row
        # yields exactly one outcome (thread preferred) — no fan-out.
        #
        # b185 honesty filter: count ONLY real draft-vs-sent comparisons —
        # ``edit_distance_pct IS NOT NULL AND COALESCE(organic,0)=0 AND
        # generated_draft <> edited_reply``. The ~82k "organic" backfill rows
        # copy the sent reply into both columns with a hardcoded
        # edit_distance_pct=0.0 (no model draft existed); including them reads
        # as a false "drafts are perfect" 0.0 everywhere. ``matched`` stays an
        # honest coverage counter so a low join rate is visible, not swallowed.
        _HONEST_FP = (
            "edit_distance_pct IS NOT NULL "
            "AND COALESCE(organic, 0) = 0 "
            "AND generated_draft <> edited_reply"
        )
        # The thread_id join is only available when the schema supports it
        # (reply_pairs table + feedback_pairs.reply_pair_id + draft_events
        # .thread_id). On older/partial DBs — and the minimal test fixtures —
        # fall back to the legacy inbound_text match alone.
        _has_thread_join = (
            _table_exists(conn, "reply_pairs")
            and _column_exists(conn, "feedback_pairs", "reply_pair_id")
            and _column_exists(conn, "draft_events", "thread_id")
        )
        if _has_thread_join:
            _OUTCOME_SQL = (
                "WITH obt AS ("
                "  SELECT rp.thread_id AS k, AVG(fp.edit_distance_pct) AS ed"
                "  FROM feedback_pairs fp JOIN reply_pairs rp ON fp.reply_pair_id = rp.id"
                "  WHERE fp.edit_distance_pct IS NOT NULL AND COALESCE(fp.organic,0)=0"
                "    AND fp.generated_draft <> fp.edited_reply"
                "    AND rp.thread_id IS NOT NULL AND rp.thread_id <> ''"
                "  GROUP BY rp.thread_id"
                "), obi AS ("
                f"  SELECT inbound_text AS k, AVG(edit_distance_pct) AS ed"
                f"  FROM feedback_pairs WHERE {_HONEST_FP}"
                "  GROUP BY inbound_text"
                ") "
                "SELECT COALESCE(de.sender_type, 'unlogged') AS st,"
                "       COALESCE(de.confidence, 'unknown') AS cf,"
                "       COALESCE(obt.ed, obi.ed) AS ed"
                "  FROM draft_events de"
                "  LEFT JOIN obt ON de.thread_id = obt.k"
                "        AND de.thread_id IS NOT NULL AND de.thread_id <> ''"
                "  LEFT JOIN obi ON de.inbound_text = obi.k"
                "  WHERE COALESCE(obt.ed, obi.ed) IS NOT NULL"
            )
        else:
            _OUTCOME_SQL = (
                "WITH obi AS ("
                f"  SELECT inbound_text AS k, AVG(edit_distance_pct) AS ed"
                f"  FROM feedback_pairs WHERE {_HONEST_FP}"
                "  GROUP BY inbound_text"
                ") "
                "SELECT COALESCE(de.sender_type, 'unlogged') AS st,"
                "       COALESCE(de.confidence, 'unknown') AS cf,"
                "       obi.ed AS ed"
                "  FROM draft_events de"
                "  JOIN obi ON de.inbound_text = obi.k"
            )
        # b192: sender_type NULL = "unlogged" (sender never recorded), distinct
        # from a classifier "unknown"; confidence keeps its "unknown" fold.
        try:
            from collections import defaultdict

            acc: dict[str, dict[str, list[float]]] = {
                "avg_edit_distance_by_sender_type": defaultdict(lambda: [0.0, 0]),
                "avg_edit_distance_by_confidence": defaultdict(lambda: [0.0, 0]),
            }
            matched = 0
            for r in conn.execute(_OUTCOME_SQL):  # noqa: S608
                matched += 1
                acc["avg_edit_distance_by_sender_type"][str(r["st"])][0] += r["ed"]
                acc["avg_edit_distance_by_sender_type"][str(r["st"])][1] += 1
                acc["avg_edit_distance_by_confidence"][str(r["cf"])][0] += r["ed"]
                acc["avg_edit_distance_by_confidence"][str(r["cf"])][1] += 1
            summary["outcome"]["matched"] = matched
            for key, buckets in acc.items():
                summary["outcome"][key] = {
                    k: {"avg_edit_distance": round(s / n, 3), "n": n}
                    for k, (s, n) in buckets.items()
                    if n
                }
        except sqlite3.OperationalError:
            pass

        return summary
    finally:
        conn.close()


def get_latest_ingest_status(database_url: str) -> dict:
    """Latest ingestion run's status, for the wizard's live progress poll.

    Maps the run-log's ``started`` to ``running``; returns ``idle`` when there's
    no run (or no table yet).
    """
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return {"status": "idle"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT status, started_at, completed_at, discovered_count, fetched_count,
                      stored_reply_pair_count, error_summary
               FROM ingest_runs ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    except sqlite3.OperationalError:
        return {"status": "idle"}
    finally:
        conn.close()
    if not row:
        return {"status": "idle"}
    return {
        "status": "running" if row["status"] == "started" else row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "discovered": row["discovered_count"],
        "fetched": row["fetched_count"],
        "reply_pairs": row["stored_reply_pair_count"],
        "error": row["error_summary"],
    }


def get_corpus_stats(database_url: str) -> dict:
    """Get corpus health statistics."""
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return {
            "total_documents": 0,
            "total_reply_pairs": 0,
            "total_feedback_pairs": 0,
            "real_draft_feedback_pairs": 0,
            "organic_feedback_pairs": 0,
            "reviewed_today": 0,
            "reviewed_this_week": 0,
            "avg_edit_distance": None,
            "embedding_pct": None,
            "outcome_metrics": {
                "accept_unchanged_pct": None,
                "low_edit_pct": None,
                "high_rating_pct": None,
                "median_edit_distance": None,
            },
        }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total_docs = _safe_count(conn, "documents")
        total_pairs = _safe_count(conn, "reply_pairs")
        total_feedback = _safe_count(conn, "feedback_pairs")

        # b185: a feedback_pair only carries a real draft-vs-sent edit signal
        # when it isn't an organic/no-draft backfill row (sent reply copied into
        # both columns with a hardcoded 0.0). The same predicate is applied to
        # every edit-distance metric below so none of them keep reporting the
        # false 0.0 / 100%-accepted picture that the 82k organic rows produce.
        _REAL_DRAFT_FEEDBACK = "edit_distance_pct IS NOT NULL AND COALESCE(organic, 0) = 0 AND generated_draft <> edited_reply"

        reviewed_today = 0
        reviewed_week = 0
        avg_edit_dist = None
        real_draft_feedback_count = 0
        organic_feedback_count = 0
        verbatim_accepted_count = 0
        try:
            reviewed_today = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
            reviewed_week = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE created_at >= DATE('now', '-7 days')").fetchone()[0]
            # Separate the corpus into "real draft-feedback" (genuine model
            # draft vs user-sent comparison) and "organic" (no-draft backfill)
            # so a caller never mistakes the 82k organic rows for quality signal.
            real_draft_feedback_count = conn.execute(
                f"SELECT COUNT(*) FROM feedback_pairs WHERE {_REAL_DRAFT_FEEDBACK}"  # noqa: S608
            ).fetchone()[0]
            organic_feedback_count = conn.execute(
                "SELECT COUNT(*) FROM feedback_pairs WHERE COALESCE(organic, 0) = 1"
            ).fetchone()[0]
            # Subset of organic: pairs where the agent's own draft was sent
            # unedited — a genuine drafter win (edit distance really 0). Kept
            # organic so it never inflates the edit-distance metrics, but counted
            # here so these zero-edit successes aren't invisible (b198). Isolated
            # try: it depends on feedback_note, which an older/minimal schema may
            # lack — a miss here must not abort the metrics that follow.
            try:
                verbatim_accepted_count = conn.execute(
                    "SELECT COUNT(*) FROM feedback_pairs "
                    "WHERE COALESCE(organic, 0) = 1 AND feedback_note LIKE 'verbatim-accepted%'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                verbatim_accepted_count = 0
            row = conn.execute(
                "SELECT AVG(edit_distance_pct) FROM "
                "(SELECT edit_distance_pct FROM feedback_pairs "
                f"WHERE {_REAL_DRAFT_FEEDBACK} ORDER BY id DESC LIMIT 50)"  # noqa: S608
            ).fetchone()
            if row and row[0] is not None:
                avg_edit_dist = round(row[0], 4)
        except sqlite3.OperationalError:
            pass

        embedding_pct = None
        try:
            if total_pairs > 0:
                with_emb = conn.execute("SELECT COUNT(*) FROM reply_pairs WHERE embedding IS NOT NULL").fetchone()[0]
                embedding_pct = round((with_emb / total_pairs) * 100, 1)
        except sqlite3.OperationalError:
            pass

        # Outcome metrics (proxy for real-world draft quality)
        outcome_metrics = {
            "accept_unchanged_pct": None,
            "low_edit_pct": None,
            "high_rating_pct": None,
            "median_edit_distance": None,
        }
        try:
            # b185: same honesty filter — these were dominated by the 82k
            # organic 0.0 rows, which made accept_unchanged_pct read ~100% and
            # median_edit_distance read 0.0 (a fake "every draft accepted
            # unchanged" signal). Restricting to real draft-vs-sent rows makes
            # them reflect actual draft quality; on an instance with no real
            # comparisons yet they correctly come back NULL rather than 100%.
            row = conn.execute(
                f"""
                SELECT
                    ROUND(100.0 * AVG(CASE WHEN edit_distance_pct <= 0.01 THEN 1.0 ELSE 0.0 END), 1) AS accept_unchanged_pct,
                    ROUND(100.0 * AVG(CASE WHEN edit_distance_pct <= 0.15 THEN 1.0 ELSE 0.0 END), 1) AS low_edit_pct,
                    ROUND(100.0 * AVG(CASE WHEN rating >= 4 THEN 1.0 ELSE 0.0 END), 1) AS high_rating_pct
                FROM feedback_pairs
                WHERE {_REAL_DRAFT_FEEDBACK}
                """  # noqa: S608
            ).fetchone()
            if row:
                outcome_metrics["accept_unchanged_pct"] = row[0]
                outcome_metrics["low_edit_pct"] = row[1]
                outcome_metrics["high_rating_pct"] = row[2]

            # Median edit distance from last 100 REAL draft-feedback rows.
            med_row = conn.execute(
                f"""
                SELECT edit_distance_pct
                FROM feedback_pairs
                WHERE {_REAL_DRAFT_FEEDBACK}
                ORDER BY id DESC
                LIMIT 100
                """  # noqa: S608
            ).fetchall()
            if med_row:
                vals = sorted(float(r[0]) for r in med_row)
                n = len(vals)
                if n % 2 == 1:
                    median_val = vals[n // 2]
                else:
                    median_val = (vals[(n // 2) - 1] + vals[n // 2]) / 2
                outcome_metrics["median_edit_distance"] = round(median_val, 4)
        except sqlite3.OperationalError:
            pass

        return {
            "total_documents": total_docs,
            "total_reply_pairs": total_pairs,
            "total_feedback_pairs": total_feedback,
            # b185: split the feedback corpus so the no-draft organic backfill
            # is never mistaken for draft-quality signal.
            "real_draft_feedback_pairs": real_draft_feedback_count,
            "organic_feedback_pairs": organic_feedback_count,
            # Subset of organic_feedback_pairs: agent drafts the user sent unedited.
            "verbatim_accepted_pairs": verbatim_accepted_count,
            "reviewed_today": reviewed_today,
            "reviewed_this_week": reviewed_week,
            # avg_edit_distance / outcome_metrics are now over real draft-vs-sent
            # rows only — NULL on an instance with no genuine comparisons yet.
            "avg_edit_distance": avg_edit_dist,
            "embedding_pct": embedding_pct,
            "outcome_metrics": outcome_metrics,
        }
    finally:
        conn.close()


def get_draft_vs_sent_stats(database_url: str, *, account: str | None = None, worst_n: int = 5, days: int = 60) -> dict:
    """How YouOS drafts compare to what the user *actually* sent.

    Built from the ground-truth outcomes ``outcome_capture`` records:

    * ``agent_pending_drafts.outcome`` (``sent`` / ``no_send``) → the send rate
      (did you reply to the queued draft) and the no-reply count — the
      over-/under-drafting signal.
    * the ``(inbound, youos_draft, your_sent)`` feedback pairs it writes (tagged
      with a known ``feedback_note``) → average edit distance, the
      high-divergence count, and the worst-N concrete examples (the "drafts that
      missed" list).

    Also returns a bounded threshold recommendation (see
    ``app.agent.threshold_tuner``) so the Stats panel can surface "raise the
    needs-reply threshold to X" with a one-click Apply. All counts are scoped to
    ``days`` so a months-old outcome doesn't anchor the picture.
    """
    from app.db.bootstrap import resolve_sqlite_path

    empty = {
        "available": False,
        "paired": 0,
        "no_send": 0,
        "sent_total": 0,
        "send_rate": None,
        "avg_edit_distance": None,
        "high_divergence": 0,
        "worst_examples": [],
        "recommendation": None,
    }

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return empty

    # The exact note outcome_capture stamps on its pairs — the only reliable way
    # to isolate real draft-vs-sent comparisons from the rest of feedback_pairs.
    note_like = "auto: real Gmail send outcome%"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Send rate + no-reply count from the decided queue outcomes.
        sent_total = no_send = 0
        try:
            acct_clause = " AND account = ?" if account else ""
            params: list = [f"-{int(days)} days"]
            if account:
                params.append(account)
            rows = conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM agent_pending_drafts "
                f"WHERE outcome IN ('sent','no_send') AND created_at >= datetime('now', ?){acct_clause} "  # noqa: S608
                "GROUP BY outcome",
                params,
            ).fetchall()
            counts = {str(r["outcome"]): int(r["n"]) for r in rows}
            sent_total = counts.get("sent", 0)
            no_send = counts.get("no_send", 0)
        except sqlite3.OperationalError:
            return empty

        decided = sent_total + no_send
        send_rate = round(sent_total / decided, 4) if decided else None

        # Pair metrics from the outcome-capture feedback pairs.
        paired = 0
        avg_ed = None
        high_div = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, ROUND(AVG(edit_distance_pct), 4) AS avg_ed, "
                "SUM(CASE WHEN edit_distance_pct >= 0.6 THEN 1 ELSE 0 END) AS high "
                "FROM feedback_pairs "
                "WHERE feedback_note LIKE ? AND created_at >= datetime('now', ?)",
                (note_like, f"-{int(days)} days"),
            ).fetchone()
            if row:
                paired = int(row["n"] or 0)
                avg_ed = row["avg_ed"]
                high_div = int(row["high"] or 0)
        except sqlite3.OperationalError:
            pass

        # Worst-N concrete examples: where the draft diverged most from the send.
        worst: list[dict] = []
        try:
            ex_rows = conn.execute(
                "SELECT inbound_text, generated_draft, edited_reply, edit_distance_pct, sender_type "
                "FROM feedback_pairs "
                "WHERE feedback_note LIKE ? AND created_at >= datetime('now', ?) "
                "ORDER BY edit_distance_pct DESC LIMIT ?",
                (note_like, f"-{int(days)} days", int(worst_n)),
            ).fetchall()

            def _snip(s: str | None, n: int = 240) -> str:
                t = (s or "").strip().replace("\r\n", "\n")
                return t if len(t) <= n else t[:n].rstrip() + "…"

            for r in ex_rows:
                worst.append({
                    "inbound": _snip(r["inbound_text"], 200),
                    "draft": _snip(r["generated_draft"]),
                    "sent": _snip(r["edited_reply"]),
                    "edit_distance": r["edit_distance_pct"],
                    "sender_type": r["sender_type"],
                })
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    # Threshold recommendation on the SAME evidence the nightly tuner uses
    # (its default recency window + only drafts created since the last
    # threshold change) — not the panel's display window, so the Apply button
    # always previews exactly what the nightly would do.
    recommendation = None
    try:
        from app.agent.scheduler import get_agent_config
        from app.agent.threshold_tuner import recommend_from_database
        from app.core.config import load_config

        current = get_agent_config()["threshold"]
        auto_on = bool(get_agent_config().get("auto_tune_threshold", True))
        agent_cfg = (load_config() or {}).get("agent")
        since = None
        if isinstance(agent_cfg, dict):
            since = str(agent_cfg.get("threshold_changed_at") or "").strip() or None
        rec = recommend_from_database(database_url, current=current, account=account, since=since)
        recommendation = {**rec.to_dict(), "auto_tune_enabled": auto_on}
    except Exception:
        recommendation = None

    return {
        "available": decided > 0 or paired > 0,
        "paired": paired,
        "no_send": no_send,
        "sent_total": sent_total,
        "send_rate": send_rate,
        "avg_edit_distance": avg_ed,
        "high_divergence": high_div,
        "worst_examples": worst,
        "recommendation": recommendation,
    }


def get_model_status(configs_dir: Path) -> dict:
    """Get model and adapter status."""
    adapter_exists = (ADAPTER_PATH / "adapters.safetensors").exists()
    lora_trained_at = None
    if adapter_exists:
        try:
            from datetime import datetime, timezone

            mtime = os.path.getmtime(ADAPTER_PATH / "adapters.safetensors")
            lora_trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except Exception:
            pass

    # Capability-aware (not just "does the adapter file exist"): without mlx_lm
    # the local model can't run at all, so drafting falls to the cloud — claiming
    # "lora" there is the false-confidence we're trying to avoid.
    import shutil

    local_available = shutil.which("mlx_lm") is not None
    if not local_available:
        gen_model = "claude"
    else:
        # b174: derive the label from the configured base model so it tracks a
        # model migration (Qwen2.5-1.5B -> Qwen3-4B) instead of lying.
        from app.core.config import model_label

        gen_model = model_label(with_adapter=adapter_exists)

    # Benchmark trend
    benchmark_trend: list[dict] = []
    if AUTORESEARCH_JSONL.exists():
        try:
            lines = AUTORESEARCH_JSONL.read_text(encoding="utf-8").strip().splitlines()
            for line in lines[-5:]:
                entry = json.loads(line)
                benchmark_trend.append(
                    {
                        "date": entry.get("run_at", ""),
                        "composite_score": entry.get("composite_score"),
                        "improvements_kept": entry.get("config_snapshot", {}).get("improvements_kept"),
                    }
                )
        except Exception:
            benchmark_trend = []
    if not benchmark_trend and AUTORESEARCH_LOG.exists():
        try:
            import re

            log_text = AUTORESEARCH_LOG.read_text(encoding="utf-8")
            entries = re.findall(r"## Run (\d{4}-\d{2}-\d{2}[^\n]*)\n(.*?)(?=\n## Run |\Z)", log_text, re.DOTALL)
            for date_str, body in entries[-5:]:
                score_match = re.search(r"composite[_\s]?score[:\s]*([\d.]+)", body, re.IGNORECASE)
                kept_match = re.search(r"improvements?\s*kept[:\s]*(\d+)", body, re.IGNORECASE)
                benchmark_trend.append(
                    {
                        "date": date_str.strip(),
                        "composite_score": score_match.group(1) if score_match else None,
                        "improvements_kept": int(kept_match.group(1)) if kept_match else None,
                    }
                )
        except Exception:
            pass

    return {
        "generation_model": gen_model,
        "local_available": local_available,
        "lora_adapter_exists": adapter_exists,
        "lora_trained_at": lora_trained_at,
        "last_finetune_run": lora_trained_at,
        "benchmark_trend": benchmark_trend,
    }


def _recent_model_counts(conn: sqlite3.Connection, limit: int = 50) -> dict[str, int]:
    """``{model_used: count}`` over the most recent *limit* drafts.

    Recent-only because an adapter trained today shouldn't be judged by drafts
    produced weeks ago on the base model.
    """
    try:
        rows = conn.execute(
            "SELECT COALESCE(model_used, 'unknown') AS k, COUNT(*) AS n FROM "
            "(SELECT model_used FROM draft_events ORDER BY id DESC LIMIT ?) GROUP BY k",
            (limit,),
        ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}
    except sqlite3.OperationalError:
        return {}


def _classify_model_used(model_used: str) -> str:
    """Bucket a raw ``model_used`` label into lora | base | cloud | other."""
    m = (model_used or "").lower()
    if "lora" in m:  # qwen2.5-1.5b-lora and per-persona qwen2.5-1.5b-lora-<type>
        return "lora"
    if m.endswith("-base"):
        return "base"
    if m == "claude" or m.startswith("ollama"):
        return "cloud"
    return "other"


def _classify_drafting(by_model: dict[str, int], adapter_trained: bool, local_available: bool) -> tuple[str, str, str, bool]:
    """Decide what's *actually* drafting → (state, label, detail, healthy).

    Prefers reality (what recent drafts used); falls back to capability (adapter
    on disk + mlx_lm) when there are no drafts yet. ``healthy`` is False whenever
    the LoRA isn't the thing drafting — that's the signal a surface should warn on.
    """
    totals = {"lora": 0, "base": 0, "cloud": 0, "other": 0}
    for model, n in by_model.items():
        totals[_classify_model_used(model)] += n
    n = sum(totals.values())

    if n:
        lora, base, cloud = totals["lora"], totals["base"], totals["cloud"]
        if lora and not base and not cloud:
            return ("personalized", "Your fine-tuned model (LoRA)",
                    f"All of the last {n} drafts used your trained LoRA adapter.", True)
        if lora and (base or cloud):
            return ("mixed", "Mostly your LoRA — some fell back",
                    f"{base} base-model and {cloud} cloud-fallback draft(s) in the last {n}. "
                    "Check that mlx_lm is installed and the adapter is trained.", False)
        if base and not lora:
            return ("base", "Base model — your LoRA is NOT in use",
                    "Recent drafts ran on the base model. Train an adapter with `youos finetune` "
                    "(or via the wizard) so drafts sound like you.", False)
        if cloud and not lora:
            return ("cloud", "Cloud fallback — not your local LoRA",
                    "Recent drafts used the cloud/Ollama fallback, not your local model. "
                    "Is mlx_lm installed and an adapter trained?", False)
        return ("unknown", "Unknown", f"The last {n} drafts have no recognizable model label.", False)

    # No drafts yet — infer from capability.
    if not local_available:
        return ("cloud", "Cloud fallback (local model unavailable)",
                'mlx_lm is not installed, so the local model can\'t run — drafts will use the cloud '
                'fallback. Install it: pip install -e ".[mlx]".', False)
    if adapter_trained:
        return ("personalized", "Your fine-tuned model (LoRA) — ready",
                "Adapter trained and the local model is available; drafts will use your LoRA. No drafts yet.", True)
    return ("base", "Base model — no LoRA trained yet",
            "No adapter trained yet — drafts will use the base model (not personalized) until you fine-tune.", False)


_READINESS_MESSAGES = {
    "not_started": "Your voice model hasn't been trained yet — drafts use the base model and won't sound like you. "
                   "Start fine-tuning from the setup wizard or Settings.",
    "training": "Training your voice model on your sent mail… drafts use the base model until it's ready.",
    "benchmarking": "Benchmarking your newly trained voice model… almost there.",
    "benchmark_pending": "Your voice model is trained but not yet benchmarked — it'll be validated by tonight's run "
                         "(or run `youos eval --golden`). Drafts may not reflect the validated model yet.",
    "ready": "Your voice model is trained and benchmarked — drafts now sound like you.",
}


def get_model_readiness(database_url: str, *, finetune_running: bool = False) -> dict:
    """Is the personalized model ready to rely on — i.e. trained AND benchmarked?

    Used to ask a user to wait before drafting on a half-baked model. Phases:
    ``not_started`` → ``training`` → ``benchmarking`` → ``benchmark_pending`` →
    ``ready``. "Benchmarked" means a golden eval ran at or after the adapter was
    trained (the wizard's fine-tune now chains the eval, so this is reachable
    without waiting for the nightly). ``finetune_running`` is supplied by the
    route layer since the in-progress handle lives there.
    """
    adapter_file = _resolve_adapter_path() / "adapters.safetensors"
    adapter_trained = adapter_file.exists()

    benchmarked = False
    if adapter_trained:
        golden = _get_var_path("golden_results.json")
        try:
            benchmarked = golden.exists() and golden.stat().st_mtime >= adapter_file.stat().st_mtime
        except OSError:
            benchmarked = False

    if finetune_running:
        phase = "benchmarking" if adapter_trained else "training"
    elif not adapter_trained:
        phase = "not_started"
    elif not benchmarked:
        phase = "benchmark_pending"
    else:
        phase = "ready"

    return {
        "phase": phase,
        "ready": phase == "ready",
        "message": _READINESS_MESSAGES[phase],
        "adapter_trained": adapter_trained,
        "benchmarked": benchmarked,
        "running": finetune_running,
    }


def get_drafting_model_status(database_url: str) -> dict:
    """What model is *actually* drafting — to prevent the silent-failure where a
    user believes drafts are personalized while they run on the base model or
    fall back to the cloud.

    Reality first (recent ``draft_events.model_used``), capability as the fallback
    (adapter on disk + ``mlx_lm`` available).
    """
    import shutil

    from app.db.bootstrap import resolve_sqlite_path

    adapter_trained = (_resolve_adapter_path() / "adapters.safetensors").exists()
    local_available = shutil.which("mlx_lm") is not None

    by_model: dict[str, int] = {}
    db_path = resolve_sqlite_path(database_url)
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            by_model = _recent_model_counts(conn)
        finally:
            conn.close()

    state, label, detail, healthy = _classify_drafting(by_model, adapter_trained, local_available)
    return {
        "state": state,
        "label": label,
        "detail": detail,
        "healthy": healthy,
        "adapter_trained": adapter_trained,
        "local_available": local_available,
        "recent_by_model": by_model,
    }


def get_pipeline_status(project_root: Path) -> dict | None:
    """Read var/pipeline_last_run.json if it exists."""
    log_path = project_root / "var" / "pipeline_last_run.json"
    if not log_path.exists():
        return None
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def get_persona_adapter_status() -> dict[str, dict]:
    """Return ``{persona: {trained: bool, mtime: iso|None, pairs_used: int|None}}``.

    One entry per sender_type cohort (internal / external_client / personal /
    automated) — "unknown" excluded since Phase 2 doesn't train an adapter
    for it. Used by the stats endpoint and the doctor check so the user
    can see which personas have a trained adapter without poking the
    filesystem.

    ``mtime`` and ``pairs_used`` come from the adapter's `meta.json`
    (written by `scripts/finetune_lora.py`) when present; otherwise mtime
    falls back to the safetensors file's mtime and pairs_used is None.
    """
    from app.core.settings import get_persona_adapter_path

    out: dict[str, dict] = {}
    for persona in ("internal", "external_client", "personal", "automated"):
        adapter_dir = get_persona_adapter_path(persona)
        sfile = adapter_dir / "adapters.safetensors"
        meta_path = adapter_dir / "meta.json"
        entry: dict = {"trained": sfile.exists(), "mtime": None, "pairs_used": None}
        if not sfile.exists():
            out[persona] = entry
            continue
        # Prefer the train metadata json; fall back to fs mtime.
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                entry["mtime"] = meta.get("trained_at")
                entry["pairs_used"] = meta.get("pairs_used")
            except Exception:
                pass
        if entry["mtime"] is None:
            try:
                from datetime import datetime, timezone

                entry["mtime"] = datetime.fromtimestamp(
                    sfile.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        out[persona] = entry
    return out


def get_embedding_coverage(database_url: str) -> dict[str, float]:
    """Fraction of rows with non-empty embeddings, per indexed table.

    Returns ``{"chunks": 0.42, "reply_pairs": 0.31}`` when both tables exist
    and have rows. Missing / empty tables are silently omitted — so a fresh
    instance returns ``{}`` rather than zeros that would imply "indexed,
    but every row failed".

    Used to answer "is semantic retrieval actually firing on this corpus?"
    from the stats endpoint and the nightly pipeline log — without this,
    the only way to tell was to add ad-hoc logging inside the retrieval
    reranker. Mirrors ``app.retrieval.service._embedding_coverage`` which
    is per-table and connection-scoped; this is the public, multi-table,
    db-path-scoped version intended for stats callers.
    """
    from app.db.bootstrap import resolve_sqlite_path

    db_path = resolve_sqlite_path(database_url)
    if not db_path.exists():
        return {}

    coverage: dict[str, float] = {}
    conn = sqlite3.connect(db_path)
    try:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in ("chunks", "reply_pairs"):
            if table not in existing_tables:
                continue
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "embedding" not in cols:
                continue
            total = _safe_count(conn, table)
            if total == 0:
                continue
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE embedding IS NOT NULL AND LENGTH(embedding) > 0"  # noqa: S608
            ).fetchone()
            embedded = row[0] if row else 0
            coverage[table] = round(embedded / total, 4)
    except sqlite3.OperationalError:
        return coverage
    finally:
        conn.close()
    return coverage
