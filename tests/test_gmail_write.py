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
    """Pins the gog call shape verified against gog 0.17.0.

    gog takes broken-out fields (--to / --subject / --body-file -) plus
    --reply-to-message-id for threading; NOT --raw / --thread-id.
    """
    captured: dict = {}

    def _fake_run(cmd, input, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["stdin"] = input
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "draft_abc", "message": {"id": "msg_xyz"}}),
            stderr="",
        )

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import create_draft

    result = create_draft(
        account="me@medicus.ai",
        reply_to_message_id="msg_42",
        to_email="alice@partner.com",
        subject="Re: pricing",
        body="Confirmed — pricing held.\nLet me know if you need a written quote.",
    )

    assert result.draft_id == "draft_abc"
    cmd = captured["cmd"]
    assert cmd[:4] == ["gog", "gmail", "drafts", "create"]
    assert cmd[cmd.index("--account") + 1] == "me@medicus.ai"
    assert cmd[cmd.index("--to") + 1] == "alice@partner.com"
    assert cmd[cmd.index("--subject") + 1] == "Re: pricing"
    assert cmd[cmd.index("--body-file") + 1] == "-"
    assert cmd[cmd.index("--reply-to-message-id") + 1] == "msg_42"
    assert "--json" in cmd and "--no-input" in cmd
    # Body goes on stdin (so multi-line / shell-hazardous content passes through).
    assert captured["stdin"] == "Confirmed — pricing held.\nLet me know if you need a written quote."
    # We don't use --raw or --thread-id anymore — gog wants broken-out fields.
    assert "--raw" not in cmd
    assert "--thread-id" not in cmd


def test_gog_skips_reply_to_flag_when_none(monkeypatch):
    captured: dict = {}
    def _fake_run(cmd, input, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d1"}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import create_draft
    create_draft(account="me@x.com", reply_to_message_id=None, to_email="t@y.com", subject="s", body="b")
    assert "--reply-to-message-id" not in captured["cmd"]


# --- gog: error paths ------------------------------------------------------


def test_gog_translates_nonzero_exit_to_gmail_write_error(monkeypatch):
    def _fake_run(cmd, input, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="gmail.compose scope not granted; reauth required",
        )
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gmail.compose scope not granted"):
        create_draft(account="me@x.com", reply_to_message_id="m", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_file_not_found_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        raise FileNotFoundError("no such file: gog")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="gog CLI not on PATH"):
        create_draft(account="me@x.com", reply_to_message_id="m", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_malformed_json_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        return SimpleNamespace(returncode=0, stdout="this is not json", stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="non-JSON stdout"):
        create_draft(account="me@x.com", reply_to_message_id="m", to_email="them@y.com", subject="s", body="b")


def test_gog_translates_missing_id_to_gmail_write_error(monkeypatch):
    def _fake_run(*a, **k):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"message": {"foo": "bar"}}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gog")

    from app.ingestion.gmail_write import GmailWriteError, create_draft

    with pytest.raises(GmailWriteError, match="no draft id"):
        create_draft(account="me@x.com", reply_to_message_id="m", to_email="them@y.com", subject="s", body="b")


# --- gws: call shape + success --------------------------------------------


def test_gws_creates_draft_and_extracts_id(monkeypatch):
    """Pin the gws call shape — verified via ``gws schema gmail.users.drafts.create``.

    gws argv is <service> <resource> <subresource> <method> with URL params
    via --params JSON and the request body via --json JSON. threadId lives
    INSIDE the request body's message object, not as a separate flag.
    """
    captured: dict = {}

    def _fake_run(cmd, capture_output, text, timeout, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "draft_gws_99", "message": {"id": "msg_gws_42", "threadId": "thr_99"}}),
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
    # gws path is gmail.users.drafts.create — note "users" subresource.
    assert cmd[:5] == ["gws", "gmail", "users", "drafts", "create"]

    # --params carries URL/query params as JSON; userId lives here.
    params = json.loads(cmd[cmd.index("--params") + 1])
    assert params == {"userId": "me@medicus.ai"}

    # --json carries the request body; threadId is INSIDE the message dict.
    body_arg = json.loads(cmd[cmd.index("--json") + 1])
    assert body_arg["message"]["threadId"] == "thr_99"
    rfc = base64.urlsafe_b64decode(body_arg["message"]["raw"]).decode("utf-8")
    assert "To: bob@partner.com" in rfc
    assert "Subject: Re: invoice" in rfc
    assert "Paid — confirmation attached." in rfc


def test_gws_uses_per_account_credentials_file(monkeypatch):
    """Multi-account safety: the write must select the per-account credentials
    file (GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE) so a draft for account B isn't
    written to account A's mailbox. Mirrors the ingestion read path."""
    captured: dict = {}

    def _fake_run(cmd, capture_output, text, timeout, env=None):
        captured["env"] = env
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d_b"}), stderr="")

    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")
    # Map account → credentials file (what ingestion.gws_credentials provides).
    monkeypatch.setattr(
        "app.ingestion.adapters._load_gws_credentials",
        lambda: {"b@medicus.ai": "/creds/b.json", "a@medicus.ai": "/creds/a.json"},
    )

    from app.ingestion.gmail_write import create_draft

    create_draft(account="b@medicus.ai", thread_id="t", to_email="x@y.com", subject="s", body="b")
    assert captured["env"]["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] == "/creds/b.json"


def test_gws_omits_thread_id_in_body_when_none(monkeypatch):
    """No threadId field when starting a brand-new thread."""
    captured: dict = {}
    def _fake_run(cmd, capture_output, text, timeout, env=None):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "d1"}), stderr="")
    monkeypatch.setattr("app.ingestion.gmail_write.subprocess.run", _fake_run)
    monkeypatch.setattr("app.core.config.get_ingestion_google_backend", lambda: "gws")

    from app.ingestion.gmail_write import create_draft
    create_draft(account="me@x.com", thread_id=None, to_email="t@y.com", subject="s", body="b")
    body_arg = json.loads(captured["cmd"][captured["cmd"].index("--json") + 1])
    assert "threadId" not in body_arg["message"]


# --- gws: error paths ------------------------------------------------------


def test_gws_translates_nonzero_exit_to_gmail_write_error(monkeypatch):
    def _fake_run(cmd, capture_output, text, timeout, env=None):
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
