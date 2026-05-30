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


# --- b130: body cap, charset-aware decode, RFC 2047 encoded-word headers ---


def test_body_length_is_capped_at_fetch_time(monkeypatch):
    # A multi-MB inbound body is stored + scored in full (O(size) per message).
    # Cap it at fetch time so a giant body can't slow every sweep.
    from app.agent.inbox_fetch import _MAX_BODY_CHARS

    huge = _payload(frm="Bob <bob@x.com>", subject="big", text="X" * (_MAX_BODY_CHARS + 50_000))
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(huge)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1
    assert len(msgs[0].body) == _MAX_BODY_CHARS


def test_html_body_cap_bounds_the_tag_strip(monkeypatch):
    # text/html is decoded then tag-stripped by a regex; the cap must apply
    # before the regex so a huge HTML body can't blow it up.
    from app.agent.inbox_fetch import _MAX_BODY_CHARS

    html = "<p>" + "Y" * (_MAX_BODY_CHARS + 50_000) + "</p>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(payload)})
    msgs = fetch_unread("me@x.com")
    assert len(msgs) == 1
    assert len(msgs[0].body) <= _MAX_BODY_CHARS


def test_non_utf8_body_decoded_with_declared_charset(monkeypatch):
    # A body declared ISO-8859-1 must decode with that charset, not get mangled
    # by a hardcoded UTF-8 decode.
    text = "Café costs £5"
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}],
        "body": {"data": base64.urlsafe_b64encode(text.encode("latin-1")).decode("ascii")},
    }
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(payload)})
    msgs = fetch_unread("me@x.com")
    assert msgs[0].body == text


def test_unknown_charset_falls_back_to_utf8(monkeypatch):
    # A bogus charset label must not raise (LookupError) — fall back to UTF-8.
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="x-bogus-9000"'}],
        "body": {"data": _b64("hello")},
    }
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(payload)})
    msgs = fetch_unread("me@x.com")
    assert msgs[0].body == "hello"


def test_rfc2047_encoded_words_in_from_and_subject_decoded(monkeypatch):
    # =?utf-8?b?...?= display names / subjects must be decoded to readable text,
    # and the email address must still parse out of the decoded From.
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": "=?UTF-8?B?w6lsw6lub3Jl?= <eleanor@x.com>"},
            {"name": "Subject", "value": "=?UTF-8?B?w6lsw6lub3Jl?="},
            {"name": "Date", "value": "Mon, 26 May 2026 09:00:00 +0000"},
        ],
        "body": {"data": _b64("hi")},
    }
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(payload)})
    msgs = fetch_unread("me@x.com")
    m = msgs[0]
    assert m.sender == "élénore <eleanor@x.com>"
    assert m.sender_email == "eleanor@x.com"
    assert m.subject == "élénore"


def test_decode_mime_words_degrades_on_garbage():
    # Direct unit: malformed encoded-word must not raise — return input verbatim.
    from app.agent.inbox_fetch import _decode_mime_words

    assert _decode_mime_words("plain text") == "plain text"
    assert _decode_mime_words("") == ""
    # Unknown charset inside an encoded word → replacement, never a crash.
    out = _decode_mime_words("=?x-bogus-9000?B?aGk=?=")
    assert isinstance(out, str)


# --- b130 review follow-ups: decode/header bounds + charset/decode coverage ---


def test_decode_b64_caps_input_not_just_output():
    # The body cap must bound the base64 *input* so a multi-MB body is never
    # fully decoded into memory — and the 6x input multiplier must still let an
    # all-4-byte-UTF-8 body reach the full char cap (a naive 4x cap under-caps it).
    from app.agent.inbox_fetch import _decode_b64

    huge_ascii = base64.urlsafe_b64encode(("X" * 5_000_000).encode()).decode("ascii")
    assert len(_decode_b64(huge_ascii, "utf-8", 100)) == 100
    emoji = base64.urlsafe_b64encode(("😀" * 200).encode()).decode("ascii")  # 4 bytes/char
    assert len(_decode_b64(emoji, "utf-8", 100)) == 100
    # Default (no max_chars) is unbounded passthrough — backwards compatible.
    assert _decode_b64(base64.urlsafe_b64encode(b"hi").decode("ascii")) == "hi"


def test_header_decode_is_length_bounded():
    # From/Subject are attacker-controlled and reach the LLM prompt; bound them
    # like the body. Both the plain and the RFC2047 paths must be capped.
    from app.agent.inbox_fetch import _MAX_HEADER_CHARS, _decode_mime_words

    assert len(_decode_mime_words("A" * 10_000_000)) == _MAX_HEADER_CHARS
    enc = "=?utf-8?B?" + base64.b64encode(("é" * 5_000_000).encode()).decode("ascii") + "?="
    assert len(_decode_mime_words(enc)) <= _MAX_HEADER_CHARS


def test_non_utf8_body_unquoted_charset(monkeypatch):
    # RFC 2045 makes the charset param a bare token; quoting is optional and
    # often omitted. Pin the unquoted branch so a future regex tightening can't
    # silently mojibake it while the quoted-charset test stays green.
    text = "Café costs £5"
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=ISO-8859-1"}],
        "body": {"data": base64.urlsafe_b64encode(text.encode("latin-1")).decode("ascii")},
    }
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(payload)})
    assert fetch_unread("me@x.com")[0].body == text


def test_inner_part_charset_governs_multipart_decode(monkeypatch):
    # The realistic shape: a multipart container with NO Content-Type wrapping an
    # ISO-8859-1 inner text part. Charset must be read from the recursed part, not
    # the container, or every non-UTF-8 multipart message mojibakes.
    text = "Café £5"
    inner = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=ISO-8859-1"}],
        "body": {"data": base64.urlsafe_b64encode(text.encode("latin-1")).decode("ascii")},
    }
    top = {"mimeType": "multipart/alternative", "parts": [inner]}  # container has no charset
    _patch(monkeypatch, [{"id": "t1"}], {"t1": _thread(top)})
    assert fetch_unread("me@x.com")[0].body == text


def test_thread_history_prior_turn_from_rfc2047_decoded(monkeypatch):
    # The prior-turn From feeds the drafter as conversation context, so it must be
    # RFC2047-decoded too (the only decode call site no other test reaches).
    prior = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": "=?UTF-8?B?w6lsw6lub3Jl?= <eleanor@x.com>"},
            {"name": "Date", "value": "Mon, 26 May 2026 09:00:00 +0000"},
        ],
        "body": {"data": _b64("earlier turn")},
    }
    latest = _payload(frm="You <you@x.com>", subject="Re: hi", text="thanks")
    thread = {"id": "t1", "messages": [{"id": "m1", "payload": prior}, {"id": "m2", "payload": latest}]}
    _patch(monkeypatch, [{"id": "t1"}], {"t1": thread})
    m = fetch_unread("me@x.com")[0]
    assert m.thread_history[0]["sender"] == "élénore <eleanor@x.com>"
    assert "=?" not in m.thread_history[0]["sender"]
