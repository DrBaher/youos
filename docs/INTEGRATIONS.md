# Integrations — driving YouOS from another agent

YouOS is a **local-first email backend** with a clean REST surface. The whole agent loop (triage, draft, queue, dismiss, push to Gmail) is also reachable as an HTTP API — which means orchestrator-style agents like [OpenClaw](https://openclaw.dev), [Hermes](https://github.com/your-org/hermes), a Telegram bot, or a Slack bot can talk to YouOS the same way `/triage` does, and surface results in whatever channel the user already lives in.

This doc covers the wiring.

> **If you're an LLM agent operating YouOS at runtime** (Hermes, OpenClaw, a chat bot, Claude in a tool-use loop) — read [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md). This doc covers the wiring; that doc covers the runtime contract (decision tree, idempotency, error handling, paraphrasing, trust boundaries, worked conversation).

## The vision

```
                 ┌────────────────────────┐
You on phone ←→  │  Telegram / WhatsApp /  │  ←→  Hermes / OpenClaw / a bot
                 │  Slack — your chat      │            │
                 └────────────────────────┘            │ HTTP + token
                                                       ↓
                                          ┌─────────────────────┐
                                          │  YouOS server       │
                                          │  (Mac, Tailscale)   │
                                          │   /api/agent/...    │
                                          └─────────────────────┘
                                                       │
                                                       │ gog / gws / native
                                                       ↓
                                                   Gmail
```

You ask "what's in my inbox?" in Telegram. Your orchestrator (Hermes / OpenClaw) calls YouOS, gets a structured summary, paraphrases it back. You ask "push #12" — orchestrator hits `POST /api/agent/pending/12/push_to_gmail`. Done. **You never leave your chat app.**

## What's already there (no changes needed)

| Surface | Where |
|---|---|
| OpenAPI spec | `GET /openapi.json` — FastAPI-generated, complete |
| Swagger UI | `GET /docs` — interactive |
| API-token auth | `X-YouOS-Token: <token>` or `Authorization: Bearer <token>` header |
| Token issuance | `youos token-create` (one-time print; stored hashed) |
| Token revocation | `youos token-revoke <prefix>` (one) · `youos token-revoke --all` |
| Per-account isolation | every endpoint accepts `account=` query param |
| Structured digest | `GET /api/agent/digest` returns summary + counts + pending preview + actions |

## Setup recipe

### 1. Make YouOS reachable

Follow `docs/REMOTE_ACCESS.md` to set up Tailscale + a non-loopback bind. Don't set a PIN if you're only going to use token auth — PIN gates browser access (cookies); tokens gate API access.

### 2. Mint an API token for your orchestrator

```bash
youos token-create
# → API token created. Paste it into the YouOS extension Options — it is not shown again:
#
#   abc123def456-LONG-RANDOM-STRING
#
# Stored hashed on disk. Revoke one with `youos token-revoke <prefix>`, all with `--all`.
```

Copy the token. Store it in your orchestrator's config (Hermes config file, OpenClaw secrets, Telegram-bot env var) — never commit it.

### 3. From your orchestrator, hit YouOS

Every call needs the token header:

```bash
curl -s -H "X-YouOS-Token: abc123..." \
     "http://bbots-mac-mini:8765/api/agent/digest?days=1" \
     | jq .summary
# → "YouOS (today): 1 pending · 0 pushed · 0 dismissed (8 sweeps)"
```

That `summary` field is what the orchestrator paraphrases into the chat bubble.

## Orchestrator playbook

A typical conversation:

**User**: "Anything important?"
**Orchestrator**: `GET /api/agent/digest?days=1` → reads `summary` + `pending_preview`
**Orchestrator** (in chat): "1 draft pending. Top: Q3 pricing from alice@partner.com (score 0.85)."

**User**: "Push #12 to Gmail Drafts"
**Orchestrator**: `POST /api/agent/pending/12/push_to_gmail`
**Orchestrator**: "Done — Gmail draft `r1234...` created on the original thread. Finish-and-send from Gmail."

**User**: "Dismiss it as noise"
**Orchestrator**: `POST /api/agent/pending/12/dismiss` with `{"reason": "noise"}`
**Orchestrator**: "Dismissed. Sender added to noise candidates."

**User**: "Save my version as a training pair" (with the user's correction in the message body)
**Orchestrator**: `POST /api/agent/pending/12/save_as_feedback_pair` with `{"edited_reply": "user's version"}`
**Orchestrator**: "Saved as training pair #47. Will feed into the next nightly LoRA retrain."

## Endpoint reference

| Verb | Path | Purpose |
|---|---|---|
| GET | `/api/agent/digest?account=&days=1` | Headline + structured counts + pending preview |
| GET | `/api/agent/pending?account=&tier=&status=&limit=&offset=` | Full pending queue. Page with `offset`; response carries `limit`/`offset`/`has_more` |
| GET | `/api/agent/pending/{id}` | Fetch one row (retry-safety after a timed-out push) |
| GET | `/api/agent/sweeps?account=&limit=&offset=` | Audit log of recent sweeps (paginated) |
| GET | `/api/agent/followups?account=` | Open loops the agent is tracking |
| GET | `/api/agent/observability?account=&days=30` | Sweep stats + dismissal aggregate + score histogram + hints |
| GET | `/api/agent/dismissal_stats?account=&days=30` | Dismissal-rate aggregate |
| GET | `/api/agent/skip_sender_candidates?account=&min_count=2&days=30` | Senders the user has dismissed as noise; ready to promote |
| POST | `/api/agent/skip_senders/promote` `{senders: [list]}` | Bulk-add to `agent.skip_senders` |
| POST | `/api/agent/pending/{id}/amend` `{amended_draft}` | Save edited draft text |
| POST | `/api/agent/pending/{id}/regenerate` `{instruction?, tone_hint?, mode?, persist?}` | Re-draft with steering (`persist:false` = preview only) |
| POST | `/api/agent/pending/{id}/dismiss` `{reason?}` | Dismiss (optional categorical reason) |
| POST | `/api/agent/pending/{id}/mark_sent` | Mark sent (for "I sent manually outside YouOS") |
| POST | `/api/agent/pending/{id}/push_to_gmail` | Create real Gmail **Draft** on original thread (does not send) |
| POST | `/api/agent/pending/{id}/send` | **Hard-gated send** — 403 unless `agent.send.enabled` + kill-switch off |
| POST | `/api/agent/pending/{id}/confirm_send` `{amended_draft?}` | **Hard-gated** one-call (optional edit →) push → send; same gates as `/send` |
| POST | `/api/agent/pending/{id}/save_as_feedback_pair` `{edited_reply, rating?, feedback_note?}` | Feed correction into LoRA training |
| POST | `/api/agent/triage` `{account, window, limit, threshold, backend?}` | Trigger a fresh sweep on demand. Rate-limited: **429 + Retry-After** within `agent.triage_min_interval_seconds` (default 60) of the last sweep |

> **Sending is off by default.** `push_to_gmail` writes a Gmail Draft and is the only outbound action available out of the box. `send`/`confirm_send` exist but return **403** until the user sets `agent.send.enabled: true` (and leaves `agent.outbound_kill_switch: false`) — the never-send-without-authorization invariant. See `AGENT_SAFETY_MODEL.md`.
>
> **No raw-inbox read, by design.** There is no endpoint to fetch arbitrary Gmail threads — agents act on YouOS's *triaged* queue. To ingest new mail, run a sweep (`POST /api/agent/triage`) and read the resulting `pending` rows.

## Token-auth contract

Every API call returning anything other than the rendered HTML pages needs **either**:
- `X-YouOS-Token: <token>` header, OR
- `Authorization: Bearer <token>` header

Tokens are stored hashed (PBKDF2 — same as PINs); plaintext is shown once at creation. `youos token-list` shows each token's prefix + creation date (never the plaintext).

Revoke a single compromised token by its prefix: `youos token-revoke <prefix>` (the prefix comes from `youos token-list`). Revoke everything with `youos token-revoke --all`. Tokens minted before per-token revocation landed have no addressable prefix — clear them with `--all`.

## OpenAPI spec for tool-discovery

LLM-driven orchestrators that want to discover the YouOS surface dynamically can fetch:

```
GET /openapi.json
```

This returns the full FastAPI-generated OpenAPI 3.x document with every endpoint, parameter, request body schema, and response shape. Tools like LangChain's `OpenAPISpec.from_url` consume this directly.

The `summary`/`description` fields on each route come from the docstrings, so the orchestrator gets human-readable explanations of what each endpoint does.

## Reference Telegram bot — `examples/telegram_bot.py`

A working ~250-line reference bot is shipped in `examples/telegram_bot.py`. Setup at the top of the file. Commands:

| Command | Calls |
|---|---|
| `/inbox` | `GET /api/agent/digest?days=1` — summary + top-5 pending with ids |
| `/push <id>` | `POST /api/agent/pending/<id>/push_to_gmail` |
| `/dismiss <id> [reason]` | `POST /api/agent/pending/<id>/dismiss {reason}` (defaults to `noise`) |
| `/find <words>` | `GET /api/agent/resolve?q=<words>` — substring-rank pending rows |
| `/digest [days]` | extended digest |
| `/help` | command list |

The bot also accepts free-text — phrases like *"push the Q3 thing"* are routed via `/api/agent/resolve` to a row id, then dispatched. Substring matching only (a real production orchestrator would route through an LLM here).

Only one Telegram user (set via `TELEGRAM_AUTHORIZED_USER` env var) can drive the bot. Anyone else gets silently ignored — without this, every Telegram user on the platform could control your inbox.

Run:

```bash
pip install 'python-telegram-bot==21.*' requests
export YOUOS_URL=http://bbots-mac-mini:8765
export YOUOS_TOKEN=<from `youos token-create`>
export YOUOS_ACCOUNT=drbaher@gmail.com  # optional; falls back to user.emails[0]
export TELEGRAM_TOKEN=<from @BotFather>
export TELEGRAM_AUTHORIZED_USER=<your Telegram numeric id; see @userinfobot>

python examples/telegram_bot.py
```

## Example: minimal Telegram bot wiring (sketch)

```python
import os, requests
from telegram.ext import Application, CommandHandler

YOUOS = os.environ["YOUOS_URL"]            # http://bbots-mac-mini:8765
TOKEN = os.environ["YOUOS_TOKEN"]
HEAD  = {"X-YouOS-Token": TOKEN}

def digest(update, ctx):
    r = requests.get(f"{YOUOS}/api/agent/digest?days=1", headers=HEAD).json()
    msg = r["summary"]
    for row in r["pending_preview"]:
        msg += f"\n#{row['id']}  {row['subject']}  ←  {row['sender']}"
    update.message.reply_text(msg)

def push(update, ctx):
    row_id = int(ctx.args[0])
    r = requests.post(f"{YOUOS}/api/agent/pending/{row_id}/push_to_gmail", headers=HEAD).json()
    update.message.reply_text(f"Pushed: {r.get('gmail_draft_id', '?')}")

def dismiss(update, ctx):
    row_id = int(ctx.args[0])
    requests.post(f"{YOUOS}/api/agent/pending/{row_id}/dismiss",
                  json={"reason": "noise"}, headers=HEAD)
    update.message.reply_text("Dismissed.")

app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(CommandHandler("inbox", digest))
app.add_handler(CommandHandler("push", push))
app.add_handler(CommandHandler("dismiss", dismiss))
app.run_polling()
```

That's ~30 lines. The complexity is in the orchestrator's NLU layer (parsing "push the Q3 thing" → `row_id=12`), not in talking to YouOS.

## Hermes / OpenClaw

YouOS already ships an OpenClaw bundle (`clawhub.json` at the repo root + `SKILL.md` describing the agent surface). For Hermes-style orchestrators, the wiring is simpler — point them at the YouOS URL + token and let them discover the surface via `/openapi.json`.

Future: a dedicated **Hermes skill manifest** (similar to clawhub.json but Hermes-flavored) is a small follow-up if the integration takes off. For now, the OpenAPI spec is the contract.

## Security model

- **Tailscale provides the network boundary.** Only devices on your Tailnet can reach the IP.
- **Tokens provide the API gate.** Even on the Tailnet, a request without a valid token gets 401.
- **PIN provides the browser gate.** If you set a PIN, browser visits to `/triage` need it. Tokens still work for API.
- **No external egress** from YouOS to anywhere except the user's Gmail. Orchestrators *pull* from YouOS; YouOS doesn't *push* to orchestrators (so a compromised orchestrator can read+act on your inbox but can't pivot back out).

If your orchestrator is on a different machine than YouOS, both need to be on the same Tailnet. If the orchestrator is hosted (cloud), you'd need either (a) Tailscale's userspace-mode networking in the orchestrator container or (b) Funnel'ed exposure — both more complex than the local-Tailscale story this doc covers.
