"""Tests for the native Google-API ingestion backend (PR 3).

Live OAuth + Google API can't run in the container, so these pin: the
absence-of-extra error path (deterministic — the `youos[google]` extra isn't
installed in CI), token-path resolution, and the request/response logic via a
mocked service object. Response *shaping* is shared with the gws backend
(`_normalize_gog_thread_payload`, `_docs_document_to_text`, `_build_drive_query`)
and covered there too.
"""

from __future__ import annotations

import base64
import importlib.util
import types
from pathlib import Path

import pytest

from app.ingestion.adapters import GoogleWorkspaceSource, NativeSource

_GOOGLE_INSTALLED = importlib.util.find_spec("googleapiclient") is not None
_OAUTHLIB_INSTALLED = importlib.util.find_spec("google_auth_oauthlib") is not None


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


# --- tiny fake Google service clients --------------------------------------


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Collection:
    """Records (method, kwargs); returns queued results FIFO (or a fixed one)."""

    def __init__(self, results: dict, log: list):
        self._results = results
        self._log = log

    def __getattr__(self, name):
        def call(**kw):
            self._log.append((name, kw))
            val = self._results[name]
            return _Req(val.pop(0) if isinstance(val, list) else val)

        return call


def _gmail_service(threads: _Collection):
    users = types.SimpleNamespace(threads=lambda: threads)
    return types.SimpleNamespace(users=lambda: users)


def _drive_service(files: _Collection):
    return types.SimpleNamespace(files=lambda: files)


def _docs_service(documents: _Collection):
    return types.SimpleNamespace(documents=lambda: documents)


def _patch_service(monkeypatch, *, gmail=None, drive=None, docs=None):
    def fake(self, account, api, version):
        return {"gmail": gmail, "drive": drive, "docs": docs}[api]

    monkeypatch.setattr(NativeSource, "_service", fake)


# --- construction / protocol -----------------------------------------------


def test_native_source_satisfies_protocol():
    assert isinstance(NativeSource(), GoogleWorkspaceSource)


def test_token_path_uses_override_dir_and_sanitizes_account():
    source = NativeSource(token_dir="/tmp/toks")
    assert source._token_path("a@x.com") == Path("/tmp/toks/a@x.com.json")
    # Slashes in an account can't escape the token dir.
    assert source._token_path("a/b").parent == Path("/tmp/toks")


# --- absence-of-extra path (deterministic when extra not installed) --------


@pytest.mark.skipif(_GOOGLE_INSTALLED, reason="google extra installed; absence path not exercised")
def test_methods_require_google_extra_when_absent():
    source = NativeSource(token_dir="/tmp/toks")
    with pytest.raises(RuntimeError, match=r"youos\[google\]"):
        source.search_threads(account="a@x.com", query="q", max_threads=1)


@pytest.mark.skipif(_OAUTHLIB_INSTALLED, reason="oauthlib installed; absence path not exercised")
def test_authorize_requires_google_extra_when_absent():
    source = NativeSource(token_dir="/tmp/toks", client_secrets_path="/tmp/cs.json")
    with pytest.raises(RuntimeError, match=r"youos\[google\]"):
        source.authorize_account("a@x.com")


# --- Gmail -----------------------------------------------------------------


def test_search_threads_paginates_and_caps(monkeypatch):
    log: list = []
    threads = _Collection(
        {
            "list": [
                {"threads": [{"id": "t1"}, {"id": "t2"}], "nextPageToken": "p2"},
                {"threads": [{"id": "t3"}, {"id": "t4"}], "nextPageToken": "p3"},
            ]
        },
        log,
    )
    _patch_service(monkeypatch, gmail=_gmail_service(threads))
    source = NativeSource()
    out = source.search_threads(account="a@x.com", query="in:sent", max_threads=3)

    assert [t["id"] for t in out] == ["t1", "t2", "t3"]
    assert log[0][0] == "list"
    assert log[0][1]["userId"] == "me"
    assert log[0][1]["q"] == "in:sent"
    assert log[0][1]["pageToken"] is None
    assert log[1][1]["pageToken"] == "p2"


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
                    "body": {"data": _b64url("Hello from native.")},
                },
            }
        ],
    }
    threads = _Collection({"get": raw_thread}, [])
    _patch_service(monkeypatch, gmail=_gmail_service(threads))
    source = NativeSource()
    result = source.get_thread(account="a@x.com", thread_id="thread123")

    assert result["thread_id"] == "thread123"
    from app.ingestion.gmail_threads import _message_body_text, _message_subject

    assert _message_body_text(result["messages"][0]) == "Hello from native."
    assert _message_subject(result["messages"][0]) == "Proposal"


