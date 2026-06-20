"""Status-label sync: reflect the YouOS queue into Gmail labels (web+mobile chips).

gog is fully mocked — these assert the reconciliation (add desired-but-missing,
remove no-longer-qualifying) for both YouOS/Drafted and YouOS/Invite-Pending.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent import status_labels
from app.db.bootstrap import (
    _migrate_agent_pending_drafts,
    _migrate_agent_pending_events,
    resolve_sqlite_path,
)


def _exec(db, sql, params):
    conn = sqlite3.connect(resolve_sqlite_path(db))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_pending_events(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


def _add_draft(db, *, mid, tid, account="me@x.com", status="pending", tier="draft"):
    _exec(
        db,
        "INSERT INTO agent_pending_drafts (message_id, thread_id, account, needs_reply_score, "
        "reasons_json, cold_outreach, tier, status) VALUES (?,?,?,?,?,?,?,?)",
        (mid, tid, account, 0.9, "[]", 0, tier, status),
    )


def _add_event(db, *, mid, tid, account="me@x.com", status="pending"):
    from app.agent import event_store

    rid = event_store.queue_pending_event(
        db, account=account, thread_id=tid, message_id=mid, title="Sync",
        start_iso="2030-01-01T15:00:00Z", end_iso="2030-01-01T15:30:00Z",
    )
    if status != "pending":
        _exec(db, "UPDATE agent_pending_events SET status=? WHERE id=?", (status, rid))


@pytest.fixture
def capture(monkeypatch):
    """Mock gog: record add/remove calls; ensure_label no-op; search returns a
    settable current set."""
    calls = {"add": [], "remove": [], "ensured": []}
    current = {status_labels.DRAFTED_LABEL: {}, status_labels.INVITE_LABEL: {}}

    def fake_ensure(*, account, name, known=None):
        calls["ensured"].append(name)

    def fake_modify(*, account, message_id, add=None, remove=None, dry_run=False):
        if add:
            calls["add"].append((message_id, add[0]))
        if remove:
            calls["remove"].append((message_id, remove[0]))
        return None

    monkeypatch.setattr("app.ingestion.gmail_write.ensure_label", fake_ensure)
    monkeypatch.setattr("app.ingestion.gmail_write.modify_message_labels", fake_modify)
    monkeypatch.setattr(
        status_labels, "_current_labelled",
        lambda account, label: current.get(label, {}),
    )
    return calls, current


def test_adds_label_to_drafted_and_invite_threads(db, capture):
    calls, _ = capture
    _add_draft(db, mid="m1", tid="t-draft")
    _add_event(db, mid="m2", tid="t-invite")
    status_labels.sync_status_labels(db, "me@x.com")
    assert ("m1", status_labels.DRAFTED_LABEL) in calls["add"]
    assert ("m2", status_labels.INVITE_LABEL) in calls["add"]
    assert not calls["remove"]


def test_removes_label_when_no_longer_pending(db, capture):
    calls, current = capture
    # Gmail currently has YouOS/Drafted on a thread that has NO live draft row.
    current[status_labels.DRAFTED_LABEL] = {"t-stale": ["mzz"]}
    status_labels.sync_status_labels(db, "me@x.com")
    assert ("mzz", status_labels.DRAFTED_LABEL) in calls["remove"]


def test_no_double_add_when_already_labelled(db, capture):
    calls, current = capture
    _add_draft(db, mid="m1", tid="t-draft")
    current[status_labels.DRAFTED_LABEL] = {"t-draft": ["m1"]}  # already has it
    status_labels.sync_status_labels(db, "me@x.com")
    assert not any(lbl == status_labels.DRAFTED_LABEL for _, lbl in calls["add"])


def test_surface_and_dismissed_rows_are_not_labelled(db, capture):
    calls, _ = capture
    _add_draft(db, mid="ms", tid="t-surface", tier="surface")
    _add_draft(db, mid="md", tid="t-dismissed", status="dismissed")
    status_labels.sync_status_labels(db, "me@x.com")
    assert not calls["add"]  # neither qualifies


def test_dismissed_event_not_labelled(db, capture):
    calls, _ = capture
    _add_event(db, mid="m2", tid="t-invite", status="dismissed")
    status_labels.sync_status_labels(db, "me@x.com")
    assert not any(lbl == status_labels.INVITE_LABEL for _, lbl in calls["add"])
