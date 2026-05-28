"""Phase 2: gmail_write — gog backend implementation + error paths.

Tests mock ``subprocess.run`` so the gog CLI doesn't actually get invoked
— what we pin is the call shape and the error translation. The single
function ``_gog_create_draft`` is the only place to change if gog's
subcommand differs from what we assumed.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest


# --- backend dispatch ------------------------------------------------------


def test_unknown_backend_raises_value_error(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "wat")
    from app.ingestion.gmail_write import create_draft

    with pytest.raises(ValueError, match="unknown ingestion.google_backend"):
        create_draft(
            account="me@x.com", thread_id=None, to_email="them@y.com",
            subject="hi", body="b",
        )


def test_gws_backend_raises_not_implemented_with_phase_2_2_message(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")
    from app.ingestion.gmail_write import create_draft

    with pytest.raises(NotImplementedError, match="gws"):
        create_draft(
            account="me@x.com", thread_id="t", to_email="them@y.com",
            subject="hi", body="b",
        )


def test_native_backend_raises_not_implemented(monkeypatch):
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "native")
    from app.ingestion.gmail_write import create_draft

    with pytest.raises(NotImplementedError, match="gmail.compose"):
        create_draft(
            account="me@x.com", thread_id="t", to_email="them@y.com",
            subject="hi", body="b",
        )


# --- gog: call shape + success --------------------------------------------


def test_gog_creates_draft_and_extracts_id(monkeypatch):
    captured: dict = {}

    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "draft_abc", "message": {"id": "msg_xyz"}}),
            stderr="",
        )

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import create_draft

    result = create_draft(
        account="me@medicus.ai", thread_id="thr_42",
        to_email="alice@partner.com", subject="Re: pricing",
        body="Confirmed — pricing held.",
    )

    assert result.draft_id == "draft_abc"
    # Command-shape contract — if gog renames any of these we'll see it here.
    cmd = captured["cmd"]
    assert cmd[:4] == ["gog", "gmail", "drafts", "create"]
    assert "--account" in cmd and cmd[cmd.index("--account") + 1] == "me@medicus.ai"
    assert "--thread-id" in cmd and cmd[cmd.index("--thread-id") + 1] == "thr_42"
    assert "--json" in cmd and "--no-input" in cmd
    assert "--raw" in cmd
    # Decode the --raw payload and verify the RFC822 fields landed.
    raw_b64 = cmd[cmd.index("--raw") + 1]
    rfc = base64.urlsafe_b64decode(raw_b64).decode("utf-8")
    assert "To: alice@partner.com" in rfc
    assert "Subject: Re: pricing" in rfc
    assert "Confirmed — pricing held." in rfc


def test_gog_skips_thread_id_flag_when_none(monkeypatch):
    captured: dict = {}
    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d1"}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import create_draft
    create_draft(account="me@x.com", thread_id=None, to_email="t@y.com", subject="s", body="b")
    assert "--thread-id" not in captured["cmd"]


# --- gog: error paths ------------------------------------------------------


def test_gog_translates_nonzero_exit_to_gmail_write_error(monkeypatch):
    def _fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="gmail.compose scope not granted; reauth required",
        )
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gmail.compose scope not granted"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_file_not_found_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        raise FileNotFoundError("no such file: gog")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gog CLI not on PATH"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_malformed_json_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        return SimpleNamespace(returncode=0, stdout="this is not json", stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="non-JSON stdout"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_missing_id_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"message": {"foo": "bar"}}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="no draft id"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")
