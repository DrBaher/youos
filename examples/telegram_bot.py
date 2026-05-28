#!/usr/bin/env python3
"""YouOS reference Telegram bot.

A 200-line, dependency-light reference orchestrator that wires Telegram
to YouOS so you can triage email from your phone without leaving the
chat. Built for the orchestrator vision (docs/INTEGRATIONS.md): the user
lives in Telegram, the bot handles the email category by calling YouOS.

What it does
============

Commands (set up via @BotFather → `/setcommands`):

    /inbox            — digest summary: pending count, top rows with ids
    /push <id>        — push row #<id> to Gmail Drafts
    /dismiss <id> [reason]
                      — dismiss row #<id> (default reason: noise)
                        reason must be one of:
                        noise, wrong_sender, wrong_content, already_handled, other
    /find <words>     — resolve a description to a row id ("find Q3 pricing")
    /digest [days]    — full digest text (default 1 day)
    /help             — show this

The bot also accepts free-text — if it sounds like an instruction
("push the Q3 thing", "dismiss the barber confirmation"), the bot
resolves the description via /api/agent/resolve and dispatches the
action. This is intentionally simple substring routing; a real
orchestrator would use an LLM here.

Setup
=====

1. On your Mac (where YouOS runs):

       youos token-create
       # → copy the token

2. On the machine running the bot (can be the same Mac, or another Tailnet
   device — but NOT a public cloud unless you've already exposed YouOS
   safely via Funnel + token auth):

       pip install python-telegram-bot==21.* requests
       export YOUOS_URL=http://bbots-mac-mini:8901
       export YOUOS_TOKEN=<the token from step 1>
       export YOUOS_ACCOUNT=drbaher@gmail.com   # optional; falls back to user.emails[0]
       export TELEGRAM_TOKEN=<from @BotFather>
       export TELEGRAM_AUTHORIZED_USER=<your numeric Telegram user id>

       python examples/telegram_bot.py

3. Talk to the bot. Try /inbox first.

Notes
=====

* Only ``TELEGRAM_AUTHORIZED_USER`` (your Telegram numeric id) can drive the
  bot. Without this every Telegram user on the platform could control your
  inbox. See @userinfobot to find your id.
* Bot doesn't persist anything; restart-safe.
* This is reference code — adopt the patterns, don't deploy unchanged to a
  cloud VPS.
"""

from __future__ import annotations

import logging
import os
import re

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("youos-tg")

YOUOS_URL = os.environ["YOUOS_URL"].rstrip("/")
YOUOS_TOKEN = os.environ["YOUOS_TOKEN"]
YOUOS_ACCOUNT = os.environ.get("YOUOS_ACCOUNT") or None
AUTHORIZED_USER = int(os.environ["TELEGRAM_AUTHORIZED_USER"])

# Default dismissal reason when /dismiss is called without one.
DEFAULT_DISMISS_REASON = "noise"
VALID_REASONS = {"noise", "wrong_sender", "wrong_content", "already_handled", "other"}

# Trigger patterns for free-text routing. Order matters — first match wins.
FREE_TEXT_ROUTES = [
    (re.compile(r"^\s*push\s+(.+?)\s*$", re.IGNORECASE), "push"),
    (re.compile(r"^\s*dismiss\s+(.+?)\s*$", re.IGNORECASE), "dismiss"),
    (re.compile(r"^\s*(?:inbox|digest|status|anything|what'?s? in)\b", re.IGNORECASE), "inbox"),
    (re.compile(r"^\s*(?:find|which|where)\s+(.+?)\s*$", re.IGNORECASE), "find"),
]


def _h() -> dict[str, str]:
    """Auth header for every YouOS call."""
    return {"X-YouOS-Token": YOUOS_TOKEN}


def _q(extra: dict | None = None) -> dict:
    """Default query params (account if set) merged with extras."""
    p: dict = {}
    if YOUOS_ACCOUNT:
        p["account"] = YOUOS_ACCOUNT
    if extra:
        p.update(extra)
    return p


def _authorized(update: Update) -> bool:
    if update.effective_user and update.effective_user.id == AUTHORIZED_USER:
        return True
    log.warning("unauthorized: %s", update.effective_user.id if update.effective_user else "no-user")
    return False


# --- command handlers -----------------------------------------------------


