"""fetch_unread: latest-message extraction + thread-history capture."""

from __future__ import annotations

import base64


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _payload(*, frm: str, subject: str, text: str, date: str = "Mon, 26 May 2026 09:00:00 +0000") -> dict:
    return {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": frm},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": date},
        ],
        "body": {"data": _b64(text)},
    }


class _FakeSource:
    def __init__(self, thread):
        self._thread = thread

    def search_threads(self, *, account, query, max_threads):
        return [{"id": "thr-1"}]

    def get_thread(self, *, account, thread_id):
        return self._thread


def _install(monkeypatch, thread):
    monkeypatch.setattr(
        "app.ingestion.adapters.get_google_source",
        lambda backend=None: _FakeSource(thread),
    )


def test_thread_history_captured_from_prior_messages(monkeypatch):
    from app.agent.inbox_fetch import fetch_unread

    thread = {
        "id": "thr-1",
        "messages": [
            {"id": "m1", "payload": _payload(frm="Alice <alice@x.com>", subject="Q3", text="Here's the deck.")},
            {"id": "m2", "payload": _payload(frm="You <you@x.com>", subject="Re: Q3", text="Thanks, reviewing.")},
            {"id": "m3", "payload": _payload(frm="Alice <alice@x.com>", subject="Re: Q3", text="Any update on pricing?")},
        ],
    }
    _install(monkeypatch, thread)

    msgs = fetch_unread("you@x.com")
    assert len(msgs) == 1
    m = msgs[0]
    # body is the latest message.
    assert "Any update on pricing?" in m.body
    # history is the two prior turns, oldest→newest, with sender + text.
    assert [h["text"] for h in m.thread_history] == ["Here's the deck.", "Thanks, reviewing."]
    assert m.thread_history[0]["sender"].startswith("Alice")


def test_no_history_for_single_message_thread(monkeypatch):
    from app.agent.inbox_fetch import fetch_unread

    thread = {
        "id": "thr-1",
        "messages": [
            {"id": "m1", "payload": _payload(frm="Bob <bob@x.com>", subject="Hi", text="Quick question?")},
        ],
    }
    _install(monkeypatch, thread)

    msgs = fetch_unread("you@x.com")
    assert msgs[0].thread_history == []
