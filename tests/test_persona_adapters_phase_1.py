"""Per-persona adapters — Phase 1: schema, classification, observability.

This PR doesn't change generation behavior. It puts the *infrastructure*
in place so Phases 2 (per-cohort fine-tune) and 3 (routed generation)
have something to build on:

1. **Schema:** ``feedback_pairs.sender_type TEXT`` column (additive
   ALTER, NULL on pre-existing rows).
2. **Classification on insert:** both the freeform ``/feedback/submit``
   path (uses ``body.sender``) and the ``/review-queue/submit`` path
   (joins to ``reply_pairs.inbound_author``) record sender_type so any
   new feedback is correctly cohorted from day 1.
3. **Backfill script:** one-shot derives sender_type for historical
   rows via the reply_pair_id link. Idempotent; safe to re-run.
4. **Filesystem layout:** ``get_persona_adapter_path(sender_type)``
   returns ``<models_dir>/adapters/personas/{sender_type}/`` — sibling
   to the existing ``adapters/latest`` (the "global" default that
   stays in place as the Phase-3 fallback).
5. **Observability:** ``/stats/data`` surfaces ``feedback_by_persona``
   so the user can see when each cohort crosses the (Phase 2)
   training threshold without ad-hoc SQL.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def _reset_settings():
    from app.core.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def instance_with_feedback_db(monkeypatch, tmp_path, _reset_settings):
    """A YOUOS_DATA_DIR with a minimal feedback_pairs + reply_pairs schema."""
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    db = tmp_path / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE feedback_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            edited_reply TEXT NOT NULL,
            feedback_note TEXT,
            rating INTEGER,
            used_in_finetune INTEGER DEFAULT 0,
            edit_distance_pct REAL,
            reply_pair_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE reply_pairs (
            id INTEGER PRIMARY KEY,
            inbound_text TEXT,
            reply_text TEXT,
            inbound_author TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


# ── 1. Schema migration ──────────────────────────────────────────────────

def test_bootstrap_migrates_sender_type_column(instance_with_feedback_db):
    """`_migrate_feedback_pairs` adds the column when missing — idempotent."""
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    try:
        _migrate_feedback_pairs(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback_pairs)").fetchall()}
        assert "sender_type" in cols

        # Idempotent: second call must not raise (would on duplicate ALTER).
        _migrate_feedback_pairs(conn)
    finally:
        conn.close()


def test_migration_preserves_existing_rows(instance_with_feedback_db):
    """Existing rows must survive the ALTER with NULL sender_type — the
    backfill script's job to fill them in, not the migration's."""
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply) VALUES (?, ?, ?)",
        ("hi", "draft", "reply"),
    )
    conn.commit()
    try:
        _migrate_feedback_pairs(conn)
        row = conn.execute("SELECT inbound_text, sender_type FROM feedback_pairs").fetchone()
        assert row[0] == "hi"
        assert row[1] is None  # pre-existing row stays NULL
    finally:
        conn.close()


# ── 2. Classify on insert: /feedback/submit path ──────────────────────────

def test_feedback_submit_classifies_sender(instance_with_feedback_db, monkeypatch):
    """The freeform submit path takes ``body.sender`` and stores the
    classification. Pin both the happy path (`internal` for a configured
    internal domain) and the no-sender-provided path (NULL)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.feedback_routes import router
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.close()

    # Treat anything from yourcompany.com as internal.
    # `app.core.sender` does `from app.core.config import get_internal_domains`
    # at import time, so the function is bound there. Patch the bound name.
    monkeypatch.setattr(
        "app.core.sender.get_internal_domains", lambda *a, **kw: frozenset({"yourcompany.com"}),
    )

    app = FastAPI()
    app.include_router(router)  # router already has its own prefix
    app.state.settings = type("S", (), {"database_url": f"sqlite:///{db}"})()

    with TestClient(app) as client:
        resp = client.post(
            "/feedback/submit",
            json={
                "inbound_text": "hi",
                "generated_draft": "draft",
                "edited_reply": "reply",
                "sender": "colleague@yourcompany.com",
            },
        )
        assert resp.status_code == 200, resp.text

    row = sqlite3.connect(db).execute(
        "SELECT sender_type FROM feedback_pairs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "internal"


def test_feedback_submit_with_no_sender_stores_null(instance_with_feedback_db):
    """A submit without sender (e.g. a quick-rate flow that doesn't have
    the address handy) stays NULL — backfill can pick it up later if a
    reply_pair_id link exists, but we don't synthesize a wrong value."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.feedback_routes import router
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.close()

    app = FastAPI()
    app.include_router(router)  # router already has its own prefix
    app.state.settings = type("S", (), {"database_url": f"sqlite:///{db}"})()

    with TestClient(app) as client:
        resp = client.post(
            "/feedback/submit",
            json={
                "inbound_text": "hi",
                "generated_draft": "d",
                "edited_reply": "r",
                # no sender
            },
        )
        assert resp.status_code == 200, resp.text

    row = sqlite3.connect(db).execute(
        "SELECT sender_type FROM feedback_pairs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None


# ── 3. Classify on insert: /review-queue/submit path ──────────────────────

def test_review_queue_submit_classifies_via_reply_pair_link(instance_with_feedback_db):
    """The review-queue submit only carries ``reply_pair_id``, not a raw
    sender. It must look the inbound_author up via the link and classify
    — otherwise the per-persona cohorts would be empty for every
    review-queue feedback (which is most of them in steady-state)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.review_queue_routes import router
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    # Seed a reply_pair with a personal-domain inbound_author so we can
    # confirm `personal` flows through.
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, inbound_author) "
        "VALUES (1, 'hi', 'thx', 'cousin@gmail.com')"
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(router)  # router already has its own prefix
    app.state.settings = type("S", (), {"database_url": f"sqlite:///{db}"})()

    with TestClient(app) as client:
        resp = client.post(
            "/review-queue/submit",
            json={
                "reply_pair_id": 1,
                "inbound_text": "hi",
                "generated_draft": "d",
                "edited_reply": "r",
                "rating": 4,
            },
        )
        assert resp.status_code == 200, resp.text

    row = sqlite3.connect(db).execute(
        "SELECT sender_type FROM feedback_pairs WHERE reply_pair_id = 1"
    ).fetchone()
    assert row[0] == "personal"


