"""Gmail-label → dismissal sync — b57.

Tests mock subprocess.run for both the search and the modify-remove calls,
so the gog CLI shape is verified by the assertions while the actual
network/auth is never touched. Live verification of the gog shape was
done at PR time (see b47 lesson — mocked tests can't catch wrong CLI).
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def db_url(tmp_path):
    db = tmp_path / "gls.db"
    conn = sqlite3.connect(db)
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts

    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def _seed_row(db_url, *, thread_id, status="pending", account="you@x.com"):
    from app.agent import store

    rid = store.upsert_pending(db_url, **{
        "message_id": f"m-{thread_id}", "thread_id": thread_id, "account": account,
        "sender": "Sender", "sender_email": "sender@x.com",
        "subject": "x", "body": "y", "received_at": None,
        "needs_reply_score": 0.7, "reasons": [], "cold_outreach": False,
        "tier": "draft", "draft": "hi", "draft_model": "m",
        "draft_repairs": [], "standing_instructions_snapshot": None,
    })
    if status == "sent":
        store.mark_sent(db_url, rid)
    elif status == "dismissed":
        store.mark_dismissed(db_url, rid, reason="other")
    return rid


def _install_gog_mocks(monkeypatch, *, search_payload=None, modify_returncode=0, modify_stderr=""):
    """Replace subprocess.run with a router that handles the two commands
    sync_gmail_label_dismissals invokes."""
    calls: list[list[str]] = []
    search_default = {"threads": []}
    payload = search_payload if search_payload is not None else search_default

    def _fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(list(cmd))
        if cmd[:3] == ["gog", "gmail", "search"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(payload), stderr="",
            )
        if cmd[:4] == ["gog", "gmail", "messages", "modify"]:
            return SimpleNamespace(
                returncode=modify_returncode,
                stdout="{}",
                stderr=modify_stderr,
            )
        return SimpleNamespace(returncode=1, stdout="", stderr=f"unexpected cmd: {cmd}")

    monkeypatch.setattr("app.agent.gmail_label_sync.subprocess.run", _fake_run)
    return calls


def test_no_labelled_threads_returns_clean_empty_result(db_url, monkeypatch):
    _install_gog_mocks(monkeypatch, search_payload={"threads": []})
    from app.agent.gmail_label_sync import sync_gmail_label_dismissals

    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url)
    assert r.dismissed == []
    assert r.skipped == []
    assert r.errors == []


def test_labelled_thread_with_pending_row_gets_dismissed_and_label_removed(db_url, monkeypatch):
    rid = _seed_row(db_url, thread_id="t-1")
    calls = _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-1", "threadId": "t-1"}],
    })

    from app.agent.gmail_label_sync import sync_gmail_label_dismissals
    # b61: pin the b57 single-label path explicitly (default now iterates all).
    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url, label="YouOS/skip")

    assert r.dismissed == [rid]
    # The modify-remove call was issued for the labelled message.
    modify_calls = [c for c in calls if c[:4] == ["gog", "gmail", "messages", "modify"]]
    assert len(modify_calls) == 1
    assert "--remove" in modify_calls[0]
    assert modify_calls[0][modify_calls[0].index("--remove") + 1] == "YouOS/skip"

    # DB confirms the row is now dismissed-as-noise.
    from app.agent import store
    row = store.get(db_url, rid)
    assert row["status"] == "dismissed"
    assert row["dismissal_reason"] == "noise"


def test_labelled_thread_dismisses_all_pending_rows_not_just_newest(db_url, monkeypatch):
    """A thread can produce multiple pending rows across sweeps (message_id
    changes). A 'skip this thread' gesture must dismiss ALL of them — the old
    behavior dismissed only the highest-id row, leaving the others to be
    re-surfaced."""
    from app.agent import store

    common = {
        "thread_id": "t-multi", "account": "you@x.com",
        "sender": "S", "sender_email": "s@x.com", "subject": "x", "body": "y",
        "received_at": None, "needs_reply_score": 0.7, "reasons": [],
        "cold_outreach": False, "tier": "draft", "draft": "hi",
        "draft_model": "m", "draft_repairs": [], "standing_instructions_snapshot": None,
    }
    rid1 = store.upsert_pending(db_url, message_id="m-a", **common)
    rid2 = store.upsert_pending(db_url, message_id="m-b", **common)

    _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-b", "threadId": "t-multi"}],
    })

    from app.agent.gmail_label_sync import sync_gmail_label_dismissals
    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url, label="YouOS/skip")

    assert set(r.dismissed) == {rid1, rid2}
    assert store.get(db_url, rid1)["status"] == "dismissed"
    assert store.get(db_url, rid2)["status"] == "dismissed"


def test_labelled_thread_with_no_pending_row_is_skipped_not_errored(db_url, monkeypatch):
    """If the user labels a thread that's not in the queue (e.g. it never
    triggered the agent in the first place), don't error — just record."""
    _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-99", "threadId": "t-99"}],
    })
    from app.agent.gmail_label_sync import sync_gmail_label_dismissals

    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url, label="YouOS/skip")
    assert r.dismissed == []
    assert r.skipped == ["t-99"]
    assert r.errors == []


