"""Extract auto-feedback pairs from your sent emails.

Compares YouOS-generated drafts against your actual replies to create
implicit training signal for fine-tuning.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from app.core.diff import similarity_ratio
from app.core.settings import get_settings
from app.db.bootstrap import resolve_sqlite_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract auto-feedback from sent email reply pairs")
    p.add_argument("--days", type=int, default=1, help="Look back N days (default: 1)")
    p.add_argument("--dry-run", action="store_true", help="Show pairs without saving")
    p.add_argument("--db", type=str, default=None, help="Database path override")
    p.add_argument("--threshold", type=float, default=0.80, help="Similarity threshold (default: 0.80)")
    p.add_argument(
        "--auto-threshold",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-calibrate threshold based on corpus size (default: True)",
    )
    p.add_argument(
        "--organic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Capture organic pairs (sent emails with no YouOS draft, default: True)",
    )
    return p.parse_args()


def _get_db_path(db_override: str | None) -> Path:
    if db_override:
        return Path(db_override)
    settings = get_settings()
    return resolve_sqlite_path(settings.database_url)


def auto_calibrate_threshold(conn: sqlite3.Connection) -> tuple[float, int]:
    """Determine similarity threshold based on corpus size.

    Returns (threshold, pair_count).
    """
    count = conn.execute("SELECT COUNT(*) FROM reply_pairs").fetchone()[0]
    if count < 100:
        return 0.65, count
    if count < 500:
        return 0.72, count
    return 0.80, count


def _prior_draft_for_inbound(conn: sqlite3.Connection, inbound_text: str) -> str | None:
    """The most recent agent-generated draft for this inbound, if one exists.

    b185: the agent logs every draft it produces to ``draft_events`` (see
    ``generation.service._log_draft_event``), keyed by the SAME ``inbound_text``
    that ``reply_pairs`` carries — the only stable key shared by the two tables.
    When organic sent-mail ingestion later finds the user's actual reply for an
    inbound the agent had drafted, that (draft, sent_reply) IS a genuine
    draft-vs-sent comparison and must be captured with a REAL edit distance
    rather than discarded as "no YouOS draft". Returns None when the table is
    absent (older instance) or no prior draft was logged for this inbound.
    """
    if not inbound_text:
        return None
    try:
        row = conn.execute(
            "SELECT generated_draft FROM draft_events "
            "WHERE inbound_text = ? AND generated_draft IS NOT NULL AND generated_draft <> '' "
            "ORDER BY id DESC LIMIT 1",
            (inbound_text,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    draft = row["generated_draft"] if isinstance(row, sqlite3.Row) else row[0]
    return draft or None


def _inbound_message_ids(metadata_json: str | None) -> list[str]:
    """The inbound message ids a reply answered, from ``reply_pairs.metadata_json``.

    Organic sent-mail ingestion records ``{"inbound_message_ids": ["..."], ...}``
    — the SPECIFIC inbound turn(s) the user replied to. This is what makes a
    turn-precise join possible (thread_id alone is too coarse — a thread holds
    many turns and the agent drafted just one)."""
    if not metadata_json:
        return []
    try:
        ids = json.loads(metadata_json).get("inbound_message_ids")
    except (ValueError, TypeError, AttributeError):
        return []
    return [str(i) for i in ids if i] if isinstance(ids, list) else []


def _agent_drafts_by_message_id(conn: sqlite3.Connection) -> dict[str, str]:
    """Map of inbound ``message_id`` → the agent's actual stored draft for it
    (b269 turn-precise join key).

    ``agent_pending_drafts.message_id`` is the inbound the agent drafted a reply
    to; matching it against a reply's ``inbound_message_ids`` yields a COHERENT
    ``(inbound, draft, sent-reply)`` triple — the draft and the reply are two
    answers to the *same* message, so no timestamp guard is needed. Prefers the
    user's in-app amendment when present, else the draft. Built once per run
    (cheap) so the capture/relabel loops are plain dict lookups. Empty if the
    queue table is absent (fixture/legacy schema)."""
    out: dict[str, str] = {}
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    except sqlite3.OperationalError:
        return out
    if not cols:
        return out
    amended = "amended_draft" if "amended_draft" in cols else "NULL"
    try:
        # ASC so a later draft for the same inbound overwrites an earlier one.
        rows = conn.execute(
            f"SELECT message_id, draft, {amended} AS amended FROM agent_pending_drafts "
            "WHERE message_id IS NOT NULL AND draft IS NOT NULL AND draft <> '' "
            "ORDER BY created_at ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        mid = r["message_id"] if isinstance(r, sqlite3.Row) else r[0]
        if not mid:
            continue
        amend = (r["amended"] if isinstance(r, sqlite3.Row) else r[2]) or ""
        draft = (r["draft"] if isinstance(r, sqlite3.Row) else r[1]) or ""
        chosen = amend.strip() or draft
        if chosen:
            out[str(mid)] = chosen
    return out


def _agent_draft_for_reply(
    metadata_json: str | None, drafts_by_mid: dict[str, str]
) -> str | None:
    """The agent's draft for the inbound this reply answered, if any (b269)."""
    for mid in _inbound_message_ids(metadata_json):
        draft = drafts_by_mid.get(mid)
        if draft:
            return draft
    return None


