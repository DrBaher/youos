"""Gmail-write capability for Phase 2 of the agent loop.

Currently exposes ``create_draft(...)`` — pushes an agent-generated draft
into the user's Gmail Drafts folder as a real reply on the original thread
(so the user finishes-and-sends from Gmail). **Never sends.** The agent
loop's "Mark sent" path remains a separate signal for "I sent it manually
elsewhere."

Backend dispatch follows the existing ``ingestion.google_backend`` setting
— all three backends implemented:

* ``gog`` (Phase 2.1) — shells out to the ``gog`` CLI
* ``gws`` (Phase 2.2) — shells out to Google's first-party ``gws`` CLI
* ``native`` (Phase 2.3) — direct googleapiclient call; needs the
  ``gmail.compose`` scope on the stored token (re-auth via ``youos setup``)

If you need to fix the exact ``gog`` or ``gws`` invocation, the commands
live in ``_gog_create_draft`` / ``_gws_create_draft`` — the only places to
change. The abstraction + the caller are stable across all three.
"""

from __future__ import annotations

import base64
import email.message
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.secure_io import write_secret
from app.ingestion.adapters import require_account_argv

logger = logging.getLogger(__name__)


def _clean_stderr(text: str | None, *, limit: int = 200) -> str:
    """Strip control chars (incl. newlines) from gog/gws stderr before embedding
    it in an exception/log message. The stderr echoes attacker-influenced values
    (a crafted label, recipient, message metadata), so a raw newline could forge
    a fake log line (CRLF) — these messages are %s-logged with the default
    formatter — and a terminal escape could reach a TTY."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", (text or "")).strip()[:limit]


def _text_to_html(text: str) -> str:
    """Minimal plain-text → HTML: escape, preserve line breaks. Enough to wrap a
    generated reply so a fetched HTML signature renders cleanly alongside it."""
    import html as _html

    return _html.escape(text or "").replace("\n", "<br>\n")


def _compose_html_with_signature(body: str, signature_html: str) -> str:
    """The reply body (as HTML) + a blank line + the user's Gmail signature."""
    return f"{_text_to_html(body)}<br><br>{signature_html}"


def get_signature(*, account: str, backend: str | None = None) -> str:
    """Best-effort fetch of the account's Gmail send-as signature (HTML).

    The Gmail API does NOT append the user's signature to API-created drafts
    (only the web composer does), so to honor "always include my signature" we
    fetch it and add it ourselves. Returns '' on any failure or when the backend
    can't provide one — the caller then pushes without a signature rather than
    failing the push.
    """
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    try:
        if name == "gog":
            return _gog_get_signature(account=account)
        if name == "native":
            return _native_get_signature(account=account)
    except Exception:
        logger.debug("signature fetch failed (account=%s backend=%s)", account, name, exc_info=True)
    return ""


def _pick_signature(items: Any, account: str) -> str:
    """From a list of sendAs resources, the signature of the one matching
    ``account`` (preferred) or the default/primary alias."""
    acct = (account or "").lower()
    fallback = ""
    for s in items or []:
        if not isinstance(s, dict):
            continue
        sig = s.get("signature") or ""
        if not sig:
            continue
        if (s.get("sendAsEmail") or "").lower() == acct:
            return sig
        if (s.get("isDefault") or s.get("isPrimary")) and not fallback:
            fallback = sig
    return fallback


