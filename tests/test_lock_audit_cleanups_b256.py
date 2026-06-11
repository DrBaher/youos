"""b256: pass-9 lock re-audit cleanups — a TimeoutError from the bounded
snapshot lock / backup deadline (b243) surfaces with detail everywhere
instead of a bare 500 / raw traceback, and a freelist-gated VACUUM skip is
reported honestly.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_snapshot_routes_map_timeout_to_503(monkeypatch, tmp_path):
    import app.api.routes as routes

    def stuck(*a, **k):
        raise TimeoutError("another snapshot operation (create/restore/prune) has held var/.snapshot.lock")

    monkeypatch.setattr(routes, "create_snapshot", stuck)
    r = client.post("/data-safety/snapshots/create")
    assert r.status_code == 503
    assert "snapshot.lock" in r.json()["detail"]  # the reason, not "Internal Server Error"

    monkeypatch.setattr(routes, "restore_snapshot", stuck)
    # restore validates the path inside the snapshots dir first — point at a
    # name under the managed root so we reach the stubbed call.
    import app.db.bootstrap as bootstrap

    db_path = bootstrap.resolve_sqlite_path(app.state.settings.database_url)
    snap = db_path.parent / "snapshots" / "manual" / "youos-x.db"
    r = client.post("/data-safety/snapshots/restore", json={"snapshot_path": str(snap)})
    assert r.status_code == 503


def test_vacuum_small_freelist_reported_distinctly(tmp_path, monkeypatch):
    from app.agent import store
    from app.db.bootstrap import _migrate_agent_pending_drafts

    db = tmp_path / "agent.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate_agent_pending_drafts(conn)
    conn.execute(
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, needs_reply_score, "
        "tier, status, created_at) VALUES ('m', 't', 'a@x.com', 0.5, 'surface', 'pending', "
        "datetime('now', '-100 days'))"
    )
    conn.commit()
    conn.close()
    removed = store.prune_agent_tables(f"sqlite:///{db}", older_than_days=90)
    assert removed["vacuum_ok"] == 1
    assert removed.get("vacuum_skipped_small") == 1  # callers say "little to reclaim", not "vacuumed"


def test_nightly_records_separate_snapshot_and_prune_durations():
    """Source-level pin for the F6 mixup: daily_snapshot's duration is
    recorded before _t is reused for store_prune."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "nightly_pipeline.py").read_text()
    snapshot_block = src.split("# -1. Daily snapshot")[1].split("# -0.5. Store retention")[0]
    assert '_record_duration("daily_snapshot", _t)' in snapshot_block
    prune_block = src.split("# -0.5. Store retention")[1].split("# 0. Corpus deduplication")[0]
    assert '_record_duration("daily_snapshot", _t)' not in prune_block
    assert '_record_duration("store_prune", _t)' in prune_block
