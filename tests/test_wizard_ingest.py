"""Run-ingestion-from-wizard + lookback (wizard ingest step).

POST /api/ingest spawns ingestion in the background bounded by a whitelisted
lookback (-> Gmail `newer_than:`); GET /api/ingest/status reports progress from
the ingest_runs log. Subprocess is mocked here so tests never launch a real
ingest.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.api import stats_routes as sr
from app.core.stats import get_latest_ingest_status
from app.main import app

client = TestClient(app)


# --- status reader ---------------------------------------------------------


def _db_with_run(tmp_path, **cols) -> str:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE ingest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT,
            accounts_json TEXT, started_at TEXT, completed_at TEXT, status TEXT,
            discovered_count INTEGER DEFAULT 0, fetched_count INTEGER DEFAULT 0,
            stored_document_count INTEGER DEFAULT 0, stored_chunk_count INTEGER DEFAULT 0,
            stored_reply_pair_count INTEGER DEFAULT 0, error_summary TEXT, error_detail TEXT,
            metadata_json TEXT, created_ts TEXT)"""
    )
    conn.execute(
        "INSERT INTO ingest_runs (run_id, source, started_at, status, discovered_count, fetched_count, stored_reply_pair_count, error_summary) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("r1", "gmail", "2026-05-25T00:00:00Z", cols.get("status", "completed"),
         cols.get("discovered", 0), cols.get("fetched", 0), cols.get("reply_pairs", 0), cols.get("error")),
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_status_idle_when_no_db(tmp_path):
    assert get_latest_ingest_status(f"sqlite:///{tmp_path/'nope.db'}") == {"status": "idle"}


def test_status_maps_started_to_running(tmp_path):
    s = get_latest_ingest_status(_db_with_run(tmp_path, status="started", discovered=12))
    assert s["status"] == "running"
    assert s["discovered"] == 12


def test_status_reports_completed_counts(tmp_path):
    s = get_latest_ingest_status(_db_with_run(tmp_path, status="completed", fetched=40, reply_pairs=120))
    assert s["status"] == "completed"
    assert s["fetched"] == 40 and s["reply_pairs"] == 120


# --- POST /api/ingest ------------------------------------------------------


def test_ingest_rejects_unknown_lookback():
    assert client.post("/api/ingest", json={"lookback": "10y"}).status_code == 400


def test_ingest_409_when_running(monkeypatch):
    monkeypatch.setattr(sr, "get_latest_ingest_status", lambda _u: {"status": "running"})
    assert client.post("/api/ingest", json={"lookback": "1y"}).status_code == 409


def test_ingest_spawns_with_dated_query(monkeypatch):
    monkeypatch.setattr(sr, "get_latest_ingest_status", lambda _u: {"status": "idle"})
    captured = {}
    monkeypatch.setattr(sr.subprocess, "Popen", lambda args, **kw: captured.update(args=args, kw=kw))
    r = client.post("/api/ingest", json={"lookback": "2y"})
    assert r.status_code == 200
    assert r.json()["query"] == "in:anywhere newer_than:2y"
    # arg list (no shell), carries the dated query and --live
    assert "--live" in captured["args"]
    assert "in:anywhere newer_than:2y" in captured["args"]
    assert captured["kw"].get("start_new_session") is True


def test_ingest_all_omits_date_filter(monkeypatch):
    monkeypatch.setattr(sr, "get_latest_ingest_status", lambda _u: {"status": "idle"})
    monkeypatch.setattr(sr.subprocess, "Popen", lambda args, **kw: None)
    r = client.post("/api/ingest", json={"lookback": "all"})
    assert r.json()["query"] == "in:anywhere"  # no newer_than


def test_ingest_status_endpoint(monkeypatch):
    monkeypatch.setattr(sr, "get_latest_ingest_status", lambda _u: {"status": "completed", "reply_pairs": 5})
    body = client.get("/api/ingest/status").json()
    assert body == {"status": "completed", "reply_pairs": 5}
