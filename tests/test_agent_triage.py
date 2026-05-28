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
