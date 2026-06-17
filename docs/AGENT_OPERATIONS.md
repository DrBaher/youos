# YouOS — Agent operations playbook

**Audience**: LLM-driven agents (Hermes, OpenClaw, a Telegram bot, Claude, a custom orchestrator) operating YouOS at runtime via the REST API.

If you're a human setting up the integration, read `docs/INTEGRATIONS.md` first. This doc assumes the wiring already works and focuses on the **runtime contract**: when to call what, how to handle edge cases, what NOT to do.

---

## 1. First contact

Before the first user-facing call, do this once per session:

1. **Probe the surface**:
   ```
   GET /openapi.json
   ```
   Re-fetch on schema mismatches. The spec is the source of truth — endpoint shapes can change between YouOS versions but the OpenAPI doc tracks reality.

2. **Resolve the user's account**:
   ```
   GET /api/agent/digest
   ```
   If 200 with a non-`null` `account` field, that's the default. If 400 (`"no account configured"`), the user has `user.emails` empty — ask the user which mailbox to operate on and pass `?account=...` on every subsequent call.

3. **Note the `triage_url`**: useful for "open the UI" hand-offs ("if you want to see the full queue, here's the link").

4. **Cache the account + token + base URL** for this session. Don't re-resolve on every call.

---

## 2. Decision tree

