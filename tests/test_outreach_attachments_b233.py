"""Outreach auto-attachments (b233).

An outreach rule can pin standing attachment file paths (e.g. a corporate
deck); the queue row carries them and push attaches them to the Gmail draft
via gog's verified ``--attach`` flag. A missing file fails the push loudly —
the template body typically promises the file, so a silently bare draft is the
one outcome we must never produce.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.agent.rules import normalize_rule, validate_rule


def _rule(**over):
    base = {
        "match": {"sender": "noreply@site.example", "subject_contains": "New demo request"},
        "action": "outreach_draft",
        "subject": "Hello from Example",
        "value": "Hi {first_name},\n\nDeck attached.\n\nBest",
    }
    base.update(over)
    return base


# --- validation / normalisation ----------------------------------------------


def test_attachments_accept_list_and_single_string():
    ok, err = validate_rule(_rule(attachments=["/tmp/deck.pdf", "/tmp/onepager.pdf"]))
    assert ok, err
    ok, err = validate_rule(_rule(attachments="/tmp/deck.pdf"))
    assert ok, err
    norm = normalize_rule(_rule(attachments="/tmp/deck.pdf"))
    assert norm["attachments"] == ["/tmp/deck.pdf"]


def test_attachments_reject_comma_path_and_non_strings():
    ok, err = validate_rule(_rule(attachments=["/tmp/a,b.pdf"]))
    assert not ok and "comma" in err
    ok, err = validate_rule(_rule(attachments=[42]))
    assert not ok


# --- triage persists row attachments -------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    from app.agent.inbox_fetch import InboxMessage

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()

    deck = tmp_path / "deck.pdf"
    deck.write_bytes(b"%PDF-1.4 fake")

    msgs = [
        InboxMessage(
            message_id="demo-1",
            thread_id="dt-1",
            account="you@example.com",
            sender="Website <noreply@site.example>",
            sender_email="noreply@site.example",
            subject="New demo request from jordan lee — Product demo",
            body="NEW DEMO REQUEST\n\nNamejordan lee Emailjordan.lee@lead.example CountryNarnia ReasonProduct demo Message hi",
            headers={},
        ),
    ]
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **k: list(msgs))
    rule = normalize_rule(_rule(attachments=[str(deck), str(tmp_path / "GONE.pdf")]))
    monkeypatch.setattr("app.agent.rules.load_rules", lambda: [rule])
    return {"database_url": f"sqlite:///{db}", "configs_dir": tmp_path, "deck": str(deck)}


def test_triage_persists_existing_attachment_and_notes_missing(env):
    from app.agent.triage import run_triage

    run_triage(account="you@example.com", database_url=env["database_url"], configs_dir=env["configs_dir"])
    conn = sqlite3.connect(env["database_url"].removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_pending_drafts").fetchone()
    conn.close()
    assert row is not None
    assert json.loads(row["attachments_json"]) == [env["deck"]]
    assert "attachment missing on disk: " in row["reasons_json"]
    assert "GONE.pdf" in row["reasons_json"]


# --- push attaches / fails loudly when the file is gone ----------------------------


def _insert_outreach_row(url, attachments):
    from app.agent import store

    return store.upsert_pending(
        database_url=url,
        message_id="demo-1", thread_id="dt-1", account="you@example.com",
        sender="Jordan Lee", sender_email="jordan.lee@lead.example",
        subject="Hello from Example", body="x", received_at=None,
        needs_reply_score=0.6, reasons=[], cold_outreach=False, tier="draft",
        draft="Hi Jordan,\n\nDeck attached.", draft_model="rule_template",
        draft_repairs=[], standing_instructions_snapshot=None, hold=True,
        to_recipients="jordan.lee@lead.example", outreach=True,
        attachments=attachments,
    )


@pytest.fixture
def push_db(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_push_passes_attachments_to_create_draft(monkeypatch, tmp_path, push_db):
    deck = tmp_path / "deck.pdf"
    deck.write_bytes(b"%PDF-1.4 fake")
    row_id = _insert_outreach_row(push_db, [str(deck)])

    captured = {}

    def _fake_create_draft(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(draft_id="gd-1")

    from app.ingestion import gmail_write

    monkeypatch.setattr(gmail_write, "create_draft", _fake_create_draft)
    monkeypatch.setattr(gmail_write, "get_signature", lambda **k: None)

    from app.agent.push import push_pending_row

    out = push_pending_row(push_db, row_id)
    assert out.ok
    assert captured["attachments"] == [str(deck)]


def test_push_fails_loudly_when_attachment_missing(monkeypatch, tmp_path, push_db):
    row_id = _insert_outreach_row(push_db, [str(tmp_path / "MOVED.pdf")])

    from app.ingestion import gmail_write

    monkeypatch.setattr(
        gmail_write, "create_draft",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not create a bare draft")),
    )
    monkeypatch.setattr(gmail_write, "get_signature", lambda **k: None)

    from app.agent import store
    from app.agent.push import push_pending_row

    out = push_pending_row(push_db, row_id)
    assert not out.ok and out.http_status == 400
    assert "MOVED.pdf" in (out.detail or "")
    # The claim was rolled back — the row is retryable, not stuck in 'sent'.
    assert store.get(push_db, row_id)["status"] == "pending"


# --- gog CLI shape -------------------------------------------------------------


def test_gog_create_draft_emits_attach_flags(monkeypatch):
    captured = {}

    def _fake_run(cmd, input, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d1"}), stderr="")

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import create_draft

    create_draft(
        account="you@example.com", to_email="lead@corp.example",
        subject="Hello", body="hi",
        attachments=["/tmp/deck.pdf", "/tmp/onepager.pdf"],
    )
    assert "--attach=/tmp/deck.pdf" in captured["cmd"]
    assert "--attach=/tmp/onepager.pdf" in captured["cmd"]


def test_gog_create_draft_rejects_comma_path(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="comma"):
        create_draft(
            account="you@example.com", to_email="lead@corp.example",
            subject="Hello", body="hi", attachments=["/tmp/a,b.pdf"],
        )


def test_non_gog_backend_rejects_attachments(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gog"):
        create_draft(
            account="you@example.com", to_email="lead@corp.example",
            subject="Hello", body="hi", attachments=["/tmp/deck.pdf"],
        )