def test_already_dismissed_row_skipped(db_url, monkeypatch):
    """A row that's already in a terminal state doesn't get re-dismissed."""
    _seed_row(db_url, thread_id="t-2", status="sent")
    _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-2", "threadId": "t-2"}],
    })
    from app.agent.gmail_label_sync import sync_gmail_label_dismissals

    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url, label="YouOS/skip")
    assert r.dismissed == []
    assert "t-2" in r.skipped


def test_label_removal_failure_keeps_dismissal_recorded(db_url, monkeypatch):
    """If gog modify --remove fails, we don't roll back the dismissal —
    the user signalled intent. Just log and move on."""
    rid = _seed_row(db_url, thread_id="t-3")
    _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-3", "threadId": "t-3"}],
    }, modify_returncode=1, modify_stderr="some transient gog error")

    from app.agent.gmail_label_sync import sync_gmail_label_dismissals
    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url, label="YouOS/skip")

    # Dismissed despite the remove-label failure.
    assert r.dismissed == [rid]
    from app.agent import store
    assert store.get(db_url, rid)["status"] == "dismissed"


def test_invalid_label_returns_empty_no_error(db_url, monkeypatch):
    """User hasn't yet created the YouOS/skip label in Gmail → gog says
    'invalid label'. Treat as 'no matches' rather than failing the sweep."""
    def _fake_run(cmd, capture_output=True, text=True, timeout=None):
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="error: invalid label 'YouOS/skip' for user",
        )
    monkeypatch.setattr("app.agent.gmail_label_sync.subprocess.run", _fake_run)

    from app.agent.gmail_label_sync import sync_gmail_label_dismissals
    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url)
    assert r == r  # smoke: no raise
    assert r.dismissed == [] and r.errors == []


# --- b61: multi-label categorical dismissal -------------------------------


