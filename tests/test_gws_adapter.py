"""Tests for the gws (Google Workspace CLI) ingestion backend (PR 2).

Live `gws` can't run in CI/the container (no authenticated Google account), so
these pin the transport, command construction, JSON-envelope handling,
single-account credential bridging, and — most importantly — that the gws
payloads are shaped exactly as YouOS's existing normalizers consume. The
Gmail path is verified to feed straight into `_message_body_text`; the Docs
path through the structural-element text walk.
"""

from __future__ import annotations

import base64
import types

import pytest

from app.ingestion import adapters
from app.ingestion.adapters import (
    GoogleWorkspaceSource,
    GwsSource,
    _docs_document_to_text,
    _unwrap_gws_envelope,
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


# --- envelope unwrapping ---------------------------------------------------


def test_unwrap_passes_through_bare_resource():
    bare = {"id": "t1", "messages": [{"id": "m1"}]}
    assert _unwrap_gws_envelope(bare) is bare


def test_unwrap_descends_result_envelope():
    assert _unwrap_gws_envelope({"result": {"threads": [1]}}) == {"threads": [1]}


def test_unwrap_descends_nested_envelope():
    assert _unwrap_gws_envelope({"response": {"data": {"files": []}}}) == {"files": []}


def test_unwrap_non_dict_is_returned_as_is():
    assert _unwrap_gws_envelope([1, 2, 3]) == [1, 2, 3]


# --- Docs structural-element text walk -------------------------------------


def test_docs_text_walks_top_level_body():
    doc = {
        "title": "Doc",
        "body": {
            "content": [
                {"paragraph": {"elements": [{"textRun": {"content": "Line one\n"}}, {"textRun": {"content": "more\n"}}]}},
                {"paragraph": {"elements": [{"textRun": {"content": "Line two\n"}}]}},
                {"sectionBreak": {}},  # non-paragraph element ignored
            ]
        },
    }
    assert _docs_document_to_text(doc, all_tabs=False) == "Line one\nmore\nLine two"


def test_docs_text_all_tabs_concatenates():
    doc = {
        "tabs": [
            {"documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Tab A\n"}}]}}]}}},
            {"documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Tab B\n"}}]}}]}}},
        ]
    }
    assert _docs_document_to_text(doc, all_tabs=True) == "Tab A\nTab B"


def test_docs_text_tabs_only_doc_falls_back_to_first_tab():
    doc = {
        "tabs": [
            {"documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "First\n"}}]}}]}}},
            {"documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Second\n"}}]}}]}}},
        ]
    }
    assert _docs_document_to_text(doc, all_tabs=False) == "First"


# --- transport (_run_json) -------------------------------------------------


def _fake_proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_json_builds_command_and_parses(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return _fake_proc(stdout='{"files": []}')

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    source = GwsSource(credentials={})
    out = source._run_json(["drive", "files", "list"], account="a@x.com", params={"q": "x"})

    assert out == {"files": []}
    assert captured["command"][:5] == ["gws", "drive", "files", "list", "--params"]
    assert captured["command"][5] == '{"q":"x"}'  # compact JSON


def test_run_json_unwraps_envelope(monkeypatch):
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: _fake_proc(stdout='{"result": {"threads": []}}'))
    source = GwsSource(credentials={})
    assert source._run_json(["gmail", "users", "threads", "list"], account=None, params={}) == {"threads": []}


