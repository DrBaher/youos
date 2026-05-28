"""Triage orchestrator — fetch → filter → draft, with a mocked Google source.

Phase 1 has no persistence; this just pins the end-to-end loop shape so
later phases (UI, scheduling, OAuth) can build on a known-good orchestrator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.inbox_fetch import InboxMessage
from app.agent.needs_reply import NeedsReplyVerdict


@pytest.fixture
def mocked_environment(monkeypatch, tmp_path):
    """A minimal triage env: stubbed inbox fetch, stubbed generate_draft, a
    tmp DB that the SenderHistory can hit without blowing up."""
    # Create an empty SQLite DB with the reply_pairs schema so the history
    # query doesn't fail.
    import sqlite3

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)"
    )
    # β: tests use the real agent_pending_drafts schema so persistence is
    # exercised end-to-end. Call the migration directly rather than copying
    # the DDL here so the test can't drift from prod.
    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    conn.commit()
    conn.close()

    msgs = [
        InboxMessage(
            message_id="m1",
            thread_id="t1",
            account="you@example.com",
            sender="Alice <alice@partner.com>",
            sender_email="alice@partner.com",
            subject="Pricing question",
            body="Hi — could you confirm the Q3 pricing? Thanks.",
            headers={},
        ),
        InboxMessage(
            message_id="m2",
            thread_id="t2",
            account="you@example.com",
            sender="newsletter@digest.com",
            sender_email="newsletter@digest.com",
            subject="Your weekly digest",
            body="Long body" * 80,
            headers={"list-unsubscribe": "<mailto:unsub@digest.com>"},
        ),
    ]
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: list(msgs))

    class _Resp:
        draft = "Hi Alice, confirmed — Q3 pricing unchanged."
        model_used = "qwen2.5-1.5b-lora"
        repairs: list[str] = []

    monkeypatch.setattr(
        "app.generation.service.generate_draft",
        lambda req, **kw: _Resp(),
    )

    return {
        "database_url": f"sqlite:///{db}",
        "configs_dir": tmp_path,
        "messages": msgs,
    }


def test_triage_drafts_for_real_inbound_skips_newsletter(mocked_environment):
    """The pricing question should be drafted; the newsletter should be skipped
    by the list-unsubscribe hard rule."""
    from app.agent.triage import run_triage

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"],
        configs_dir=env["configs_dir"],
    )

    assert result.fetched == 2
    assert result.kept == 1, f"expected 1 draft, got {result.kept}"
    assert len(result.drafts) == 1
    assert len(result.skipped) == 1

    drafted = result.drafts[0]
    assert drafted.message.subject == "Pricing question"
    assert drafted.draft == "Hi Alice, confirmed — Q3 pricing unchanged."
    assert drafted.model_used == "qwen2.5-1.5b-lora"
    assert drafted.error is None

    skipped_msg, skipped_verdict = result.skipped[0]
    assert skipped_msg.subject == "Your weekly digest"
    assert not skipped_verdict.needs_reply


def test_triage_records_draft_errors_without_crashing(mocked_environment, monkeypatch):
    """A generation failure on one message must not kill the whole sweep —
    it's recorded with an ``error`` field, and other messages still draft."""
    from app.agent.triage import run_triage

    def _boom(*a, **k):
        raise RuntimeError("warm server down")

    monkeypatch.setattr("app.generation.service.generate_draft", _boom)

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"],
        configs_dir=env["configs_dir"],
    )

    # The pricing question still tried (and recorded the error); newsletter
    # still skipped. The sweep finished.
    assert result.fetched == 2
    errored = [d for d in result.drafts if d.error]
    assert len(errored) == 1
    assert "warm server down" in errored[0].error
    # kept counts only successful drafts.
    assert result.kept == 0


# --- β: persistence behaviour ---------------------------------------------


def test_triage_persists_drafts_and_is_idempotent_on_repeat_run(mocked_environment):
    """``run_triage`` persists drafts into agent_pending_drafts and is
    idempotent on the Gmail message_id — a second run with the same inbound
    must not create duplicates."""
    from app.agent.triage import run_triage
    from app.agent.store import list_pending

    env = mocked_environment

    r1 = run_triage(account="you@example.com", database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r1.persisted == 1  # the pricing question — newsletter is hard-skipped
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["subject"] == "Pricing question"
    assert rows[0]["tier"] == "draft"
    assert rows[0]["status"] == "pending"
    assert rows[0]["draft"] == "Hi Alice, confirmed — Q3 pricing unchanged."

    # Second run: same message_ids → upsert IGNOREs → no new rows.
    r2 = run_triage(account="you@example.com", database_url=env["database_url"], configs_dir=env["configs_dir"])
    assert r2.persisted == 0
    rows2 = list_pending(env["database_url"])
    assert len(rows2) == 1, "repeated triage must not duplicate"


def test_triage_dry_run_does_not_persist(mocked_environment):
    from app.agent.triage import run_triage
    from app.agent.store import list_pending

    env = mocked_environment
    result = run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        persist=False,
    )
    assert result.persisted == 0
    assert list_pending(env["database_url"]) == []


# --- δ: standing instructions threaded into the prompt + snapshotted -------


def test_standing_instructions_threaded_into_draft_request(mocked_environment, monkeypatch):
    """The triage orchestrator passes ``standing_instructions`` into the
    ``DraftRequest`` so generation can inject it via the same ``extra_constraint``
    hook the cold-outreach nudge uses."""
    seen: dict = {}
    def _spy(req, **kw):
        seen["standing_instructions"] = getattr(req, "standing_instructions", None)
        class _Resp:
            draft = "ok"; model_used = "stub"; repairs: list[str] = []
        return _Resp()
    monkeypatch.setattr("app.generation.service.generate_draft", _spy)

    env = mocked_environment
    from app.agent.triage import run_triage
    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        standing_instructions="today I'm OOO; politely decline meetings",
        persist=False,
    )
    assert seen["standing_instructions"] == "today I'm OOO; politely decline meetings"


def test_standing_instructions_snapshotted_per_row(mocked_environment):
    """Each persisted row records the standing instructions that were active
    when the draft was generated — auditability after the user changes them."""
    env = mocked_environment
    from app.agent.triage import run_triage
    from app.agent.store import list_pending

    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
        standing_instructions="be brief",
    )
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["standing_instructions_snapshot"] == "be brief"


def test_standing_instructions_falls_back_to_config(mocked_environment, monkeypatch):
    """When the caller doesn't pass ``standing_instructions``, the
    orchestrator reads it from ``agent.standing_instructions`` config so the
    background scheduler + manual ``youos triage`` both pick it up."""
    monkeypatch.setattr(
        "app.agent.scheduler.get_agent_config",
        lambda: {
            "enabled": True, "interval_minutes": 15, "accounts": [],
            "window": "24h", "limit": 25, "threshold": 0.6, "notify_macos": True,
            "standing_instructions": "from config",
        },
    )
    env = mocked_environment
    from app.agent.triage import run_triage
    from app.agent.store import list_pending

    run_triage(
        account="you@example.com",
        database_url=env["database_url"], configs_dir=env["configs_dir"],
    )
    rows = list_pending(env["database_url"])
    assert len(rows) == 1
    assert rows[0]["standing_instructions_snapshot"] == "from config"
