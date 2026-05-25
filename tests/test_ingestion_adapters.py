"""Tests for the pluggable Google Workspace ingestion backend (PR 1).

These pin the adapter seam introduced to decouple ingestion from the OpenClaw
`gog` CLI: the default backend stays `gog` (zero behavior change), the factory
honors `ingestion.google_backend`, the reserved `gws`/`native` backends fail
loud-but-clear, and `GogSource` faithfully delegates to the existing `_gog_*`
helpers.
"""

from __future__ import annotations

import pytest

from app.core.config import get_ingestion_google_backend
from app.ingestion import adapters
from app.ingestion.adapters import GogSource, GoogleWorkspaceSource, GwsSource, NativeSource, get_google_source

# --- config accessor -------------------------------------------------------


def test_backend_defaults_to_gog_when_unset():
    assert get_ingestion_google_backend({}) == "gog"
    assert get_ingestion_google_backend({"ingestion": {}}) == "gog"


def test_backend_reads_configured_value_case_insensitively():
    assert get_ingestion_google_backend({"ingestion": {"google_backend": "gws"}}) == "gws"
    assert get_ingestion_google_backend({"ingestion": {"google_backend": "  Native "}}) == "native"


def test_unknown_backend_degrades_to_gog():
    # A typo must not break ingestion at config-read time — degrade to the
    # always-available default. (The doctor is responsible for flagging it.)
    assert get_ingestion_google_backend({"ingestion": {"google_backend": "goog"}}) == "gog"
    assert get_ingestion_google_backend({"ingestion": {"google_backend": ""}}) == "gog"


# --- factory ---------------------------------------------------------------


def test_default_source_is_gog(monkeypatch):
    monkeypatch.setattr(adapters, "get_ingestion_google_backend", lambda: "gog")
    source = get_google_source()
    assert isinstance(source, GogSource)
    assert source.name == "gog"


def test_explicit_backend_override_wins_over_config(monkeypatch):
    # config says gog, explicit arg says gws -> arg wins.
    monkeypatch.setattr(adapters, "get_ingestion_google_backend", lambda: "gog")
    assert isinstance(get_google_source(backend="gws"), GwsSource)


def test_config_drives_factory(monkeypatch):
    monkeypatch.setattr(adapters, "get_ingestion_google_backend", lambda: "gws")
    assert isinstance(get_google_source(), GwsSource)


def test_gws_backend_returns_gws_source():
    source = get_google_source(backend="gws")
    assert isinstance(source, GwsSource)
    assert source.name == "gws"


def test_native_backend_returns_native_source():
    source = get_google_source(backend="native")
    assert isinstance(source, NativeSource)
    assert source.name == "native"


def test_unknown_explicit_backend_raises_valueerror():
    with pytest.raises(ValueError, match="Unknown ingestion.google_backend"):
        get_google_source(backend="nope")


def test_gog_source_satisfies_protocol():
    assert isinstance(GogSource(), GoogleWorkspaceSource)


# --- GogSource delegation --------------------------------------------------


def test_gog_source_delegates_gmail_calls(monkeypatch):
    calls: dict[str, dict] = {}

    def fake_search(*, account, query, max_threads):
        calls["search"] = {"account": account, "query": query, "max_threads": max_threads}
        return [{"threadId": "t1"}]

    def fake_get(*, account, thread_id):
        calls["get"] = {"account": account, "thread_id": thread_id}
        return {"thread_id": thread_id}

    monkeypatch.setattr("app.ingestion.gmail_threads._gog_search_threads", fake_search)
    monkeypatch.setattr("app.ingestion.gmail_threads._gog_get_thread", fake_get)

    source = GogSource()
    assert source.search_threads(account="a@x.com", query="in:sent", max_threads=7) == [{"threadId": "t1"}]
    assert source.get_thread(account="a@x.com", thread_id="t1") == {"thread_id": "t1"}
    assert calls["search"] == {"account": "a@x.com", "query": "in:sent", "max_threads": 7}
    assert calls["get"] == {"account": "a@x.com", "thread_id": "t1"}


def test_gog_source_delegates_docs_calls(monkeypatch):
    calls: dict[str, dict] = {}

    def fake_drive_search(*, account, query, max_docs, raw_query):
        calls["drive_search"] = {"account": account, "query": query, "max_docs": max_docs, "raw_query": raw_query}
        return [{"id": "d1"}]

    def fake_docs_info(*, account, doc_id):
        calls["docs_info"] = {"account": account, "doc_id": doc_id}
        return {"id": doc_id}

    def fake_drive_get(*, account, doc_id):
        calls["drive_get"] = {"account": account, "doc_id": doc_id}
        return {"id": doc_id}

    def fake_docs_cat(*, account, doc_id, max_bytes, all_tabs):
        calls["docs_cat"] = {"account": account, "doc_id": doc_id, "max_bytes": max_bytes, "all_tabs": all_tabs}
        return "doc body"

    monkeypatch.setattr("app.ingestion.google_docs._gog_drive_search", fake_drive_search)
    monkeypatch.setattr("app.ingestion.google_docs._gog_docs_info", fake_docs_info)
    monkeypatch.setattr("app.ingestion.google_docs._gog_drive_get", fake_drive_get)
    monkeypatch.setattr("app.ingestion.google_docs._gog_docs_cat", fake_docs_cat)

    source = GogSource()
    assert source.drive_search(account="a@x.com", query="proposal", max_docs=3, raw_query=False) == [{"id": "d1"}]
    assert source.docs_info(account="a@x.com", doc_id="d1") == {"id": "d1"}
    assert source.drive_get(account="a@x.com", doc_id="d1") == {"id": "d1"}
    assert source.docs_cat(account="a@x.com", doc_id="d1", max_bytes=1000, all_tabs=True) == "doc body"
    assert calls["drive_search"]["raw_query"] is False
    assert calls["docs_cat"] == {"account": "a@x.com", "doc_id": "d1", "max_bytes": 1000, "all_tabs": True}
