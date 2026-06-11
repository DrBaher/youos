"""b244: retention correctness — surface/decided pending rows are bounded,
un-mined feedback signal and recipient-trust evidence survive, run ledgers
are covered, the daily cap counts only draft-tier rows, and a skipped VACUUM
is reported instead of swallowed.
"""

from __future__ import annotations

import sqlite3

from app.agent.store import count_persisted_today, prune_agent_tables, recipient_trust
from app.autoresearch.run_log import ensure_table as ensure_autoresearch_table
from app.db.bootstrap import _migrate_agent_digest_runs, _migrate_agent_pending_drafts


def _db(tmp_path):
    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")  # match prod (bootstrap sets WAL)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_digest_runs(conn)
    conn.commit()
    conn.close()
    return db


def _add(db, mid, *, tier="draft", status="pending", days_ago=0, mined=0,
         decided=0, send_state=None, sender="s@x.com"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, sender_email, "
        "needs_reply_score, tier, status, created_at, feedback_captured, outcome_captured, send_state) "
        "VALUES (?, 't', 'a@x.com', ?, 0.5, ?, ?, datetime('now', ?), ?, ?, ?)",
        (mid, sender, tier, status, f"-{days_ago} days", mined, decided, send_state),
    )
    conn.commit()
    conn.close()


def _ids(db):
    conn = sqlite3.connect(db)
    rows = {r[0] for r in conn.execute("SELECT message_id FROM agent_pending_drafts")}
    conn.close()
    return rows


def test_aged_surface_and_decided_pending_rows_are_pruned(tmp_path):
    db = _db(tmp_path)
    _add(db, "surface-old", tier="surface", days_ago=100)
    _add(db, "surface-new", tier="surface", days_ago=5)
    _add(db, "draft-old-undecided", tier="draft", days_ago=100)            # user may still act
    _add(db, "draft-old-decided", tier="draft", days_ago=100, decided=1)   # outcome recorded -> dead row
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_pending_drafts_surface"] == 1
    assert removed["agent_pending_drafts_decided"] == 1
    assert _ids(db) == {"surface-new", "draft-old-undecided"}


def test_unmined_terminal_rows_get_grace_then_prune(tmp_path):
    db = _db(tmp_path)
    _add(db, "unmined-100d", status="dismissed", days_ago=100, mined=0)  # within 4x grace -> kept
    _add(db, "unmined-400d", status="dismissed", days_ago=400, mined=0)  # past grace -> pruned
    _add(db, "mined-100d", status="dismissed", days_ago=100, mined=1)    # mined -> normal horizon
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_pending_drafts"] == 1          # mined-100d
    assert removed["agent_pending_drafts_unmined"] == 1  # unmined-400d
    assert _ids(db) == {"unmined-100d"}


def test_confirmed_send_trust_evidence_survives_prune(tmp_path):
    db = _db(tmp_path)
    _add(db, "auto-sent", status="sent", days_ago=400, mined=1, send_state="sent")
    _add(db, "user-marked-sent", status="sent", days_ago=400, mined=1, send_state=None)
    assert recipient_trust(f"sqlite:///{db}", "s@x.com") == 2
    prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert _ids(db) == {"auto-sent", "user-marked-sent"}
    assert recipient_trust(f"sqlite:///{db}", "s@x.com") == 2  # trust not silently decayed


def test_run_ledgers_are_pruned(tmp_path):
    db = _db(tmp_path)
    ensure_autoresearch_table(f"sqlite:///{db}")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO autoresearch_runs (run_tag, created_ts) VALUES ('old', datetime('now','-100 days'))")
    conn.execute("INSERT INTO autoresearch_runs (run_tag, created_ts) VALUES ('new', datetime('now','-5 days'))")
    conn.execute(
        "INSERT INTO agent_digest_runs (name, account, period_key, status, created_at) "
        "VALUES ('d','a@x.com','2026-01-01','sent', datetime('now','-100 days'))"
    )
    conn.commit()
    conn.close()
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["autoresearch_runs"] == 1
    assert removed["agent_digest_runs"] == 1


def test_vacuum_skip_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A VACUUM that can't run (busy DB / snapshot lock held) must be visible
    in the result, not silently swallowed like the old bare ``except: pass``."""
    from contextlib import contextmanager

    from app.core import data_safety

    @contextmanager
    def held_lock(db_path):
        raise TimeoutError("another snapshot operation holds the lock")
        yield  # pragma: no cover

    monkeypatch.setattr(data_safety, "snapshot_lock", held_lock)
    db = _db(tmp_path)
    _add(db, "surface-old", tier="surface", days_ago=100)
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["agent_pending_drafts_surface"] == 1  # deletes still landed
    assert removed["vacuum_ok"] == 0                     # ...but the skip is visible


def test_vacuum_ok_when_uncontended(tmp_path):
    db = _db(tmp_path)
    _add(db, "surface-old", tier="surface", days_ago=100)
    removed = prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["vacuum_ok"] == 1


def test_daily_cap_counts_draft_tier_only(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        _add(db, f"surface-{i}", tier="surface", days_ago=0)
    _add(db, "draft-0", tier="draft", days_ago=0)
    # 3 surfaces must not consume the draft budget (the agent silently stopped
    # drafting at "daily cap reached" while surfaces kept persisting uncapped).
    assert count_persisted_today(f"sqlite:///{db}", account="a@x.com") == 1
