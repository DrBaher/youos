"""b260: triage/retrieval scale fixes from the pass-10 performance audit.

P1: SenderHistory was instantiated 3× per sweep (main scoring, mailbox
routing, forward routing), each a separate cache of leading-wildcard LIKE
full scans — a sender seen in all three was scanned 3×. The sweep now builds
ONE history and threads it through. P2: the per-retrieve embedding-coverage
COUNT was a full table scan; a partial index makes it a narrow index scan.
"""

from __future__ import annotations

import sqlite3

from app.agent import triage


def test_sweep_helpers_reuse_shared_history(monkeypatch):
    """The two routing helpers accept and reuse a passed-in history instead of
    building their own — pinning the b260 threading so the 3× scan can't
    silently return."""
    built = {"n": 0}

    class _FakeHistory:
        def count_for(self, _email):
            return 0

    def fake_from_db(url):
        built["n"] += 1
        return _FakeHistory()

    monkeypatch.setattr(triage.SenderHistory, "from_database_url", staticmethod(fake_from_db))

    shared = triage.SenderHistory.from_database_url("sqlite:///x")
    assert built["n"] == 1
    # Both helpers, given the shared instance, must NOT build another.
    triage._maybe_apply_mailbox_actions(None, "a@x.com", [], history=shared)
    triage._maybe_forward(None, "a@x.com", [], history=shared)
    assert built["n"] == 1  # still one — no per-helper rebuild


def test_embedded_partial_index_created_and_used(tmp_path):
    from scripts.index_embeddings import _ensure_embedding_columns

    db = tmp_path / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_text TEXT)")
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT)")
    conn.commit()

    _ensure_embedding_columns(conn)

    idx = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_reply_pairs_embedded" in idx
    assert "idx_chunks_embedded" in idx

    for i in range(50):
        conn.execute(
            "INSERT INTO reply_pairs (embedding) VALUES (?)", (b"vec" if i % 2 else None,)
        )
    conn.commit()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM reply_pairs "
        "WHERE embedding IS NOT NULL AND LENGTH(embedding) > 0"
    ).fetchall()
    plan_text = " ".join(str(r) for r in plan)
    assert "idx_reply_pairs_embedded" in plan_text  # index used, not a full SCAN
    # and the coverage helper returns the right fraction
    from app.retrieval.service import _embedding_coverage

    assert _embedding_coverage(conn, "reply_pairs") == 0.5
