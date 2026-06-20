"""Whitelisted feature flags — the single set of toggles surfaced by the
`youos config` CLI, the settings page, and the onboarding wizard.

Restricting writes to this whitelist is what makes a (web) config-write path
safe: it can only touch these known keys, never clobber arbitrary config.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.core.config import load_config, save_config

# Each flag: key (dotted), label, type (bool|choice), default, help; choice
# flags also carry `choices`. Keep this list in sync with the config schema.
KNOWN_FLAGS: list[dict[str, Any]] = [
    {
        "key": "generation.multi_candidate.enabled",
        "label": "Multi-candidate drafting",
        "type": "bool",
        "default": False,
        "help": "Generate several drafts and keep the best (slower; more model calls).",
    },
    {
        "key": "generation.repair.enforce_greeting_closing",
        "label": "Enforce greeting/closing",
        "type": "bool",
        "default": False,
        "help": "Add the persona greeting/closing if the model dropped them.",
    },
    {
        "key": "generation.repair.strip_trailing_signature",
        "label": "Strip trailing signature",
        "type": "bool",
        "default": False,
        "help": "Remove a duplicate sign-off from the generated draft.",
    },
    {
        "key": "generation.log_drafts",
        "label": "Log draft events",
        "type": "bool",
        "default": True,
        "help": "Record each draft's conditions so the nightly can learn from them.",
    },
    {
        "key": "generation.abstain.min_quality",
        "label": "Draft-abstain quality floor",
        "type": "float",
        "default": 0.5,
        "min": 0.0,
        "max": 1.0,
        "help": (
            "On the autonomous sweep, withhold a draft whose quality score "
            "(voice + structure; empty/fallback ~0.1) is below this and surface "
            "the email for review with no draft. Lower = offer more mediocre "
            "drafts you can edit (higher recall); higher = withhold more. "
            "Clamped 0.0–1.0. Interactive /draft always drafts; separate from the "
            "auto_push quality floor and never affects the never-send path."
        ),
    },
    {
        "key": "autoresearch.draft_quality_weighting",
        "label": "Draft-quality autoresearch weighting",
        "type": "bool",
        "default": False,
        "help": "Tune retrieval toward the sender cohorts whose drafts you edit most.",
    },
    {
        "key": "personas.routing_enabled",
        "label": "Per-persona generation routing",
        "type": "bool",
        "default": False,
        "help": "Use a per-sender-type LoRA adapter when one is trained.",
    },
    {
        "key": "review.draft_model",
        "label": "Drafting model",
        "type": "choice",
        "default": "auto",
        "choices": ["auto", "local", "claude"],
        "help": "Which model drafts: 'auto' = local fine-tuned model when trained (else Claude); 'local' = always local; 'claude' = always cloud.",
    },
    {
        "key": "model.server.enabled",
        "label": "Warm local-model server",
        "type": "bool",
        "default": True,
        "help": "Keep the local model loaded once (served warm) so drafts are fast. Apple Silicon only; falls back to cloud if unavailable.",
    },
    {
        "key": "ingestion.google_backend",
        "label": "Google ingestion backend",
        "type": "choice",
        "default": "gog",
        "choices": ["gog", "gws", "native"],
        "help": "Which tool fetches Gmail/Docs: the OpenClaw gog CLI, Google's gws CLI, or the native API.",
    },
    {
        "key": "agent.enabled",
        "label": "Autonomous triage",
        "type": "bool",
        "default": False,
        "help": "Sweep your unread inbox in the background and draft replies into /triage. Never auto-sends. Off by default — opt-in.",
    },
    {
        "key": "agent.interval_minutes",
        "label": "Triage interval (minutes)",
        "type": "int",
        "default": 15,
        "help": "How often the background loop sweeps unread mail. Minimum 1; the loop enforces a 60-second floor for safety.",
    },
    {
        "key": "agent.threshold",
        "label": "Needs-reply threshold",
        "type": "float",
        "default": 0.6,
        "min": 0.4,
        "max": 0.85,
        "help": (
            "Score cutoff for drafting a reply. Higher = stricter (fewer "
            "false positives, but more real mail missed); lower = looser. "
            "Clamped to 0.4–0.85. Prefer feedback-driven tuning over manual "
            "tweaks. Read by the background scheduler and /api/agent/triage."
        ),
    },
    {
        "key": "agent.auto_tune_threshold",
        "label": "Auto-tune needs-reply threshold from outcomes",
        "type": "bool",
        "default": True,
        "help": (
            "Let the nightly nudge the needs-reply threshold based on whether "
            "you actually reply to queued drafts: raise it when most go "
            "unanswered (over-drafting), lower it when almost all earn a reply. "
            "Bounded and conservative (one small step per run). Turn off to pin "
            "the threshold to its manual value."
        ),
    },
    {
        "key": "agent.notify_macos",
        "label": "macOS notification on new drafts",
        "type": "bool",
        "default": True,
        "help": "Fire a desktop notification when a triage sweep persists new drafts. Silently no-ops on non-Darwin.",
    },
    {
        "key": "agent.standing_instructions",
        "label": "Standing instructions",
        "type": "text",
        "default": "",
        "help": (
            "Free-form guidance threaded into every triage draft "
            "(e.g. \"today I'm out of office; politely decline meetings\"). "
            "Snapshotted with each draft for auditability. Empty = none."
        ),
    },
    {
        "key": "agent.skip_senders",
        "label": "Never draft for these senders",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains the agent skips outright. "
            "Use exact emails (alice@x.com) or @domain (@bigcorp.com) to "
            "skip a whole org. Hard-skip; runs before scoring."
        ),
    },
    {
        "key": "agent.daily_draft_cap",
        "label": "Daily draft cap",
        "type": "int",
        "default": 50,
        "help": (
            "Maximum new drafts the agent will persist in one UTC day, "
            "per account. Defends against a runaway loop on a noisy "
            "inbox. Set to 0 to disable."
        ),
    },
    {
        "key": "agent.strict_local",
        "label": "Strict local-only (no cloud fallback during triage)",
        "type": "bool",
        "default": False,
        "help": (
            "Refuse cloud fallback during background triage — if the "
            "local model is unavailable, the message is logged as an "
            "error rather than drafted via Claude. Doesn't affect "
            "interactive /feedback or /draft."
        ),
    },
    {
        "key": "agent.accounts",
        "label": "Accounts to sweep (override user.emails)",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated email addresses the background scheduler "
            "should sweep on each tick. Empty falls back to "
            "``user.emails`` (most users don't need to set this — set "
            "user.emails and it Just Works). Use this if you want the "
            "agent to ignore one of your configured accounts."
        ),
    },
    {
        "key": "agent.auto_push.enabled",
        "label": "Auto-push high-confidence drafts to Gmail Drafts",
        "type": "bool",
        "default": False,
        "help": (
            "After a sweep, automatically create a Gmail DRAFT (never sends) for "
            "high-confidence replies to known, whitelisted senders. The human "
            "still finishes-and-sends from Gmail. Off by default; even when on, "
            "it stays in dry-run until you turn dry-run off."
        ),
    },
    {
        "key": "agent.auto_push.dry_run",
        "label": "Auto-push dry-run (log only)",
        "type": "bool",
        "default": True,
        "help": (
            "When on, auto-push only LOGS what it would push (no Gmail write) so "
            "you can watch it for a week before trusting it. Turn off to actually "
            "create the drafts."
        ),
    },
    {
        "key": "agent.auto_push.confidence_floor",
        "label": "Auto-push confidence floor",
        "type": "float",
        "default": 0.85,
        "min": 0.6,
        "max": 1.0,
        "help": "Only auto-push drafts whose needs-reply score is at least this. Clamped 0.6–1.0.",
    },
    {
        "key": "agent.auto_push.quality_floor",
        "label": "Auto-push draft-quality floor",
        "type": "float",
        "default": 0.5,
        "min": 0.0,
        "max": 1.0,
        "help": (
            "Only auto-push when the DRAFT's quality score (voice fidelity + "
            "structure, generic acks ~0) is at least this — so a high "
            "needs-reply score plus a weak draft is held for review, not pushed."
        ),
    },
    {
        "key": "agent.auto_push.known_sender_min_pairs",
        "label": "Auto-push: min prior reply pairs with the sender",
        "type": "int",
        "default": 3,
        "help": "Only auto-push to senders you've already corresponded with at least this many times.",
    },
    {
        "key": "agent.auto_push.daily_push_cap",
        "label": "Auto-push daily cap (per account)",
        "type": "int",
        "default": 5,
        "help": "Maximum drafts auto-pushed per UTC day per account. Bounds blast radius. 0 disables auto-push.",
    },
    {
        "key": "agent.auto_push.whitelist",
        "label": "Auto-push sender whitelist",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains eligible for auto-push. REQUIRED "
            "— with an empty whitelist nothing is auto-pushed (safety). Use exact "
            "emails (alice@x.com) or @domain (@partner.com)."
        ),
    },
    {
        "key": "agent.notify_webhook_url",
        "label": "Proactive push webhook URL",
        "type": "text",
        "default": "",
        "help": (
            "When set, the agent POSTs a digest summary here after a sweep that "
            "has something actionable (change-detected + throttled) so you/your "
            "bot are nudged without polling. The ONE place YouOS makes an "
            "outbound request — metadata only (counts + truncated subjects), "
            "never message bodies. Empty = no push (default)."
        ),
    },
    {
        "key": "agent.notify_webhook_secret",
        "label": "Proactive push webhook secret",
        "type": "text",
        "default": "",
        "help": "Optional shared secret sent as the X-YouOS-Secret header so your receiver can verify the push is from YouOS.",
    },
    {
        "key": "agent.gmail_push.enabled",
        "label": "Gmail real-time push (Pub/Sub)",
        "type": "bool",
        "default": False,
        "help": (
            "Accept Gmail watch→Pub/Sub notifications at /api/gmail/push and fire "
            "an immediate triage sweep (vs the ~15-min poll). Requires the token "
            "below AND Google Cloud setup — see integrations/gmail-pubsub/README.md. "
            "Inert until both this and the token are set."
        ),
    },
    {
        "key": "agent.gmail_push.token",
        "label": "Gmail push shared secret",
        "type": "text",
        "default": "",
        "help": (
            "Shared secret the Pub/Sub push subscription must send as ?token=… on "
            "the webhook URL (the endpoint is public, so this authenticates it). "
            "Use a long random value; compared constant-time."
        ),
    },
    {
        "key": "agent.notify_min_interval_minutes",
        "label": "Min minutes between webhook pushes",
        "type": "int",
        "default": 10,
        "help": "Throttle: at most one webhook push per account per this many minutes, and only when the queue state changed.",
    },
    {
        "key": "agent.summarize_threads.enabled",
        "label": "Summarize long threads",
        "type": "bool",
        "default": False,
        "help": (
            "For a reply on a long thread, generate a 2-3 line 'what changed' "
            "catch-up (on-device, via the warm local model) and store it on the "
            "queued row. Off by default; needs the warm model server."
        ),
    },
    {
        "key": "agent.adjudication.enabled",
        "label": "Borderline LLM adjudication (broadcast veto)",
        "type": "bool",
        "default": False,
        "help": (
            "On borderline needs-reply scores (just over the threshold), ask "
            "the warm local model whether the message is personal or a "
            "broadcast and veto a draft if it's a broadcast. On-device; only "
            "ever demotes, never promotes. Off by default; needs the warm "
            "model server."
        ),
    },
    {
        "key": "agent.adjudication.high",
        "label": "Adjudication upper band",
        "type": "float",
        "default": 0.8,
        "min": 0.6,
        "max": 1.0,
        "help": (
            "Only adjudicate would-be drafts whose needs-reply score is below "
            "this — the least-certain passes. Above it the heuristic is "
            "trusted. Clamped 0.6–1.0."
        ),
    },
    {
        "key": "agent.send.enabled",
        "label": "Allow the agent to SEND drafts (master switch)",
        "type": "bool",
        "default": False,
        "help": (
            "Master switch for any programmatic send (manual API or autonomous "
            "auto-send). Default OFF — with it off, YouOS only ever creates "
            "Gmail drafts, never sends. Crossing this is outward-facing and "
            "hard to reverse; turn it on deliberately."
        ),
    },
    {
        "key": "agent.outbound_kill_switch",
        "label": "Outbound kill-switch (block ALL sends)",
        "type": "bool",
        "default": False,
        "help": (
            "When ON, blocks every send regardless of other flags — one switch "
            "to stop all outbound instantly. Does not affect draft creation."
        ),
    },
    {
        "key": "agent.auto_send.enabled",
        "label": "Autonomous auto-send (opt-in)",
        "type": "bool",
        "default": False,
        "help": (
            "Let the agent SEND drafts that sat past the delay window, passed "
            "escalation (auto-act, low-stakes), and reached enough recipient "
            "trust. Also needs agent.send.enabled. Default OFF, and SHADOW "
            "(log-only) until you set agent.auto_send.mode=live. Caveat: if you "
            "send a pushed draft manually from Gmail, live mode can't tell and "
            "may resend — only enable live once you let the agent own the thread."
        ),
    },
    {
        "key": "agent.auto_send.mode",
        "label": "Auto-send mode",
        "type": "text",
        "default": "shadow",
        "help": (
            "'shadow' (default) records what auto-send WOULD do without touching "
            "Gmail — a safe soak. 'live' actually sends. Switch to live only "
            "after watching shadow runs."
        ),
    },
    {
        "key": "agent.auto_send.delay_minutes",
        "label": "Auto-send delay / undo window (minutes)",
        "type": "int",
        "default": 60,
        "help": (
            "How long a pushed draft must sit before auto-send considers it — "
            "your window to catch a bad one. Auto-send never fires in the same "
            "sweep that created the draft."
        ),
    },
    {
        "key": "agent.auto_send.min_recipient_trust",
        "label": "Auto-send min recipient trust",
        "type": "int",
        "default": 3,
        "help": (
            "How many prior replies to a recipient you must have kept before "
            "auto-send will send to them — a gradual, per-recipient rollout. "
            "New recipients never auto-send until trust accrues."
        ),
    },
    {
        "key": "agent.auto_send.max_per_sweep",
        "label": "Auto-send max per sweep",
        "type": "int",
        "default": 5,
        "help": "Upper bound on how many drafts a single sweep may auto-send.",
    },
    {
        "key": "agent.auto_send.daily_send_cap",
        "label": "Auto-send daily cap",
        "type": "int",
        "default": 5,
        "help": (
            "Max real sends per UTC day across all sweeps — the blast-radius "
            "bound (mirrors the auto-push daily cap). 0 (or less) DISABLES "
            "auto-send entirely; there is no 'unlimited' setting by design."
        ),
    },
    {
        "key": "agent.actions.enabled",
        "label": "Rule-driven mailbox routing (label / archive / star)",
        "type": "bool",
        "default": False,
        "help": (
            "Let the agent ROUTE inbound mail per agent.rules — apply a Gmail "
            "label, archive (route out of the inbox), or star. Account-internal "
            "and reversible (full undo ledger). Off by default; dry-run by "
            "default even when on (see agent.actions.dry_run)."
        ),
    },
    {
        "key": "agent.actions.dry_run",
        "label": "Mailbox routing: dry-run (log only)",
        "type": "bool",
        "default": True,
        "help": (
            "When on (the default), routing records what it WOULD do without "
            "touching Gmail — a safe soak. Turn off to actually apply labels / "
            "archive / star."
        ),
    },
    {
        "key": "agent.actions.daily_cap",
        "label": "Mailbox routing daily cap",
        "type": "int",
        "default": 50,
        "help": "Max real routing actions per UTC day across all sweeps. 0 (or less) DISABLES routing entirely (no 'unlimited' setting, by design).",
    },
    {
        "key": "agent.actions.allow_forward",
        "label": "Mailbox routing: allow forwarding (outbound)",
        "type": "bool",
        "default": False,
        "help": (
            "Lets a 'forward' rule actually send mail to another address. "
            "OFF by default and IRREVERSIBLE (a sent forward can't be undone). "
            "A real forward also requires agent.send.enabled on + the outbound "
            "kill-switch off + routing not in dry-run — every gate must be open."
        ),
    },
    {
        "key": "agent.digests.enabled",
        "label": "Digest tasks (collect → summarize → email)",
        "type": "bool",
        "default": False,
        "help": (
            "Master switch for scheduled digest tasks (configured under "
            "agent.digests.items): each runs a Gmail query, summarizes the "
            "matches with the local model, and sends ONE digest email. OFF by "
            "default; a real send also requires agent.send.enabled + the "
            "outbound kill-switch off (delivery defaults to your own inbox)."
        ),
    },
    {
        "key": "agent.wire.enabled",
        "label": "The Wire (newsletter digest → one HTML email)",
        "type": "bool",
        "default": False,
        "help": (
            "Master switch for The Wire: once a day (default weekdays 19:00) it "
            "fetches the day's newsletters across accounts, extracts every story "
            "with the cloud model, groups them into themed sections, and sends "
            "ONE rich HTML digest — then archives the sources (except an "
            "allow-list). OFF by default; a real send also requires "
            "agent.send.enabled + the outbound kill-switch off. Tune under "
            "agent.wire in youos_config.yaml (hour, weekdays_only, days_back, "
            "summary_model, skip/promo/archive lists)."
        ),
    },
    {
        "key": "agent.extract_facts.enabled",
        "label": "Harvest facts from drafted mail",
        "type": "bool",
        "default": False,
        "help": (
            "When the agent drafts a reply, extract concrete facts the sender "
            "stated (addresses, dates, deadlines) into your memory so this and "
            "future replies are grounded instead of invented. Rule-based, "
            "on-device. Off by default — it writes to your memory table."
        ),
    },
    {
        "key": "agent.calendar.enabled",
        "label": "Propose meeting times from your calendar",
        "type": "bool",
        "default": False,
        "help": (
            "When the agent drafts a reply to a meeting request, read your "
            "calendar free/busy (via the gog CLI) and offer concrete open slots "
            "in the draft. Never creates events — proposes times you send. "
            "Off by default; needs the gog calendar scope authorized. Tune "
            "preferred times in youos_config.yaml under agent.calendar: "
            "preferred_weekdays (e.g. [tue, thu]) + work_start_hour/"
            "work_end_hour for the daily range."
        ),
    },
    {
        "key": "agent.calendar.create_events.enabled",
        "label": "Create calendar events (with Meet links + invites)",
        "type": "bool",
        "default": False,
        "help": (
            "Let the agent create a Google Calendar event (with a Google Meet "
            "link) when you APPROVE a confirmed meeting in the review queue. With "
            "invites it emails the attendees, so it crosses the never-send "
            "frontier: even with this on, nothing is created unless "
            "agent.send.enabled is true and the outbound kill-switch is off. "
            "Network-locked — set it with `youos config set`, not over the API. "
            "Off by default."
        ),
    },
    {
        "key": "agent.calendar.auto_confirm.enabled",
        "label": "Auto-detect meeting confirmations into the queue",
        "type": "bool",
        "default": False,
        "help": (
            "When someone replies accepting one of the open slots the agent "
            "proposed, detect which slot they chose and queue a calendar-event "
            "for your one-tap approval. Detection only writes to the pending-"
            "events queue — it never creates an event; approving does. Off by "
            "default."
        ),
    },
    {
        "key": "agent.calendar.auto_confirm.model",
        "label": "Model for meeting-confirmation detection",
        "type": "text",
        "default": "cloud",
        "help": (
            "Which model decides whether a reply confirmed a proposed slot. "
            "'cloud' (default) uses Claude — much better recall on terse "
            "acceptances ('perfect', a thumbs-up, 'invite to follow') but sends "
            "the reply text off-device; 'local' keeps it on-device (no egress) "
            "at lower recall. Only runs when auto_confirm is on; an unavailable "
            "cloud model falls back to local."
        ),
    },
    {
        "key": "agent.calendar.daily_event_cap",
        "label": "Max calendar events created per day",
        "type": "int",
        "default": 5,
        "help": (
            "Blast-radius cap: the agent will not create more than this many "
            "calendar events in a day (UTC). 0 disables event creation entirely."
        ),
    },
    {
        "key": "agent.labels.status_sync",
        "label": "Tag inbox threads with YouOS status labels",
        "type": "bool",
        "default": False,
        "help": (
            "Reflect YouOS's queue into Gmail labels so the inbox LIST shows "
            "thread state at a glance (colored chips, web + mobile — add-ons "
            "can't draw list icons): YouOS/Drafted, YouOS/Invite-Pending, "
            "YouOS/Follow-up-Owed, YouOS/Awaiting-Reply, YouOS/Urgent, "
            "YouOS/Needs-Review. Only touches those YouOS-owned labels; "
            "reversible. Off by default."
        ),
    },
    {
        "key": "agent.triage.include_read_window",
        "label": "Age ceiling for the read-mail scan",
        "type": "text",
        "default": "90d",
        "help": (
            "When agent.triage.include_read is on, bound the whole-inbox scan to "
            "this Gmail newer_than: window (e.g. '90d', '180d') so a large/old "
            "inbox doesn't make every sweep fetch hundreds of threads. Empty = no "
            "ceiling (scan all inbox mail regardless of age)."
        ),
    },
    {
        "key": "agent.triage.include_read",
        "label": "Draft unanswered read mail too (not just unread)",
        "type": "bool",
        "default": False,
        "help": (
            "By default the sweep only drafts UNREAD inbox mail. With this on it "
            "scans the WHOLE inbox regardless of read state or age and drafts "
            "every thread you haven't replied to yet (skips ones where your "
            "message is the latest). Catches mail you read but never answered. "
            "Costs more model time and can surface drafts for mail you "
            "deliberately left; the daily draft cap still bounds it. Never sends."
        ),
    },
    {
        "key": "agent.vip_senders",
        "label": "VIP senders (prioritized)",
        "type": "text",
        "default": "",
        "help": (
            "Comma-separated emails or @domains whose mail is prioritized — a "
            "strong needs-reply boost so it clears the threshold and sorts to "
            "the top of the queue. Their automation/newsletters are still "
            "hard-skipped. Use exact emails (cofounder@x.com) or @domain."
        ),
    },
    {
        "key": "agent.followup_owed_days",
        "label": "Follow-up: flag unanswered inbound after N days",
        "type": "int",
        "default": 2,
        "help": "A queued email you haven't acted on for this many days is flagged as 'owed' in the digest + /api/agent/followups.",
    },
    {
        "key": "agent.followup_wait_days",
        "label": "Follow-up: flag awaiting-reply after N days",
        "type": "int",
        "default": 4,
        "help": "A reply you pushed/sent with no newer thread activity for this many days is flagged as 'awaiting reply'.",
    },
    {
        "key": "agent.auto_promote_skip_senders",
        "label": "Auto-promote senders dismissed as noise 3+ times",
        "type": "bool",
        "default": False,
        "help": (
            "When a sender has been dismissed as 'noise' 3+ times in the "
            "last 30 days, automatically add them to skip_senders at the "
            "end of each sweep — no click required. Off by default; even "
            "with it on, the user can review promotions in the audit log "
            "and remove any in /settings."
        ),
    },
]

_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in KNOWN_FLAGS}

# Send-frontier flags (b259): the toggles that ARM real outbound (send,
# auto-send, forward, the digest send path) or DISARM the kill-switch. The
# never-send invariant must be tamper-proof against a NETWORK config-write —
# a token-authed orchestrator (the documented integration model) is
# all-or-nothing, so if these were network-writable a compromised/over-broad
# token could flip agent.send.enabled and then send, which the audit
# probe-confirmed. They are therefore set out-of-band ONLY: `youos config
# set` (CLI, local shell) or a direct config-file edit. set_flag refuses them
# on the network path.
SEND_FRONTIER_FLAGS: frozenset[str] = frozenset({
    "agent.send.enabled",
    "agent.outbound_kill_switch",
    "agent.auto_send.enabled",
    "agent.auto_send.mode",
    "agent.actions.allow_forward",
    "agent.digests.enabled",
    "agent.wire.enabled",
    "agent.calendar.create_events.enabled",
})


def known_keys() -> list[str]:
    return [f["key"] for f in KNOWN_FLAGS]


def _get_dotted(cfg: dict, key: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_dotted(cfg: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def coerce_value(flag: dict, raw: Any) -> Any:
    """Coerce a raw (often string) value to the flag's type, or raise ValueError."""
    if flag["type"] == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"expected a boolean (true/false), got {raw!r}")
    if flag["type"] == "choice":
        s = str(raw).strip()
        if s not in flag["choices"]:
            raise ValueError(f"expected one of {flag['choices']}, got {raw!r}")
        return s
    if flag["type"] == "int":
        # Without this branch an int flag fell through to ``return raw`` and a
        # non-numeric value (e.g. an empty /settings field, or ``"abc"`` via
        # /api/config/set) was persisted verbatim — then every bare ``int(...)``
        # read of it (e.g. the scheduler's interval) raised, killing the agent
        # loop. Reject it here so set_flag raises -> the API returns 400.
        if isinstance(raw, bool):  # bool is an int subclass; a flag value isn't a bool
            raise ValueError(f"expected an integer, got {raw!r}")
        try:
            v = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected an integer, got {raw!r}") from exc
        lo, hi = flag.get("min"), flag.get("max")
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)
        return v
    if flag["type"] == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"expected a number, got {raw!r}") from exc
        lo, hi = flag.get("min"), flag.get("max")
        if lo is not None:
            v = max(float(lo), v)
        if hi is not None:
            v = min(float(hi), v)
        return v
    return raw