def _capture_organic_pairs(conn: sqlite3.Connection, *, dry_run: bool = False) -> int:
    """Capture sent replies with inbound context.

    For each sent reply with no row yet, we look up whether the agent had
    *already* drafted a reply for that inbound (``draft_events``, keyed on
    ``inbound_text``):

    * **prior draft exists** → a GENUINE draft-vs-sent feedback pair
      ``(prior_draft, sent_reply)`` with a REAL ``edit_distance_pct`` from
      ``similarity_ratio`` and ``organic=0`` — this is the learnable signal the
      loop was missing (b185).
    * **no prior draft** → an organic backfill pair (sent reply copied into
      both columns, ``organic=1``). It is recorded for corpus/training purposes
      but is EXCLUDED from the draft-quality learning join (it has no model
      draft to compare), so it never fabricates a false 0.0 "perfect draft"
      signal.
    """
    # Ensure row_factory is set for dict-style access
    conn.row_factory = sqlite3.Row

    # Ensure organic column exists
    cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback_pairs)").fetchall()}
    if "organic" not in cols:
        conn.execute("ALTER TABLE feedback_pairs ADD COLUMN organic BOOLEAN DEFAULT 0")
    # b235: the quality filter below references it; self-heal like `organic`.
    rp_cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_pairs)").fetchall()}
    if "quality_score" not in rp_cols:
        conn.execute("ALTER TABLE reply_pairs ADD COLUMN quality_score REAL DEFAULT 1.0")

    import re as _re

    _ACK_PATTERN = _re.compile(
        r"^\s*(ok|okay|k|sure|thanks|thank you|ty|thx|noted|got it|will do|sounds good|great|perfect|"
        r"received|ack|acknowledged|\+1|roger|copy that|understood)\s*[.!]?\s*$",
        _re.IGNORECASE,
    )

    # b269: minimal/legacy reply_pairs schemas (and most fixtures) may lack
    # metadata_json — reference it only when present.
    md_expr = "rp.metadata_json" if "metadata_json" in rp_cols else "NULL"
    rows = conn.execute(
        f"""
        SELECT rp.id, rp.inbound_text, rp.reply_text, {md_expr} AS metadata_json
        FROM reply_pairs rp
        WHERE rp.auto_feedback_processed = 0
          AND COALESCE(rp.quality_score, 1.0) > 0
          AND rp.id NOT IN (SELECT DISTINCT reply_pair_id FROM feedback_pairs WHERE reply_pair_id IS NOT NULL)
          AND LENGTH(rp.reply_text) >= 10
        """
    ).fetchall()

    # b269: turn-precise map of inbound message_id → the agent's actual draft.
    drafts_by_mid = _agent_drafts_by_message_id(conn)

    count = 0
    for row in rows:
        reply = (row["reply_text"] or "").strip()
        inbound = row["inbound_text"]
        # E11: skip pure acknowledgments
        if _ACK_PATTERN.match(reply):
            continue

        # b269: prefer the turn-precise message_id join (the agent's actual stored
        # draft for the exact inbound this reply answered) over the brittle
        # inbound_text equality — the latter matched ~1% of rows. Fall back to the
        # inbound_text lookup for replies with no message-id metadata.
        prior_draft = _agent_draft_for_reply(
            row["metadata_json"], drafts_by_mid
        ) or _prior_draft_for_inbound(conn, inbound)
        is_real = bool(prior_draft) and prior_draft.strip() != reply

        if dry_run:
            tag = "real" if is_real else "organic"
            print(f"  [{tag}] pair {row['id']}: {(inbound or '')[:60]}...")
        elif is_real:
            # Genuine draft-vs-sent pair: real distance, non-organic, counted.
            real_ed = round(1.0 - similarity_ratio(prior_draft, reply), 4)
            conn.execute(
                """
                INSERT INTO feedback_pairs
                    (reply_pair_id, inbound_text, generated_draft, edited_reply, feedback_note, edit_distance_pct, rating, used_in_finetune, organic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (row["id"], inbound, prior_draft, reply, "real draft-vs-sent (prior agent draft)", real_ed, 4, 0),
            )
            conn.execute(
                "UPDATE reply_pairs SET auto_feedback_processed = 1 WHERE id = ?",
                (row["id"],),
            )
        else:
            # Two sub-cases that the earlier `is_real` test collapses together —
            # both stay organic=1 (EXCLUDED from the edit-distance join, so the
            # mean stays honest), but they are NOT the same thing and were
            # previously indistinguishable:
            #   * verbatim win: the agent DID draft this and the user sent it
            #     unedited — a genuine drafter win (edit distance really is 0),
            #     which silently vanished into "no draft" backfill before.
            #   * no draft: there was nothing to compare against.
            # Record the win distinguishably (note + rating) so the drafter's
            # zero-edit successes are observable (see stats.verbatim_accepted_pairs)
            # without polluting any learning/metric (organic=1 keeps it out).
            verbatim_win = bool(prior_draft) and prior_draft.strip() == reply
            if verbatim_win:
                note, rating, gen = "verbatim-accepted (agent draft sent unedited)", 5, prior_draft
            else:
                note, rating, gen = "organic pair — no YouOS draft", 3, reply
            # b160: persist reply_pair_id so the NOT IN guard above actually dedups
            # (it was NULL, making the guard inert → every organic pair re-inserted
            # forever), and mark the source processed so it isn't re-captured.
            conn.execute(
                """
                INSERT INTO feedback_pairs
                    (reply_pair_id, inbound_text, generated_draft, edited_reply, feedback_note, edit_distance_pct, rating, used_in_finetune, organic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], inbound, gen, reply, note, 0.0, rating, 0, 1),
            )
            conn.execute(
                "UPDATE reply_pairs SET auto_feedback_processed = 1 WHERE id = ?",
                (row["id"],),
            )
        count += 1
    return count