async def cmd_inbox(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    r = requests.get(f"{YOUOS_URL}/api/agent/digest", params=_q({"days": 1}), headers=_h(), timeout=15)
    if not r.ok:
        await update.message.reply_text(f"YouOS error: {r.status_code} {r.text[:200]}")
        return
    body = r.json()
    lines = [body["summary"]]
    if body.get("pending_preview"):
        lines.append("")
        for row in body["pending_preview"]:
            tier = row.get("tier", "?")
            score = row.get("needs_reply_score") or 0.0
            subject = (row.get("subject") or "(no subject)")[:80]
            sender = (row.get("sender") or row.get("sender_email") or "(unknown)")[:50]
            lines.append(f"#{row['id']} [{tier} {score:.2f}] {subject}  ←  {sender}")
        lines.append("")
        lines.append("Tap: /push <id>, /dismiss <id> [reason]")
    await update.message.reply_text("\n".join(lines))


async def cmd_push(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /push <row-id>")
        return
    try:
        row_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text(f"Not a row id: {ctx.args[0]!r}")
        return
    r = requests.post(f"{YOUOS_URL}/api/agent/pending/{row_id}/push_to_gmail", headers=_h(), timeout=30)
    if not r.ok:
        await update.message.reply_text(f"Push failed: {r.status_code} {r.text[:200]}")
        return
    gid = r.json().get("gmail_draft_id", "?")
    await update.message.reply_text(f"✓ Pushed row #{row_id}. Gmail draft: {gid}\nOpen Gmail to send.")


async def cmd_dismiss(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /dismiss <row-id> [reason]")
        return
    try:
        row_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text(f"Not a row id: {ctx.args[0]!r}")
        return
    reason = ctx.args[1] if len(ctx.args) > 1 else DEFAULT_DISMISS_REASON
    if reason not in VALID_REASONS:
        await update.message.reply_text(
            f"Unknown reason: {reason!r}\nAllowed: {', '.join(sorted(VALID_REASONS))}"
        )
        return
    r = requests.post(
        f"{YOUOS_URL}/api/agent/pending/{row_id}/dismiss",
        json={"reason": reason}, headers=_h(), timeout=15,
    )
    if not r.ok:
        await update.message.reply_text(f"Dismiss failed: {r.status_code} {r.text[:200]}")
        return
    await update.message.reply_text(f"✓ Dismissed row #{row_id} as {reason}.")


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /find <words>")
        return
    q = " ".join(ctx.args)
    r = requests.get(f"{YOUOS_URL}/api/agent/resolve", params=_q({"q": q, "limit": 5}), headers=_h(), timeout=15)
    if not r.ok:
        await update.message.reply_text(f"Resolve failed: {r.status_code} {r.text[:200]}")
        return
    body = r.json()
    if body["count"] == 0:
        await update.message.reply_text(f"No match for {q!r}.")
        return
    lines = [f"Found {body['count']} for {q!r}:"]
    for row in body["rows"]:
        subject = (row.get("subject") or "(no subject)")[:80]
        sender = (row.get("sender") or row.get("sender_email") or "(unknown)")[:50]
        lines.append(f"#{row['id']}  {subject}  ←  {sender}")
    await update.message.reply_text("\n".join(lines))


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    days = int(ctx.args[0]) if ctx.args else 1
    r = requests.get(f"{YOUOS_URL}/api/agent/digest", params=_q({"days": days}), headers=_h(), timeout=15)
    if not r.ok:
        await update.message.reply_text(f"Digest failed: {r.status_code} {r.text[:200]}")
        return
    body = r.json()
    # Re-render in our compact form (chat-friendly).
    lines = [body["summary"]]
    if body.get("dismissal_by_reason"):
        lines.append("")
        lines.append("Dismissals: " + ", ".join(f"{k}={v}" for k, v in body["dismissal_by_reason"].items()))
    if body.get("auto_promoted"):
        lines.append(f"Auto-skipped: {', '.join(body['auto_promoted'][:3])}")
    if body.get("triage_url"):
        lines.append(f"Review: {body['triage_url']}/triage")
    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "YouOS bot commands:\n"
        "/inbox            — digest summary + top pending\n"
        "/push <id>        — push row to Gmail Drafts\n"
        "/dismiss <id> [reason] — noise/wrong_sender/wrong_content/already_handled/other\n"
        "/find <words>     — resolve a description to a row id\n"
        "/digest [days]    — extended digest\n"
        "/help             — this"
    )


# --- free-text router ------------------------------------------------------


async def free_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Minimal substring router for non-/command messages. A real orchestrator
    would route through an LLM here (give it the message + the user's recent
    digest as context, ask for an action tuple)."""
    if not _authorized(update):
        return
    text = update.message.text or ""
    for pat, action in FREE_TEXT_ROUTES:
        m = pat.match(text)
        if not m:
            continue
        if action == "inbox":
            await cmd_inbox(update, ctx)
            return
        # push/dismiss/find all want a description after the verb.
        target = (m.group(1) if m.groups() else "").strip()
        if not target:
            await update.message.reply_text(f"What should I {action}?")
            return
        # If the target is already a row id, dispatch directly.
        if target.startswith("#"):
            target = target[1:]
        if target.isdigit():
            row_id = int(target)
        else:
            # Resolve the description.
            r = requests.get(f"{YOUOS_URL}/api/agent/resolve", params=_q({"q": target, "limit": 3}), headers=_h(), timeout=15)
            if not r.ok or r.json()["count"] == 0:
                await update.message.reply_text(f"No match for {target!r}. Try /find {target}.")
                return
            hits = r.json()["rows"]
            if len(hits) > 1:
                lines = [f"Multiple matches for {target!r} — be specific:"]
                lines += [f"#{h['id']}  {h.get('subject', '')[:60]}  ←  {h.get('sender', '')[:40]}" for h in hits]
                await update.message.reply_text("\n".join(lines))
                return
            row_id = hits[0]["id"]
        # Now dispatch via the existing handlers.
        ctx.args = [str(row_id)]
        if action == "push":
            await cmd_push(update, ctx)
        elif action == "dismiss":
            await cmd_dismiss(update, ctx)
        elif action == "find":
            ctx.args = target.split()
            await cmd_find(update, ctx)
        return

    await update.message.reply_text(
        f"Didn't recognise that — try /inbox, /find {text[:30]}, or /help"
    )


# --- entry point -----------------------------------------------------------


def main() -> None:
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("dismiss", cmd_dismiss))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    log.info("YouOS Telegram bot starting; talking to %s as authorised user %s", YOUOS_URL, AUTHORIZED_USER)
    app.run_polling()


if __name__ == "__main__":
    main()
