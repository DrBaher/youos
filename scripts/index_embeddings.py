#!/usr/bin/env python3
"""Index embeddings for chunks and reply_pairs.

Processes rows with a NULL embedding OR a stale embedding (one produced by a
different embedding model than the currently-configured one). Stores results in
the DB. Interruptible and resumable — skips rows already embedded by the current
model.

Self-heal (b177): when the embedding model changes, rows tagged with the old
``embedding_model_id`` are re-embedded automatically on the next run. Legacy
rows with ``embedding_model_id IS NULL`` predate the tag and are treated as
matching the current model (no forced re-embed on upgrade) — they only get
(re)embedded if their ``embedding`` itself is NULL.

Usage:
    python3 scripts/index_embeddings.py              # index NULL + stale rows
    python3 scripts/index_embeddings.py --limit 100  # index only N rows
    python3 scripts/index_embeddings.py --table reply_pairs  # only reply pairs
    python3 scripts/index_embeddings.py --reindex    # force re-embed ALL rows
    python3 scripts/index_embeddings.py --dry-run    # show count of pending rows
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.embeddings import get_embedding, get_embedding_model_id, serialize_embedding
from app.core.settings import get_settings
from app.db.bootstrap import resolve_sqlite_path

BATCH_SIZE = 50


def _ensure_embedding_columns(conn: sqlite3.Connection) -> None:
    """Add embedding columns to tables that exist yet.

    Existence-guarded ALTER: on a fresh instance pre-ingest the `chunks`
    and `reply_pairs` tables don't exist, so an unconditional
    ``ALTER TABLE chunks ADD COLUMN`` would raise OperationalError and the
    indexer step in the nightly would WARN-out. We just no-op for tables
    that aren't there yet — ingestion creates them, and the next indexer
    run picks them up.

    ``embedding_model_id`` is stored alongside ``embedding`` so a future
    model swap can identify (and re-embed) rows that were written with a
    different embedding model. NULL on legacy rows means "trust as-is",
    so existing instances aren't forced to re-embed on upgrade.
    """
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in ("chunks", "reply_pairs"):
        if table not in existing_tables:
            # Table will be created by ingestion; nothing to migrate yet.
            continue
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "embedding" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding BLOB")
            print(f"  Migrated: added embedding column to {table}")
        if "embedding_model_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding_model_id TEXT")
            print(f"  Migrated: added embedding_model_id column to {table}")
    conn.commit()


def _pending_where(model_id: str, *, reindex: bool) -> tuple[str, tuple]:
    """SQL WHERE clause (and params) selecting rows that need (re)embedding.

    - ``--reindex``: every row (force a full rebuild in the current space).
    - default: rows with no embedding at all, OR rows tagged with a *different*
      embedding model than the current one (stale — vectors in another space).
      Legacy rows (``embedding_model_id IS NULL``) are trusted as current and
      only picked up when their ``embedding`` is NULL.
    """
    if reindex:
        return "1=1", ()
    return (
        "embedding IS NULL "
        "OR (embedding_model_id IS NOT NULL AND embedding_model_id != ?)",
        (model_id,),
    )


def _count_pending(conn: sqlite3.Connection, table: str, model_id: str, *, reindex: bool) -> int:
    where, params = _pending_where(model_id, reindex=reindex)
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()  # noqa: S608
    return row[0] if row else 0


def _get_text_for_row(row: sqlite3.Row, table: str) -> str:
    if table == "chunks":
        return row["content"] or ""
    else:  # reply_pairs
        inbound = row["inbound_text"] or ""
        reply = row["reply_text"] or ""
        return f"{inbound}\n{reply}"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _index_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    reindex: bool = False,
) -> int:
    """Index pending rows in a single table. Returns number of rows processed.

    "Pending" = NULL embedding, or an embedding tagged with a different model
    than the current one (stale), or — with ``reindex`` — every row.
    """
    # Pre-first-ingest the chunks/reply_pairs tables don't exist yet.
    # `_ensure_embedding_columns` already no-ops in that case; mirror it
    # here so the indexer completes cleanly on a fresh instance instead
    # of erroring out the nightly's embedding step.
    if not _table_exists(conn, table):
        print(f"  {table}: table not created yet (pre-ingest)")
        return 0

    # Snapshot the model id once per run rather than re-resolving per row —
    # consistent with what _load_model() will use, and cheap. Needed for the
    # staleness query too, so resolve it before counting.
    model_id = get_embedding_model_id()

    total_pending = _count_pending(conn, table, model_id, reindex=reindex)
    if dry_run:
        label = "rows (forced reindex)" if reindex else "rows pending (NULL or stale)"
        print(f"  {table}: {total_pending} {label} [model={model_id}]")
        return 0

    if total_pending == 0:
        print(f"  {table}: all rows already embedded with {model_id}")
        return 0

    target = min(total_pending, limit) if limit else total_pending
    print(f"  {table}: {total_pending} pending rows, will process {target} [model={model_id}]")

    where, where_params = _pending_where(model_id, reindex=reindex)

    # Fail loud BEFORE touching any data if the embedding model can't load:
    # warm it once up front. A systemic load failure (wrong model id, missing
    # weights, MLX/Metal context unavailable) must abort the run rather than let
    # the per-row loop below mass-overwrite valid vectors with empty-blob
    # markers (the b177 data-loss footgun). One cheap probe embed surfaces it.
    try:
        _ = get_embedding("warmup")
    except Exception as exc:
        raise RuntimeError(
            f"Embedding model {model_id!r} failed to load/run; aborting before "
            f"writing any rows to avoid corrupting the existing index: {exc}"
        ) from exc

    # Id cursor for monotonic pagination. Necessary for ``--reindex`` (WHERE is
    # always-true, so re-querying without a cursor would keep returning the same
    # first batch forever) and harmless in the default path (where each UPDATE
    # also removes the row from the predicate).
    cursor_id = 0
    processed = 0
    # Guard against a systemic mid-run failure (e.g. the model context dies
    # after warmup): abort instead of marching through the whole table writing
    # empty blobs over good data.
    consecutive_failures = 0
    max_consecutive_failures = 20
    while processed < target:
        batch_limit = min(BATCH_SIZE, target - processed)
        conn.row_factory = sqlite3.Row
        if table == "chunks":
            rows = conn.execute(
                f"SELECT id, content, embedding FROM chunks "  # noqa: S608
                f"WHERE ({where}) AND id > ? ORDER BY id LIMIT ?",
                (*where_params, cursor_id, batch_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, inbound_text, reply_text, embedding FROM reply_pairs "  # noqa: S608
                f"WHERE ({where}) AND id > ? ORDER BY id LIMIT ?",
                (*where_params, cursor_id, batch_limit),
            ).fetchall()

        if not rows:
            break
        cursor_id = rows[-1]["id"]

        for row in rows:
            text = _get_text_for_row(row, table)
            if not text.strip():
                # Store a zero-length blob to mark as "processed but empty";
                # record model_id even for empties so stale-detection is uniform.
                conn.execute(
                    f"UPDATE {table} SET embedding = ?, embedding_model_id = ? WHERE id = ?",
                    (b"", model_id, row["id"]),
                )
                processed += 1
                consecutive_failures = 0
                continue

            try:
                emb = get_embedding(text)
                blob = serialize_embedding(emb)
                conn.execute(
                    f"UPDATE {table} SET embedding = ?, embedding_model_id = ? WHERE id = ?",
                    (blob, model_id, row["id"]),
                )
                consecutive_failures = 0
            except Exception as exc:
                print(f"  WARNING: failed to embed {table} id={row['id']}: {exc}")
                consecutive_failures += 1
                # Do NOT clobber an existing valid embedding with an empty blob:
                # a transient embed failure must never destroy good data. Only
                # write the empty-blob marker when the row had nothing usable to
                # begin with (NULL/empty), so we don't infinitely retry it.
                had_valid = row["embedding"] is not None and len(row["embedding"]) >= 4
                if not had_valid:
                    conn.execute(
                        f"UPDATE {table} SET embedding = ?, embedding_model_id = ? WHERE id = ?",
                        (b"", model_id, row["id"]),
                    )
                if consecutive_failures >= max_consecutive_failures:
                    conn.commit()
                    raise RuntimeError(
                        f"{consecutive_failures} consecutive embedding failures on "
                        f"{table} — aborting to protect the existing index. Last error: {exc}"
                    ) from exc

            processed += 1

        conn.commit()
        pct = (processed / target) * 100
        print(f"  Embedded {processed}/{target} {table} ({pct:.1f}%)...")

    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Index embeddings for YouOS corpus")
    parser.add_argument("--limit", type=int, default=None, help="Max rows to process")
    parser.add_argument(
        "--table",
        choices=["chunks", "reply_pairs"],
        default=None,
        help="Only process this table",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show pending counts without processing")
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Force re-embed ALL rows with the current embedding model (full rebuild)",
    )
    args = parser.parse_args()

    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    if not db_path.exists():
        print(f"Database not found at {db_path}. Run bootstrap_db.py first.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_embedding_columns(conn)

        tables = [args.table] if args.table else ["chunks", "reply_pairs"]
        start = time.time()
        total = 0
        for table in tables:
            total += _index_table(
                conn, table, limit=args.limit, dry_run=args.dry_run, reindex=args.reindex
            )

        if not args.dry_run:
            elapsed = time.time() - start
            print(f"Done. Processed {total} rows in {elapsed:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
