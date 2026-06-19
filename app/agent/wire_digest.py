"""The Wire — comprehensive newsletter digest (ported from the OpenClaw skill).

Unlike :mod:`app.agent.digest_tasks` (a flat one-line-per-email summary of a
Gmail query), the Wire is a *newsletter* digest: it fetches the **bodies** of
the day's newsletters across accounts, asks the cloud model to extract every
individual story, deduplicate across sources, and group them into themed
sections, then delivers ONE rich Gmail-safe **HTML** email and archives the
sources (except a never-archive allow-list). Each issue carries a sequential
**edition** number.

It reuses the digest engine's safety machinery so the never-send invariant is
unchanged:

* ``agent.wire.enabled`` — master switch (default **false**),
* ``agent.send.enabled`` — the shared send frontier (default false),
* ``agent.outbound_kill_switch`` — blocks all outbound when on.

A real send needs all three open; otherwise the run records ``blocked`` and
nothing is sent. A dry-run/preview builds the HTML and returns it without
sending, archiving, claiming the period, or bumping the edition.

At-most-once per day: the run claims ``(name="Wire", account="*", period_key)``
via the same UNIQUE-index ledger the forward/digest paths use, so even with
overlapping scheduler ticks the Wire is sent at most once per day. The edition
is bumped only after a successful send.

Cloud egress: the summarizer is the frontier model (``summary_model: cloud``),
so the collected newsletter bodies are sent off-device — the same model the
original skill used. Newsletters are bulk/marketing mail, not private
correspondence; the digest is read-only and crosses no new outbound frontier
beyond the gated send.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# --- defaults (ported from the OpenClaw collect.py; config can override) -----

_DEFAULT_DAYS_BACK = 2
_DEFAULT_MAX_EMAILS = 150
_DEFAULT_MAX_BODY_LINES = 150
_DEFAULT_HOUR = 19
_READ_TIMEOUT = 30
_LIST_TIMEOUT = 60

# Work accounts only contribute categorized newsletters/updates (no personal
# correspondence is pulled into the digest).
_WORK_ACCOUNTS = ("baher@medicus.ai",)

# Senders never worth a digest entry — transactional, shipping, billing, work
# tooling, event spam, and the digest's own address (avoid self-ingest).
_SKIP_FROM = (
    "calendar-notification@google.com", "paypal", "stripe",
    "no-reply@accounts.google.com", "shipment-tracking@amazon",
    "drive-shares-dm-noreply@google.com", "comments-noreply@docs.google.com",
    "uber@uber.com", "baby-walz", "gurkerl", "lieferando", "improvmx.com",
    "emailmeter.com", "sesam-vitale.fr", "noreply@campaign.eventbrite.com",
    "noreply@e.economist.com", "nytimes@e.newyorktimes.com",
    "subscriptions@message.bloomberg.com", "support@fly.io", "noreply@fly.io",
    "deploy@vercel.com", "hello@emailmeter.com", "support@improvmx.com",
    "southsummit", "viennaup", "eventbrite.com",
    "hypesportsinnovation.com", "frontiers.health",
    "noreply@google.com", "notify@google.com", "brz.gv.at",
    "fireflies.ai", "notion.so", "slack.com", "asana.com",
    "monday.com", "clickup.com", "jira", "confluence",
    "zoom.us", "calendly.com", "loom.com", "notifications@github.com",
)

_SKIP_SUBJECT = (
    "receipt", "payment confirmation", "security alert",
    "out for delivery", "shipping notification", "tracking number",
    "your trial", "congrats on deploying", "your weekly report is ready",
    "trouble processing your payment", "payment failed",
    "problem billing your account", "billing your account",
    "jetzt platz sichern", "get your tickets", "class starts soon",
    "updating our subscriber agreement", "we're updating",
    "meeting recap", "meeting summary", "meeting notes",
    "new demo request", "demo request from", "play today's games",
    # Calendar invites are real mail, not newsletters — never digest/archive them.
    "invitation:", "updated invitation:", "canceled event:", "accepted:", "declined:",
    # The Wire must not ingest YouOS's own digests (recursion).
    "youos digest:",
)

# Tagged [PROMO] in the content dump so the model routes them to Promotions
# (1 line each) rather than dropping or over-summarizing them.
_PROMO_FROM = (
    "nespresso", "ekster", "24s.com", "mrporter.com", "net-a-porter",
    "endclothing.com", "ssense.com", "matchesfashion", "farfetch",
    "mytheresa", "zalando", "asos.com", "hm.com", "zara.com",
    "uniqlo", "cos.com", "arket.com", "mango.com",
    "apple.com", "samsung.com", "sonos.com",
    "dyson.com", "rimowa.com",
)

# Senders whose mail is NEVER archived after a digest (stays in the inbox).
_ARCHIVE_EXCLUSIONS = ("benedict evans", "ben-evans.com", "benedictevans")

# Reused ledger identity (shares agent_digest_runs / agent_digest_items tables).
_WIRE_NAME = "Wire"
_WIRE_ACCOUNT = "*"  # one digest spans all accounts


@dataclass
class WireSpec:
    enabled: bool = False
    hour: int = _DEFAULT_HOUR
    minute: int = 0
    weekdays_only: bool = True       # Mon–Fri (the original cron was 0 19 * * 1-5)
    days_back: int = _DEFAULT_DAYS_BACK
    from_account: str = ""           # blank → first configured account
    deliver_to: str = ""             # blank → from_account's own inbox
    max_emails: int = _DEFAULT_MAX_EMAILS
    max_body_lines: int = _DEFAULT_MAX_BODY_LINES
    summary_model: str = "cloud"     # 'cloud' (Claude) | 'local' (warm model)
    skip_from: tuple[str, ...] = field(default_factory=lambda: _SKIP_FROM)
    skip_subject: tuple[str, ...] = field(default_factory=lambda: _SKIP_SUBJECT)
    promo_from: tuple[str, ...] = field(default_factory=lambda: _PROMO_FROM)
    archive_exclusions: tuple[str, ...] = field(default_factory=lambda: _ARCHIVE_EXCLUSIONS)


def load_wire_spec() -> WireSpec:
    """Read ``agent.wire`` into a WireSpec (safe defaults on any problem)."""
    try:
        from app.core.config import load_config

        cfg = load_config() or {}
        a = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        w = a.get("wire") if isinstance(a, dict) else None
    except Exception:
        w = None
    if not isinstance(w, dict):
        w = {}

    def _list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        v = w.get(key)
        if isinstance(v, list) and v:
            return tuple(str(x).strip().lower() for x in v if str(x).strip())
        return default

    def _int(key: str, default: int) -> int:
        try:
            return int(w.get(key, default))
        except (TypeError, ValueError):
            return default

    return WireSpec(
        enabled=bool(w.get("enabled", False)),
        hour=max(0, min(23, _int("hour", _DEFAULT_HOUR))),
        minute=max(0, min(59, _int("minute", 0))),
        weekdays_only=bool(w.get("weekdays_only", True)),
        days_back=max(1, _int("days_back", _DEFAULT_DAYS_BACK)),
        from_account=str(w.get("from_account") or "").strip(),
        deliver_to=str(w.get("deliver_to") or "").strip(),
        max_emails=max(1, _int("max_emails", _DEFAULT_MAX_EMAILS)),
        max_body_lines=max(10, _int("max_body_lines", _DEFAULT_MAX_BODY_LINES)),
        summary_model=str(w.get("summary_model") or "cloud").strip().lower(),
        skip_from=_list("skip_from", _SKIP_FROM),
        skip_subject=_list("skip_subject", _SKIP_SUBJECT),
        promo_from=_list("promo_from", _PROMO_FROM),
        archive_exclusions=_list("archive_exclusions", _ARCHIVE_EXCLUSIONS),
    )


# --- edition tracking -------------------------------------------------------
# Stored per-instance at var/wire_edition.json (mirrors the OpenClaw
# edition-tracker.json shape). Seeded from the OpenClaw tracker's lastEdition so
# the YouOS sequence continues seamlessly (the user asked to continue from #68).

_OPENCLAW_TRACKER = "/Users/bbot/.openclaw/skills/the-wire/references/edition-tracker.json"


def _edition_path():
    from app.core.settings import get_var_dir

    return get_var_dir() / "wire_edition.json"


def _seed_last_edition() -> int:
    """Best-effort read of the OpenClaw tracker's lastEdition for continuity."""
    try:
        with open(_OPENCLAW_TRACKER, encoding="utf-8") as f:
            return int(json.load(f).get("lastEdition", 0))
    except Exception:
        return 0


