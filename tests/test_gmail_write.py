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


# gws backend is implemented in Phase 2.2 — its dedicated suite lives below.


# native backend implemented in Phase 2.3 — dedicated suite at the tail of the file.


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


# --- gws: call shape + success --------------------------------------------


def test_gws_creates_draft_and_extracts_id(monkeypatch):
    """Pin the gws call shape — every flag asserted below is a single place
    to update if your local gws uses different syntax. The body is the same
    base64url-encoded RFC822 we send to gog (Gmail API shape)."""
    captured: dict = {}

    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "draft_gws_99", "message": {"id": "msg_gws_42"}}),
            stderr="",
        )

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import create_draft

    result = create_draft(
        account="me@medicus.ai", thread_id="thr_99",
        to_email="bob@partner.com", subject="Re: invoice",
        body="Paid — confirmation attached.",
    )

    assert result.draft_id == "draft_gws_99"
    cmd = captured["cmd"]
    assert cmd[:4] == ["gws", "gmail", "drafts", "create"]
    # gws conventions: --user (not --account) + --threadId (camelCase).
    assert "--user" in cmd and cmd[cmd.index("--user") + 1] == "me@medicus.ai"
    assert "--threadId" in cmd and cmd[cmd.index("--threadId") + 1] == "thr_99"
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"
    assert "--raw" in cmd
    raw_b64 = cmd[cmd.index("--raw") + 1]
    rfc = base64.urlsafe_b64decode(raw_b64).decode("utf-8")
    assert "To: bob@partner.com" in rfc
    assert "Subject: Re: invoice" in rfc
    assert "Paid — confirmation attached." in rfc


def test_gws_skips_thread_id_flag_when_none(monkeypatch):
    captured: dict = {}
    def _fake_run(cmd, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d1"}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import create_draft
    create_draft(account="me@x.com", thread_id=None, to_email="t@y.com", subject="s", body="b")
    assert "--threadId" not in captured["cmd"]


# --- gws: error paths ------------------------------------------------------


def test_gws_translates_nonzero_exit_to_gmail_write_error(monkeypatch):
    def _fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="permission denied: gmail.compose scope missing",
        )
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gmail.compose scope missing"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_gws_translates_file_not_found_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        raise FileNotFoundError("gws not installed")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gws CLI not on PATH"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_gws_translates_missing_id_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"messageOnly": True}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="no draft id"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


# --- native: call shape + success -----------------------------------------


class _FakeDraftsResource:
    """Mimics google-api-python-client's chain: service.users().drafts().create(...).execute()."""

    def __init__(self, capture: dict, result: dict | None = None, raise_exc: Exception | None = None):
        self._capture = capture
        self._result = result if result is not None else {"id": "draft_native_77", "message": {"id": "m77"}}
        self._raise_exc = raise_exc

    def create(self, userId, body):
        self._capture["userId"] = userId
        self._capture["body"] = body
        return self

    def execute(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


def _install_fake_native_service(monkeypatch, *, capture=None, result=None, raise_exc=None):
    """Replace _native_gmail_service so tests don't hit OAuth / network."""
    capture = capture if capture is not None else {}

    class _FakeUsers:
        def drafts(self):
            return _FakeDraftsResource(capture, result=result, raise_exc=raise_exc)

    class _FakeService:
        def users(self):
            return _FakeUsers()

    monkeypatch.setattr(
        "app.ingestion.gmail_write._native_gmail_service",
        lambda *, account: _FakeService(),
    )
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "native")
    return capture


def test_native_creates_draft_and_extracts_id(monkeypatch):
    capture = _install_fake_native_service(monkeypatch)
    from app.ingestion.gmail_write import create_draft

    result = create_draft(
        account="me@medicus.ai", thread_id="thr_native_42",
        to_email="ops@partner.com", subject="Re: outage post-mortem",
        body="Acknowledged — post-mortem attached.",
    )

    assert result.draft_id == "draft_native_77"
    # Call-shape contract for the Gmail REST API.
    assert capture["userId"] == "me"
    msg = capture["body"]["message"]
    assert msg["threadId"] == "thr_native_42"
    rfc = base64.urlsafe_b64decode(msg["raw"]).decode("utf-8")
    assert "To: ops@partner.com" in rfc
    assert "Subject: Re: outage post-mortem" in rfc
    assert "Acknowledged — post-mortem attached." in rfc


def test_native_skips_thread_id_field_when_none(monkeypatch):
    capture = _install_fake_native_service(monkeypatch)
    from app.ingestion.gmail_write import create_draft

    create_draft(account="me@x.com", thread_id=None, to_email="t@y.com", subject="s", body="b")
    msg = capture["body"]["message"]
    assert "threadId" not in msg


# --- native: error paths ---------------------------------------------------


def test_native_translates_403_to_reauth_hint(monkeypatch):
    """Missing gmail.compose scope is the most common failure mode for
    users who only authorized for read-only ingestion; the error must
    point at the re-auth flow."""

    class _Resp:
        status = 403

    exc = type("HttpError", (Exception,), {})()
    exc.resp = _Resp()

    _install_fake_native_service(monkeypatch, raise_exc=exc)
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gmail.compose OAuth scope"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_native_translates_401_to_reauth_hint(monkeypatch):
    class _Resp:
        status = 401

    exc = type("HttpError", (Exception,), {})()
    exc.resp = _Resp()

    _install_fake_native_service(monkeypatch, raise_exc=exc)
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gmail.compose OAuth scope"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_native_translates_generic_exception_with_context(monkeypatch):
    _install_fake_native_service(monkeypatch, raise_exc=RuntimeError("network unreachable"))
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="network unreachable"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_native_translates_missing_id_to_gmail_write_error(monkeypatch):
    _install_fake_native_service(monkeypatch, result={"message": {"id": "x"}})  # no top-level id
    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="no id"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")


def test_native_translates_credentials_runtime_error_to_gmail_write_error(monkeypatch):
    """If credentials are missing / can't load, the helper raises RuntimeError;
    we surface it as a GmailWriteError so the agent route returns 502 instead
    of crashing into a 500."""

    def _raise(*, account):
        raise RuntimeError("No stored Google credentials for me@x.com at /var/google_tokens/me@x.com.json. Authorize first via `youos setup`.")

    monkeypatch.setattr("app.ingestion.gmail_write._native_gmail_service", _raise)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "native")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="No stored Google credentials"):
        create_draft(account="me@x.com", thread_id="t", to_email="them@y.com", subject="s", body="b")
