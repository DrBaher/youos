"""fetch_unread: latest-message extraction + thread-history capture."""

from __future__ import annotations

import base64

from app.agent.inbox_fetch import fetch_unread


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


# --- robustness: malformed/attacker-influenced MIME must not abort the sweep ---


def _thread(payload):
    return {"messages": [{"id": "m1", "payload": payload}]}


def _patch(monkeypatch, thread_list, threads_by_id):
    """Install a fake source that lists ``thread_list`` and returns the matching
    thread per id (supports multiple threads, unlike the single-thread _install)."""
    class _MultiSource:
        def search_threads(self, *, account, query, max_threads):
            return thread_list

        def get_thread(self, *, account, thread_id):
            return threads_by_id[thread_id]

    monkeypatch.setattr(
        "app.ingestion.adapters.get_google_source",
        lambda backend=None: _MultiSource(),
    )


def test_malformed_headers_do_not_raise(monkeypatch):
    # headers is not a list / entries are not dicts → degrade, not crash.
    bad = {"headers": "not-a-list", "mimeType": "text/plain",
           "body": {"data": _b64("hi")}}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(bad)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1 and msgs[0].body == "hi"

    bad2 = {"headers": ["junk", 42, None], "mimeType": "text/plain",
            "body": {"data": _b64("yo")}}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(bad2)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1 and msgs[0].body == "yo"


def test_malformed_parts_do_not_raise(monkeypatch):
    # parts contains non-dict entries → skipped, not crashed.
    bad = {"mimeType": "multipart/mixed", "parts": ["junk", None, 7,
           {"mimeType": "text/plain", "body": {"data": _b64("found")}}]}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(bad)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1 and msgs[0].body == "found"


def test_malformed_base64_body_degrades_to_empty(monkeypatch):
    # non-ASCII / wrong-length base64 → '' rather than ValueError/binascii.Error.
    bad = {"mimeType": "text/plain", "body": {"data": "ünïcödé-not-b64"}}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(bad)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1 and msgs[0].body == ""


def test_non_str_base64_data_degrades(monkeypatch):
    bad = {"mimeType": "text/plain", "body": {"data": 12345}}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(bad)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1 and msgs[0].body == ""


def test_deeply_nested_parts_do_not_recurse_overflow(monkeypatch):
    # Build a parts tree far deeper than CPython's recursion limit.
    node = {"mimeType": "text/plain", "body": {"data": _b64("deep")}}
    for _ in range(5000):
        node = {"mimeType": "multipart/mixed", "parts": [node]}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(node)})
    msgs = fetch_unread("me@x.com")
    # Bounded recursion → no RecursionError; body past the depth cap is just empty.
    assert len(msgs) == 1
    assert msgs[0].body == ""
    assert msgs[0].has_attachment is False


def test_one_bad_thread_does_not_abort_the_sweep(monkeypatch):
    # b129 widened the per-thread guard to cover PARSING, not just the fetch.
    # Force a deterministic parse-time exception on the bad thread (a payload
    # marked _boom) and assert the good thread still triages.
    import app.agent.inbox_fetch as inbox_fetch

    real_extract = inbox_fetch._extract_text

    def _boom_extract(payload):
        if isinstance(payload, dict) and payload.get("_boom"):
            raise RuntimeError("simulated parse failure")
        return real_extract(payload)

    monkeypatch.setattr(inbox_fetch, "_extract_text", _boom_extract)

    bad = {"mimeType": "text/plain", "_boom": True, "body": {"data": _b64("x")}}
    good = {"mimeType": "text/plain", "body": {"data": _b64("ok")}}
    _patch(monkeypatch, [{"id": "bad"}, {"id": "good"}],
           {"bad": _thread(bad), "good": _thread(good)})
    msgs = inbox_fetch.fetch_unread("me@x.com")
    bodies = [m.body for m in msgs]
    assert bodies == ["ok"]  # bad thread skipped, good thread survived