| User intent | Endpoint | Follow-up |
|---|---|---|
| "Anything important?" / "What's in my inbox" | `GET /api/agent/digest` | Paraphrase `summary`; if `pending_count > 0`, list top 3 from `pending_preview` |
| "Push the X to Gmail" | `GET /api/agent/resolve?q=X` → `POST /api/agent/pending/<id>/push_to_gmail` | If `count == 1`, push. If `count > 1`, disambiguate (§5). If `count == 0`, suggest `/inbox`. Creates a Gmail **Draft** — does NOT send |
| "Redraft X / make it shorter / change the tone" | `GET /api/agent/resolve?q=X` → `POST /api/agent/pending/<id>/regenerate {instruction, tone_hint?, mode?, persist?}` | Re-runs generation with steering. `persist:false` = preview only (don't overwrite the stored draft); response carries `draft` + `model_used` |
| "Send the X" / "send it for real" | `GET /api/agent/resolve?q=X` → `POST /api/agent/pending/<id>/confirm_send {amended_draft?}` | **Hard-gated** — returns 403 unless the user has enabled `agent.send.enabled` and the kill-switch is off. If 403, fall back to `push_to_gmail` and tell the user to finish-and-send from Gmail. See §7 |
| "Dismiss the X" / "Skip the X" | `GET /api/agent/resolve?q=X` → `POST /api/agent/pending/<id>/dismiss {reason}` | Same disambiguation. Default `reason: "noise"` unless user qualifies |
| "Save my version as a training pair" | `POST /api/agent/pending/<id>/save_as_feedback_pair {edited_reply}` | The user's correction goes in `edited_reply` |
| "What did the agent do today/this week" | `GET /api/agent/sweeps?limit=10` | Render sweep timestamps + counts |
| "How's the filter doing" | `GET /api/agent/observability` | Read `hints` — the agent has rule-based interpretations baked in |
| "Run triage now" | `POST /api/agent/triage` | Synchronous; can take 30-60s per account |
| "Stop drafting for sender X" | `POST /api/agent/skip_senders/promote {senders: ["X"]}` | Already-present senders go in `already_present`, no error |
| "Find the X email" | `GET /api/agent/resolve?q=X` | Return the row list with ids; don't act |

Don't substitute one for another. `/resolve` is read-only; `/dismiss` and `/push_to_gmail` are write actions. Calling `/dismiss` when the user asked "find" is a betrayal.

---

## 3. Idempotency

| Endpoint | Idempotent? | Why |
|---|---|---|
| `GET /api/agent/digest` | Yes | Read-only |
| `GET /api/agent/pending` | Yes | Read-only |
| `GET /api/agent/sweeps` | Yes | Read-only |
| `GET /api/agent/observability` | Yes | Read-only |
| `GET /api/agent/resolve` | Yes | Read-only |
| `POST /api/agent/pending/{id}/dismiss` | Yes | `mark_dismissed` is upsert-style; second call just re-writes the same status |
| `POST /api/agent/pending/{id}/mark_sent` | Yes | Same — sets timestamp again, harmless |
| `POST /api/agent/pending/{id}/amend` | Yes | Overwrites `amended_draft` |
| `POST /api/agent/pending/{id}/regenerate` | **NO** | Generates a fresh draft each call (different text). With `persist:false` it has no side effect (preview only) and is safe to repeat |
| `POST /api/agent/pending/{id}/push_to_gmail` | **NO** (guarded) | Creates a **new Gmail draft each call**. An atomic claim makes a re-push of an already-pushed row return the existing `gmail_draft_id` with `pushed_already:true` rather than duplicating — but a retry *after a timeout* can still duplicate; use the retry check below |
| `POST /api/agent/pending/{id}/send` / `confirm_send` | **NO** | Sends mail. Never auto-retry a send whose outcome you don't know — check the row's send state first |
| `POST /api/agent/pending/{id}/save_as_feedback_pair` | **NO** | Inserts a new `feedback_pairs` row each call |
| `POST /api/agent/triage` | **NO** | Each call runs a fresh sweep against Gmail. Rate-limited: a sweep within `agent.triage_min_interval_seconds` (default 60) of the last one returns **429 + Retry-After** (see §4) |
| `POST /api/agent/skip_senders/promote` | **Effectively yes** | Already-present senders are silently de-duped |

**On `push_to_gmail` retry**: if the first call timed out, check `GET /api/agent/pending/{id}` — if `gmail_draft_id` is set, the push succeeded despite the timeout. Don't retry. If it's still NULL, retry once.

---

## 4. HTTP error handling

| Code | Meaning | User-facing message |
|---|---|---|
| 200 | Success | (parse the body) |
| 400 | Bad request — usually missing/invalid param | Surface the response body's `detail` field verbatim |
| 401 | Token missing or invalid | "Your YouOS API token has expired or was rotated. Mint a new one with `youos token-create` and update the bot config." |
| 403 | Forbidden — sending disabled, kill-switch on, or origin not allowed | "Sending is turned off (YouOS is draft-only by default). I pushed a Gmail draft instead — open Gmail to send." Don't retry; this is a deliberate gate |
| 404 | Row doesn't exist | "I couldn't find row #N — try /inbox to see what's currently pending" |
| 409 | Conflict — e.g. amending a row already pushed | Surface the `detail`; re-read the row with `GET /api/agent/pending/{id}` before retrying |
| 422 | Pydantic validation | Surface the response body. Common: empty `edited_reply`, missing `q`, negative `offset` |
| 429 | Too many sweeps — triage requested within the cooldown | Read the `Retry-After` header (seconds). "Last sweep was just now; I'll re-check in N s." Do NOT loop — honor Retry-After |
| 501 | Backend not implemented | "Native Gmail backend needs the `gmail.compose` OAuth scope. Run `youos setup` to re-authorize, or switch to gog/gws." |
| 502 | Backend error during Gmail write | "Gmail write failed: <response detail>. Try again, or check `gog auth list`." |
| 504 / timeout | Long-running call (usually `/triage`) | "Triage is still running; try `/inbox` in 30s." |
| 5xx (other) | Server-side bug | "YouOS hit an internal error. Tell the user to check `/tmp/youos-serve.log` on the Mac." |

**Never silently swallow a 5xx.** The user needs to know something is wrong so they can fix it.

---

## 5. Disambiguation pattern

When `GET /api/agent/resolve` returns `count > 1`:

1. **Do not pick automatically.** Even if one row has a much higher `match_score`, the user's intent is ambiguous.

2. **Present the rows compactly** with row ids — the row id is the action handle.

3. **Ask which one** in a single short bubble:
   > Found two matches: #12 (Q3 pricing from Alice, today) and #15 (Q3 budget revision from Bob, yesterday). Which one?

4. When the user replies with a row id ("the alice one" → "#12" or "alice's"), use `q=alice` to re-resolve if you don't have a row id directly.

**Do NOT** chain `/resolve` calls to filter further client-side. The substring matcher is shallow on purpose — if the first pass returned 5 matches, asking the user is cheaper than guessing.

---

## 6. Paraphrasing the digest

The `summary` field looks like:
> YouOS (today): 4 pending · 0 pushed · 0 dismissed (11 sweeps)

**Don't restate verbatim** if the user asked a focused question. Instead:

| User asked | Paraphrase |
|---|---|
| "Anything important?" | "Four emails to look at. Top: Q3 pricing from Alice." |
| "How's my inbox?" | "Mostly quiet — 4 pending, agent's swept 11 times today." |
| "What's pending?" | (list `pending_preview` rows, skip the headline) |
| "Did the agent draft anything today?" | "Yes — 4 drafts ready. Want to push any to Gmail?" |
| "Anything new since this morning?" | (re-issue `/digest?days=1`; compare to previous if you cached one) |

**Empty queue**:
- "Inbox is clear — nothing waiting." (preferred)
- NOT: "0 pending, 0 pushed, 0 dismissed across 0 sweeps." (mechanical)

**Long lists** (`pending_preview` has 5 rows):
- Show top 3 with subject + sender; offer to list the rest.
- Compact format: `#13 [draft 0.70] Q3 pricing  ←  alice@partner.com`

---

## 7. Trust boundaries

What YouOS will never let you do:

- **Send mail by default.** `POST .../send` and `POST .../confirm_send` exist, but they are **hard-gated OFF**: every send requires `agent.send.enabled: true` AND `agent.outbound_kill_switch: false`, both of which default to draft-only. With the defaults, those endpoints return **403** and the only outbound action available to you is `push_to_gmail` (writes a Gmail Draft; the user finishes-and-sends). Never claim "I sent it" off the back of a `push_to_gmail` — say "I pushed the draft to Gmail; open Gmail to send." Only treat a send as done after a `2xx` from `send`/`confirm_send`. (The flags being on is the user's explicit, standing authorization; see `AGENT_SAFETY_MODEL.md`.)
- **Read raw mail content outside the queue.** You can read what's already in `agent_pending_drafts` (subject + body of inbounds the agent processed). You cannot fetch arbitrary Gmail threads via this API — by design, YouOS exposes its *triaged judgment*, not raw mailbox access. To pull in new mail, trigger a sweep (`POST /api/agent/triage`, rate-limited) and read the resulting queue.
- **Modify identity / accounts.** Adding accounts is a human-driven `gog auth login` + `user.emails` config edit.
- **Disable safety guards.** `agent.strict_local` / `agent.daily_draft_cap` / `agent.skip_senders` are user-set; the agent can read them via `/api/config/flags` but should not change them on the user's behalf without explicit confirmation.

What the agent SHOULD NOT do without explicit confirmation:

- **Call `mark_sent`** unless the user said "I sent that" (not "push it"). `mark_sent` is the "I sent outside YouOS" signal — calling it after a `push_to_gmail` would be wrong (the row is already in status='sent' from the push).
- **Call `push_to_gmail` on a `surface` tier row.** Surface rows have no draft. The endpoint returns 400 but the right behavior is to say "that row was just surfaced for review — there's no draft to push."
- **Auto-promote senders to skip_senders** unless explicitly asked, even if you see they have 3+ noise dismissals. The user has `agent.auto_promote_skip_senders` for that.
- **Re-trigger triage** in a tight loop. One call ~ one fetch+draft cycle (30-60s, uses gog auth + model). The server now **enforces** this: a sweep within `agent.triage_min_interval_seconds` (default 60) of the last one returns 429 + `Retry-After`. Honor the header rather than retrying blindly, and tell the user "the last sweep was N seconds ago — try again at HH:MM."

---

## 8. Per-action side effects (DB + Gmail)

| Action | `agent_pending_drafts` change | `feedback_pairs` change | Gmail change |
|---|---|---|---|
| `dismiss` | `status → dismissed`, `dismissal_reason` set, `dismissed_at` set | — | — |
| `mark_sent` | `status → sent`, `sent_at` set | — | — |
| `amend` | `status → amended`, `amended_draft` set | — | — |
| `push_to_gmail` | `status → sent`, `gmail_draft_id` set, `sent_at` set | — | New draft created in Gmail Drafts folder on the original thread |
| `save_as_feedback_pair` | (row stays untouched — it can also be pushed/dismissed separately) | New row inserted | — |
| `skip_senders/promote` | (next sweep will hard-skip these senders) | — | — |
| `triage` | New rows inserted for messages above threshold | — | — |
| Background scheduler tick (every `agent.interval_minutes`) | Same as `triage` per account | — | — |

Knowing the side effects lets you give honest confirmations: "Done — Gmail draft `r1234` created, ready in your Drafts folder. The triage queue is updated."

---

## 9. Multi-account

If the user has multiple accounts (`user.emails`), every endpoint takes `?account=...`. If you don't pass one, you get `user.emails[0]` — usually fine, but:

- For "anything in my work mail" the user means a specific account. Ask if ambiguous.
- For "agent digest" without qualifier, the headline `account` field in the response tells you which mailbox you got. If the user wanted the other one, re-fetch with explicit `?account=`.
- For `/api/agent/triage` (manual trigger), always pass `account=` explicitly. The default account on a 2-account user is too ambiguous for a write action.

---

## 10. Conversational patterns to follow / avoid

### Follow

- **Confirm before destructive actions**: "Dismiss row #14 (EON Agent — Morning Light) as `wrong_content`? (yes/no)" — but only if the action is being inferred from a description. Direct `/dismiss 14 wrong_content` doesn't need confirmation.
- **Surface the action handle** in confirmations: "Pushed row #13 to Gmail (draft id `r1234`)." — lets the user verify on Gmail's side.
- **Quote what changed**: "Added `newsletter@daily.com` to skip_senders. They won't be drafted again."
- **Mention the next sweep timing** when relevant: "Effective on the next sweep (in ~14 minutes)."

### Avoid

- **Speculation about content**: don't paraphrase the body of an email back at the user. The user will recognize what's in their inbox by subject + sender — they don't need you to summarize body content they didn't ask about.
- **Defensive language**: "I think this might be what you mean, possibly..." — just propose the match and act, or ask.
- **Restating the user's instruction**: "You asked me to push row #13" — wastes a bubble.
- **Reporting zero counts**: "Dismissed 0 errors, 0 warnings." — irrelevant.

---

## 11. When the user wants to learn the agent

These are advanced flows. The agent (you) should generally not initiate them — the user usually drives them:

- **Save as training pair** (`/save_as_feedback_pair`): the user has a better version of the agent's draft. Take their reply text and POST it. The next nightly LoRA retrain consumes the row.
- **Adjust standing instructions** (`/api/config/set` with `agent.standing_instructions`): user is going OOO or wants different tone for a week. Write to the flag. Tell the user: "Set. Next sweep will use this in every draft prompt."
- **Set a threshold** (`/api/config/set` with `agent.threshold`): user is getting too many false positives (or missing real mail). 0.6 default; up to ~0.75 for stricter, down to 0.5 for looser. Encourage feedback-driven tuning (dismissals + `noise` reason) over manual threshold tweaks.
- **Adjust the draft-abstain floor** (`/api/config/set` with `generation.abstain.min_quality`): on the autonomous sweep a draft scoring below this quality floor is withheld and the email is surfaced for review with *no* draft. 0.5 default. Lower it (e.g. 0.25) when the user reports threads surfaced with no draft that they'd have replied to — it offers more mediocre-but-editable drafts (higher recall) while still withholding empty/degenerate (~0.1) ones. Raise it to be more conservative. Interactive `/draft` always drafts regardless; this never affects the never-send path.

> **Advanced (YAML / infra):** `snapshots.keep_daily` (default 30) bounds retained daily DB snapshots under `var/snapshots/daily/`. On a tight disk, lower it (e.g. 10) — the nightly's `prune_snapshots` enforces it. Not a `/settings` toggle; set in `youos_config.yaml` or via the config file directly.

---

## 12. Versioning + capability discovery

This doc reflects the surface as of YouOS **b63**. The contract is:

1. `GET /openapi.json` is the canonical surface — if this doc and the spec disagree, the spec wins.
2. `app.state.settings.version` (also surfaced in `/healthz` and `/readyz`) is the running version. Compare to your cached docs version on first contact; refetch openapi if mismatched.
3. New endpoints land in `/api/agent/`. Old endpoints don't get removed — they get a `deprecated: true` flag on the OpenAPI operation.

---

## 13. Worked example — full conversation

User says: *"What's in my inbox today?"*

```
GET /api/agent/digest?days=1
→ {
    "summary": "YouOS (today): 4 pending · 0 pushed · 0 dismissed (11 sweeps)",
    "pending_preview": [
      {"id": 13, "tier": "draft", "needs_reply_score": 0.7,
       "subject": "Would you be interested in...", "sender": "Standart..."},
      {"id": 14, "tier": "draft", "needs_reply_score": 0.7,
       "subject": "The Science Behind Morning Light...", "sender": "EON Agent..."},
      ...
    ],
    "account": "drbaher@gmail.com",
    "triage_url": "http://bbots-mac-mini:8765"
  }

Agent → User: "Four drafts ready. Top ones:
  #13 Random Coffee Vienna (Standart Community)
  #14 Morning Light (EON Agent)
What do you want to do?"
```

User: *"Push the coffee one"*

```
GET /api/agent/resolve?q=coffee
→ {"count": 1, "rows": [{"id": 13, "subject": "Would you be interested in \"Random Coffee\"...", ...}]}

POST /api/agent/pending/13/push_to_gmail
→ {"ok": true, "gmail_draft_id": "r1234567890", "row": {"status": "sent", ...}}

Agent → User: "Pushed #13 to Gmail (draft `r1234567890`). Open Gmail to send."
```

User: *"Dismiss the morning light one as wrong content"*

```
GET /api/agent/resolve?q=morning light
→ {"count": 1, "rows": [{"id": 14, "subject": "The Science Behind Morning Light..."}]}

POST /api/agent/pending/14/dismiss {"reason": "wrong_content"}
→ {"ok": true, "row": {"status": "dismissed", "dismissal_reason": "wrong_content", ...}}

Agent → User: "Dismissed #14 as wrong_content. The agent will use this as a training signal in tonight's LoRA retrain."
```

User: *"How's my filter doing?"*

```
GET /api/agent/observability
→ {
    "sweep": {"sweeps": 11, "successful": 11, ...},
    "dismissals": {"by_reason": {"wrong_content": 1, ...}},
    "score_histogram": {...},
    "hints": []
  }

Agent → User: "11 sweeps today, all clean. Just one dismissal so far (the one you just did). Filter looks healthy."
```

This is the steady-state shape. Each user turn → 1–2 YouOS calls → 1 chat bubble back. Nothing else.

---

## 14. See also

- `docs/INTEGRATIONS.md` — architecture + setup recipe + reference Telegram bot
- `docs/REMOTE_ACCESS.md` — Tailscale, PIN, multi-account, Gmail-label dismissals
- `docs/USAGE.md` — CLI command reference
- `docs/ARCHITECTURE.md` — internal architecture of the agent loop
- `SKILL.md` — OpenClaw skill manifest (trigger phrases, install)
- `GET /openapi.json` — canonical surface contract
- `GET /docs` — Swagger UI
