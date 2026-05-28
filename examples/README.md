# YouOS — examples

Reference orchestrators and integration sketches. These are **not** part of YouOS itself — they show how to wire YouOS into an existing chat / orchestration platform via the REST API documented in `docs/INTEGRATIONS.md`.

## What's here

- **`telegram_bot.py`** — a ~250-line reference Telegram bot. Commands: `/inbox`, `/push <id>`, `/dismiss <id> [reason]`, `/find <words>`, `/digest [days]`, `/help`. Also routes free-text instructions like "push the Q3 thing" via `/api/agent/resolve`. Only the configured Telegram user can drive it. Setup at the top of the file.

## What might land here later

- **`slack_bot.py`** — same surface area, slash-command style.
- **`hermes_skill.json`** — a Hermes-style skill manifest pointing at the YouOS endpoints.
- **`openclaw_integration_test.sh`** — a smoke-test script that hits the full surface end-to-end from outside.

## Patterns these examples follow

1. **Auth via `X-YouOS-Token` header.** Mint with `youos token-create`; store in the orchestrator's env, never commit.
2. **Per-call query params** — `?account=...` on every read, default to user.emails[0] if not configured.
3. **`/api/agent/digest` as the entry point** — orchestrator's first call when the user asks "anything important?"
4. **`/api/agent/resolve` for description → row id** — when the user references something by description ("the Q3 thing"), resolve before acting.
5. **`/api/agent/pending/{id}/{push_to_gmail,dismiss,save_as_feedback_pair}` for actions** — every action is one POST.
6. **Trust boundary at the user, not the orchestrator** — only the authorised user(s) can drive the bot; anyone else gets silently ignored.

See `docs/INTEGRATIONS.md` for the full architecture + security model.