def read_edition_state() -> dict[str, Any]:
    """Current edition state ``{lastEdition, history}``. Seeds from the OpenClaw
    tracker the first time so YouOS continues the same numbering."""
    path = _edition_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "lastEdition" in data:
            data.setdefault("history", [])
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.info("wire edition state unreadable (%s); reseeding", exc)
    return {"lastEdition": _seed_last_edition(), "history": []}


def next_edition() -> int:
    return int(read_edition_state().get("lastEdition", 0)) + 1


def _bump_edition(edition: int, *, date: str, emails: int, stories: int) -> None:
    """Persist a sent edition (atomic write under var/)."""
    state = read_edition_state()
    state["lastEdition"] = edition
    entry = {"edition": edition, "date": date, "emails_processed": emails,
             "stories_processed": stories}
    state["history"] = [entry, *state.get("history", [])][:200]
    path = _edition_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not persist wire edition %s: %s", edition, exc)


# --- collection -------------------------------------------------------------


def _run(cmd: list[str], timeout: int) -> str:
    from app.ingestion.adapters import require_account_argv

    require_account_argv(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gog call failed: {exc}") from exc
    if r.returncode != 0:
        raise RuntimeError(f"gog exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
    return r.stdout or ""


def _list_account(account: str, spec: WireSpec) -> list[dict[str, str]]:
    if account in _WORK_ACCOUNTS:
        query = f"in:inbox (category:promotions OR category:updates) newer_than:{spec.days_back}d"
    else:
        query = f"in:inbox newer_than:{spec.days_back}d"
    cmd = ["gog", "gmail", "messages", "search", "--account", account,
           "--max", str(spec.max_emails), "--json", "--results-only", "--no-input",
           "--", query]
    try:
        raw = _run(cmd, _LIST_TIMEOUT)
    except RuntimeError as exc:
        logger.info("wire list failed for %s: %s", account, exc)
        return []
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    msgs = data if isinstance(data, list) else (data.get("messages") or data.get("results") or data.get("threads") or [])
    out = []
    for m in msgs:
        mid = m.get("id") or m.get("messageId")
        if not mid:
            continue
        out.append({
            "id": str(mid), "account": account,
            "from": str(m.get("from") or m.get("sender") or ""),
            "subject": str(m.get("subject") or "(no subject)"),
        })
    return out


def _should_skip(frm: str, subject: str, spec: WireSpec) -> bool:
    fr = frm.lower()
    if any(p in fr for p in spec.skip_from):
        return True
    subj = subject.lower().replace("’", "'").replace("‘", "'")
    return any(p in subj for p in spec.skip_subject)


def _is_promo(frm: str, spec: WireSpec) -> bool:
    fr = frm.lower()
    return any(p in fr for p in spec.promo_from)


def _read_body(account: str, mid: str, max_lines: int) -> str:
    cmd = ["gog", "gmail", "read", "--account", account, "--no-input", "--", mid]
    try:
        raw = _run(cmd, _READ_TIMEOUT)
    except RuntimeError:
        return ""
    return "\n".join(raw.split("\n")[:max_lines])


def collect_wire(spec: WireSpec, accounts: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Fetch + filter the day's newsletters across ``accounts``. Returns
    ``(items, manifest)`` where each item carries its truncated body and a
    ``promo`` flag; manifest is the lightweight id/account/from/subject list used
    for archiving."""
    listed: list[dict[str, str]] = []
    for account in accounts:
        listed.extend(_list_account(account, spec))

    seen: set[str] = set()
    kept: list[dict[str, str]] = []
    for e in listed:
        eid = e["id"]
        if eid in seen:
            continue
        seen.add(eid)
        if _should_skip(e["from"], e["subject"], spec):
            continue
        kept.append(e)

    items: list[dict[str, str]] = []
    for e in kept:
        body = _read_body(e["account"], e["id"], spec.max_body_lines)
        items.append({**e, "body": body, "promo": _is_promo(e["from"], spec)})
    manifest = [{"id": e["id"], "account": e["account"], "from": e["from"], "subject": e["subject"]}
                for e in kept]
    return items, manifest


# --- summarization → HTML ---------------------------------------------------

# Locked Gmail-safe shell (ported from assets/digest-template.html). The model
# fills only the inner section cards; the shell guarantees the styling/layout.
_TEMPLATE_HEAD = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>The Wire Digest</title>
  <style>
    body {{ margin:0; padding:0; background:#f5f7fb; font-family:Arial,Helvetica,sans-serif; color:#111827; line-height:1.5; }}
    .wrap {{ max-width:680px; margin:0 auto; padding:20px 12px 28px; }}
    .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:18px; margin-bottom:14px; }}
    h1 {{ margin:0; font-size:24px; line-height:1.2; color:#0f172a; }}
    .subtitle {{ margin-top:6px; font-size:13px; color:#475569; }}
    h2 {{ margin:0 0 10px; font-size:17px; color:#111827; border-bottom:1px solid #e5e7eb; padding-bottom:8px; }}
    ul, ol {{ margin:0; padding-left:20px; }}
    li {{ margin:0 0 10px; font-size:14px; }}
    .source {{ color:#64748b; font-size:12px; }}
    .footer {{ text-align:center; color:#64748b; font-size:12px; margin-top:10px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>📡 The Wire — #{edition}</h1>
      <div class="subtitle">{subtitle}</div>
    </div>
{sections}
    <div class="footer">Compiled by YouOS • {deliver_to}</div>
  </div>
</body>
</html>"""

# Markers that mean the model echoed the template instead of writing real
# content — mirrors send-digest.sh's reject grep. A digest containing any of
# these is treated as a failed render (never sent).
_PLACEHOLDER_MARKERS = (
    "story headline", "concrete headline", "placeholder",
    "coverage item captured", "noteworthy update in this theme",
    "one-line promotion summary", "1-2 sentence summary with specific facts",
    "#n",
)

_WIRE_SECTIONS = (
    "🤖 AI & Tech", "💰 Markets & Economy", "💸 Fundraising & Deals",
    "🌍 Geopolitics & World", "🔬 Science", "🏥 Health & Wellness",
    "🎨 Culture & Design", "👔 Style & Fashion", "☕ Lifestyle & Food",
    "🎬 Entertainment", "🇦🇹 Vienna & Austria", "✍️ Ideas & Essays",
    "📱 Products & Tools", "🔗 Interesting Links",
)


def _content_dump(items: list[dict[str, str]]) -> str:
    parts = []
    for e in items:
        tag = "[PROMO] " if e.get("promo") else ""
        parts.append(f"---\n## {tag}[{e['from']}] {e['subject']}\n\n{e.get('body','')}\n")
    return "\n".join(parts)


def _wire_prompt(items: list[dict[str, str]], edition: int) -> str:
    sections = "\n".join(f"- {s}" for s in _WIRE_SECTIONS)
    return f"""You are compiling "The Wire", a comprehensive daily newsletter digest, edition #{edition}.

Below is the raw content of {len(items)} newsletter/promotional emails (deduplicated by id). \
Extract EVERY individual story/item — a newsletter with 6 stories yields 6 entries; NEVER \
collapse a whole newsletter into one line. Then group entries into themed sections and output \
Gmail-safe HTML.

OUTPUT: ONLY the inner HTML section cards — no <html>, <head>, <body>, no surrounding header \
(those are added for you). Return a sequence of:
  <div class="card"><h2>SECTION TITLE</h2><ul>...</ul></div>
(use <ol> for Top Stories). Output raw HTML only — NO markdown (`**`, `#`, ```), NO commentary, \
NO placeholder/filler text.

REQUIRED ORDER:
1. <div class="card"><h2>Top Stories</h2><ol> — exactly 3 concise <li> of the day's biggest items.
2. Themed sections, ONLY the non-empty ones, in this order:
{sections}
   • 💸 Fundraising & Deals is MANDATORY even with 1–2 deals: list EVERY funding round, \
acquisition, IPO, investment. Format each: <strong>Company</strong> — $Amount, round, lead \
investor(s), 1-line description (include valuation if mentioned).
   • 🔗 Interesting Links is the quick-hits catch-all and must be the LAST themed section.
3. <div class="card"><h2>🛍️ Promotions</h2><ul> — items marked [PROMO]; EXACTLY one <li> each, \
never a full summary.

RULES:
- Each story <li>: <strong>Concrete factual headline</strong> <span class="source">(Source)</span> \
— 1–2 sentences with specific names/numbers. "OpenAI shipped GPT-5 at $200/mo", not "article about AI".
- Deduplicate across sources: the same story from 3 newsletters = ONE entry citing all three in the source span.
- Rich stories (2+ angles) get depth; single-fact items go to 🔗 Interesting Links as one line.
- Skip empty sections (except Fundraising). Capture substance; drop nothing.

CONTENT:
{_content_dump(items)}
"""


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    return t.strip()


def _validate_sections(html: str) -> tuple[bool, str]:
    low = html.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return False, f"contains placeholder text: {marker!r}"
    if "<h2>" not in low or "<li>" not in low:
        return False, "no section/list content"
    if "**" in html or "```" in html:
        return False, "contains markdown artifacts"
    return True, ""


def _fallback_sections(items: list[dict[str, str]]) -> str:
    """Deterministic non-AI render (model off/unavailable): a single grouped
    'Newsletters' card listing each subject. Never placeholder text, so it
    passes validation and a run still produces a real digest."""
    import html as _html

    def esc(s: str) -> str:
        return _html.escape(str(s or ""))

    stories = "\n".join(
        f'      <li><strong>{esc(e["subject"])}</strong> '
        f'<span class="source">({esc(e["from"])})</span></li>'
        for e in items
    )
    return (
        '    <div class="card"><h2>Newsletters</h2>\n      <ul>\n'
        f"{stories}\n      </ul>\n    </div>"
    )


def build_wire_html(items: list[dict[str, str]], edition: int, *, model: str = "cloud",
                    complete_fn=None, now: datetime | None = None) -> tuple[str, int]:
    """Build the full Wire HTML for ``items``. Returns ``(html, story_count)``.
    Uses the cloud model to extract+group stories; falls back to a deterministic
    grouped list if the model is unavailable or its output fails validation."""
    from app.agent.digest_tasks import _user_tz

    now = now or datetime.now(_user_tz())
    subtitle = f"{now.strftime('%A, %B %d, %Y')} • {len(items)} newsletters"

    sections = ""
    if complete_fn is None:
        from app.core.completion import select_completion

        complete_fn = select_completion(model, max_tokens=8000, temperature=0.3)

    if complete_fn is not None:
        try:
            raw = _strip_code_fence(complete_fn(_wire_prompt(items, edition)))
            ok, why = _validate_sections(raw)
            if ok:
                sections = raw
            else:
                logger.info("wire model output rejected (%s); using fallback render", why)
        except Exception as exc:
            logger.info("wire summarization failed (%s); using fallback render", exc)

    if not sections:
        sections = _fallback_sections(items)

    story_count = sections.lower().count("<li")
    deliver = ""
    html = _TEMPLATE_HEAD.format(
        edition=edition,
        subtitle=f"{subtitle} • {story_count} stories",
        sections=sections,
        deliver_to=deliver or "YouOS",
    )
    return html, story_count


# --- orchestration ----------------------------------------------------------


def _wire_gates() -> dict[str, bool]:
    """Master switch (agent.wire.enabled) + the shared send frontier."""
    from app.agent.send import _send_config

    spec = load_wire_spec()
    send = _send_config()
    return {"enabled": spec.enabled, "send_enabled": bool(send["enabled"]),
            "kill_switch": bool(send["kill_switch"])}


def _accounts() -> list[str]:
    from app.core.config import get_ingestion_accounts, load_config

    return list(get_ingestion_accounts(load_config()))


def is_due(spec: WireSpec, now: datetime) -> bool:
    """Same bounded catch-up window as digests, plus the weekdays-only gate."""
    from app.agent.digest_tasks import _CATCH_UP_HOURS

    if spec.weekdays_only and now.weekday() >= 5:  # Sat/Sun
        return False
    now_min = now.hour * 60 + now.minute
    target = spec.hour * 60 + spec.minute
    return target <= now_min < target + _CATCH_UP_HOURS * 60


def run_wire(database_url: str, *, now: datetime | None = None, dry_run: bool = False,
             days_back: int | None = None) -> dict[str, Any]:
    """Collect → summarize → (gated) send HTML → archive → bump edition.

    ``dry_run`` builds + returns the HTML WITHOUT claiming the period, sending,
    archiving, or bumping the edition (a safe preview, works even when the master
    switch is off). ``days_back`` overrides the configured collection window for
    this run only (e.g. a one-time 7-day backfill); the daily cadence is
    unchanged."""
    from app.agent.digest_tasks import (
        _claim_period,
        _period_done,
        _period_key,
        _record_digested,
        _undigested,
        _update_run,
        _user_tz,
        reap_stale_digest_runs,
    )
    from app.db.bootstrap import ensure_agent_schema

    spec = load_wire_spec()
    if days_back is not None:
        spec.days_back = max(1, int(days_back))
    accounts = _accounts()
    if not accounts:
        return {"status": "error", "name": _WIRE_NAME, "detail": "no configured accounts"}
    from_account = spec.from_account or accounts[0]
    to = spec.deliver_to or from_account
    now_local = now or datetime.now(_user_tz())
    edition = next_edition()

    try:
        ensure_agent_schema(database_url)
    except Exception as exc:
        logger.info("wire schema self-heal skipped: %s", exc)

    # Preview: read-only. Build from whatever is in the window now.
    if dry_run:
        items, _ = collect_wire(spec, accounts)
        if not items:
            return {"status": "preview", "name": _WIRE_NAME, "edition": edition,
                    "count": 0, "html": "", "to": to}
        html, stories = build_wire_html(items, edition, model=spec.summary_model, now=now_local)
        return {"status": "preview", "name": _WIRE_NAME, "edition": edition,
                "count": len(items), "stories": stories, "to": to,
                "subject": _subject(edition, now_local), "html": html}

    gates = _wire_gates()
    if not gates["enabled"]:
        return {"status": "disabled", "name": _WIRE_NAME}

    period = _period_key("daily", now_local)
    if _period_done(database_url, _WIRE_NAME, _WIRE_ACCOUNT, period):
        return {"status": "skipped_done", "name": _WIRE_NAME, "period": period}
    try:
        reap_stale_digest_runs(database_url)
    except Exception as exc:
        logger.info("wire reaper skipped: %s", exc)

    items, manifest = collect_wire(spec, accounts)
    # Empty-day rule (skill step 4): do NOT send/archive/bump on an empty day.
    if not items:
        return {"status": "empty", "name": _WIRE_NAME, "period": period, "edition": edition}

    # Per-message dedup so a 2-day window doesn't re-digest yesterday's items.
    fresh = _undigested(database_url, _WIRE_NAME, _WIRE_ACCOUNT, items)
    if not fresh:
        return {"status": "empty", "name": _WIRE_NAME, "period": period, "edition": edition}
    fresh_ids = {it["id"] for it in fresh}
    manifest = [m for m in manifest if m["id"] in fresh_ids]

    if gates["kill_switch"]:
        return {"status": "blocked", "name": _WIRE_NAME, "period": period, "detail": "outbound kill-switch is on"}
    if not gates["send_enabled"]:
        return {"status": "blocked", "name": _WIRE_NAME, "period": period, "detail": "agent.send.enabled is false"}

    run_id = _claim_period(database_url, _WIRE_NAME, _WIRE_ACCOUNT, period)
    if run_id is None:
        return {"status": "skipped_done", "name": _WIRE_NAME, "period": period}

    html, stories = build_wire_html(fresh, edition, model=spec.summary_model, now=now_local)
    subject = _subject(edition, now_local)
    plain = f"The Wire #{edition} — {len(fresh)} newsletters, {stories} stories. View as HTML."

    from app.ingestion import gmail_write
    try:
        res = gmail_write.send_email(account=from_account, to=to, subject=subject,
                                     body=plain, body_html=html)
    except Exception as exc:
        _update_run(database_url, run_id, "error", message_count=len(fresh), detail=f"send failed: {exc}")
        return {"status": "error", "name": _WIRE_NAME, "period": period, "detail": str(exc)}

    _record_digested(database_url, _WIRE_NAME, _WIRE_ACCOUNT, list(fresh_ids), period)
    _update_run(database_url, run_id, "sent", message_count=len(fresh), body=html,
                sent_message_id=res.message_id, detail=f"sent to {to}; edition {edition}")

    archived = _archive(manifest, spec)
    _bump_edition(edition, date=now_local.strftime("%Y-%m-%d"), emails=len(fresh), stories=stories)
    _update_run(database_url, run_id, "sent", detail=f"sent to {to}; edition {edition}; archived {archived}")
    return {"status": "sent", "name": _WIRE_NAME, "period": period, "edition": edition,
            "to": to, "count": len(fresh), "stories": stories,
            "archived": archived, "sent_message_id": res.message_id}


def _subject(edition: int, now: datetime) -> str:
    return f"📡 The Wire #{edition} — {now.strftime('%a, %b %d')}"


def _archive(manifest: list[dict[str, str]], spec: WireSpec) -> int:
    """Archive collected sources except never-archive senders (allow-list)."""
    from app.ingestion import gmail_write

    archived = 0
    for m in manifest:
        frm = m.get("from", "").lower()
        if any(p in frm for p in spec.archive_exclusions):
            continue
        try:
            gmail_write.modify_message_labels(account=m["account"], message_id=m["id"],
                                              add=[], remove=["INBOX"])
            archived += 1
        except Exception as exc:
            logger.info("wire archive of %s failed: %s", m.get("id"), exc)
    return archived


def run_due_wire(database_url: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Scheduler entry point: run the Wire iff enabled + due + not yet done
    today. No-op (returns None) otherwise."""
    spec = load_wire_spec()
    if not spec.enabled:
        return None
    from app.agent.digest_tasks import _user_tz

    now_local = now or datetime.now(_user_tz())
    if not is_due(spec, now_local):
        return None
    try:
        return run_wire(database_url, now=now_local)
    except Exception as exc:
        logger.warning("wire run failed: %s", exc)
        return {"status": "error", "name": _WIRE_NAME, "detail": str(exc)}