def test_run_json_sets_per_account_credentials_env(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _fake_proc(stdout="{}")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    source = GwsSource(credentials={"work@x.com": "/creds/work.json"})
    source._run_json(["drive", "files", "list"], account="work@x.com", params={})
    assert captured["env"]["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] == "/creds/work.json"


def test_run_json_raises_on_non_rate_limit_error(monkeypatch):
    monkeypatch.setattr(adapters.subprocess, "run", lambda *a, **k: _fake_proc(returncode=1, stderr="bad request"))
    source = GwsSource(credentials={})
    with pytest.raises(ValueError, match="failed: bad request"):
        source._run_json(["drive", "files", "list"], account=None, params={})


def test_run_json_retries_rate_limit_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _fake_proc(returncode=1, stderr="rateLimitExceeded")

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    monkeypatch.setattr(adapters.time, "sleep", lambda _s: None)
    source = GwsSource(credentials={})
    # After exhausting backoff, it raises with the rate-limit error detail
    # (mirrors the gog backend's _run_gog_json behavior exactly).
    with pytest.raises(ValueError, match="rateLimitExceeded"):
        source._run_json(["drive", "files", "list"], account=None, params={})
    # 4 backoff attempts + 1 final = 5 invocations.
    assert calls["n"] == len(adapters._GWS_BACKOFF_SECONDS) + 1


def test_run_json_timeout_raises(monkeypatch):
    import subprocess as real_subprocess

    def fake_run(command, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd=command, timeout=adapters.GWS_TIMEOUT_SECONDS)

    monkeypatch.setattr(adapters.subprocess, "run", fake_run)
    source = GwsSource(credentials={})
    with pytest.raises(ValueError, match="timed out"):
        source._run_json(["gmail", "users", "threads", "get"], account=None, params={})


# --- Gmail methods ---------------------------------------------------------


def test_search_threads_paginates_and_caps(monkeypatch):
    pages = [
        {"threads": [{"id": "t1"}, {"id": "t2"}], "nextPageToken": "p2"},
        {"threads": [{"id": "t3"}, {"id": "t4"}], "nextPageToken": "p3"},
    ]
    calls = []

    def fake_run_json(self, args, *, account, params):
        calls.append((args, params))
        return pages[len(calls) - 1]

    monkeypatch.setattr(GwsSource, "_run_json", fake_run_json)
    monkeypatch.setattr(adapters.time, "sleep", lambda _s: None)
    source = GwsSource(credentials={})
    out = source.search_threads(account="a@x.com", query="in:sent", max_threads=3)

    assert [t["id"] for t in out] == ["t1", "t2", "t3"]  # capped at 3
    assert calls[0][0] == ["gmail", "users", "threads", "list"]
    assert calls[0][1]["q"] == "in:sent"
    assert calls[0][1]["userId"] == "me"
    assert calls[1][1]["pageToken"] == "p2"  # second page used the token


def test_search_threads_stops_when_no_token(monkeypatch):
    monkeypatch.setattr(GwsSource, "_run_json", lambda self, args, *, account, params: {"threads": [{"id": "t1"}]})
    source = GwsSource(credentials={})
    out = source.search_threads(account="a@x.com", query="q", max_threads=None)
    assert [t["id"] for t in out] == ["t1"]


def test_get_thread_returns_normalizer_compatible_payload(monkeypatch):
    raw_thread = {
        "id": "thread123",
        "messages": [
            {
                "id": "msg1",
                "threadId": "thread123",
                "labelIds": ["SENT"],
                "internalDate": "1716631200000",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "Me <me@example.com>"},
                        {"name": "Subject", "value": "Proposal"},
                    ],
                    "body": {"data": _b64url("Hello, this is the body.")},
                },
            }
        ],
    }
    monkeypatch.setattr(GwsSource, "_run_json", lambda self, args, *, account, params: raw_thread)
    source = GwsSource(credentials={})
    result = source.get_thread(account="a@x.com", thread_id="thread123")

    assert result["thread_id"] == "thread123"
    assert result["messages"][0]["id"] == "msg1"
    # The gws Gmail payload feeds the existing normalizer directly.
    from app.ingestion.gmail_threads import _message_body_text, _message_subject

    assert _message_body_text(result["messages"][0]) == "Hello, this is the body."
    assert _message_subject(result["messages"][0]) == "Proposal"


# --- Drive / Docs methods --------------------------------------------------


def test_drive_search_raw_query_passes_q_verbatim(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        GwsSource,
        "_run_json",
        lambda self, args, *, account, params: captured.update({"args": args, "params": params}) or {"files": []},
    )
    source = GwsSource(credentials={})
    source.drive_search(account="a@x.com", query="name contains 'spec'", max_docs=None, raw_query=True)
    assert captured["args"] == ["drive", "files", "list"]
    assert captured["params"]["q"] == "name contains 'spec'"


