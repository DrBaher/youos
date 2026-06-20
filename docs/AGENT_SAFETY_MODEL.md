# YouOS agent — safety model & action architecture

> Audience: **developers** working on YouOS's agent. This is the internal
> reference for how the agent acts on mail and how the "never-send / never-act
> by default" invariant is enforced. For the orchestrator-facing REST contract
> see [AGENT_OPERATIONS.md](AGENT_OPERATIONS.md); for the digest design see
> [DIGEST_DESIGN.md](DIGEST_DESIGN.md).
>
> Keep this in sync when you add a path that sends mail or mutates the mailbox.
> Last verified by a cross-cutting audit at **v0.2.0b127** (2026-05-30).

## The core invariant

**Never-send / never-act by default.** Out of the box YouOS drafts replies and
queues them for review; it never sends mail and never mutates the user's mailbox
unless the user has explicitly opened the relevant gates. Every code path that
crosses this boundary is gated, and every gate defaults to the safe (off) value.

Two kinds of boundary-crossing:
- **Outbound (send):** a real email leaves the account. Irreversible. Highest bar.
- **Mailbox mutation:** a label/archive/star/read/important change. Reversible,
  account-internal. Lower bar, but still gated + capped + undoable.

## The chokepoints — the only code that actually sends or mutates

All real Gmail side effects go through **`app/ingestion/gmail_write.py`** (which
performs **no gating itself** — every caller must gate). The complete set:

| function | effect | boundary | gated by (caller) |
|---|---|---|---|
| `send_draft` | send an existing Gmail draft | **outbound** | the send frontier (below) |
| `forward_message` | forward an inbound message | **outbound, irreversible** | send frontier + `allow_forward` |
| `send_email` | compose + send a new email | **outbound** | send frontier (digest `inbox` dest) |
| `create_calendar_event` | create a Google Calendar event (Meet link + invites) | **outbound** when it has attendees (`--send-updates=all` emails invites); else mutation | send frontier + `agent.calendar.create_events.enabled` (`app/agent/calendar_events.py`) |
| `modify_message_labels` | add/remove labels (archive/star/read/important) | mutation | the action framework |
| `ensure_label` | create a user label | mutation | the action framework |

One mutation lives **outside** `gmail_write`: `gmail_label_sync._gog_modify_remove_label`
removes a `YouOS/skip*` label the *user* applied (a reversal of the user's own
gesture, not an autonomous edit). It runs every sweep, gated only by
`agent.enabled`. It is the single mailbox mutation outside the action framework —
intentionally, because it only un-does a YouOS label the user added.

## The gate map (flags + defaults)

All under `agent.*` in `youos_config.yaml`; defaults are the **safe** value.

### Send frontier (outbound) — `app/agent/send.py`
| flag | default | effect |
|---|---|---|
| `agent.send.enabled` | **false** | master programmatic-send switch; required for any real send |
| `agent.outbound_kill_switch` | **false** | when true, blocks **every** send path regardless of any other flag |

`send.py:send_pending_row` is the single chokepoint for all three *draft*-send
entrypoints (manual API, `confirm_send`, auto-send). `forward_message` and the
digest `inbox` send are **separate** chokepoints that each independently re-check
the same two flags. **The kill-switch is the universal off-switch across all
three send paths.**

### Auto-send (autonomous reply sending) — `app/agent/triage.py`
| flag | default | effect |
|---|---|---|
| `agent.auto_send.enabled` | **false** | allow the sweep to send a queued draft without a human |
| `agent.auto_send.mode` | `shadow` | `shadow` runs the full path but records-only (no Gmail send) |
| `agent.auto_send.daily_send_cap` | 0 (off) | max real sends/day |

Auto-send additionally gates on confidence×stakes, recipient trust, and a
delay/undo window, then funnels through `send_pending_row` (so the kill-switch +
`send.enabled` still apply).