def _gog_get_signature(*, account: str) -> str:
    """``gog gmail settings sendas list`` → signature HTML (verified gog 0.17.0)."""
    cmd = ["gog", "gmail", "settings", "sendas", "list", "--account", account, "--json", "--no-input"]
    require_account_argv(cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        return ""
    data = json.loads(result.stdout or "[]")
    items = data if isinstance(data, list) else (
        data.get("sendAs") or data.get("result") or data.get("items") or []
    )
    return _pick_signature(items, account)


def _native_get_signature(*, account: str) -> str:
    service = _native_gmail_service(account=account)
    resp = service.users().settings().sendAs().list(userId="me").execute()
    return _pick_signature(resp.get("sendAs", []), account)


class GmailWriteError(RuntimeError):
    """Raised when a backend can't create the draft. Caller (the agent
    route) translates to an HTTP error with a useful message."""


@dataclass
class GmailDraftResult:
    """Outcome of a successful draft creation. ``draft_id`` is Gmail's id
    for the new draft (the value we persist on the row); ``raw_response``
    is the backend's full payload, kept for debugging."""

    draft_id: str
    raw_response: dict[str, Any]


@dataclass
class GmailSendResult:
    """Outcome of a successful draft SEND. ``message_id`` is the Gmail id of
    the sent message (distinct from the draft id); ``raw_response`` is the
    backend's full payload."""

    message_id: str
    raw_response: dict[str, Any]


@dataclass
class GmailForwardResult:
    """Outcome of a successful FORWARD (an outbound send). ``message_id`` is the
    Gmail id of the newly-sent forwarded message; ``to`` echoes the recipients;
    ``raw_response`` is the backend's full payload."""

    message_id: str
    to: str
    raw_response: dict[str, Any]


def create_draft(
    *,
    account: str,
    thread_id: str | None = None,
    reply_to_message_id: str | None = None,
    to_email: str,
    subject: str,
    body: str,
    cc: str | None = None,
    signature_html: str | None = None,
    attachments: list[str] | None = None,
    backend: str | None = None,
) -> GmailDraftResult:
    """Create a Gmail draft addressed to ``to_email`` on the user's ``account``.

    Threading: pass ``reply_to_message_id`` (the Gmail message ID of the
    inbound the draft replies to). The gog backend (the only one with a
    verified CLI shape) sets In-Reply-To / References / threadId from this
    id; gws/native fall back to ``thread_id`` if given.

    ``attachments`` (b233) are local file paths attached to the draft —
    supported on the gog backend only (verified ``--attach`` flag, gog 0.22.0);
    requesting them on gws/native raises ``GmailWriteError`` rather than
    silently creating a draft whose body promises a file that isn't there.

    ``backend`` overrides the configured ``ingestion.google_backend``;
    leave None to use the default.
    """
    from app.core.config import get_ingestion_google_backend

    # When a signature is supplied the draft is sent as HTML (body + signature),
    # so the user's formatted Gmail signature renders. Otherwise it stays plain.
    body_html = _compose_html_with_signature(body, signature_html) if signature_html else None

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if attachments and name != "gog":
        raise GmailWriteError(
            f"attachments are only supported on the gog backend (configured: {name!r})"
        )
    if name == "gog":
        return _gog_create_draft(
            account=account, reply_to_message_id=reply_to_message_id,
            to_email=to_email, subject=subject, body=body, cc=cc, body_html=body_html,
            attachments=attachments,
        )
    if name == "gws":
        return _gws_create_draft(
            account=account, thread_id=thread_id,
            to_email=to_email, subject=subject, body=body, cc=cc, html=body_html,
        )
    if name == "native":
        return _native_create_draft(
            account=account, thread_id=thread_id,
            to_email=to_email, subject=subject, body=body, cc=cc, html=body_html,
        )
    raise ValueError(f"unknown ingestion.google_backend: {name!r}")


def send_draft(
    *,
    account: str,
    draft_id: str,
    dry_run: bool = False,
    backend: str | None = None,
) -> GmailSendResult:
    """Send an EXISTING Gmail draft by id — the actual outbound action.

    Sends the exact draft already in Gmail (no body re-marshaling, so what the
    user reviewed is what goes out). ``dry_run`` passes the backend's own
    no-change flag so the call exercises the real CLI path without sending.

    This is the only function in YouOS that can send mail. Its callers gate it
    behind explicit, default-off flags + a kill-switch; it performs no gating
    itself. Only the ``gog`` backend has a verified send shape today.
    """
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name == "gog":
        return _gog_send_draft(account=account, draft_id=draft_id, dry_run=dry_run)
    raise NotImplementedError(
        f"send_draft is only implemented for the gog backend (got {name!r})"
    )


def _gog_send_draft(*, account: str, draft_id: str, dry_run: bool) -> GmailSendResult:
    """Verified ``gog gmail drafts send <draftId>`` invocation (gog 0.17.0).

    Shape (confirmed via ``gog gmail drafts send --help``):
        gog gmail drafts send <draftId> --account <email> --json --no-input --force
    ``--force`` skips the interactive confirmation (we're non-interactive);
    ``--no-input`` makes a missing confirmation fail rather than hang;
    ``--dry-run`` (when requested) makes gog print the intended action and exit
    0 without sending. On a real send the Google API returns the sent Message
    resource ``{ "id": "...", "threadId": "...", ... }``.
    """
    cmd: list[str] = [
        "gog", "gmail", "drafts", "send", draft_id,
        "--account", account,
        "--json", "--no-input", "--force",
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        require_account_argv(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH — install via Homebrew or set up the native backend") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gog gmail drafts send timed out (30s)") from exc

    if result.returncode != 0:
        stderr = _clean_stderr(result.stderr)
        raise GmailWriteError(f"gog send returned exit {result.returncode}: {stderr or 'no stderr'}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog send returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    # dry-run may return an empty/intent payload; tolerate a missing id there.
    message_id = str(payload.get("id") or payload.get("messageId") or "")
    if not message_id and not dry_run:
        raise GmailWriteError(f"gog send returned no message id; payload={payload!r}")

    logger.info("sent gmail draft %s for account=%s (dry_run=%s)", draft_id, account, dry_run)
    return GmailSendResult(message_id=message_id, raw_response=payload)


def forward_message(
    *,
    account: str,
    message_id: str,
    to: str,
    note: str | None = None,
    backend: str | None = None,
) -> GmailForwardResult:
    """Forward an existing inbound message to new recipients — an OUTBOUND send.

    This crosses the never-send boundary, so callers MUST gate it behind the
    send frontier (``agent.send.enabled`` + the outbound kill-switch) plus the
    dedicated ``agent.actions.allow_forward`` opt-in — exactly as ``send_draft``
    is gated. It performs no gating itself. Original attachments are included by
    default (Gmail forward semantics). Only the ``gog`` backend has a verified
    forward shape today.
    """
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name == "gog":
        return _gog_forward(account=account, message_id=message_id, to=to, note=note)
    raise NotImplementedError(
        f"forward_message is only implemented for the gog backend (got {name!r})"
    )


def _gog_forward(*, account: str, message_id: str, to: str, note: str | None) -> GmailForwardResult:
    """Verified ``gog gmail forward --to=<addr> <messageId>`` invocation (gog 0.17.0).

    Shape (confirmed via ``gog gmail forward --help``):
        gog gmail forward <messageId> --to <addr> --account <email> --json --no-input
    ``--to`` is required (comma-separated); original attachments are included by
    default. On success the Google API returns the sent Message resource
    ``{"id": "...", ...}``.
    """
    cmd: list[str] = [
        "gog", "gmail", "forward", message_id,
        "--to", to,
        "--account", account,
        "--json", "--no-input",
    ]
    if note:
        cmd += ["--note", note]

    try:
        require_account_argv(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH — install via Homebrew or set up the native backend") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gog gmail forward timed out (30s)") from exc

    if result.returncode != 0:
        stderr = _clean_stderr(result.stderr)
        raise GmailWriteError(f"gog forward returned exit {result.returncode}: {stderr or 'no stderr'}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog forward returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    sent_id = str(payload.get("id") or payload.get("messageId") or "")
    logger.info("forwarded gmail message %s to %s for account=%s", message_id, to, account)
    return GmailForwardResult(message_id=sent_id, to=to, raw_response=payload)


def send_email(
    *,
    account: str,
    to: str,
    subject: str,
    body: str,
    backend: str | None = None,
) -> GmailSendResult:
    """Compose and send a NEW email (not a reply or forward) — an OUTBOUND send,
    used by the digest task to deliver a summary. Crosses the never-send
    boundary, so callers MUST gate it behind the send frontier
    (``agent.send.enabled`` + outbound kill-switch). Only the ``gog`` backend
    has a verified shape today."""
    from app.core.config import get_ingestion_google_backend

    name = (backend or get_ingestion_google_backend()).strip().lower()
    if name == "gog":
        return _gog_send_email(account=account, to=to, subject=subject, body=body)
    raise NotImplementedError(
        f"send_email is only implemented for the gog backend (got {name!r})"
    )


def _gog_send_email(*, account: str, to: str, subject: str, body: str) -> GmailSendResult:
    """Verified ``gog gmail send --to --subject --body`` invocation (gog 0.17.0).

    Shape (confirmed via ``gog gmail send --help``):
        gog gmail send --to <addr> --subject <s> --body <b> --account <email> --json --no-input
    On success the Google API returns the sent Message resource ``{"id": ...}``.
    """
    cmd: list[str] = [
        "gog", "gmail", "send",
        "--to", to,
        "--subject", subject,
        "--body", body,
        "--account", account,
        "--json", "--no-input",
    ]
    try:
        require_account_argv(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH — install via Homebrew or set up the native backend") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gog gmail send timed out (30s)") from exc

    if result.returncode != 0:
        stderr = _clean_stderr(result.stderr)
        raise GmailWriteError(f"gog send returned exit {result.returncode}: {stderr or 'no stderr'}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog send returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    sent_id = str(payload.get("id") or payload.get("messageId") or "")
    logger.info("sent new email to %s for account=%s", to, account)
    return GmailSendResult(message_id=sent_id, raw_response=payload)


# --- gog backend -----------------------------------------------------------


def _build_rfc822(
    *, to_email: str, subject: str, body: str, cc: str | None = None, html: str | None = None,
) -> bytes:
    """Build an RFC 822 message. Subject already includes the ``Re:`` prefix
    if the caller wants threading; Gmail handles thread continuity from the
    explicit ``thread_id`` we pass alongside the message. ``cc`` is a
    comma-separated recipient string (reply-all keeps the thread's Cc). When
    ``html`` is given the message is multipart/alternative (plain + HTML) so a
    fetched Gmail signature renders."""
    msg = email.message.EmailMessage()
    msg["To"] = to_email
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def _gog_create_draft(
    *,
    account: str,
    reply_to_message_id: str | None,
    to_email: str,
    subject: str,
    body: str,
    cc: str | None = None,
    body_html: str | None = None,
    attachments: list[str] | None = None,
) -> GmailDraftResult:
    """Verified ``gog gmail drafts create`` invocation (gog 0.17.0).

    gog wants the message fields broken out — ``--to`` / ``--subject`` /
    ``--body-file -`` (body via stdin to avoid shell escaping). Threading
    is by **message id** (not thread id): ``--reply-to-message-id`` sets
    In-Reply-To, References, *and* threadId in one shot.

    Plain body is sent on stdin via ``--body-file -`` so multi-line / shell-
    hazardous content passes through unmangled. When ``body_html`` is given
    (e.g. body + Gmail signature) it's passed via the verified ``--body-html``
    flag instead, producing an HTML draft.
    """
    cmd: list[str] = [
        "gog", "gmail", "drafts", "create",
        "--account", account,
        "--to", to_email,
        "--subject", subject,
        "--json", "--no-input",
    ]
    stdin: str | None
    if body_html is not None:
        cmd += ["--body-html", body_html]  # verified flag: --body-html=STRING
        stdin = None
    else:
        cmd += ["--body-file", "-"]
        stdin = body
    if cc:
        cmd += ["--cc", cc]   # verified flag: gog gmail drafts create --cc=STRING (comma-separated)
    if reply_to_message_id:
        cmd += ["--reply-to-message-id", reply_to_message_id]
    for att in attachments or []:
        # Verified flag (gog 0.22.0): --attach=ATTACH,... (repeatable). The
        # =-joined single-token form is used so a path can never be parsed as
        # the next flag; Kong splits the value on commas, so a comma-bearing
        # path cannot be passed faithfully — reject it instead of attaching
        # the wrong files.
        if "," in att:
            raise GmailWriteError(
                f"attachment path contains a comma (unsupported by the gog --attach flag): {att!r}"
            )
        cmd += [f"--attach={att}"]

    try:
        require_account_argv(cmd)
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH — install via Homebrew or set up the native backend") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gog gmail drafts create timed out (30s)") from exc

    if result.returncode != 0:
        stderr = _clean_stderr(result.stderr)
        raise GmailWriteError(f"gog returned exit {result.returncode}: {stderr or 'no stderr'}")

    # The Google API returns a Draft resource: { "id": "...", "message": {...} }.
    # gog passes this through unchanged when --json is set.
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    draft_id = payload.get("id") or payload.get("draftId") or ""
    if not draft_id:
        raise GmailWriteError(f"gog returned no draft id; payload={payload!r}")

    logger.info(
        "created gmail draft %s for account=%s reply_to=%s",
        draft_id, account, reply_to_message_id,
    )
    return GmailDraftResult(draft_id=str(draft_id), raw_response=payload)


# --- gws backend -----------------------------------------------------------


def _gws_create_draft(
    *,
    account: str,
    thread_id: str | None,
    to_email: str,
    subject: str,
    body: str,
    cc: str | None = None,
    html: str | None = None,
) -> GmailDraftResult:
    """Verified ``gws gmail users drafts create`` invocation (gws Google
    Workspace CLI, current as of 2026-05).

    gws is the official Google Workspace CLI; its argv convention is
    ``<service> <resource> [<subresource>] <method>`` with URL/query
    params as JSON via ``--params`` and request body as JSON via
    ``--json``. The full path here is ``gmail.users.drafts.create``:
    ``userId`` goes in ``--params``, and the Draft resource (``{"message":
    {"raw": ..., "threadId": ...}}``) goes in ``--json``.

    Schema source-of-truth: ``gws schema gmail.users.drafts.create``.

    Mailbox selection: ``gws`` is single-account per credential — the
    ``userId`` in ``--params`` does NOT pick the mailbox, the credentials file
    does. So for a multi-account setup we MUST set
    ``GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`` for ``account`` (from the
    ``ingestion.gws_credentials`` map), exactly as the read path
    (``adapters.GwsSource._run_json``) does. Without this, a draft for account
    B — containing B's private inbound content — lands in whatever mailbox the
    ambient gws credentials point at (often account A): a cross-account leak.
    With no mapping for the account, the ambient credentials are used as-is
    (correct for the single-account case).
    """
    import os

    from app.ingestion.adapters import _resolve_gws_credentials_file

    rfc = _build_rfc822(to_email=to_email, subject=subject, body=body, cc=cc, html=html)
    raw_b64 = base64.urlsafe_b64encode(rfc).decode("ascii")

    request_body: dict[str, Any] = {"message": {"raw": raw_b64}}
    if thread_id:
        request_body["message"]["threadId"] = thread_id

    cmd: list[str] = [
        "gws", "gmail", "users", "drafts", "create",
        "--params", json.dumps({"userId": account}),
        "--json", json.dumps(request_body),
    ]

    # Mirror the read path: select the per-account credentials file so the draft
    # is written to the intended mailbox, not the ambient default. On a configured
    # multi-account instance, refuse rather than silently draft to the wrong
    # mailbox (b161).
    env = os.environ.copy()
    try:
        creds = _resolve_gws_credentials_file(account)
    except ValueError as exc:
        raise GmailWriteError(str(exc)) from exc
    if creds:
        env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(creds)

    try:
        require_account_argv(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except FileNotFoundError as exc:
        raise GmailWriteError(
            "gws CLI not on PATH — switch ingestion.google_backend to gog or install gws"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError("gws gmail drafts create timed out (30s)") from exc

    if result.returncode != 0:
        stderr = _clean_stderr(result.stderr)
        raise GmailWriteError(f"gws returned exit {result.returncode}: {stderr or 'no stderr'}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gws returned non-JSON stdout: {result.stdout[:200]!r}") from exc

    # gws returns the bare Draft resource (matches the REST API exactly):
    # {"id": "...", "message": {"id": "...", "threadId": "..."}}
    draft_id = payload.get("id") or payload.get("draftId") or ""
    if not draft_id:
        raise GmailWriteError(f"gws returned no draft id; payload={payload!r}")

    logger.info("created gmail draft %s via gws (account=%s thread=%s)", draft_id, account, thread_id)
    return GmailDraftResult(draft_id=str(draft_id), raw_response=payload)


# --- native backend --------------------------------------------------------


# Scope needed to *write* drafts. The native ingestion backend uses
# gmail.readonly; the write path needs gmail.compose. We don't merge these
# into _NATIVE_SCOPES in adapters.py because a user who only ingests
# shouldn't be forced into a re-auth — read-only is the safer default.
# Re-auth instructions live in the error message below.
_NATIVE_WRITE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)

_NATIVE_REAUTH_HINT = (
    "Native backend draft creation needs the gmail.compose OAuth scope; "
    "your current token is read-only. Re-authorize: "
    "`youos setup` (or call NativeSource.authorize_account with "
    "the write scopes)."
)


def _native_create_draft(
    *,
    account: str,
    thread_id: str | None,
    to_email: str,
    subject: str,
    body: str,
    cc: str | None = None,
    html: str | None = None,
) -> GmailDraftResult:
    """Create a draft via Google's REST API directly.

    Mirrors the gog/gws shape (RFC822 → base64url → raw) but uses the
    google-api-python-client to call ``drafts().create()`` rather than
    shelling out. Requires the ``gmail.compose`` scope on the stored
    token — missing scope translates to a clear re-auth message.

    Tests mock ``googleapiclient.discovery.build`` so the auth + API
    stack is exercised by the call shape, not the network.
    """
    rfc = _build_rfc822(to_email=to_email, subject=subject, body=body, cc=cc, html=html)
    raw_b64 = base64.urlsafe_b64encode(rfc).decode("ascii")

    try:
        service = _native_gmail_service(account=account)
    except RuntimeError as exc:
        # Credentials missing, expired without refresh-token, or scope-narrow
        # — translate to GmailWriteError so the agent route returns 502 with
        # an actionable message instead of a generic 500.
        raise GmailWriteError(str(exc)) from exc

    request_body: dict[str, Any] = {"message": {"raw": raw_b64}}
    if thread_id:
        request_body["message"]["threadId"] = thread_id

    try:
        result = service.users().drafts().create(
            userId="me", body=request_body,
        ).execute()
    except Exception as exc:
        # googleapiclient.errors.HttpError has .resp.status; we check by
        # attribute access so the test mocks don't need to construct one.
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        if status_code in (401, 403):
            raise GmailWriteError(
                f"{_NATIVE_REAUTH_HINT} (Google returned HTTP {status_code})"
            ) from exc
        raise GmailWriteError(f"native drafts.create failed: {exc}") from exc

    draft_id = result.get("id") if isinstance(result, dict) else None
    if not draft_id:
        raise GmailWriteError(f"native drafts.create returned no id; payload={result!r}")

    logger.info(
        "created gmail draft %s via native (account=%s thread=%s)",
        draft_id, account, thread_id,
    )
    return GmailDraftResult(draft_id=str(draft_id), raw_response=result if isinstance(result, dict) else {})


def _native_gmail_service(*, account: str) -> Any:
    """Load the user's stored OAuth credentials for ``account`` (with write
    scope) and return a Gmail v1 service object.

    Reuses the same token-storage convention as ``NativeSource`` in
    ``app.ingestion.adapters`` (token files keyed by account email under
    ``var/google_tokens/``). Doesn't refresh tokens that lack a
    refresh_token — that's a re-auth case.
    """
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Native backend needs the google extra: pip install youos[google]"
        ) from exc

    from app.core.settings import get_instance_root
    from app.ingestion.adapters import _assert_token_account, _harden_token_dir, _native_config

    token_dir_cfg = (_native_config().get("google_token_dir") or "").strip()
    token_dir = (
        Path(token_dir_cfg).expanduser()
        if token_dir_cfg else get_instance_root() / "var" / "google_tokens"
    )
    # Normalized like adapters._token_path (b245), with verbatim legacy fallback.
    safe = account.strip().lower().replace("/", "_").replace("\\", "_")
    token_path = token_dir / f"{safe}.json"
    if not token_path.exists():
        legacy = token_dir / f"{account.replace('/', '_').replace(chr(92), '_')}.json"
        if legacy.exists():
            token_path = legacy
    if not token_path.exists():
        raise RuntimeError(
            f"No stored Google credentials for {account!r} at {token_path}. "
            "Authorize first via `youos setup`."
        )

    creds = Credentials.from_authorized_user_file(
        str(token_path), scopes=list(_NATIVE_WRITE_SCOPES),
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            _harden_token_dir(token_path)
            # 0o600: this file holds the OAuth refresh_token + client_secret.
            write_secret(token_path, creds.to_json())
        else:
            raise RuntimeError(_NATIVE_REAUTH_HINT)

    # Refuse a swapped / mis-consented token before drafting into the wrong mailbox.
    _assert_token_account(creds, account, token_path)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- label / route operations (the agent-action framework) -----------------
# Account-internal mailbox mutations (apply a label, archive, star). Unlike
# create_draft/send_draft these never leave the mailbox; they're reversible
# (every add has a remove), which is what makes the undo ledger possible. gog
# shapes verified against gog 0.17.0: `labels list`, `labels create <name>`,
# `messages modify <id> --add "a,b" --remove "c,d"`.

@dataclass
class GmailModifyResult:
    message_id: str
    added: list[str]
    removed: list[str]
    raw_response: dict[str, Any]


def _gog(args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        require_account_argv(args)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise GmailWriteError("gog CLI not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GmailWriteError(f"gog timed out: {' '.join(args[:4])}") from exc


def list_labels(*, account: str) -> set[str]:
    """Return the set of existing label NAMES on the account (gog gmail labels list)."""
    r = _gog(["gog", "gmail", "labels", "list", "--account", account, "--json", "--no-input"])
    if r.returncode != 0:
        raise GmailWriteError(f"gog labels list exit {r.returncode}: {_clean_stderr(r.stderr, limit=160)}")
    try:
        payload = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GmailWriteError(f"gog labels list non-JSON: {r.stdout[:160]!r}") from exc
    items = payload if isinstance(payload, list) else (payload.get("labels") or payload.get("result") or [])
    return {str(x.get("name")) for x in items if isinstance(x, dict) and x.get("name")}


def ensure_label(*, account: str, name: str, known: set[str] | None = None) -> None:
    """Create the label if it doesn't already exist (so a subsequent --add by
    name resolves). System labels (INBOX/STARRED/…) always exist; user labels
    may need creating. Idempotent.

    ``known`` is an optional caller-supplied set of existing label names (a
    per-sweep cache) — when given, the existence check uses it instead of a
    fresh ``labels list`` subprocess per call, and a newly-created name is added
    to it so later calls in the same sweep see it."""
    if not name or name.upper() in _SYSTEM_LABELS:
        return
    existing = known if known is not None else list_labels(account=account)
    if name in existing:
        return
    # `--` end-of-flags before the label name so a name beginning with '-'
    # (an option-injection via a crafted rule value) isn't parsed as a gog flag.
    r = _gog(["gog", "gmail", "labels", "create", "--account", account, "--json", "--no-input", "--", name])
    if r.returncode != 0:
        stderr = (r.stderr or "").strip().lower()
        if not ("already exists" in stderr or "exists" in stderr):  # race/case diff is fine
            raise GmailWriteError(f"gog labels create {name!r} exit {r.returncode}: {_clean_stderr(r.stderr, limit=160)}")
    if known is not None:
        known.add(name)


# Gmail's reserved system labels (modify accepts these without creating them).
_SYSTEM_LABELS = {
    "INBOX", "STARRED", "IMPORTANT", "UNREAD", "SPAM", "TRASH", "SENT", "DRAFT",
    "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def modify_message_labels(
    *,
    account: str,
    message_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    dry_run: bool = False,
) -> GmailModifyResult:
    """Add/remove labels on one message (gog gmail messages modify <id>
    --add "..." --remove "..."). ``dry_run`` passes gog's own --dry-run so the
    real CLI path runs without changing the mailbox. The caller's higher-level
    dry-run (agent.actions.dry_run) skips this entirely and only logs intent."""
    add = [a for a in (add or []) if a]
    remove = [r for r in (remove or []) if r]
    if not add and not remove:
        return GmailModifyResult(message_id, [], [], {})
    cmd = ["gog", "gmail", "messages", "modify", message_id, "--account", account, "--json", "--no-input"]
    if add:
        cmd += ["--add", ",".join(add)]
    if remove:
        cmd += ["--remove", ",".join(remove)]
    if dry_run:
        cmd.append("--dry-run")
    r = _gog(cmd)
    if r.returncode != 0:
        raise GmailWriteError(f"gog messages modify exit {r.returncode}: {_clean_stderr(r.stderr, limit=160)}")
    try:
        payload = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    logger.info("modified labels on %s (add=%s remove=%s dry_run=%s)", message_id, add, remove, dry_run)
    return GmailModifyResult(message_id=message_id, added=add, removed=remove, raw_response=payload)