def test_drive_search_non_raw_builds_fulltext_docs_query(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        GwsSource,
        "_run_json",
        lambda self, args, *, account, params: captured.update({"params": params}) or {"files": [{"id": "d1"}]},
    )
    source = GwsSource(credentials={})
    out = source.drive_search(account="a@x.com", query="proposal", max_docs=5, raw_query=False)
    assert "fullText contains 'proposal'" in captured["params"]["q"]
    assert "application/vnd.google-apps.document" in captured["params"]["q"]
    assert out == [{"id": "d1"}]


def test_docs_info_and_cat_share_one_document_fetch(monkeypatch):
    document = {
        "documentId": "d1",
        "title": "My Doc",
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Body text\n"}}]}}]},
    }
    calls = {"n": 0}

    def fake_run_json(self, args, *, account, params):
        calls["n"] += 1
        assert args == ["docs", "documents", "get"]
        return document

    monkeypatch.setattr(GwsSource, "_run_json", fake_run_json)
    source = GwsSource(credentials={})

    info = source.docs_info(account="a@x.com", doc_id="d1")
    text = source.docs_cat(account="a@x.com", doc_id="d1", max_bytes=0, all_tabs=False)

    assert info == {"documentId": "d1", "title": "My Doc"}
    assert text == "Body text"
    assert calls["n"] == 1  # cached: documents.get fetched once for both


def test_docs_cat_truncates_to_max_bytes(monkeypatch):
    document = {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "abcdefghij"}}]}}]}}
    monkeypatch.setattr(GwsSource, "_run_json", lambda self, args, *, account, params: document)
    source = GwsSource(credentials={})
    assert source.docs_cat(account="a@x.com", doc_id="d1", max_bytes=4, all_tabs=False) == "abcd"


def test_drive_get_requests_metadata_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        GwsSource,
        "_run_json",
        lambda self, args, *, account, params: captured.update({"args": args, "params": params})
        or {"id": "d1", "name": "Doc", "webViewLink": "http://x", "createdTime": "2026-01-01T00:00:00Z"},
    )
    source = GwsSource(credentials={})
    out = source.drive_get(account="a@x.com", doc_id="d1")
    assert captured["args"] == ["drive", "files", "get"]
    assert captured["params"]["fileId"] == "d1"
    assert "webViewLink" in captured["params"]["fields"]
    assert out["name"] == "Doc"


def test_gws_source_satisfies_protocol():
    assert isinstance(GwsSource(credentials={}), GoogleWorkspaceSource)


# --- b161: gws identity binding (case-insensitive + refuse on configured miss) ---


def test_resolve_gws_credentials_is_case_insensitive():
    from app.ingestion.adapters import _resolve_gws_credentials_file

    m = {"Work@X.com": "/creds/work.json"}
    assert _resolve_gws_credentials_file("work@x.com", m) == "/creds/work.json"
    assert _resolve_gws_credentials_file("WORK@X.COM", m) == "/creds/work.json"


def test_resolve_gws_credentials_refuses_unmapped_on_configured_map():
    from app.ingestion.adapters import _resolve_gws_credentials_file

    with pytest.raises(ValueError, match="refusing to fall back"):
        _resolve_gws_credentials_file("other@x.com", {"work@x.com": "/creds/work.json"})


def test_resolve_gws_credentials_ambient_when_no_map():
    from app.ingestion.adapters import _resolve_gws_credentials_file

    assert _resolve_gws_credentials_file("a@x.com", {}) is None      # no map → ambient
    assert _resolve_gws_credentials_file(None, {"a@x.com": "/c"}) is None  # no account → ambient


def test_run_json_refuses_unmapped_account_on_configured_map(monkeypatch):
    source = GwsSource(credentials={"work@x.com": "/creds/work.json"})

    def _no_run(*a, **k):
        raise AssertionError("must not invoke gws for an unmapped account")

    monkeypatch.setattr(adapters.subprocess, "run", _no_run)
    with pytest.raises(ValueError, match="refusing to fall back"):
        source._run_json(["drive", "files", "list"], account="other@x.com", params={})