### Action framework (mailbox routing) — `app/agent/actions.py`
| flag | default | effect |
|---|---|---|
| `agent.actions.enabled` | **false** | allow label/archive/star/mark_* routing |
| `agent.actions.dry_run` | **true** | record intent only; touch nothing |
| `agent.actions.daily_cap` | 50 | max real actions/day (0 disables) |
| `agent.actions.allow_forward` | **false** | allow the outbound `forward` action |

A real `forward` requires **all of**: `actions.enabled` AND not `dry_run` AND
`allow_forward` AND `send.enabled` AND kill-switch off (five independent gates).
Any closed gate records `blocked` and sends nothing.

### Calendar event creation — `app/agent/calendar_events.py`
| flag | default | effect |
|---|---|---|
| `agent.calendar.auto_confirm.enabled` | **false** | detect a counterparty accepting a proposed slot → queue an event for approval (detection-only; creates nothing) |
| `agent.calendar.create_events.enabled` | **false** | allow an **approved** event to be created (Meet link + invites) |
| `agent.calendar.daily_event_cap` | 5 | max events created/day (0 disables) |

The flow is **detect → queue → human approve → create**. Detection
(`meeting_confirm`) only writes a `pending` row to `agent_pending_events`; the
event is created only when the user approves it. Creating an event with
attendees emails calendar invites, so `apply_pending_event` requires **all of**:
kill-switch off AND `send.enabled` AND `calendar.create_events.enabled` AND
under the daily cap. A closed gate records the reason and **leaves the row
`pending`** (so opening the gate and re-approving works); it never consumes the
row. `create_events.enabled` is in `SEND_FRONTIER_FLAGS` (network-locked — set it
with `youos config set`, never over the API).

### Digest tasks — `app/agent/digest_tasks.py`
| flag | default | effect |
|---|---|---|
| `agent.digests.enabled` | **false** | master switch for digest tasks |

A digest's `destination` decides whether it crosses the send boundary:
- **`agent`** (default): compute + **store** the body for pickup; **sends
  nothing** → never touches the send frontier (needs only `digests.enabled`).
- **`inbox`**: email the digest → requires `digests.enabled` + `send.enabled` +
  kill-switch off (a closed gate records `blocked`).

`then_archive` (archive source messages) is **inbox-only** — it runs only after a
real send and is rejected by `validate_digest` for the `agent` destination, so it
can never become an ungated mutation on the no-send path.

## At-most-once (no double-send / double-act)

Every mutating path has a DB-enforced claim so concurrent sweeps (even in
separate processes — the in-memory per-account lock does **not** span processes)
can't act twice:

| path | claim mechanism |
|---|---|
| draft send | `store.begin_send` conditional `UPDATE … WHERE send_state IN (…)` |
| calendar event create | `event_store.claim_event_create` conditional `UPDATE … WHERE status='pending'` (+ partial UNIQUE `idx_agent_pending_events_slot_claim` blocks duplicate live slots at queue time) |
| push to Gmail | `store.begin_push` conditional UPDATE (`gmail_draft_id IS NULL`) |
| forward | partial UNIQUE index `idx_agent_actions_forward_claim` + INSERT-claims-before-send |
| label/archive/star | `_has_status` dedup (incl. `undone`/`undoing`) before apply |
| digest period | UNIQUE `idx_digest_runs_period` `_claim_period` before send/store |
| digest dedup | UNIQUE `idx_digest_items_dedup`, INSERT OR IGNORE on send |
| undo | atomic `applied → undoing` claim, rollback on gog failure |

A `forward` is **irreversible**: `undo_action` refuses any `forward` row. Label
mutations are reversible: undo is the inverse label add/remove.

## Rules of thumb for changing this code

1. **Any new send/mutate call must go through `gmail_write` and be gated by a
   caller** — never call `gog` to send/modify from elsewhere.
2. **New gates default off.** Read with a safe-false default and an `isinstance`
   guard so malformed config fails closed.
3. **The kill-switch must short-circuit before any new send path.**
4. **Claim before you act**, with a DB-unique/conditional-update claim (not a
   read-then-write), so it holds across processes.
5. **dry-run / shadow / preview must never call the real `gog` send/modify.**
6. **Update this doc** (and the coverage table) when you add a boundary-crossing
   path.