# --- Drive / Docs ----------------------------------------------------------


def test_drive_search_non_raw_builds_fulltext_docs_query(monkeypatch):
    log: list = []
    files = _Collection({"list": {"files": [{"id": "d1"}]}}, log)
    _patch_service(monkeypatch, drive=_drive_service(files))
    out = NativeSource().drive_search(account="a@x.com", query="proposal", max_docs=5, raw_query=False)
    assert out == [{"id": "d1"}]
    q = log[0][1]["q"]
    assert "fullText contains 'proposal'" in q
    assert "application/vnd.google-apps.document" in q


def test_drive_search_raw_query_passes_q_verbatim(monkeypatch):
    log: list = []
    files = _Collection({"list": {"files": []}}, log)
    _patch_service(monkeypatch, drive=_drive_service(files))
    NativeSource().drive_search(account="a@x.com", query="name contains 'x'", max_docs=None, raw_query=True)
    assert log[0][1]["q"] == "name contains 'x'"


def test_docs_info_and_cat_share_one_document_fetch(monkeypatch):
    document = {
        "documentId": "d1",
        "title": "My Doc",
        "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Body text\n"}}]}}]},
    }
    log: list = []
    documents = _Collection({"get": document}, log)
    _patch_service(monkeypatch, docs=_docs_service(documents))
    source = NativeSource()

    info = source.docs_info(account="a@x.com", doc_id="d1")
    text = source.docs_cat(account="a@x.com", doc_id="d1", max_bytes=0, all_tabs=False)

    assert info == {"documentId": "d1", "title": "My Doc"}
    assert text == "Body text"
    assert sum(1 for entry in log if entry[0] == "get") == 1  # cached across both


def test_docs_cat_truncates_to_max_bytes(monkeypatch):
    document = {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "abcdefghij"}}]}}]}}
    documents = _Collection({"get": document}, [])
    _patch_service(monkeypatch, docs=_docs_service(documents))
    assert NativeSource().docs_cat(account="a@x.com", doc_id="d1", max_bytes=4, all_tabs=False) == "abcd"


def test_drive_get_requests_metadata_fields(monkeypatch):
    log: list = []
    files = _Collection({"get": {"id": "d1", "name": "Doc", "webViewLink": "http://x"}}, log)
    _patch_service(monkeypatch, drive=_drive_service(files))
    out = NativeSource().drive_get(account="a@x.com", doc_id="d1")
    assert out["name"] == "Doc"
    assert log[0][1]["fileId"] == "d1"
    assert "webViewLink" in log[0][1]["fields"]


def test_native_oauth_token_file_written_owner_only(tmp_path, monkeypatch):
    """b142: the refreshed OAuth token (refresh_token + client_secret) must be
    written 0o600, not world-readable (the b134 secret-perms sibling that was
    missed). Injects a minimal fake google namespace so the lazy imports in
    _load_credentials resolve without the real library."""
    import os
    import stat
    import sys
    import types

    class _Creds:
        valid = False
        expired = True
        refresh_token = "rt"

        def refresh(self, _req):
            pass

        def to_json(self):
            return '{"refresh_token": "rt", "client_secret": "cs"}'

    cred_mod = types.ModuleType("google.oauth2.credentials")
    cred_mod.Credentials = type(
        "Credentials", (), {"from_authorized_user_file": staticmethod(lambda path, scopes: _Creds())}
    )
    req_mod = types.ModuleType("google.auth.transport.requests")
    req_mod.Request = lambda: None
    for name, mod in [
        ("google", types.ModuleType("google")),
        ("google.oauth2", types.ModuleType("google.oauth2")),
        ("google.oauth2.credentials", cred_mod),
        ("google.auth", types.ModuleType("google.auth")),
        ("google.auth.transport", types.ModuleType("google.auth.transport")),
        ("google.auth.transport.requests", req_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    src = NativeSource(token_dir=str(tmp_path))
    tok = tmp_path / "me@x.com.json"
    tok.write_text("{}")
    os.chmod(tok, 0o644)  # start world-readable
    src._load_credentials("me@x.com")
    assert oct(stat.S_IMODE(os.stat(tok).st_mode)) == "0o600"
