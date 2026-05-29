# Digest design

Status: **proposal / under design** (2026-05-29). Digests are built but **off**
(`agent.digests.enabled=false`); this doc settles the model before we enable.

## What a digest is

A **digest** turns a set of emails into one summary, on a cadence. It is a
*scheduled batch task* — distinct from the per-message [rules](../app/agent/rules.py)
(which route one message at a time). Three things define a digest, and all three
are the user's to choose:

| part | answers | example |
|------|---------|---------|
| **query** | *which* emails | `category:promotions newer_than:1d` |
| **prompt** | *what to make of them* | "Group by sender; flag anything time-sensitive; one line each." |
| **destination** | *where the summary goes* | `inbox` or `agent` |

Plus the operational knobs already built: `schedule` (daily/weekly), `weekday`,
`hour`/`minute`, `account`, `summary_model` (local/cloud), `max_messages`,
`then_archive`, per-message dedup.

## 1. Content is a user prompt (not hardcoded)

Today the summary instruction is fixed in code. Instead, each digest carries a
**`prompt`** — the user's own instruction for what the digest should be:

- *"What needs my attention? Flag anything time-sensitive or awaiting my reply."*
- *"One skimmable line per newsletter, grouped by topic."*
- *"Extract every action item and deadline as a checklist."*

This subsumes the "what is a digest *for*" question — the user encodes the
purpose. The prompt is the core instruction; YouOS still wraps it with the
fetched items (sender/subject/date) and falls back to a plain list if the model
is unavailable. Empty `prompt` ⇒ a sensible default ("concise skimmable digest").
Local or cloud (`summary_model`) summarizes per the prompt.

## 2. Destination: `inbox` or `agent`

- **`inbox`** — YouOS composes and **sends** the digest as an email to the
  account's inbox (or `deliver_to`). This is a real outbound send, so it stays
  behind the full send frontier: `agent.digests.enabled` **and**
  `agent.send.enabled` **and** the outbound kill-switch off. At-most-once per
  period via the claim.

- **`agent`** — YouOS **computes the digest and returns it**; it sends nothing.
  An orchestrating agent collects it via the CLI / MCP / REST API and delivers
  it wherever it likes (Telegram, Slack, a chat reply, …). **This path does not
  cross the never-send frontier** — no email leaves YouOS, so it needs only
  `agent.digests.enabled`, not `send.enabled`. YouOS produces data; the agent
  owns delivery.

This is the key safety property: the default, lowest-risk way to use digests is
`destination: agent` — YouOS never sends anything; it just answers "here's your
digest" when asked. The inbox destination is the deliberate, gated exception for
people who want it to land in their mailbox without an orchestrator.

## 3. How the agent collects an `agent`-destination digest

Two complementary modes (recommend supporting **both**):

- **On-demand (pull):** the orchestrator asks for it now —
  `POST /api/agent/digests/run {name}` returns the computed body inline, or a CLI
  `youos digest run <name>`. (This is today's preview, promoted to a first-class
  mode.) Stateless; no schedule needed.
- **Scheduled + stored:** the scheduler computes `agent`-destination digests on
  their cadence and **stores the body** in the run ledger (status `ready`). The
  orchestrator polls `GET /api/agent/digests` (or a `…/pending` endpoint / CLI)
  for ready digests, reads the body, and delivers it. The period claim keeps it
  to once per period; the orchestrator just reads the latest ready run.

**Decided ▸ support both.** On-demand always works; the scheduler additionally
computes `agent` digests on cadence and stores the body (status `ready`) for an
orchestrator that only polls. Pickup is a `ready → collected` ack so a digest is
delivered once.

## 4. Dedup & accounts (already built)

- **Per-message dedup** (`agent_digest_items`): a message is summarized at most
  once per digest. Open ▸ keep per-digest scope, or make it global so a message
  digested by *any* digest isn't repeated by another.
- **Per-account** (`account` field): a digest runs for one account or all. For
  `agent` destination, "all accounts" could instead mean *one digest aggregating
  across accounts* — open decision.

## 5. Build delta from what exists

Already built: query, schedule (daily/weekly + weekday + hour/minute + bounded
catch-up), per-account, summary_model, dedup, `inbox` send (gated), `/digests`
UI + CRUD API, on-demand run/preview.

To build for this design:
1. **`prompt`** field on the digest spec → threaded into `build_digest_body`.
2. **`destination`** field (`inbox` | `agent`); `agent` skips the email/send-gate
   path and returns/stores the body instead.
3. (If scheduled+stored chosen) a **body column** on `agent_digest_runs` + a
   pull endpoint and a `youos digest` CLI command.
4. UI + validation for the two new fields.

## Decisions (signed off 2026-05-29)

1. **Pickup model** for `agent` destination: **both** — on-demand pull AND
   scheduled+stored (`ready → collected` ack).
2. **Default destination**: **`agent`** (compute + return, no send) — the safe,
   ungated default. `inbox` is the explicit, send-gated opt-in.
3. **Prompt default** (when blank): a concise skimmable digest — one short line
   per email + a "worth attention" line (today's wording).
4. **Dedup scope**: **per-digest** (current). Global is a possible later change.
5. **Cross-account**: **one digest per account** (current). Aggregation later.