def list_flags(config: dict | None = None) -> list[dict[str, Any]]:
    """All known flags with their current effective value (config or default).

    Send-frontier flags carry ``network_locked: True`` (b259) so the settings
    UI can disable their controls — they are CLI/config-file only.
    """
    cfg = config if config is not None else load_config()
    return [
        {
            **f,
            "value": _get_dotted(cfg, f["key"], f["default"]),
            "network_locked": f["key"] in SEND_FRONTIER_FLAGS,
        }
        for f in KNOWN_FLAGS
    ]


def get_flag(key: str, config: dict | None = None) -> Any:
    if key not in _BY_KEY:
        raise KeyError(key)
    cfg = config if config is not None else load_config()
    return _get_dotted(cfg, key, _BY_KEY[key]["default"])


class SendFrontierWriteError(PermissionError):
    """A send-frontier flag was set on a network path (b259). Caught by the
    API route and turned into a 403 directing the user to the CLI."""


def set_flag(
    key: str, raw_value: Any, *, config_path: Path | None = None, allow_send_frontier: bool = True
) -> Any:
    """Validate + coerce + persist a flag. Returns the stored value.

    Raises ``KeyError`` for an unknown (non-whitelisted) key and ``ValueError``
    for a value that doesn't fit the flag's type. When ``allow_send_frontier``
    is False (the network path passes this), a send-frontier flag raises
    ``SendFrontierWriteError`` instead of being written — the never-send
    invariant stays tamper-proof against a token-authed caller (b259).
    """
    if not allow_send_frontier and key in SEND_FRONTIER_FLAGS:
        raise SendFrontierWriteError(
            f"{key} controls the send frontier and cannot be set over the network; "
            "set it locally with `youos config set` or edit youos_config.yaml"
        )
    if key not in _BY_KEY:
        if key == "server.pin":
            # server.pin isn't a feature flag — it's a hashed credential. Point
            # the user at the command that actually sets it (and hashes it),
            # instead of a bare "unknown flag" KeyError or a plaintext write.
            raise KeyError(
                "server.pin is not a feature flag; set it with `youos config set-pin <PIN>` "
                "(stored hashed, never plaintext)"
            )
        raise KeyError(f"unknown flag {key!r}; known: {', '.join(known_keys())}")
    value = coerce_value(_BY_KEY[key], raw_value)
    cfg = copy.deepcopy(load_config(config_path))
    if key == "agent.threshold":
        # Stamp the change time so the auto-tuner only counts outcomes from
        # drafts created under the new value (manual Apply and `youos config
        # set` must reset the tuner's evidence window just like the nightly).
        old = _get_dotted(cfg, key, _BY_KEY[key]["default"])
        try:
            unchanged = abs(float(old) - float(value)) <= 1e-9
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            from datetime import datetime, timezone

            _set_dotted(
                cfg,
                "agent.threshold_changed_at",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
    _set_dotted(cfg, key, value)
    save_config(cfg, config_path)
    return value


def derive_os_name(name: str | None) -> str:
    """Personalize the product name from the user's name: ``Baher`` → ``BaherOS``.

    The idea behind YouOS: during setup it becomes *your* OS. Uses the first name
    token, preserving its internal casing (so ``McAvoy`` → ``McAvoyOS``); empty
    input falls back to the generic ``YouOS``.
    """
    raw = (name or "").strip()
    if not raw:
        return "YouOS"
    first = raw.split()[0]
    return f"{first[0].upper()}{first[1:]}OS"


def set_identity(
    name: str | None = None,
    emails: list[str] | None = None,
    *,
    display_name: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist the user's identity (``user.name`` / ``user.emails`` / ``display_name``).

    Used by the onboarding wizard / settings. Not a feature flag, but the same
    controlled, validated write path. When a name is set, the display name is
    auto-derived as ``<First>OS`` (e.g. ``BaherOS``) — unless an explicit
    ``display_name`` is given, or the user has already chosen a custom one (we
    only update display names that still track the previous derived value).
    Returns the stored identity.
    """
    cfg = copy.deepcopy(load_config(config_path))
    user = cfg.setdefault("user", {})
    if not isinstance(user, dict):
        user = {}
        cfg["user"] = user
    old_name = user.get("name", "")
    if name is not None:
        user["name"] = str(name).strip()
    if emails is not None:
        if not isinstance(emails, list):
            raise ValueError("emails must be a list")
        user["emails"] = [str(e).strip() for e in emails if str(e).strip()]
    if display_name is not None:
        user["display_name"] = str(display_name).strip()
    elif name is not None:
        # Auto-derive <First>OS, but don't clobber a custom brand: only set it
        # when there's no display name yet or the current one still tracks the
        # old name's derived value.
        current = str(user.get("display_name", "")).strip()
        if not current or current == derive_os_name(old_name):
            user["display_name"] = derive_os_name(name)
    save_config(cfg, config_path)
    return {
        "name": user.get("name", ""),
        "emails": user.get("emails", []),
        "display_name": user.get("display_name", ""),
    }