def test_multi_label_iterates_all_known_labels_when_label_is_none(db_url, monkeypatch):
    """Default behavior (``label=None``): iterate every label in
    LABEL_TO_REASON. The mock returns matches for one label and an
    empty list for others; verify gog search is called once per known
    label (so any new entry added to the map auto-participates)."""
    from app.agent import store
    rid = _seed_row(db_url, thread_id="t-multi-1")
    store.upsert_pending(db_url, **{
        "message_id": "m-multi-2", "thread_id": "t-multi-2", "account": "you@x.com",
        "sender": "X", "sender_email": "x@x.com", "subject": "x", "body": "y",
        "received_at": None, "needs_reply_score": 0.7, "reasons": [],
        "cold_outreach": False, "tier": "draft", "draft": "hi",
        "draft_model": "m", "draft_repairs": [],
        "standing_instructions_snapshot": None,
    })

    search_calls: list[str] = []

    def _fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:3] == ["gog", "gmail", "search"]:
            # cmd[3] is "label:YouOS/skip-noise" etc. Record which label was searched.
            search_calls.append(cmd[3])
            # Return a match only for YouOS/skip-wrong-content (so we can assert
            # that the corresponding row gets dismissed with reason='wrong_content').
            if cmd[3] == "label:YouOS/skip-wrong-content":
                import json
                from types import SimpleNamespace
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"threads": [{"id": "m-multi-1", "threadId": "t-multi-1"}]}),
                    stderr="",
                )
            import json
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout=json.dumps({"threads": []}), stderr="")
        # modify --remove
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("app.agent.gmail_label_sync.subprocess.run", _fake_run)
    from app.agent.gmail_label_sync import LABEL_TO_REASON, sync_gmail_label_dismissals

    r = sync_gmail_label_dismissals(account="you@x.com", database_url=db_url)

    # Every label in the map got searched.
    expected_searches = {f"label:{lbl}" for lbl in LABEL_TO_REASON}
    assert set(search_calls) == expected_searches
    # The row that matched wrong-content got dismissed with the right reason.
    assert r.dismissed == [rid]
    assert store.get(db_url, rid)["dismissal_reason"] == "wrong_content"


def test_explicit_label_keeps_b57_single_label_behavior(db_url, monkeypatch):
    """Backwards compat: passing label='YouOS/skip' (the b57 default)
    processes only that label, mapping to reason='noise'."""
    rid = _seed_row(db_url, thread_id="t-b57")
    _install_gog_mocks(monkeypatch, search_payload={
        "threads": [{"id": "m-b57", "threadId": "t-b57"}],
    })

    from app.agent import store
    from app.agent.gmail_label_sync import sync_gmail_label_dismissals
    r = sync_gmail_label_dismissals(
        account="you@x.com", database_url=db_url, label="YouOS/skip",
    )
    assert r.dismissed == [rid]
    assert store.get(db_url, rid)["dismissal_reason"] == "noise"


def test_label_to_reason_map_includes_all_dismissal_buckets():
    """The map must cover every dismissal reason except, by design,
    legacy-NULL — apply one of the labels and get the right reason."""
    from app.agent.gmail_label_sync import LABEL_TO_REASON
    from app.agent.store import DISMISSAL_REASONS

    reasons_in_map = set(LABEL_TO_REASON.values())
    # Every categorical reason has at least one corresponding label
    # (so chat-side dismissal can carry the same reason granularity as
    # the /triage dismiss selector).
    for reason in DISMISSAL_REASONS:
        assert reason in reasons_in_map, f"no label maps to {reason!r}"


def test_run_triage_calls_label_sync_at_start(monkeypatch, tmp_path):
    """End-to-end: run_triage hits the label sync before fetching unread."""
    # Use a minimal DB with the right schema.
    db = tmp_path / "rt.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reply_pairs (id INTEGER PRIMARY KEY, inbound_author TEXT)")
    from app.db.bootstrap import _migrate_agent_audit, _migrate_agent_pending_drafts
    _migrate_agent_pending_drafts(conn)
    _migrate_agent_audit(conn)
    conn.commit()
    conn.close()
    db_url = f"sqlite:///{db}"

    # Patch the name triage actually calls (it does `from inbox_fetch import
    # fetch_unread`, so the binding lives on the triage module). Patching the
    # inbox_fetch path only worked by import-order luck.
    monkeypatch.setattr("app.agent.triage.fetch_unread", lambda *a, **kw: [])

    called = {"yes": False}
    def _spy(*, account, database_url, label="YouOS/skip"):
        called["yes"] = True
        from app.agent.gmail_label_sync import LabelSyncResult
        return LabelSyncResult(dismissed=[], skipped=[], errors=[])
    monkeypatch.setattr("app.agent.gmail_label_sync.sync_gmail_label_dismissals", _spy)

    from app.agent.triage import run_triage
    run_triage(
        account="you@x.com", database_url=db_url, configs_dir=tmp_path,
        window="24h", limit=5,
    )
    assert called["yes"] is True