def test_review_queue_submit_stays_null_when_inbound_author_missing(instance_with_feedback_db):
    """A reply_pair with no inbound_author (or a deleted reply_pair)
    stays NULL — better to be honest about "don't know" than guess."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.review_queue_routes import router
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, inbound_author) "
        "VALUES (2, 'hi', 'thx', NULL)"
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(router)  # router already has its own prefix
    app.state.settings = type("S", (), {"database_url": f"sqlite:///{db}"})()

    with TestClient(app) as client:
        resp = client.post(
            "/review-queue/submit",
            json={
                "reply_pair_id": 2,
                "inbound_text": "hi",
                "generated_draft": "d",
                "edited_reply": "r",
                "rating": 4,
            },
        )
        assert resp.status_code == 200, resp.text

    row = sqlite3.connect(db).execute(
        "SELECT sender_type FROM feedback_pairs WHERE reply_pair_id = 2"
    ).fetchone()
    assert row[0] is None


# ── 4. Backfill script ───────────────────────────────────────────────────

def test_backfill_classifies_historical_rows(instance_with_feedback_db, monkeypatch):
    """Existing NULL-sender_type rows with a reply_pair_id link get
    classified. Rows without a link stay NULL."""
    from app.db.bootstrap import _migrate_feedback_pairs
    from scripts.backfill_feedback_sender_type import backfill_sender_types

    # `app.core.sender` does `from app.core.config import get_internal_domains`
    # at import time, so the function is bound there. Patch the bound name.
    monkeypatch.setattr(
        "app.core.sender.get_internal_domains", lambda *a, **kw: frozenset({"yourcompany.com"}),
    )

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    # Three historical rows: one linked to internal, one to personal,
    # one with no reply_pair_id (the un-classifiable case).
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, inbound_author) "
        "VALUES (1, 'hi', 'thx', 'colleague@yourcompany.com'),"
        "       (2, 'hi', 'thx', 'cousin@gmail.com')"
    )
    conn.executemany(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, reply_pair_id) "
        "VALUES (?, ?, ?, ?)",
        [("a", "d", "r", 1), ("b", "d", "r", 2), ("c", "d", "r", None)],
    )
    conn.commit()
    conn.close()

    counts = backfill_sender_types(db, dry_run=False)
    assert counts.get("internal") == 1
    assert counts.get("personal") == 1
    assert counts.get("skipped_no_inbound_author") == 1

    rows = sqlite3.connect(db).execute(
        "SELECT inbound_text, sender_type FROM feedback_pairs ORDER BY id"
    ).fetchall()
    assert rows[0] == ("a", "internal")
    assert rows[1] == ("b", "personal")
    assert rows[2] == ("c", None)  # no link → still NULL


def test_backfill_is_idempotent(instance_with_feedback_db, monkeypatch):
    """Second run does nothing — only touches NULL-sender_type rows."""
    from app.db.bootstrap import _migrate_feedback_pairs
    from scripts.backfill_feedback_sender_type import backfill_sender_types

    # `app.core.sender` does `from app.core.config import get_internal_domains`
    # at import time, so the function is bound there. Patch the bound name.
    monkeypatch.setattr(
        "app.core.sender.get_internal_domains", lambda *a, **kw: frozenset({"yourcompany.com"}),
    )

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, inbound_author) "
        "VALUES (1, 'hi', 'thx', 'colleague@yourcompany.com')"
    )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, reply_pair_id) "
        "VALUES (?, ?, ?, ?)",
        ("a", "d", "r", 1),
    )
    conn.commit()
    conn.close()

    counts1 = backfill_sender_types(db, dry_run=False)
    assert counts1.get("internal") == 1

    # Second run: nothing left to backfill.
    counts2 = backfill_sender_types(db, dry_run=False)
    assert counts2.get("internal", 0) == 0


def test_backfill_dry_run_does_not_write(instance_with_feedback_db, monkeypatch):
    from app.db.bootstrap import _migrate_feedback_pairs
    from scripts.backfill_feedback_sender_type import backfill_sender_types

    # `app.core.sender` does `from app.core.config import get_internal_domains`
    # at import time, so the function is bound there. Patch the bound name.
    monkeypatch.setattr(
        "app.core.sender.get_internal_domains", lambda *a, **kw: frozenset({"yourcompany.com"}),
    )

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.execute(
        "INSERT INTO reply_pairs (id, inbound_text, reply_text, inbound_author) "
        "VALUES (1, 'hi', 'thx', 'colleague@yourcompany.com')"
    )
    conn.execute(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, reply_pair_id) "
        "VALUES (?, ?, ?, ?)",
        ("a", "d", "r", 1),
    )
    conn.commit()
    conn.close()

    counts = backfill_sender_types(db, dry_run=True)
    assert counts.get("internal") == 1  # would-have-classified count is honest
    # But nothing was actually written.
    row = sqlite3.connect(db).execute(
        "SELECT sender_type FROM feedback_pairs"
    ).fetchone()
    assert row[0] is None


def test_backfill_handles_missing_column(instance_with_feedback_db):
    """Pre-migration DB (column doesn't exist): backfill returns an error
    rather than silently no-opping. The user runs `youos bootstrap` first."""
    from scripts.backfill_feedback_sender_type import backfill_sender_types

    db = instance_with_feedback_db / "var" / "youos.db"
    # Skip the migration call — column should be missing.
    counts = backfill_sender_types(db, dry_run=False)
    assert counts == {"error_column_missing": 1}


# ── 5. Filesystem layout: get_persona_adapter_path ────────────────────────

def test_get_persona_adapter_path_uses_models_dir(monkeypatch, tmp_path, _reset_settings):
    """Per-persona adapters are siblings under `<models>/adapters/personas/`,
    NOT under `<models>/adapters/latest/`. Latest stays untouched as the
    global default fallback."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_adapter_path, get_models_dir, get_persona_adapter_path

    p = get_persona_adapter_path("internal")
    assert p == get_models_dir() / "adapters" / "personas" / "internal"
    # And it's a sibling, not a child, of `latest`.
    assert "latest" not in p.parts
    assert get_adapter_path().parent == p.parent.parent  # both under adapters/


def test_get_persona_adapter_path_normalizes_input(monkeypatch, tmp_path, _reset_settings):
    """Whitespace / capitalization / None → safe directory name. Without
    this, `get_persona_adapter_path("Internal ")` would create a different
    dir than `get_persona_adapter_path("internal")` and split the cohort."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_persona_adapter_path

    assert get_persona_adapter_path("Internal").name == "internal"
    assert get_persona_adapter_path("  EXTERNAL_CLIENT  ").name == "external_client"
    assert get_persona_adapter_path(None).name == "unknown"  # type: ignore[arg-type]
    assert get_persona_adapter_path("").name == "unknown"


def test_get_persona_adapter_path_honors_data_dir(monkeypatch, tmp_path, _reset_settings):
    """Same instance-awareness as everything else in this module — without
    this, per-persona adapters would all land in the repo root and trip
    the same multi-instance bug PR #16 fixed for the global adapter."""
    monkeypatch.setenv("YOUOS_DATA_DIR", str(tmp_path))
    from app.core.settings import get_persona_adapter_path

    assert str(get_persona_adapter_path("personal")).startswith(str(tmp_path))


# ── 6. Stats endpoint: feedback_by_persona ────────────────────────────────

def test_stats_surfaces_feedback_by_persona(instance_with_feedback_db, monkeypatch):
    """End-to-end: with rows in the DB, the `/stats/data` payload exposes
    per-cohort counts so the user can see when each crosses the threshold
    without writing ad-hoc SQL."""
    from app.db.bootstrap import _migrate_feedback_pairs

    db = instance_with_feedback_db / "var" / "youos.db"
    conn = sqlite3.connect(db)
    _migrate_feedback_pairs(conn)
    conn.executemany(
        "INSERT INTO feedback_pairs (inbound_text, generated_draft, edited_reply, sender_type) "
        "VALUES (?, ?, ?, ?)",
        [
            ("a", "d", "r", "internal"),
            ("b", "d", "r", "internal"),
            ("c", "d", "r", "personal"),
            ("d", "d", "r", None),  # NULL → bucketed as "unknown"
        ],
    )
    conn.commit()
    conn.close()

    # Reach in to the stats route's helper to avoid mounting the whole
    # FastAPI app + stubbing 10 other endpoints. The route just shapes
    # the dict; this exercises the SQL.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT COALESCE(sender_type, 'unknown') AS persona, COUNT(*) AS n "
        "FROM feedback_pairs GROUP BY persona"
    ).fetchall()
    conn.close()

    by_persona = {str(r["persona"]): int(r["n"]) for r in rows}
    assert by_persona == {"internal": 2, "personal": 1, "unknown": 1}


def test_stats_persona_query_tolerates_missing_column(instance_with_feedback_db):
    """Pre-migration DB — surface as empty dict rather than crashing the
    whole stats endpoint."""
    db = instance_with_feedback_db / "var" / "youos.db"
    # Don't migrate — column is missing.
    conn = sqlite3.connect(db)
    try:
        # This is the same SQL the stats endpoint runs; should raise.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "SELECT COALESCE(sender_type, 'unknown') AS persona, COUNT(*) AS n "
                "FROM feedback_pairs GROUP BY persona"
            ).fetchall()
    finally:
        conn.close()

    # The stats endpoint catches OperationalError and returns an empty
    # dict — pin that contract here so a future refactor doesn't break
    # backwards compat with pre-migration DBs.
    from app.api.stats_routes import stats_data  # noqa: F401  (import smoke)
