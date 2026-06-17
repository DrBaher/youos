"""Draft-time signal capture (Draft PR B).

`draft_history` is only written at feedback time. This pins the new append-only
`draft_events` log written on every generated draft, capturing the
exemplar ids / intent / sender_type / confidence the draft was produced with.
Fully fault-isolated and config-gated (default on).
"""

from __future__ import annotations

import json
import sqlite3

from app.db.bootstrap import _migrate_draft_events
from app.generation import service as svc
from app.generation.service import _draft_logging_enabled, _log_draft_event


def _db_url(tmp_path) -> tuple[str, str]:
    db = tmp_path / "t.db"
    return f"sqlite:///{db}", str(db)


def _sample(**over):
    base = dict(
        inbound_text="Can we meet Tuesday?",
        draft="Sure, Tuesday works.",
        account_email="me@x.com",
        sender="john@acme.com",
        sender_type="external_client",
        detected_mode="work",
        intent="scheduling",
        confidence="high",
        confidence_reason="strong matches",
        model_used="qwen2.5-1.5b-base",
        retrieval_method="fts+semantic",
        exemplar_ids=["rp-1", "rp-2"],
        length_flag="ok",
    )
    base.update(over)
    return base


# --- config gate -----------------------------------------------------------


def test_logging_enabled_default_true(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    assert _draft_logging_enabled() is True


def test_logging_can_be_disabled(monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"log_drafts": False}})
    assert _draft_logging_enabled() is False


# --- writing ---------------------------------------------------------------


def test_log_draft_event_writes_row_and_self_heals_table(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    url, path = _db_url(tmp_path)
    # No table created beforehand — the logger must CREATE IF NOT EXISTS.
    assert _log_draft_event(url, **_sample()) is True

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT inbound_text, sender_type, intent, confidence, exemplar_ids, length_flag, model_used FROM draft_events"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "Can we meet Tuesday?"
    assert row[1] == "external_client"
    assert row[2] == "scheduling"
    assert row[3] == "high"
    assert json.loads(row[4]) == ["rp-1", "rp-2"]
    assert row[5] == "ok"
    assert row[6] == "qwen2.5-1.5b-base"


def test_log_draft_event_persists_thread_id(tmp_path, monkeypatch):
    """b269: thread_id is the stable key joining a logged draft to the user's
    actual sent reply (reply_pairs.thread_id). It must round-trip."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    url, path = _db_url(tmp_path)
    assert _log_draft_event(url, **_sample(thread_id="thread-abc123")) is True
    conn = sqlite3.connect(path)
    try:
        val = conn.execute("SELECT thread_id FROM draft_events").fetchone()[0]
    finally:
        conn.close()
    assert val == "thread-abc123"


def test_log_draft_event_self_heals_thread_id_on_legacy_table(tmp_path, monkeypatch):
    """An instance whose draft_events predates thread_id must gain the column on
    the next write (CREATE TABLE IF NOT EXISTS can't add it)."""
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    url, path = _db_url(tmp_path)
    # Simulate a legacy table: the exact pre-b269 shape — every column except
    # thread_id (the only thing the self-heal should add).
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE draft_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL,
                account_email TEXT, sender TEXT, sender_type TEXT,
                detected_mode TEXT, intent TEXT, confidence TEXT,
                confidence_reason TEXT, model_used TEXT, retrieval_method TEXT,
                exemplar_ids TEXT NOT NULL DEFAULT '[]', length_flag TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    assert _log_draft_event(url, **_sample(thread_id="t-legacy")) is True
    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_events)").fetchall()}
        val = conn.execute("SELECT thread_id FROM draft_events").fetchone()[0]
    finally:
        conn.close()
    assert "thread_id" in cols
    assert val == "t-legacy"


def test_log_draft_event_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {"generation": {"log_drafts": False}})
    url, path = _db_url(tmp_path)
    assert _log_draft_event(url, **_sample()) is False
    # Nothing written / table never created.
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='draft_events'").fetchone()
    finally:
        conn.close()
    assert exists is None


def test_log_draft_event_never_raises(monkeypatch):
    # A DB failure must not break drafting: returns False, no exception.
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk gone")

    monkeypatch.setattr(svc, "_connect", boom)
    assert _log_draft_event("sqlite:////nonexistent/x.db", **_sample()) is False


def test_empty_exemplar_ids_serialized_as_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.load_config", lambda *a, **k: {})
    url, path = _db_url(tmp_path)
    assert _log_draft_event(url, **_sample(exemplar_ids=[])) is True
    conn = sqlite3.connect(path)
    try:
        val = conn.execute("SELECT exemplar_ids FROM draft_events").fetchone()[0]
    finally:
        conn.close()
    assert json.loads(val) == []


# --- migration -------------------------------------------------------------


def test_migration_creates_draft_events_table():
    conn = sqlite3.connect(":memory:")
    try:
        _migrate_draft_events(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_events)").fetchall()}
    finally:
        conn.close()
    assert {"inbound_text", "exemplar_ids", "intent", "sender_type", "length_flag", "thread_id"} <= cols


def test_migration_self_heals_thread_id_on_legacy_table():
    """_migrate_draft_events adds thread_id to a table created before b269."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE draft_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "inbound_text TEXT NOT NULL, generated_draft TEXT NOT NULL, "
            "exemplar_ids TEXT NOT NULL DEFAULT '[]', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        _migrate_draft_events(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_events)").fetchall()}
    finally:
        conn.close()
    assert "thread_id" in cols