def _relabel_mislabeled_organic_pairs(conn: sqlite3.Connection, *, dry_run: bool = False) -> int:
    """Rescue feedback_pairs recorded organic that DO have a real agent draft for
    the same inbound (b269 message_id join).

    A reply ingested before the agent drafted its inbound is captured organic (no
    draft existed yet). When the agent later drafts that same inbound (it triages
    after the user already replied — common, since the user replies on mobile),
    that ``(draft, sent-reply)`` is a genuine edit pair. This pass finds such rows,
    recomputes the REAL edit distance, and flips them to ``organic=0`` so they
    re-enter learning. Mostly a forward safety net — pairs the capture pass already
    matched are already ``organic=0``, so this is typically near-zero. Idempotent;
    only touches ``organic=1`` rows whose draft genuinely differs from the sent
    reply. Minimal/fixture schemas (no metadata_json / no queue) cleanly no-op."""
    conn.row_factory = sqlite3.Row
    rp_cols = {row[1] for row in conn.execute("PRAGMA table_info(reply_pairs)").fetchall()}
    if "metadata_json" not in rp_cols:
        return 0
    drafts_by_mid = _agent_drafts_by_message_id(conn)
    if not drafts_by_mid:
        return 0
    rows = conn.execute(
        """
        SELECT fp.id, fp.edited_reply, rp.metadata_json
        FROM feedback_pairs fp
        JOIN reply_pairs rp ON rp.id = fp.reply_pair_id
        WHERE fp.organic = 1 AND rp.metadata_json IS NOT NULL
        """
    ).fetchall()
    relabeled = 0
    for r in rows:
        reply = (r["edited_reply"] or "").strip()
        if not reply:
            continue
        draft = _agent_draft_for_reply(r["metadata_json"], drafts_by_mid)
        if not draft or draft.strip() == reply:
            continue  # no real draft, or verbatim — correctly organic, leave it
        real_ed = round(1.0 - similarity_ratio(draft, reply), 4)
        if not dry_run:
            conn.execute(
                "UPDATE feedback_pairs SET organic = 0, generated_draft = ?, "
                "edit_distance_pct = ?, feedback_note = ?, used_in_finetune = 0 "
                "WHERE id = ?",
                (draft, real_ed, "relabeled real draft-vs-sent (b269 message-id join)", r["id"]),
            )
        relabeled += 1
    return relabeled


