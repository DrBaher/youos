#!/usr/bin/env python3
"""Index embeddings for chunks and reply_pairs.

Processes rows with NULL embedding, stores results in DB.
Interruptible and resumable — skips already-embedded rows.

Usage:
    python3 scripts/index_embeddings.py              # index all unembedded rows
    python3 scripts/index_embeddings.py --limit 100  # index only N rows
    python3 scripts/index_embeddings.py --table reply_pairs  # only reply pairs
    python3 scripts/index_embeddings.py --dry-run    # show count of unembedded rows
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.embeddings import get_embedding, serialize_embedding
from app.core.settings import get_settings
from app.db.bootstrap import resolve_sqlite_path

BATCH_SIZE = 50


def _ensure_embedding_columns(conn: sqlite3.Connection) -> None:
    """Add embedding BLOB columns if they don't exist yet."""
    for table in ("chunks", "reply_pairs"):
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "embedding" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN embedding BLOB")
            conn.commit()
            print(f"  Migrated: added embedding column to {table}")


def _count_unembedded(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE embedding IS NULL").fetchone()
    return row[0] if row else 0


def _get_text_for_row(row: sqlite3.Row, table: str) -> str:
    if table == "chunks":
        return row["content"] or ""
    else:  # reply_pairs
        inbound = row["inbound_text"] or ""
        reply = row["reply_text"] or ""
        return f"{inbound}\n{reply}"


def _index_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    """Index unembedded rows in a single table. Returns number of rows processed."""
    total_unembedded = _count_unembedded(conn, table)
    if dry_run:
        print(f"  {table}: {total_unembedded} unembedded rows")
        return 0

    if total_unembedded == 0:
        print(f"  {table}: all rows already embedded")
        return 0

    target = min(total_unembedded, limit) if limit else total_unembedded
    print(f"  {table}: {total_unembedded} unembedded rows, will process {target}")

    processed = 0
    while processed < target:
        batch_limit = min(BATCH_SIZE, target - processed)
        conn.row_factory = sqlite3.Row
        if table == "chunks":
            rows = conn.execute(
                "SELECT id, content FROM chunks WHERE embedding IS NULL LIMIT ?",
                (batch_limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, inbound_text, reply_text FROM reply_pairs WHERE embedding IS NULL LIMIT ?",
                (batch_limit,),
            ).fetchall()

        if not rows:
            break

        for row in rows:
            text = _get_text_for_row(row, table)
            if not text.strip():
                # Store a zero-length blob to mark as "processed but empty"
                conn.execute(
                    f"UPDATE {table} SET embedding = ? WHERE id = ?",
                    (b"", row["id"]),
                )
                processed += 1
                continue

            try:
                emb = get_embedding(text)
                blob = serialize_embedding(emb)
                conn.execute(
                    f"UPDATE {table} SET embedding = ? WHERE id = ?",
                    (blob, row["id"]),
                )
            except Exception as exc:
                print(f"  WARNING: failed to embed {table} id={row['id']}: {exc}")
                # Store empty blob to avoid infinite retry
                conn.execute(
                    f"UPDATE {table} SET embedding = ? WHERE id = ?",
                    (b"", row["id"]),
                )

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
    parser.add_argument("--dry-run", action="store_true", help="Show unembedded counts without processing")
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
            total += _index_table(conn, table, limit=args.limit, dry_run=args.dry_run)

        if not args.dry_run:
            elapsed = time.time() - start
            print(f"Done. Processed {total} rows in {elapsed:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