def extract_auto_feedback(
    *,
    days: int = 1,
    dry_run: bool = False,
    db_path: Path | None = None,
    threshold: float = 0.80,
    auto_threshold: bool = True,
    database_url: str | None = None,
    configs_dir: Path | None = None,
    organic: bool = True,
) -> dict:
    """Capture draft-vs-sent feedback pairs. Returns summary dict.

    b269: this no longer regenerates a draft per reply pair via the local LLM
    (~144s each, ~88% of the nightly). The agent's *actual* draft is recovered by
    a turn-precise join on the inbound ``message_id`` (``agent_pending_drafts`` ↔
    ``reply_pairs.inbound_message_ids``) — both the genuine learning signal and a
    backfill that relabels pairs the old inbound_text join had mislabeled organic.
    ``threshold``/``database_url``/``configs_dir`` are kept
    for signature compatibility; the corpus size is still logged."""
    if db_path is None:
        db_path = _get_db_path(None)

    if database_url is None:
        database_url = f"sqlite:///{db_path}"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if auto_threshold:
            _, corpus_count = auto_calibrate_threshold(conn)
            print(f"Corpus: {corpus_count} reply pairs")
        # Check if auto_feedback_processed column exists
        cols = [row[1] for row in conn.execute("PRAGMA table_info(reply_pairs)").fetchall()]
        if "quality_score" not in cols:
            # b235: quality filter references it; older fixture/instance DBs
            # may predate the bootstrap migration.
            conn.execute("ALTER TABLE reply_pairs ADD COLUMN quality_score REAL DEFAULT 1.0")
            cols.append("quality_score")
        if "auto_feedback_processed" not in cols:
            print("Error: auto_feedback_processed column missing. Run bootstrap_db.py first.")
            return {"captured": 0, "total": 0, "skipped": 0, "errors": 0, "organic": 0, "relabeled": 0}
        # Self-heal the organic flag up front so the real-pair before/after counts
        # below work on a DB that predates it (also done in _capture_organic_pairs).
        fp_cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback_pairs)").fetchall()}
        if "organic" not in fp_cols:
            conn.execute("ALTER TABLE feedback_pairs ADD COLUMN organic BOOLEAN DEFAULT 0")

        total = conn.execute(
            "SELECT COUNT(*) FROM reply_pairs "
            "WHERE auto_feedback_processed = 0 AND COALESCE(quality_score, 1.0) > 0"
        ).fetchone()[0]
        print(f"Found {total} unprocessed reply pairs")

        # Single capture pass (thread_id-aware): emits real pairs (organic=0) when
        # the agent actually drafted the thread, organic backfill otherwise. No
        # LLM regeneration.
        real_before = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE organic = 0").fetchone()[0]
        captured = 0
        if organic:
            captured = _capture_organic_pairs(conn, dry_run=dry_run)
        real_after = conn.execute("SELECT COUNT(*) FROM feedback_pairs WHERE organic = 0").fetchone()[0]
        real_captured = real_after - real_before

        # Backfill: rescue pairs the old inbound_text join left mislabeled organic.
        relabeled = _relabel_mislabeled_organic_pairs(conn, dry_run=dry_run)

        if not dry_run:
            conn.commit()

        action = "Would capture" if dry_run else "Captured"
        print(
            f"{action} {captured} pairs ({real_captured} real draft-vs-sent, "
            f"{captured - real_captured} organic); relabeled {relabeled} mislabeled-organic"
        )
        return {
            "captured": captured,
            "real": real_captured,
            "organic": captured - real_captured,
            "relabeled": relabeled,
            "total": total,
            "skipped": 0,
            "errors": 0,
        }
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db) if args.db else None
    extract_auto_feedback(
        days=args.days,
        dry_run=args.dry_run,
        db_path=db_path,
        threshold=args.threshold,
        auto_threshold=args.auto_threshold,
        organic=args.organic,
    )


if __name__ == "__main__":
    main()
