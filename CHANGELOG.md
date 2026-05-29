# Changelog

## v0.2.0-beta.116 — 2026-05-29

### Digest tuning + hardening: choose local or cloud summary, and the fetch-cap fix

Fine-tuning the digest task before it goes live:

- **Configurable summary model (per digest).** New `summary_model: local | cloud` on a digest spec. `local` (default) uses the warm on-device model — **no egress**. `cloud` uses Claude (via the existing `_call_claude_cli`) for a sharper summary; note it sends the matched messages' **senders/subjects/dates** (not bodies) off-device. The summary model never affects the send gates — it only shapes the digest body, and falls back to a plain itemised list if the chosen model is unavailable.
- **Tighter summary prompt.** The small local model was rambling and repeating itself; the prompt now asks for one short bullet per email + a single "Worth attention" line, no preamble, no repetition, under 150 words.
- **Hardening — the fetch cap.** `gog gmail messages search` defaults to `--max=10`, and the digest fetch wasn't passing `--max` — so a `max_messages: 50` digest silently only ever saw 10. It now passes `--max <max_messages>`, so the configured cap actually applies.

A focused review confirmed the cloud path is summary-only (no send-gate/at-most-once/dry-run impact), errors fall back safely, and the subject/sender text is passed as a single argv element (no shell injection). +3 tests. Digests remain off by default.

## v0.2.0-beta.115 — 2026-05-29

### Digest preview works before the feature is enabled

Small follow-up to b114: `run_digest`'s `dry_run` preview is read-only (fetch + summarize; no send, no DB write, no period claim), so it now runs **regardless of `agent.digests.enabled`** — you can preview exactly what a digest would contain while the feature (and sending) stays fully gated off. Only *real* runs require the master flag. +1 test.

## v0.2.0-beta.114 — 2026-05-29

### Digest tasks — collect → summarize → deliver one email (scheduled)

A new abstraction beyond per-message rules: a **digest task** runs a Gmail query, summarizes the matching messages *together* with the warm local model, and sends **one** digest email — optionally archiving the collected messages afterward. This is the "pull a collection, summarise, deliver in one digest" workflow that the per-message rule engine (and the NL box) structurally couldn't express.

Configured under `agent.digests` (a dict so the master flag and specs coexist):
```yaml
agent:
  digests:
    enabled: false
    items:
      - name: Newsletters
        query: "label:Newsletters newer_than:7d"   # any Gmail query
        schedule: daily            # daily | weekly
        hour: 7                    # local-tz hour at/after which it may run
        deliver_to: ""             # empty = your own inbox
        then_archive: false
        max_messages: 50
```

- **Outbound, so hard-gated** like `forward`: a real send needs `agent.digests.enabled` **and** `agent.send.enabled` **and** the outbound kill-switch off. Any closed gate records `blocked` and sends nothing. Delivery defaults to your own inbox.
- **At-most-once per period** — each run atomically claims `(name, account, period_key)` via a UNIQUE index (`agent_digest_runs`), the same cross-process claim that fixed the forward double-send, so overlapping sweeps can't double-send a daily/weekly digest.
- **Summarized by the local model** (no egress), always falling back to a plain itemised list so a digest is never empty.
- Runs from the scheduler each tick (no-op until due: local hour ≥ `hour`, not yet run this period). New `POST /api/agent/digests/run` (default a safe **dry-run preview** — builds the body without sending or consuming the period) and `GET /api/agent/digests` (specs + run history). New compose-and-send primitive `gmail_write.send_email` (verified `gog gmail send`).

An adversarial multi-agent audit (gate-bypass / double-send / robustness lenses) confirmed two reliability bugs (both *never*-send, not over-send), fixed before merge: (1) the period was claimed *before* the gate/fetch checks, so a closed send-gate (or an empty inbox at the digest hour, or a transient fetch error) **permanently consumed the period** — now fetch + gates are checked first and the period is claimed only immediately before the actual send, so a blocked/empty/error run stays re-runnable; (2) the digest path never called `ensure_agent_schema`, so an API-triggered run on a schema-stale instance could 500 on a missing table — `run_digest` now self-heals the schema like `run_triage`.

+17 tests. Never-send boundary unchanged — digests stay off until both the digest and send gates are explicitly opened.

## v0.2.0-beta.113 — 2026-05-29

### `forward` action — the first outbound routing action, hard-gated

A rule can now **forward** a matching message to another address (`{match: {...}, action: forward, value: "dest@example.com"}`) via the native `gog gmail forward` (attachments preserved). Forwarding *sends mail*, so unlike the reversible label actions it lives on a **separate, maximally-gated path**:

- **Five independent gates, all defaulting to never-forward.** A real forward requires `agent.actions.enabled` AND routing not in dry-run AND the new `agent.actions.allow_forward` (default **off**) AND `agent.send.enabled` (the existing send frontier) AND the outbound kill-switch off. Any closed gate records the action as `blocked` and sends nothing. In dry-run it records intent only.
- **Irreversible — no undo.** A sent forward can't be recalled, so `undo_action` refuses any `forward` row.
- **At-most-once.** A message+destination already applied/errored/in-flight is never re-sent; the ledger row is claimed as `forwarding` *before* the gog call so a crash mid-send can't cause a re-forward, and an errored forward is not auto-retried (it surfaces in the ledger for manual handling).
- Authorable everywhere: the `/rules` builder (with an inline irreversibility/gating warning), the NL box ("forward any invoice to jane@books.com"), and the REST API. `validate_rule` requires a well-formed destination email and rejects the `intent` predicate (routing runs before classification).

This is the only outbound routing action and it stays **off by default** — the never-send boundary is unchanged unless you explicitly open all the gates.

An adversarial multi-agent audit (gate-bypass / double-send / undo-refusal lenses) caught a real **TOCTOU race** before merge: the `forwarding` claim was a non-atomic check-then-insert, so two concurrent sweeps in *separate* processes (e.g. the in-server scheduler overlapping a manual `youos triage`) could both pass the dedup read and double-send. Fixed with a DB-enforced atomic claim — a partial `UNIQUE` index scoped to live forward rows (`idx_agent_actions_forward_claim`), with the claim insert catching `IntegrityError` (the loser doesn't send) — mirroring how `store.begin_send` serializes the draft-send frontier. Scoped to `forward` so the retryable label actions are untouched.

+11 tests (incl. the cross-process atomic-claim regression).

## v0.2.0-beta.112 — 2026-05-29

### Natural-language rule authoring — describe a rule, the local model drafts it (framework, 5/N)

Final step of the user-composable filters/actions series: describe a rule in plain English and the warm local model turns it into a structured `{match, action, value}` rule for you to review and save.

- **`POST /api/agent/rules/parse`** `{text}` → `{ok, rule, error}`. Sends the description to the warm local model (no egress) with a few-shot prompt covering the full 13-key match vocabulary + 10 actions, extracts the JSON, coerces stringified bools/numbers, and validates with the same `validate_rule` gate.
- **Confirm before save** — the parse **never** persists. On the `/rules` page a "Describe it in plain English" box pre-fills the builder with the parsed rule; the user reviews/edits and clicks Save (which goes through the normal CRUD). Even an invalid parse pre-fills best-effort so it can be fixed by hand.
- **Failure-isolated** — model off / unreachable / unparseable answer returns `ok=False` with a friendly message and falls back to manual building; it never raises into the page. JSON extraction is string-aware so a regex value with braces (`\d{2,3}`) doesn't break parsing.

Verified end-to-end against the real warm model: *"archive newsletters older than a week"* → `{subject_contains: "newsletter", older_than_days: 7} → archive`.

+11 tests. Local-only (uses the warm model server, the same one drafting uses); never-send/never-act boundary unchanged.

## v0.2.0-beta.111 — 2026-05-29

### Rule builder UI — author filters/actions without editing YAML (framework, 4/N)

Fourth step toward user-composable filters + actions: a `/rules` page (plain HTML/vanilla-JS, on the shared design system) that drives the existing CRUD/validate API, so rules can be built in the browser instead of by hand-editing config.

- **Condition builder** — add/remove ANDed `match` conditions; each picks a predicate from the full 13-key vocabulary and renders the right control (text / keyword-list / true-false / number / regex).
- **Action picker** — all 10 actions (skip / decline / prepend / hold + label / archive / star / mark_read / mark_important / mark_unimportant); the value field appears only for `label` (label name) and `prepend` (instruction text).
- **Live validation + plain-English preview** — every change re-validates against `POST /api/agent/rules/validate` and shows the rule as a sentence ("When older than 30 days AND has attachment → mark important"); Save is disabled until valid.
- **Your rules** — the saved list, each editable/deletable in place.
- **Recent routing actions** — the agent-action ledger (status badges) with one-click **Undo** for applied actions.

A "Rules" link is now in the shared nav across the app pages. Verified in both light and dark themes via headless render.

+2 tests. No new backend surface (reuses the b108 CRUD + b106 actions endpoints); never-send/never-act boundary unchanged.

## v0.2.0-beta.110 — 2026-05-29

### Richer mailbox actions — mark read / important (framework, 3/N)

Third step toward user-composable filters + actions. The mailbox-routing vocabulary doubles from 3 actions to 6:

- **`mark_read`** — clear the unread flag (remove `UNREAD`).
- **`mark_important`** — add to the Important tab (add `IMPORTANT`).
- **`mark_unimportant`** — remove from the Important tab.

Each is a reversible Gmail label mutation, so it slots straight into the existing agent-action framework: gated behind `agent.actions.enabled` (default off), dry-run by default, daily-capped, idempotent across sweeps, ledgered, and undoable (undo is just the inverse label swap). No new write path — `mark_read`/`mark_important` reuse the same `modify_message_labels` route `archive`/`star` already use, and the system labels (`UNREAD`/`IMPORTANT`) bypass label-creation.

A focused review caught a real bug before merge: the routing-enable gate in `_maybe_apply_mailbox_actions` had a hand-copied `("label","archive","star")` whitelist, so a rule set using *only* a new action would have silently no-op'd. The gate now tests the single source of truth (`rules.MAILBOX_ACTIONS`).

Outbound tasks (forward) stay behind the never-send frontier and are intentionally not exposed here; destructive ones (trash) are deliberately omitted pending an explicit opt-in.

+4 tests (incl. a regression test for the gate). Never-send/never-act boundary unchanged.

## v0.2.0-beta.109 — 2026-05-29

### Richer rule filters — match on recipients, attachments, age, contacts, regex (framework, 2/N)

Second step toward user-composable filters + actions. The rule `match` vocabulary grows from 6 keys to 13, so a rule can target far more than sender/subject:

- **`to_contains` / `cc_contains`** — keyword (or list) substring on the `To` / `Cc` headers ("anything sent to my team alias").
- **`subject_regex` / `body_regex`** — case-insensitive `re.search` for precise patterns (`invoice #\d+`). Invalid patterns are rejected at save time.
- **`has_attachment`** — true/false; detects a real MIME attachment (a part carrying a filename).
- **`known_contact`** — true/false; whether you have prior reply pairs with the sender (via `SenderHistory`). Pairs well with `cold_outreach` to single out genuine strangers.
- **`older_than_days` / `newer_than_days`** — message age in days from its `Date` header. A message with an unparseable/missing date never matches a recency predicate (we don't route what we can't date).

All predicates are still ANDed within a rule and work for both draft-shaping (`skip`/`decline`/`prepend`/`hold`) and mailbox routing (`label`/`archive`/`star`). Runtime matching is exception-safe — a bad regex, an undatable message, or an extreme-year `Date` header degrades to "no match", never a crashed sweep (the draft-loop rule eval is failure-isolated like the calendar/summary steps).

Hardened after an adversarial multi-agent audit of the new matching path: `validate_rule` (and the authoring API) now reject non-finite ages (`NaN`/`Infinity` would silently make a recency clause an always-true no-op), non-boolean flag values (a quoted `"false"` is truthy → inverted predicate), null/empty regex patterns, and the `intent` predicate on routing actions (routing runs before intent classification, so it would never fire). `body_regex` matching is length-capped to bound work on large bodies; rule regexes remain operator-trusted (not ReDoS-analysed). `message_age_days` now also catches `OverflowError`/`OSError`.

+13 tests. Never-send/never-act boundary unchanged; routing stays gated + dry-run by default.

## v0.2.0-beta.108 — 2026-05-29

### Rules authoring API — manage filters/actions without editing YAML (framework, 1/N)

First step toward user-composable filters + actions/tasks: a REST surface to manage `agent.rules` so an orchestrator (OpenClaw) or a UI can create/edit/delete rules without hand-editing config.

- New `GET /api/agent/rules` (each rule + its index), `POST /api/agent/rules` (append), `PUT /api/agent/rules/{index}`, `DELETE /api/agent/rules/{index}`, and `POST /api/agent/rules/validate` (dry-validate for a builder UI / NL preview).
- Extracted shared validation in `rules.py`: `validate_rule` (clear errors — unknown match key, unknown action, label without value, comma/reserved label name) + `normalize_rule` (now used by `load_rules` too) + `save_rules` (the single validated write path → config). `MATCH_KEYS` is the recognised filter vocabulary.

+12 tests. Pure rule storage (no mailbox mutation); never-send/never-act unaffected.

## v0.2.0-beta.107 — 2026-05-29

### Fix 7 bugs from the routing-framework audit (b106)

An adversarial audit of the b106 mailbox-routing framework confirmed 7 real bugs (2 refuted). All fixed:

- **(HIGH) Undo was silently re-applied on the next sweep.** The live-apply dedup only treated `status='applied'` as "done", but `undo_action` flips the row to `'undone'` — so the next 15-min sweep re-fired the rule and re-applied exactly what the user just undid. Dedup now also blocks on `'undone'`/`'undoing'`, so a deliberate undo stays undone (re-applying is never the default).
- **(MED) `agent.actions.daily_cap = 0` meant *unlimited*** (it fell into the `float('inf')` branch) — opposite of the auto-push/auto-send caps. Now `≤0` disables routing entirely; help text corrected.
- **(MED) An archived message was still classified, drafted, and persisted** in the same sweep. A message routed to `archive` is now dropped from the draft pipeline (in dry-run too, so the soak previews real behavior).
- **(LOW) Atomic undo:** `undo_action` now claims the row (`applied → undoing`) before the gog call, so a retried/concurrent undo can't double-run it (rolls back to `applied` on failure).
- **(LOW) Label-name safety:** `load_rules` drops `label` rules whose value contains a comma (gog's `--add` is comma-delimited → would create two wrong labels) or lives in the reserved `YouOS/` namespace (would fight the dismissal-label sync).
- **(LOW) Performance:** the existing-label set is now fetched once per sweep (cached) instead of a `gog labels list` subprocess per matched message.

+11 tests. Never-mutate-by-default invariant intact throughout.

## v0.2.0-beta.106 — 2026-05-29

### Beyond drafting: rule-driven mailbox routing (label / archive / star) — the agent-action framework

YouOS can now **act on** inbound mail, not just draft replies. `agent.rules` gains mailbox-routing actions that run on **every fetched message** (routing isn't tied to drafting):

```yaml
agent:
  rules:
    - match: {domain: "@recruiters.com"}
      action: label
      value: Recruiting
    - match: {subject_contains: newsletter}
      action: archive          # route out of the inbox
    - match: {body_contains: [urgent, asap]}
      action: star
```

- **Same guardrails as the send frontier:** opt-in `agent.actions.enabled` (default **false**), `agent.actions.dry_run` (default **true** — records intent without touching Gmail), and `agent.actions.daily_cap` (50). Account-internal + **reversible**.
- **Full accountability + undo:** every action is logged to a new `agent_actions` ledger; `GET /api/agent/actions` lists them and `POST /api/agent/actions/{id}/undo` reverses an applied one (re-adds INBOX / removes the label / unstars). Idempotent across sweeps (an applied action is never re-applied; a logged dry-run never blocks a later live apply).
- **Verified `gog` shapes** (`labels list` / `labels create <name>` / `messages modify <id> --add … --remove …`): label = add the label (created if missing), archive = remove `INBOX`, star = add `STARRED`. New `gmail_write.list_labels` / `ensure_label` / `modify_message_labels`; new `app/agent/actions.py` executor; wired into the sweep as `_maybe_apply_mailbox_actions`.
- **Deferred (outbound-sensitive):** `forward` — routes mail out of the mailbox, so it belongs with the send frontier's gating, not here.

+31 tests (`test_agent_actions.py` + rule/route + API list/undo cases). Never-send boundary unchanged (labeling/archiving is account-internal, not outbound).

## v0.2.0-beta.105 — 2026-05-29

### Cap the Stats "pairs reviewed" bar at 100%

Feedback pairs can exceed reply pairs (organic + correction rows accumulate), so the Corpus-Health "reviewed" coverage showed e.g. **"(399%)"** with an overflowing bar — reads like a bug. Clamped the displayed percentage (and the bar width) to 100% in `templates/stats.html`. Re-captured the Stats landing-page screenshots (dark + light) so they now show 100%.

## v0.2.0-beta.104 — 2026-05-29

### Theme-aware, brand-correct landing-page screenshots + `?theme` deep-link

- **Light *and* dark screenshots.** The landing page now serves light screenshots in light mode and dark in dark mode — driven by CSS keyed on both `prefers-color-scheme` and the manual `:root[data-theme]` toggle. Added `01-draft-reply-light.png` / `02-stats-light.png` alongside the dark defaults.
- **Fixed the personal brand leaking into public screenshots.** The captures showed the maintainer's instance brand ("BaherOS" — the `<First>OS` personalization) in the header and "BaherOS Stats" title. Re-rendered them generically as "YouOS" (the brand was swapped to the generic name only for capture, then restored).
- **New `?theme=light|dark` deep-link** honored by the app pages and the landing page (pre-paint, no flash; doesn't persist), and **`/feedback?notour`** to suppress the first-run tour. Both make reproducible headless captures possible (documented in `screenshots/CAPTURE.md`) and double as shareable theme-pinned links.

No app-behavior change beyond the opt-in `?theme` / `?notour` query params. Full suite green.

## v0.2.0-beta.103 — 2026-05-29

### One-call confirmed send + draft-in-notification (orchestrator/OpenClaw flow)

Smooths the human-in-the-loop flow "agent drafts → notify me → I review/edit → I confirm → it sends" down to a clean orchestrator surface.

- New `POST /api/agent/pending/{id}/confirm_send` — the single action an orchestrator fires when the user approves: (optional final edit via `amended_draft`) → create the Gmail draft → send it, in one call. The gate is checked **first**, so a disabled send / armed kill-switch returns 403 **before** any Gmail draft is created (no orphan drafts). Still a *human-confirmed* send — gated by `agent.send.enabled` + the kill-switch, distinct from autonomous `auto_send`. An edit sent here is tagged `amended_by='user'`, so it feeds the correction loop. If the draft is created but the send fails, the error names the draft id so the caller can retry `/send`.
- The digest's `pending_preview` (and thus the post-sweep webhook payload an orchestrator consumes) now carries the **draft body inline** (`draft`, capped at 2000 chars, user's edit preferred) plus `quality_score` — so a notification can show "the email and the reply" without a callback.

+5 tests. Never-send default unchanged (`send.enabled` still off by default; `confirm_send` 403s until you opt in).

## v0.2.0-beta.102 — 2026-05-29

### Keep drafting on-device: retry locally before the cloud + cap huge inputs

A live sweep on baheros showed a draft falling back to the cloud after the local model returned empty output. On an empty local draft the old code jumped **straight to Claude** — sending private mail to the cloud for what's usually a transient/cold-start hiccup.

- **Retry locally once before any cloud fallback.** When the local model returns empty/near-empty, `generate_draft` now re-runs the local model one more time; only if *that* is also empty does it fall back. Keeps the reply on-device for the common transient case. (`strict_local` still never touches the cloud — it raises instead.)
- **Cap the inbound fed to the model** (`generation.max_inbound_chars`, default 4000; 0 = off). A very long email (e.g. an auto-generated meeting-notes dump) can overflow the small local model's context and force a cloud fallback; the tail is trimmed in the prompt only — the retrieval query still uses the full text, so retrieval is unaffected.

+5 tests (`test_local_fallback_guard.py`: cap truncates a huge inbound; empty→local-retry recovers on-device; empty-twice→cloud; strict-local never hits cloud). Privacy + never-send boundary unchanged.

## v0.2.0-beta.101 — 2026-05-29

### The agent self-heals its own DB before sweeping (silent-failure fix)

Found live on baheros: after the autonomy work shipped (b85+), the **scheduled agent failed every single sweep** — `OperationalError: table agent_pending_drafts has no column named quality_score` — fetching mail then crashing at the persist step with zero drafts, invisibly, for hours. The cause: the new columns only get added by `bootstrap_database`, which needs `docs/schema.sql`; an instance whose server wasn't restarted (or was started with an instance-relative path that can't find the schema file) stays on the old schema while the new code expects the new columns.

- New `ensure_agent_schema(database_url)` runs the agent-table migrations idempotently with **no schema file required** — they all `CREATE TABLE IF NOT EXISTS` then `ALTER TABLE ADD COLUMN`, so they create-or-upgrade on any DB.
- `run_triage` calls it at the start of every sweep (cheap, idempotent, failure-isolated). A stale instance DB now upgrades itself in place instead of failing every tick.

+3 tests reproduce the exact production failure (a pre-b85 DB) and prove the sweep now self-heals and drafts. Never-send boundary unchanged.

## v0.2.0-beta.100 — 2026-05-29

### Fix 8 bugs from a second adversarial audit (Phase C+D, b94–b99)

A second 14-agent adversarial review (this time over Phase C+D + verifying the b96 fixes) confirmed 8 real bugs (2 refuted). All fixed — two were HIGH and partially undermined guarantees the changelogs had claimed:

- **(HIGH) Draft-stakes veto read the pre-edit draft.** Auto-send's high-stakes guard scanned `draft`, but the body actually sent is `amended_draft or draft` (push.py). An edited/regenerated draft that invents a price/commitment bypassed the veto. Now scans the body that will actually go out.
- **(HIGH) `hold` was in-memory only.** A `hold` rule blocked auto-push in the same sweep, but the flag wasn't persisted — so a *manually* pushed held row could later be auto-sent. `hold` is now a persisted column and `due_for_auto_send` excludes held rows.
- **(HIGH) Machine `/regenerate` re-drafts were mined as human corrections.** `_classify_row` treated any `amended_draft` as a rating-4 gold pair, but `/regenerate` writes a machine re-draft the user never approved (the same hazard `recipient_trust` already guards against). Added an `amended_by` provenance column (`user`/`machine`); only human edits become correction pairs.
- **(MED) `daily_send_cap=0` meant *unlimited*** — the opposite of the auto-push cap it claims to mirror. Now `≤0` disables auto-send entirely; help text corrected.
- **(MED) `sweep_health` mislabeled broken-model placeholder drafts as cloud fallbacks** (wrong remediation). Sentinel models (`none`/`error`) and `[no model…]` placeholder bodies now count as empty (model-down), not cloud.
- **(MED) Recovery announcements reached macOS only** — webhook/headless operators never learned the agent healed. Recovery now routes through `_alert` (all channels) **and** clears the per-(kind,account) failure debounce so a later re-failure isn't suppressed.
- **(MED) `classify_sweep_failure` mis-routed some real Google auth/rate errors to `unknown`** (RefreshError, insufficient scopes, RESOURCE_EXHAUSTED). Patterns extended.

+12 tests. Never-send boundary unchanged; every fix is conservative.

## v0.2.0-beta.99 — 2026-05-29

### Daily accountability report surfaces what the agent sent (autonomy Phase D)

The agent digest — the "what I did / what needs you" report (text/chat/html/json, pushable to a webhook) — predated the send frontier, so it never showed what was actually *sent*. Now it does, closing the accountability loop.

- `DigestData` gains `auto_sent_count` and `shadow_sent_count`, derived from the honest `send_state` on already-loaded rows (no new query). The text report shows "Auto-sent: N" and "Shadow-sent (soak, not actually sent): N" (each only when non-zero); both surface in the JSON payload for orchestrators.
- Updated `docs/AUTONOMY_ROADMAP.md` with the Phases A–D shipped status (b85–b99) and the rationale for the two deliberately-deferred items (the agent-action framework — YAGNI until a second action type exists; approval-by-reply — belongs in the orchestrator/chat layer, not YouOS core).

+1 test (`test_agent_digest.py`). Read-only; never-send boundary unchanged.

## v0.2.0-beta.98 — 2026-05-29

### Richer policy grammar: content rules + a `hold` action (autonomy Phase D)

`agent.rules` could only match on sender / domain / intent / cold-outreach, and could only `skip` / `decline` / `prepend`. It couldn't express "anything mentioning legal or money — let me handle it." Now it can, and that's the human-agent contract made concrete: the agent drafts the reply but a person always finishes-and-sends.

- New **content predicates** `subject_contains` / `body_contains` — a keyword or list of keywords, case-insensitive substring, matches if any hits. ANDed with the other match keys as usual.
- New **`hold` action** — still drafts the reply (so it's ready) but marks the row so it's **excluded from auto-push and auto-send**. Because auto-send only ever acts on auto-pushed drafts, blocking the push blocks the send too — a hold rule guarantees a human decides.

```yaml
agent:
  rules:
    - match: {body_contains: [legal, contract, lawsuit]}
      action: hold
    - match: {subject_contains: invoice}
      action: hold
```

Threaded through `apply_rules` → `TriageDraft.hold` → the auto-push gate. +9 tests (`test_agent_rules.py`, `test_agent_triage.py`). Stays inside the never-act boundary.

## v0.2.0-beta.97 — 2026-05-29

### Proactive alerting: a degraded agent reaches you, not just the log (autonomy Phase C)

A background agent that silently stops drafting — expired Google auth, a dead model server serving empty drafts, a sweep crashing every tick — is worse than no agent, because you think it's working. Now those failure modes become actionable alerts on every configured channel (macOS + webhook), not a line in a log.

- New `app/agent/alerts.py`: `classify_sweep_failure()` maps a sweep error to a kind (`auth` / `rate_limit` / `network` / `unknown`) and a remediation ("Re-authenticate: gog auth login"). `sweep_health()` assesses a *completed* sweep's drafts — even a "successful" sweep is unhealthy if most drafts fell back to the cloud (local model down) or came back empty — and flags a spike past a threshold (`min_drafts`-gated so a tiny sweep isn't judged).
- New scheduler `_alert()` fires on macOS **and** webhook, debounced per (kind, account) so a recurring cause (auth expired every tick) alerts once per window, not every sweep. The failure path now classifies + alerts on every channel; a healthy-but-degraded sweep (cloud-fallback or empty-output spike) raises its own alert.
- Note: browser-flow Google OAuth can't be auto-refreshed non-interactively, so for an `auth` failure the alert *is* the recovery path (it tells you exactly what to run). A process-not-running watchdog is intentionally external (launchd/cron), since an in-process loop can't alert on its own death.

+10 tests (`test_alerts.py`). Never-send boundary unchanged.

## v0.2.0-beta.96 — 2026-05-29

### Fix 11 bugs found by an adversarial audit of the autonomy + send frontier (b85–b95)

A 30-agent adversarial review of the autonomy work confirmed 11 real bugs (13 other findings refuted). All fixed here:

**Correctness**
- **Feedback capture missed the most common positive.** `_classify_row` keyed the "sent unchanged" positive on `send_state='sent'` (the auto-send path, off by default), but the normal manual-send path (`mark_sent`) sets `status='sent'` and leaves `send_state` NULL — so kept-and-sent drafts were silently skipped *and burned* (`feedback_captured=1`), permanently losing the signal. Now recognizes the manual-send positive, and leaves in-flight pushed drafts (`send_state='draft_created'`) un-burned so a later sweep captures their real outcome.
- **`recipient_trust` counted unreviewed re-drafts.** The `/regenerate` endpoint writes `status='amended'` (persist defaults true) for a machine-only re-draft the user never approved, inflating auto-send trust. Trust now counts confirmed sends only (`send_state='sent'`, or manual `status='sent'`+NULL `send_state`) — never `amended`.
- **Auto-send TOCTOU.** A dismiss landing between `due_for_auto_send` selection and `begin_send` could still send. `begin_send` now requires `status != 'dismissed'` in its atomic claim and reports a `dismissed` state.
- **High-stakes drafts could auto-send.** `assess_stakes` scanned only the inbound; a draft that *itself* invents a price/commitment slipped through (verify treats money as warning-only). Auto-send now runs `assess_stakes` on the draft text too and holds it.
- **`delay_minutes=0` voided the undo window** — auto-send could fire on a draft created earlier in the same sweep. Clamped to ≥ 1.
- **Auto-send gated on the raw score, not the calibrated probability** — calibration could never tighten the act decision. `calibrated_score` is now persisted on the row and passed to `decide_action`.

**Safety / survivability**
- **No daily auto-send cap** (only `max_per_sweep`). Added `agent.auto_send.daily_send_cap` (default 5) + `store.count_sent_today` — a real blast-radius bound mirroring auto-push.
- **`max_per_sweep` (and the new daily cap) weren't in the flag whitelist** — now settable via the safe config surface.
- **No reaper for rows stuck in `send_state='sending'`** after a crash. Added `store.reap_stale_sending` (bounded by age), run at the start of each auto-send pass.

**Tests**
- Added a test proving send is **blocked under real default config** (no `_send_config` stub), and an **end-to-end `run_triage`** test that auto-sends an eligible past draft (shadow) while leaving the same-sweep draft untouched (delay window). Plus daily-cap, draft-stakes, TOCTOU, and reaper tests.

Never-send boundary unchanged; every fix makes autonomy *more* conservative.

## v0.2.0-beta.95 — 2026-05-29

### Harden the golden-eval gate against a broken model (autonomy Phase C)

The adapter-promotion gate (b75) only rejects a *relative* composite drop. But if the model is outright broken — every golden case returns an empty draft — the eval scores a low composite that the gate can't distinguish from a real regression, and on a first run (no baseline) it would **promote the broken adapter**. Worse, an empty draft could score `warn` (0 words trivially passes the brevity check). Fixed both:

- `score_case`: an empty/whitespace draft is now a **hard fail** (never `warn`), and carries an `empty` flag. `run_golden_eval` summary gains `empty_count`, `empty_rate`, and a `degenerate` flag (empty_rate > 0.5).
- `should_promote` / `gate_after_eval`: new `eval_degenerate` parameter is a **hard refuse that overrides everything** — including the "missing values ⇒ promote" first-run default and an apparently-good composite. A degenerate eval forces a rollback; the agent never keeps an adapter "validated" by an untrustworthy eval.
- Nightly `step_golden_eval` now **fails loud** on a degenerate eval (`[ALERT]`, returns failure) and the pipeline records it as an error; the adapter gate reads `degenerate` from the results file and refuses promotion.

+6 tests (`test_golden_eval.py`, `test_adapter_promotion.py`). Never-send boundary unchanged.

## v0.2.0-beta.94 — 2026-05-29

### Capture the agent's own draft outcomes as feedback (autonomy Phase C)

The model fine-tunes on the user's historical sent mail; its *own* queued drafts — dismissed, edited, kept-and-sent — were never fed back, so the live false positives and bad drafts never became negative signal. The loop trained on the old corpus, not its own mistakes. This closes that loop.

- New `app/agent/feedback_capture.py`: `capture_queue_feedback()` mines terminal `agent_pending_drafts` rows into `feedback_pairs` — **edited→kept** becomes a correction pair (generated `draft` vs the user's `amended_draft`, rating 4, edit distance via `core.diff`); **sent unchanged** becomes a strong positive (rating 5); **dismissed `wrong_content`** becomes a negative pair (rating 2). `noise` / `wrong_sender` dismissals are *classifier* signals (the precision harness owns them), so they're skipped here.
- Idempotent via a new `feedback_captured` marker column (migration) — each terminal row is mined exactly once. Captured pairs land with `used_in_finetune=0` so the next nightly fine-tune picks them up.
- New nightly step `step_capture_queue_feedback`, run just before auto-feedback so its pairs flow into the same fine-tune. Best-effort; never fails the run.

+8 tests (`test_feedback_capture.py`). Read-mostly (inserts feedback rows + flips the marker); never-send boundary unchanged.

## v0.2.0-beta.93 — 2026-05-29

### Autonomous auto-send — the policy ladder (autonomy Phase B, completes the send frontier)

Wires the send path (b91) and the escalation policy (b92) into the sweep as an opt-in autonomous send — built as a **policy ladder** so trust is earned, not assumed: draft-queue → auto-draft → **shadow-send** (log-only soak) → **live-send to known recipients after a delay**.

- New `_maybe_auto_send` pass in the sweep: for drafts that have sat past the **undo/delay window** (so auto-send never fires in the same sweep that created the draft), it re-applies the escalation decision (`auto_act` only — high-stakes routes to a human) and a **per-recipient trust gate**, then sends via the hard-gated send path. `mode='shadow'` (the default) records a soak-only send without touching Gmail.
- New store helpers: `recipient_trust` (counts prior *kept* replies to a recipient — new contacts score 0, so auto-send never fires on an un-vetted relationship) and `due_for_auto_send` (the delay-window query).
- New flags (all conservative): `agent.auto_send.enabled` (default **false**), `agent.auto_send.mode` (default **shadow**), `agent.auto_send.delay_minutes` (60), `agent.auto_send.min_recipient_trust` (3). A live send still also requires `agent.send.enabled` and is blocked instantly by `agent.outbound_kill_switch`.
- **Caveat documented in the flag help:** if you send a pushed draft manually from Gmail, live mode can't detect that and may resend — only enable live once you let the agent own the thread.

Also fixed a latent test-isolation bug: `test_run_triage_calls_label_sync_at_start` patched `inbox_fetch.fetch_unread` (worked only by import-order luck); now patches the name `triage` actually binds.

+9 tests (`test_auto_send.py`). **Default behavior unchanged** — with `agent.auto_send.enabled` off (the default), nothing sends.

## v0.2.0-beta.92 — 2026-05-29

### Confidence × stakes escalation (autonomy Phase B)

Whether to *act* on a draft isn't just "is the draft good" — it's the draft's quality crossed with the **stakes** of the message. A flawless draft to a lawyer about a contract should still go to a human; a good draft confirming a coffee time can act on its own.

- New `app/agent/escalation.py`: `assess_stakes(subject, body)` flags money / legal / firm-commitment language (contract, invoice, payment, wire, legal, attorney, deadline, currency amounts, …) with word-boundaried patterns (so "contractor" doesn't trip "contract"). `decide_action(...)` maps (draft quality × confidence × stakes) → one of `auto_act` / `queue` / `ask` / `skip`. **High stakes is a hard veto on `auto_act`** — those always escalate to `ask`. Prefers the calibrated probability (b88) over the raw needs-reply score when available. Pure and deterministic; the autonomous send path (next) consumes the verdict.
- Wired now as a high-stakes guard on **auto-push**: a whitelisted, high-confidence, high-quality draft is still held for review when the inbound is high-stakes — auto-push only makes itself *more* conservative.
- New `agent.escalation.*` config (`auto_act_floor` 0.8, `confidence_floor` 0.85, `high_stakes_blocks` true).

+12 tests (`test_escalation.py` + the high-stakes auto-push guard). Never-send boundary unchanged.

## v0.2.0-beta.91 — 2026-05-29

### The send path + honest state model + kill-switch (autonomy Phase B, hard-gated)

The first step across the never-send boundary — built so it **cannot send by default**. This adds the *capability* to send a queued draft; autonomous auto-send (with confidence×stakes escalation and a delay/undo window) is a later, separately-gated step.

- New `gmail_write.send_draft()` — sends an **existing** Gmail draft by id (the exact draft the user could have reviewed, no body re-marshaling). Verified `gog` shape (`gog gmail drafts send <id> --account … --json --no-input --force`, confirmed against gog 0.17.0 `--help`); `dry_run` passes gog's `--dry-run`. Only the gog backend is implemented.
- New `app/agent/send.py` `send_pending_row()` — the one gated send path. **Two gates checked before any Gmail call**: `agent.outbound_kill_switch` (when on, blocks everything) and `agent.send.enabled` (master switch, default **false** — a real send requires it). `shadow=True` runs the full path but records a soak-only send without touching Gmail. Requires an already-pushed draft; idempotent (atomic `begin_send`/`finalize_send`/`abort_send` claim mirrors the push guard); a backend error rolls back to `draft_created`.
- **Honest send state** — new `send_state` column (migration), explicit instead of the overloaded `status='sent'`: `draft_created` (a Gmail draft exists) / `shadow` (simulated send) / `sent` (actually sent), plus `sent_message_id` + `actually_sent_at`. Existing pushed rows backfill to `draft_created`.
- New endpoint `POST /api/agent/pending/{id}/send` (`shadow` / `dry_run` options). New flags `agent.send.enabled` + `agent.outbound_kill_switch`.

+12 tests (`test_send_frontier.py`: gating, kill-switch, shadow, real send, requires-pushed-draft, idempotency, rollback, state machine, verified gog command). **Default behavior unchanged: with `agent.send.enabled` off (the default), YouOS still only ever creates drafts.**

## v0.2.0-beta.90 — 2026-05-29

### Fact grounding in the sweep (autonomy Phase A3)

The model dodged or invented availability / addresses because (a) the sweep never populated the facts table — `extract_and_save` only ran on manual paths — and (b) nothing told the model to ground its answer. Two complementary fixes:

- **Prompt grounding rule** — when the inbound poses a question that asks for a concrete detail (address, email, phone, price, availability, date/time, link), `assemble_prompt` adds a `[GROUNDING]` rule: state only facts from the inbound / thread / facts context, else ask or follow up — never invent. Gated on `_inbound_requests_fact`, so ordinary replies keep the unchanged prompt (minimising golden-eval drift) and the guard appears exactly where invention is a risk. Pairs with verify-before-accept (b89): the prompt prevents inventions, verify catches any that slip through.
- **Harvest facts during the sweep** — when the agent drafts a reply to real mail, it can now extract concrete facts the sender stated into your memory (`extract_and_save`, rule-based, on-device), so this and future replies are grounded. New flag `agent.extract_facts.enabled` (bool, default false — it writes to your memory table); runs only for drafted messages (not everything fetched), failure-isolated.

+8 tests (`test_fact_grounding.py` + sweep extraction cases). Never-send boundary unchanged.

## v0.2.0-beta.89 — 2026-05-29

### Verify-before-accept — catch hallucinated specifics before acting (autonomy Phase A3)

A draft that *reads* well can still be unsafe to act on: written in the wrong language, or stating a concrete email / link / number the model invented (it appears nowhere in the inbound or thread). For an autonomous agent those are the dangerous failures — a fluent reply that quotes a made-up address.

- New `app/generation/verify.py`: `verify_draft()` runs cheap deterministic checks and splits findings into **blocking** (language mismatch, invented email address, invented link — almost never legitimate in a reply) and **warnings** (an amount or a time/date not found in the inbound — often legitimate, e.g. proposing a meeting slot or restating a known price, so surfaced but not blocking). Grounding corpus = inbound + thread history; the account and sender addresses count as allowed participants.
- Wired into generation: every `generate_draft` now computes verify (failure-isolated — never blocks *drafting*). A **blocking** issue collapses the draft's `quality_score` (→ ≤0.1), so the existing auto-push quality floor (b85) holds the draft for human review — verify reuses the act-gate rather than adding a new one. Findings are surfaced on `DraftResponse.verify_issues`.

+11 tests (`test_verify_draft.py`). Verify only ever *holds* a draft for review — never sends, never loosens. Never-send boundary unchanged.

## v0.2.0-beta.88 — 2026-05-29

### Calibrate the needs-reply score to a real probability (autonomy Phase A2)

The classifier's score is an additive heuristic — 0.85 does **not** mean "85% likely to deserve a reply." Calibration fixes that: it learns, from the user's own past verdicts, what fraction of messages at each score level actually deserved a reply, and maps a raw score to that empirical probability. A calibrated probability is what an *act* decision should eventually gate on, because it can be tied to a real precision target instead of a meaningless cutoff.

- New `app/agent/calibration.py`: bins labeled `(score, outcome)` pairs (outcome from the same truth-mapping the precision harness uses), Laplace-smooths each bin's positive rate, then enforces monotonicity with pool-adjacent-violators (isotonic regression). Deterministic, dependency-free. `Calibrator.probability(score)` interpolates between knots; JSON-serializable; persisted to `var/triage_calibrator.json`.
- **Dormant until there's data**: `fit()` returns `None` below `min_samples` (50). A fresh instance (baheros today has 0 decided rows) keeps the raw heuristic; the calibrator self-activates as real verdicts accumulate.
- Wired into the sweep (`_maybe_calibrate`): when a calibrator exists, each verdict gets a `calibrated_score` and a `calibrated P=…` reason (persisted in `reasons_json`); never changes the `needs_reply` decision. New nightly step `step_fit_calibrator` (after the precision snapshot) refits from the last 90 days of verdicts.

+10 tests (`test_calibration.py`: PAV monotonicity/weighting, min-sample dormancy, interpolation/clamping, serialize roundtrip, fit-from-database). Never-send boundary unchanged.

## v0.2.0-beta.87 — 2026-05-29

### Real-mail triage precision, tracked over time (autonomy Phase A2)

The fixture harness scores the classifier against hand-labeled cases; it had never measured the agent on the *actual* inbox. Now we score the draft decision against the user's own verdicts on the queue — the only ground truth that reflects live behavior — and record it nightly so the false-positive rate is visible to the operator *and* to autoresearch.

- New `app/evaluation/real_mail_eval.py`: `evaluate_real_mail()` mines decided `agent_pending_drafts` rows into a confusion matrix. What the agent *predicted* is its tier (`draft` = positive, `surface` = abstain/negative); what was *true* is the verdict — `sent`/`amended` (and `dismissed:wrong_content`) = the message deserved a reply; `dismissed:noise`/`wrong_sender` = it didn't; `already_handled`/`other`/no-reason/`pending` = excluded (can't label the needs-reply decision). Reports precision/recall/F1 + the false positives broken down by reason and by sender.
- New `triage_precision_history` table (migration) + `record_snapshot` / `precision_history` / `run_and_record`. A new nightly step (`step_triage_precision` in `nightly_pipeline.py`, after autoresearch) snapshots it each run; read-only, best-effort, never fails the pipeline.
- New read endpoint `GET /api/agent/precision` returns the live metric (recomputed now) plus the recorded history for the /triage observability surface.

+14 tests (`test_real_mail_eval.py`). Read-only over the queue; never-send boundary unchanged.

## v0.2.0-beta.86 — 2026-05-29

### Borderline LLM adjudication — a broadcast veto (autonomy Phase A2)

The needs-reply classifier is a fast additive heuristic; on scores just over the threshold it can't reliably tell a personal note that looks automated from a broadcast that looks personal — the live false positives ("thanks, I'll check it out" to a newsletter) live exactly in this band. The warm local model is right there, so we now ask it.

- New `app/agent/adjudicate.py`: for a would-be draft whose needs-reply score is in a narrow band just over the threshold, `adjudicate()` asks the warm model one constrained question — PERSONAL or BROADCAST — and returns a verdict. On-device (no egress), temperature 0, failure-isolated (model unavailable / unparseable answer ⇒ no veto, the heuristic stands).
- Wired into the sweep right after classification (`_maybe_adjudicate`): a BROADCAST verdict **demotes** the message to surface-for-review (it still shows under "Review skipped", never silently buried). Adjudication only ever **demotes** — it can't promote a message the heuristic rejected — and never touches VIP senders.
- New flags: `agent.adjudication.enabled` (bool, default false; needs the warm model server) and `agent.adjudication.high` (float, default 0.8, clamped 0.6–1.0 — the upper edge of the band; above it the heuristic is trusted).

+12 tests (`test_adjudicate.py` for the parse/verdict helpers + veto/keep/no-op cases in `test_agent_triage.py`). Never-send boundary unchanged.

## v0.2.0-beta.85 — 2026-05-29

### Per-draft quality gate (autonomy Phase A1)

Auto-push gated on the *needs-reply* score — "this email deserves a reply" — not on whether the **draft itself** is any good. A perfect verdict plus a weak or contentless draft still auto-pushed. This is the foundation for trustworthy autonomy (see `docs/AUTONOMY_ROADMAP.md`): the agent must know when its own output is good enough to act.

- New `draft_quality_score(draft, …)` in `app/generation/service.py` blends **voice fidelity** (averaged `voice_match` vs the user's top retrieved replies — deterministic, ~0 extra cost) with **structural fit** (`_score_candidate`: length + greeting/closing). Collapses to ~0 for unusable drafts; discounts empty-output retries (×0.7) and non-LoRA cloud/base fallbacks (×0.85, less likely to be in-voice). Computed at generation time, failure-isolated (never blocks the draft), and surfaced on `DraftResponse.quality_score`.
- New `_is_generic_ack`: flags contentless acknowledgements ("thanks for the update", "got it, thanks", "I'll check it out") by **dominance** — once the ack phrase is stripped, almost nothing of substance remains — so a real reply that merely *opens* with "thanks for the update" isn't penalized. Generic acks are driven to ≤0.15, directly killing the live newsletter false positives.
- Auto-push now gates on the draft's quality too: new `agent.auto_push.quality_floor` flag (float, default 0.5, clamped 0.0–1.0). A draft with no quality score (scoring failed) is treated as below the floor — conservative when the agent can't judge itself. The needs-reply floor still applies; both must pass.
- Persisted end-to-end: `quality_score` column on `agent_pending_drafts` (migration), threaded through `upsert_pending` and `TriageDraft`.
- New `docs/AUTONOMY_ROADMAP.md`: the full path from "drafts in your voice" to "processes email autonomously" (Phases A–D), from a 34-agent gap analysis. Thesis: the machinery to *act* is nearly there; the gap is *trust* — make the act-decision trustworthy (quality + calibration + abstain + verify) **before** crossing the send boundary.

+11 tests (`test_draft_quality_gate.py` + auto-push quality-gate cases). Never-send boundary unchanged.

## v0.2.0-beta.84 — 2026-05-29

### Triage false positive: non-English / order-confirmation mail

Found by turning the agent on against the real baheros inbox (dry-run): a German Amazon order confirmation (`bestellbestaetigung@amazon.de`, subject "Ordered: …") got drafted — the transactional-template detector was English-only, and a corpus-noise reply pair gave it a false +0.20 history boost.

- `TRANSACTIONAL_TEMPLATE_PAT` now covers German order/shipping/payment terms (`bestellbestätigung`, `auftragsbestätigung`, `versandbestätigung`, `zahlungsbestätigung`, `ihre bestellung`, `rechnungsnummer`) plus more English ones (`your order`, `order number/#`, `tracking number`, `out for delivery`, `has shipped`) and the colon-anchored `Ordered:` / `Bestellt:` subject prefix (won't fire on prose like "I ordered the report").
- A transactional match now also suppresses the prior-history boost, so an ingested confirmation in the corpus can't lift a new confirmation over threshold.

+3 tests; a genuine human "could you confirm the delivery address for the order I placed?" still drafts.

## v0.2.0-beta.83 — 2026-05-29

### Long-thread catch-up summaries

For a reply on a long thread, the agent can now generate a 2–3 line "what changed / what's open" summary so you can catch up without re-reading the whole conversation.

- New `app/agent/thread_summary.py`: `summarize_thread` builds a transcript from the structured `thread_history` (no re-fetch) and summarizes it on the **warm local model** (on-device, no egress). Length-gated (`min_messages`, default 4) so a two-message exchange isn't summarized; failure-isolated — no summary never blocks drafting.
- `thread_summary` column on `agent_pending_drafts` (idempotent migration); persisted by triage and surfaced on the row + in the digest `pending_preview`.
- Config: `agent.summarize_threads.enabled` (default false; needs the warm model server) + `min_messages`.

This was the last open item from `docs/AUDIT_2026-05.md` — the autonomy/accuracy/observability audit is now fully worked through. +5 tests.

## v0.2.0-beta.82 — 2026-05-29

### Calendar-aware meeting replies

When the agent drafts a reply to a meeting request, it can now read your calendar free/busy and offer **concrete open slots** ("Tue 2:00–2:30 PM or Wed 10:00–10:30 AM?") instead of "happy to meet, when works for you?".

- New `app/agent/calendar.py`: `fetch_busy` (via the verified `gog calendar freebusy --json` shape), `compute_open_slots` (pure, timezone-aware, deterministic — one slot per business day for spread, within work hours, skipping busy), `format_slots`, `propose_open_slots`.
- Triage wiring: when `agent.calendar.enabled` and the inbound's intent is `meeting_request`, the agent fetches slots for the account (respecting `user.timezone`) and injects them into that draft's instructions so the model proposes real times. Failure-isolated; **never creates events** — it proposes times you send (stays inside the never-act boundary).
- Config: `agent.calendar.enabled` (default false; needs the gog calendar scope) + `agent.calendar.{business_days,work_start_hour,work_end_hour,slot_minutes,max_slots}` defaults.

CLI shape verified live before coding (per the "verify the real CLI" lesson). gws/native backends raise NotImplementedError for now. +7 tests (pure slot logic + parser).

## v0.2.0-beta.81 — 2026-05-29

### Autoresearch keep/revert bar — tuned from real data

With the prompt surface finally live (b80), a run showed the "answer-first" `system_prompt_suffix` variant genuinely improving the draft: `kw 0.38 → 0.41`, `composite 0.453 → 0.463` (+0.010). But it was reverted, because the hard-coded `improved` bar was **+0.02** — set blind, and now demonstrably discarding real wins.

- `compare_scorecards` thresholds are configurable (`improve_threshold` / `regress_threshold` in `configs/autoresearch.yaml`), default **0.01** (down from the implicit 0.02), so a genuine ~1-composite-point gain is kept. The optimizer loads them once per run. Tune up if your eval's run-to-run noise floor is higher (verify by scoring the same config twice).

This is the last piece: with b76–b80, autoresearch mutations now move the eval; b81 lets it actually *keep* the improvements. +3 tests.

## v0.2.0-beta.80 — 2026-05-29

### The prompt surface was a no-op — now it's real

A live run isolated the last cause of autoresearch's flatness: mutating the prompt variant changed nothing because the key it rewrote — `drafting_prompt` — **is read by nothing**. Generation's `assemble_prompt` uses `system_prompt` (service.py:1106); `drafting_prompt` existed only in the mutator. So autoresearch's prompt surface has been a guaranteed no-op since it was added.

- **`system_prompt_suffix`** (new, optional): generation now appends it to the system prompt. Kept separate from `system_prompt` so tuning drafting *style* can't clobber the instance's persona/brand prompt. Default empty = no change.
- The autoresearch prompt surface now mutates `system_prompt_suffix` with persona-preserving, additive instruction variants (baseline / "answer-first, skip pleasantries" / "skimmable, bullet-points") — so a variant change actually changes the draft and the eval can respond.

This was the missing piece: with the b77 cache bypass + b79 surface ordering, the prompt surface now genuinely moves the draft, giving autoresearch a lever on the `pass=20%` generation-quality headroom. The legacy `drafting_prompt` key is left as harmless dead config.

## v0.2.0-beta.79 — 2026-05-29

### Autoresearch optimizes the draft-changing surfaces first

`pass=20%` is a generation/prompt problem, but `get_mutable_surfaces` listed the prompt template **last** — after ~20 retrieval surfaces — so a short run (`--max-iter 6`) never reached it, and the nightly spent most of its budget on retrieval knobs that (per b78) rarely change exemplar selection.

Surfaces are now ordered by what actually moves the draft: **prompt template → per-mode reply length → retrieval → composite weights**. Combined with the b77 cache bypass, prompt/length mutations now change the draft and the eval can respond, so autoresearch targets the surface with real headroom. +1 test.

(Decoding params — `generation.decoding.temperature`/`top_p` — live in `youos_config.yaml` behind the `load_config` lru-cache; mutating them needs cache-invalidation plumbing to avoid a silent no-op, so that's a deliberate follow-up rather than a rushed addition.)

## v0.2.0-beta.78 — 2026-05-29

### Score normalization — make boosts + semantic actually reorder (and let autoresearch optimize)

A retrieval-only diagnostic settled it: even a *drastic* config (recency/account → 0.9, semantic_weight → 0.0 vs 0.4) returned the **identical** top-5 reply-pairs — scores shifted but ranking never did. Cause: raw BM25 lexical scores (~5–12, big gaps) dwarf the additive metadata boosts (tenths) and the [0,1] semantic blend, so `recency/account/sender` boosts and `semantic_weight` are **near-inert in production**, and autoresearch's retrieval mutations can't change which exemplars are selected → it could never move the eval.

- **`retrieval.normalize_scores`** (new config, default **false**): min-max normalizes the lexical score to [0,1] across each candidate pool **before truncation**, so the metadata boosts and the semantic blend operate on a comparable scale and can change which results survive. `_normalize_pool` preserves each match's quality/subject multiplier. Applied at all five retrieval truncation points. Off by default → zero production change until opted in per instance (A/B draft quality first).
- **top_k pinning fixed in the autoresearch eval**: retrieval uses `request.top_k or config.top_k`, and the eval's `DraftRequest` default `top_k=5` was overriding the config — so top_k mutations were silent no-ops. The eval now reads top_k from the (mutated) config.

+3 tests. To validate on an instance: set `normalize_scores: true` in its `configs/retrieval/defaults.yaml`, re-run autoresearch, and confirm the per-component deltas now move (and improvements get kept).

## v0.2.0-beta.77 — 2026-05-29

### Root-cause fix: autoresearch was blind to its own mutations

The b76 per-component diagnostics paid off immediately. A live run showed every retrieval-param mutation producing **byte-identical** sub-scores (`pass 0.20→0.20  kw 0.38→0.38  conf 0.85→0.85`) — proof the mutated config had *zero* effect on the eval, not that it moved below threshold.

Cause: the **exemplar cache**. `generate_draft` calls `_apply_cached_order`, which reorders retrieval's output to put previously-cached exemplars first. Populated on the baseline eval, it then pinned the same exemplars into every candidate's prompt — so changing `top_k`, recency/account weights, etc. re-ranked retrieval but the cache forced identical exemplars → identical drafts → identical scores. **This is why autoresearch had kept 0 improvements.**

Fix: `DraftRequest.use_exemplar_cache` (default True = production behavior). The autoresearch eval (`scripts/run_autoresearch.py`) now sets it False, so `generate_draft` skips the cache read/apply/write and each candidate's retrieval config actually drives the exemplars — and the eval. Production drafting is unchanged (cache still on). +1 test.

## v0.2.0-beta.76 — 2026-05-29

### Autoresearch: sensitivity + diagnosability

On the live instance autoresearch kept **0 improvements/night** — every mutation logged `composite 0.42 → 0.42` and reverted. Two changes so the loop can register (and we can diagnose) real progress:

- **Graded composite** (`scorer.py`): a `warn` case now gets **half credit** on the pass term instead of zero, so an improvement that lifts a case fail→warn registers instead of being a binary cliff. The displayed `pass_rate` stays the strict passed/total — only the optimization objective is graded. The acceptance bar is unchanged, so it won't accept noise.
- **Per-component diagnostics** (`optimizer.py`): each iteration now records and prints the baseline→candidate **pass / keyword / confidence** sub-scores (3-decimal composite), and the JSONL run log carries `iteration_components`. This makes a flat run *diagnosable*: if the sub-scores are identical across a mutation, the eval isn't responding to the mutated config (a wiring bug) — as opposed to moving below threshold.

+2 tests. (Whether the loop now keeps improvements is verified by running it on the live corpus — the diagnostics tell us which case it is.)

## v0.2.0-beta.75 — 2026-05-29

### Adapter-promotion gate — no silent regressions

`finetune_lora` wrote the new adapter straight into `models/adapters/latest/` and the warm server reloaded it, so a bad nightly retrain silently degraded every draft with no rollback. Now the nightly gates promotion on the golden-eval composite:

- **Before** fine-tuning: snapshot the current adapter to `models/adapters/previous/` and record the prior run's golden composite as the baseline.
- **After** the (now-real) golden eval: keep the new adapter only if its composite holds/improves within tolerance (0.02); otherwise **roll back** to the snapshot (restored with a fresh mtime so the warm server reloads the good adapter). The outcome is logged as `adapter_gate` in the run summary.

New `app/evaluation/promotion.py` (`should_promote` / `snapshot_adapter` / `restore_adapter` / `gate_after_eval`) is pure + unit-tested (+5 tests); the nightly wires it around the existing finetune/golden-eval steps. (The end-to-end train→eval→rollback cycle is validated on a live instance.)

## v0.2.0-beta.74 — 2026-05-29

### Proactive push — the agent reaches out

The digest was pull-only and the only push was a macOS notification (useless when you're away from the Mac). Now the background agent can POST a digest summary to a webhook after a sweep, so you — or your Telegram/OpenClaw bot — get nudged without polling.

- New `agent.notify_webhook_url` (+ optional `agent.notify_webhook_secret`, `agent.notify_min_interval_minutes`). Off by default; this is the one place YouOS makes an outbound request.
- Pushes only when there's something actionable (pending/owed/awaiting > 0), the queue state **changed** since the last push, and the min-interval elapsed — a quiet or unchanged inbox stays quiet.
- **Metadata only**: summary line, counts, and the pending preview (truncated subjects + senders). Never message bodies or draft text. Secret sent as `X-YouOS-Secret`. Documented in PRIVACY.md.

+4 tests.

## v0.2.0-beta.73 — 2026-05-29

### Structured standing-instruction rules

`agent.standing_instructions` was one global string prepended to every draft. New `app/agent/rules.py` adds durable, conditional rules so the agent follows policies, not just a hint — "always decline recruiters", "for client X note I'll CC my partner", "for meeting requests propose Tue/Thu", "skip cold outreach".

Rules live under `agent.rules` in `youos_config.yaml` (a list). Each `match` (ANDed) supports `sender` (exact), `domain` (`@x.com`), `intent` (a label), `cold_outreach` (bool); actions are `skip` (don't draft), `decline` (draft a polite decline), `prepend` (inject `value`). The triage loop evaluates rules per message: a `skip` rule drops the message, otherwise the matched instructions fold into that draft's standing instructions (global + per-rule) and are snapshotted on the row so you can see why a draft took a stance. Intent matching only classifies when a rule needs it. All actions stay draft-only. +8 tests.

## v0.2.0-beta.72 — 2026-05-29

### Voice-match gating in multi-candidate drafting

The metric the whole product rests on — does it sound like *you* — was computed only offline. The live multi-candidate path picked the "best" draft by length-fit + has-a-signoff, which is orthogonal to voice and could discard the most voice-faithful candidate for running a few words long.

Now, when generation has retrieved the user's real replies, `_rank_candidates` scores each candidate's `voice_match` (averaged across the top 3 exemplars, deterministic components → zero extra model cost) and weights it as the primary ranking signal alongside the structural terms. Averaging across several exemplars (not matching one) avoids rewarding verbatim parroting. The chosen candidate's `voice_match` is surfaced on the candidate dict. Backward-compatible: with no exemplars the ranking is unchanged. +1 test.

## v0.2.0-beta.71 — 2026-05-29

### VIP sender routing

Autonomy is prioritization, not just filtering — the one email from your co-founder matters more than ten from strangers. New `agent.vip_senders` flag (comma-separated emails / `@domains`): mail from a VIP gets a strong needs-reply boost (+0.25) so it clears the threshold even if it carried a penalty, and ranks to the top of the score-ordered queue. The verdict carries a `vip` flag and a "VIP sender (prioritized)" reason (visible on the row).

VIPs don't bypass the noise filters: hard-skips (newsletters, automation domains, CI, mailer-daemon) run first and return, so a VIP domain's newsletter is still skipped — only mail that survives to scoring gets the boost. Threaded through `classify` / `classify_many` / `get_agent_config` / the triage sweep. +4 tests.

## v0.2.0-beta.70 — 2026-05-29

### Triage accuracy is now measurable

The audit's sharpest accuracy finding was that triage quality was *unobservable* — the only signal was the post-hoc dismissal rate, and false negatives (real mail the filter buried) left no trace. New harness:

- `app/evaluation/triage_eval.py` — `evaluate_triage` (precision/recall/F1/accuracy + confusion matrix + the list of misclassified cases), `threshold_sweep`, and `best_threshold` (F1-maximizing, ties favor recall).
- `scripts/eval_triage.py` — CLI over a labelled JSONL corpus; `--sweep` prints the precision/recall trade-off across thresholds so you can pick `agent.threshold` from data instead of guessing. Point `--corpus` at your own mail.
- `configs/triage_corpus.jsonl` — a starter labelled set (newsletters, mailer-daemon, CI, booking confirmations, trivial acks vs. real questions/requests).

+3 tests.

## v0.2.0-beta.69 — 2026-05-29

### Thread context into autonomous drafting

The biggest draft-accuracy fix: the background agent was drafting blind. `fetch_unread` pulled the whole thread but kept only the latest message, then `strip_quoted_text` removed any inline quotes — so on an ongoing thread the model saw a single message with no history and could confidently answer the wrong question or re-ask something already settled.

- `InboxMessage` now carries `thread_history` (the prior turns `fetch_unread` already had in hand — last 4, oldest→newest, sender + truncated text).
- `DraftRequest.thread_history` threads it to generation; `generate_draft` prefers this structured history over the brittle regex `From:`-block extraction (which `strip_quoted_text` had usually already defeated), feeding it into the existing `[THREAD HISTORY] … [CURRENT MESSAGE]` prompt block.
- The agent passes `msg.thread_history` through on every triage draft.

+3 tests (history captured from a multi-message thread; none for a single-message thread; history reaches `generate_draft`). Still draft-only, still local — no trust-boundary change.

## v0.2.0-beta.68 — 2026-05-29

### Follow-up tracking — the two open loops

A real assistant never lets a thread fall through the cracks. New `app/agent/followups.py` tracks both:

- **Owed inbound** — queued mail you haven't acted on, aging past `agent.followup_owed_days` (default 2). "Bob's email from Tuesday is still unanswered."
- **Awaiting reply** — replies you pushed/sent with no newer activity on the thread after `agent.followup_wait_days` (default 4). "You emailed Alice 4 days ago, no reply."

Read-only over the existing `agent_pending_drafts` table — no new writes, no Gmail egress. Surfaced via:
- `GET /api/agent/followups` (per-account; orchestrator's "anything I'm forgetting?" answer)
- the digest (text, chat, and JSON) — `owed_count` / `awaiting_count` + previews, so the Telegram/OpenClaw bot can nudge you.

Timestamps are parsed in Python (tolerating email-style `...Z` ISO and SQLite's space format). The awaiting-reply check is a DB-only heuristic (infers "they replied" from newer thread activity) — a soft nudge, not a guarantee. +4 tests.

## v0.2.0-beta.67 — 2026-05-29

### Tiered auto-push to Gmail Drafts (opt-in, dry-run first)

The first rung up the autonomy ladder — and it stays fully inside the never-send boundary: after a sweep, YouOS can automatically create a Gmail **Draft** (never sends) for high-confidence replies to known, whitelisted senders, so they're waiting in your Drafts folder when you open Gmail instead of sitting in `/triage`.

Off by default and **dry-run by default** — turn it on and watch the log say what it *would* push for a week before letting it write. New `agent.auto_push.*` flags (all whitelisted, settable via `/settings` or `youos config set`):

- `agent.auto_push.enabled` (bool, default false)
- `agent.auto_push.dry_run` (bool, default true) — log-only until you turn it off
- `agent.auto_push.whitelist` (emails / `@domains`) — **required**; empty = nothing is pushed
- `agent.auto_push.confidence_floor` (default 0.85, clamped 0.6–1.0)
- `agent.auto_push.known_sender_min_pairs` (default 3) — must have prior history with the sender
- `agent.auto_push.daily_push_cap` (default 5, per UTC day per account; 0 disables)

A row is auto-pushed only if it cleared all of: enabled, whitelist match, not cold-outreach, score ≥ floor, prior-pairs ≥ min, and under the daily cap. Cold-outreach replies are never auto-pushed. It reuses the idempotent push path (no duplicate drafts), is failure-isolated (a push error never breaks the sweep), and reports outcomes on `TriageResult.auto_pushed`.

## v0.2.0-beta.66 — 2026-05-29

### Autonomy hardening — trust + turn-it-on sprint

Acts on the verified findings in `docs/AUDIT_2026-05.md` (a 55-agent audit of the agent loop, accuracy, and robustness). Closes every Tier-0 correctness/safety bug that blocked trusting the autonomous agent unattended, plus the highest-value bounded accuracy/observability wins. Full suite green; +14 tests.

**Send-safety (the only paths that touch the mailbox):**
- `push_to_gmail` is now **idempotent**. Each backend call creates a *new* Gmail draft, so a retry, double-click, or two concurrent orchestrators previously left duplicate drafts. New atomic claim (`store.begin_push` / `finalize_push` / `abort_push`) serializes the write; a re-push returns the existing `gmail_draft_id` with `pushed_already: true`, and a backend failure rolls the claim back so retries work. Logic lives in one shared place (`app/agent/push.py`) so the route and future auto-push can't diverge.
- **gws multi-account fix**: `gmail_write._gws_create_draft` now sets `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` per account (mirroring the read path), preventing a draft for one account landing in another account's Drafts.

**Don't-die-silently:**
- A sweep that raises (expired gog auth, network down) now **always logs an `agent_audit` row** with the error and re-raises — previously it logged nothing and the observability success-rate stayed green while the agent was dead. The scheduler tracks consecutive failures and notifies once on the first failure transition (and on recovery).
- **Heartbeat**: `sweep_aggregate` exposes `last_sweep_at` / `last_successful_sweep_at` / `seconds_since_last_sweep`; `/api/agent/observability` adds a staleness hint and surfaces which model is actually drafting.
- `doctor` now checks gog **auth validity** (bounded `gog auth list` probe), not just that the binary is on PATH — the #1 cause of an unattended agent silently stopping.

**Concurrency / locking:**
- Agent DB connections use the tuned `bootstrap.connect` (busy_timeout + WAL) instead of a raw `sqlite3.connect`, so a sweep colliding with the nightly or a manual run no longer hits immediate `database is locked`.
- Per-account sweep lock: a scheduled tick overlapping a manual/API triage is now skipped rather than both running and each consuming the daily-draft-cap budget.
- Gmail-label sync dismisses **all** pending rows for a labelled thread (not just the newest), so "skip this thread" isn't silently partial.

**Accuracy:**
- The needs-reply classifier scores the **new content only** — quoted reply history and the trailing signature are stripped before looking for questions/imperatives/length, and trivial acknowledgements ("thanks", "will do") are penalized rather than drafted. Kills the biggest thread-reply false-positive class.

**Learning loop:**
- The nightly **golden eval now scores real drafts**. It was calling `run_golden_eval()` with no generator, so every case scored against an empty string — the one quality checkpoint after fine-tuning was a no-op. Now instance-aware (resolves DB + configs from settings).

**API / config:**
- New `GET /api/agent/pending/{id}` (the retry-safety check `AGENT_OPERATIONS.md` mandates was previously impossible to perform).
- New `POST /api/agent/pending/{id}/regenerate` — re-draft a queued row *in your voice* with a free-form instruction (e.g. "shorter; decline the meeting"), instead of pasting verbatim replacement text.
- `agent.threshold` is now a whitelisted flag (float, clamped 0.4–0.85) so the documented `/api/config/set` tuning actually works.
- The auth middleware re-reads config per request, so a PIN / origin allowlist set after startup takes effect without a server restart (closing an exposure window on remote-reachable instances).

## v0.2.0-beta.65 — 2026-05-28

### Public docs site — agent discoverability

Closes the discoverability gap: `docs/AGENT_OPERATIONS.md` (b64) only existed in the repo. An LLM agent crawling the public web couldn't find it without scraping GitHub. This PR publishes the docs as `https://youos.drbaher.com/docs/<NAME>.html` (rendered for humans) + `https://youos.drbaher.com/docs/<NAME>.md` (raw markdown for agent tool-use context).

**New `scripts/build_docs.py`** — at GitHub Pages deploy time, walks the curated doc list, renders each `docs/*.md` to HTML using `python-markdown` (fenced code + tables + TOC extensions), copies the raw markdown alongside, and writes:

- `_site/docs/<NAME>.html` — styled to match the landing page (dark/light palette match)
- `_site/docs/<NAME>.md` — raw markdown (canonical form for LLM agents)
- `_site/docs/index.html` — docs index with title + blurb for each doc
- `_site/llms.txt` — emerging convention ([llmstxt.org](https://llmstxt.org)) for LLM-agent discovery; top-level summary + pointers
- `_site/robots.txt` + `_site/sitemap.xml` — for crawler discovery

5 docs published: `AGENT_OPERATIONS`, `INTEGRATIONS`, `REMOTE_ACCESS`, `USAGE`, `ARCHITECTURE`.

**`.github/workflows/pages.yml`** updated:
- Trigger paths include `docs/**` (so doc edits redeploy)
- Sets up Python 3.11
- `pip install markdown>=3.5`
- Runs `python scripts/build_docs.py` after the site/ copy step

**`site/index.html`** updated:
- "Docs" link in hero CTA row
- "Building an AI agent that handles email?" callout under the CTA pointing at the agent operations playbook (rendered + raw markdown URLs) and the OpenAPI spec

**Each rendered page** carries:
- Canonical link to itself
- `rel="alternate" type="text/markdown"` pointing to the raw .md (so LLM tools and content negotiators find the canonical form)
- "View raw markdown ↓" chip in the header
- Mirror note in the footer naming the source path in the repo

**`llms.txt`** content (top of root, plain text, by convention):

```
# YouOS

> Local-first personal email copilot. Background agent sweeps unread inbox,
> drafts replies, queues for review; exposes REST + OpenAPI for orchestrators
> (Hermes / OpenClaw / Telegram bot).

## For LLM agents operating YouOS

- [Agent operations playbook](https://youos.drbaher.com/docs/AGENT_OPERATIONS.md): ...
- [Integrations](https://youos.drbaher.com/docs/INTEGRATIONS.md): ...
- OpenAPI spec: every YouOS instance serves it at `GET /openapi.json`.

## For humans setting up
...
```

**Verified locally**: `python scripts/build_docs.py` produces all 5 rendered HTML pages + matching .md copies + index + llms.txt + sitemap.xml. The index page correctly omits the "raw markdown" chip + alternate link (it has no .md counterpart).

**Why this matters for the orchestrator vision**: an LLM agent encountering YouOS via search engine, an LLM training crawl, or an `llms.txt` probe can immediately find the runtime contract. The agent doesn't need access to the GitHub repo — every operating doc is at a stable public URL with both rendered and raw forms.

## v0.2.0-beta.64 — 2026-05-28

### `docs/AGENT_OPERATIONS.md` — runtime contract for LLM-driven orchestrators

Audit gap from b59-b63: we shipped wiring docs (INTEGRATIONS.md), command reference (USAGE.md), security setup (REMOTE_ACCESS.md), and a working Telegram bot. But no **runtime operating contract** for an LLM agent driving YouOS — when to call `/digest` vs `/resolve`, how to handle multi-match disambiguation, HTTP-error → user-facing message mapping, idempotency notes, what NOT to do.

**New `docs/AGENT_OPERATIONS.md`** — 14 sections targeted at LLM agents (Hermes, OpenClaw, a chat bot, Claude in a tool-use loop):

1. **First contact** — probe `/openapi.json`, resolve user's account via `/digest`, cache base URL + token
2. **Decision tree** — table of user intents → endpoints → follow-ups
3. **Idempotency** — per-endpoint matrix; `push_to_gmail` and `save_as_feedback_pair` are NOT idempotent (warning)
4. **HTTP error handling** — code → meaning → user-facing message table
5. **Disambiguation pattern** — when `/resolve` returns multiple rows
6. **Paraphrasing the digest** — concrete rewrites of `summary` based on user's question style
7. **Trust boundaries** — what YouOS won't let you do; what the agent SHOULD NOT do without confirmation
8. **Per-action side effects** — DB + Gmail changes per endpoint, so confirmations stay honest
9. **Multi-account** — when to pass `?account=` explicitly
10. **Conversational patterns** to follow / avoid
11. **Learning the agent** — feedback_pairs path, standing_instructions, threshold tuning
12. **Versioning + capability discovery** — `/openapi.json` is canonical; this doc reflects b63 surface
13. **Worked example** — full multi-turn conversation showing the steady-state shape (each user turn → 1–2 calls → 1 bubble)
14. **See also** — pointers to INTEGRATIONS.md, REMOTE_ACCESS.md, USAGE.md, ARCHITECTURE.md, SKILL.md, /openapi.json, /docs

**Pointers added** in:
- `SKILL.md` — after the existing Integrations section (so OpenClaw / ClawHub agents reading the skill land here)
- `docs/INTEGRATIONS.md` — top-of-page callout (so human integrators direct their LLM at the runtime doc)

No code changes. Pure documentation surface gap closed.

## v0.2.0-beta.63 — 2026-05-28

### Reference Telegram bot — `examples/telegram_bot.py`

A working ~250-line reference orchestrator that wires Telegram to YouOS so the orchestrator vision is demonstrable end-to-end. Pairs with `docs/INTEGRATIONS.md` (the recipe) and `/api/agent/resolve` (b62, the NLU helper).

**Commands**:
- `/inbox` — calls `/api/agent/digest`, surfaces summary + top-5 pending with row ids
- `/push <id>` — `POST /api/agent/pending/<id>/push_to_gmail`
- `/dismiss <id> [reason]` — `POST .../dismiss` (default `noise`); validates against the 5-reason whitelist
- `/find <words>` — `GET /api/agent/resolve?q=<words>`
- `/digest [days]` — extended digest with by-reason + auto-promoted
- `/help` — command list

**Free-text routing** — phrases like `"push the Q3 thing"`, `"dismiss the barber confirmation"`, `"anything important?"` get routed via regex patterns to the right command, with row-id resolution via `/api/agent/resolve`. Substring matching only; a real production orchestrator would route through an LLM here (but the YouOS surface is the same either way).

**Security**: `TELEGRAM_AUTHORIZED_USER` env var pins exactly one Telegram numeric user id allowed to drive the bot. Anyone else is silently ignored. Without this, any Telegram user could find the bot and control your inbox.

**Dependencies**: `python-telegram-bot==21.*` + `requests`. Bot can run on the same Mac as YouOS or on any Tailnet device.

`examples/README.md` describes the patterns reusable for `slack_bot.py`, `hermes_skill.json`, etc.

**Verification**: file parses cleanly (`python3 -m py_compile`); `ruff check examples/` clean. Not exercised against a live Telegram bot in this PR (no test bot configured) — the wiring is small and the API contracts it calls are already fully tested via `tests/test_agent_routes.py` (38 passing).

## v0.2.0-beta.62 — 2026-05-28

### `GET /api/agent/resolve?q=...` — orchestrator NLU helper

The orchestrator vision: user says "push the Q3 pricing email to Gmail" in Telegram → Hermes calls YouOS to figure out which row that refers to → dispatches the action. This PR adds the row-lookup helper that closes the gap between the user's natural-language reference and the agent's row IDs.

**`GET /api/agent/resolve?q=<substring>&account=&status=pending&limit=5`**

Returns matching rows ranked by:
1. Subject substring (earlier match = higher score)
2. Sender / sender_email substring

Each result has `id`, `tier`, `subject`, `sender`, `sender_email`, `needs_reply_score`, `match_field`, `match_score`. The orchestrator picks the top result automatically, or disambiguates in the chat bubble ("I found two matches; which one?") when multiple come back.

**Why substring not fuzzy/embedding**: chat instructions are short and the user typically mentions a real word from the subject or sender. Substring covers the dominant case. Fuzzy / embedding-based resolution is a future feature behind this clean interface.

**Live-verified** on baheros:

```
$ curl /api/agent/resolve?q=random
count: 1
  #13  [subject=72]  'Would you be interested in "Random Coffee" in Vienna?'

$ curl /api/agent/resolve?q=eon.health
count: 1
  #14  [sender]  sender: 'agent@eon.health'  subject: 'The Science Behind...'
```

The match scores show what's working — `random` hit subject at offset 72 chars in; `eon.health` matched sender_email.

**Tests** — 4 new:
- `test_resolve_finds_pending_row_by_subject_substring`
- `test_resolve_finds_row_by_sender_substring`
- `test_resolve_returns_empty_count_when_no_match`
- `test_resolve_requires_q_param`

30 route tests pass; `ruff check` clean.

**Orchestrator usage**:

```python
# User: "Push the Q3 pricing email to Gmail"
hits = http.get(f"{YOUOS}/api/agent/resolve?q=Q3+pricing").json()["rows"]
if len(hits) == 1:
    http.post(f"{YOUOS}/api/agent/pending/{hits[0]['id']}/push_to_gmail")
elif len(hits) > 1:
    bot.send("Found multiple: " + ", ".join(f"#{h['id']} {h['subject']}" for h in hits))
else:
    bot.send("No match — try a different phrase.")
```

## v0.2.0-beta.61 — 2026-05-28

### Multi-label categorical Gmail-label dismissal

b57 shipped one label → one reason (`YouOS/skip` → `noise`). The /triage dismiss selector exposes 5 categorical reasons. This PR closes that gap so chat-side (Gmail-label) dismissal carries the same granularity:

| Gmail label | Dismissal reason |
|---|---|
| `YouOS/skip` | `noise` (b57 default; backwards compat) |
| `YouOS/skip-noise` | `noise` |
| `YouOS/skip-wrong-sender` | `wrong_sender` |
| `YouOS/skip-wrong-content` | `wrong_content` |
| `YouOS/skip-handled` | `already_handled` |
| `YouOS/skip-other` | `other` |

**Behavior change**: `sync_gmail_label_dismissals(label=None)` (new default) iterates every entry in the map. `label="X"` (explicit) still processes only that one — b57 callers preserved.

**CLI**: `youos sync-labels` (no `--label`) now sweeps all 6 categorical labels. Pass `--label X` to restrict.

**Why this matters for the orchestrator vision**: a Telegram/Slack bot can now say "dismiss the Q3 row as wrong content" → orchestrator routes to `YouOS/skip-wrong-content` OR calls `POST /api/agent/pending/{id}/dismiss` with `{reason: "wrong_content"}`. Either way, `wrong_content` dismissals flow into the LoRA training queue; `noise` continues to feed `skip_senders`. The categorical signal flows end-to-end.

**New `LABEL_TO_REASON` map** in `app/agent/gmail_label_sync.py` — single source of truth.

**Verified**: a completeness test asserts every entry in `store.DISMISSAL_REASONS` has at least one label mapping. Any future reason added without a label fails the test.

**Tests** — 3 new + 4 existing tests updated to pass `label="YouOS/skip"` explicitly (preserving b57 single-label semantics; the new tests cover the iterate-all default).

`docs/REMOTE_ACCESS.md` updated with the full label-to-reason table.

## v0.2.0-beta.60 — 2026-05-28

### ClawHub refresh — orchestrator surface visible in the registry

The YouOS bundle is already published on ClawHub but `clawhub.json` + `SKILL.md` predated the orchestrator vision (b59). Refreshed both so the ClawHub listing accurately reflects what users get today.

**`clawhub.json`**:
- Description rewritten to mention the background agent + REST + OpenAPI surface
- Tags extended: `agent`, `orchestrator-backend`, `openapi`, `telegram`, `tailscale`

**`SKILL.md`**:
- 9 new trigger phrases for orchestrator-style invocations (`"anything important in my inbox"`, `"triage my email"`, `"push to gmail drafts"`, `"dismiss as noise"`, etc.) so Hermes-style routers correctly direct intent at YouOS
- New "Integrations (orchestrator backend)" section between "Autonomous triage" and "How it works" — describes the surface area, auth, network setup, and points at `docs/INTEGRATIONS.md` for the recipe
- New paragraph in the triage section documenting Gmail-label remote dismissal (b57)

No code changes — pure metadata/docs surface. Bundle remains text-only per ClawHub convention; rebuild via `scripts/prepare_clawhub_release.sh` when ready to publish.

## v0.2.0-beta.59 — 2026-05-28

### Orchestrator integration — drive YouOS from Hermes / OpenClaw / Telegram bot

User vision: "users set this IP on their local model like OpenClaw and Hermes and they handle email processing and triage through the same agent channel — usually Telegram, WhatsApp, or Slack." The end-user lives in their chat app; an orchestrator handles email by calling YouOS.

**What was already there** (audit at PR time): `/openapi.json` (12 endpoints), `/docs` (Swagger), `X-YouOS-Token` auth, `youos token-create`, per-account isolation on every endpoint.

**What this PR adds** (the missing pieces for chat-bubble UX):

1. **`GET /api/agent/digest?account=&days=1`** — orchestrator-facing endpoint mirroring `youos digest --format json`. Returns `summary` headline + counts + `pending_preview` (top-5 with action handles).

2. **`youos digest --format chat`** — compact text rendering with the summary headline + top-5 pending rows + auto-promoted senders + Tailscale URL. Designed for Telegram-bubble use (~1500 chars).

3. **`summary_line()` helper** — one-line headline ≤120 chars for push-notification / chat-bubble use.

4. **`pending_preview` field** on `DigestData` — top-5 rows captured at build-time so the chat formatter is pure.

**Live-verified**: `youos digest --format chat` shows clean rows with IDs; `GET /api/agent/digest` returns the same JSON.

**New `docs/INTEGRATIONS.md`** — the wiring recipe:
- ASCII architecture diagram (Telegram ↔ Orchestrator ↔ YouOS ↔ Gmail)
- Setup: Tailscale + `youos token-create` + paste token into orchestrator config
- Orchestrator playbook with 4 example dialogs
- Endpoint reference table
- Token-auth contract
- ~30-line Telegram bot example
- Security model

**Tests** — 5 new (digest formatters + route).

The agent loop is now driveable from any chat orchestrator without the user leaving their existing app.

## v0.2.0-beta.58 — 2026-05-28

### Multi-account end-to-end — verified + documented

Live-verified that `baher@medicus.ai` works alongside `drbaher@gmail.com` through the full agent loop on baheros.

**Verifications**:
- `gog auth list` shows both accounts authed
- `youos triage --account baher@medicus.ai` swept 8 unread threads, all hard-skipped cleanly (~20s vs ~40s for drbaher)
- `agent_audit` per-account: counts isolated
- `/api/agent/observability?account=...` returns per-account stats
- Scheduler with `interval_minutes=1` swept both accounts sequentially each tick (drbaher 40s → medicus 20s)

**Two findings + fixes**:

1. **`agent.accounts` wasn't a settable flag** — read by `get_agent_config()` but absent from the feature-flag whitelist, so `youos config set agent.accounts ...` failed with `unknown flag`. Added as `text` flag with comma-or-list parsing.

2. **`get_agent_config` now parses `agent.accounts` through `_parse_skip_senders`** so a CLI-set string works identically to a YAML list (case-folded, deduped, trimmed).

**Tests** — 2 new: comma-string parsing, list-form parsing.

**`docs/REMOTE_ACCESS.md`** — new "Multi-account setup" section:
- gog auth + `youos config set user.emails 'a@x.com, b@y.com'`
- Per-account vs global matrix (queue, audit, observability, dismissal stats are per-account; everything else is global)
- `agent.accounts` as the override knob
- Verification steps + typical per-account sweep duration

The architecture supported multi-account from the start (every store/API/CLI path takes `account=`); this PR confirms the end-to-end flow and fills the one CLI-affordance gap.

## v0.2.0-beta.57 — 2026-05-28

### Gmail-label dismissal signal — dismiss from any client

Final piece of the remote-access series (b54 docs → b55 mobile UI → b56 digest → **b57 remote dismissal**). Dismissing a queued draft previously required opening `/triage`. This PR lets you dismiss **from any Gmail client** by applying a Gmail label.

**Convention**: create a Gmail label called `YouOS/skip`. Apply it to the original inbound thread when you see an agent draft you want to dismiss. Next sweep, the matching `agent_pending_drafts` row is dismissed-as-noise; the label is removed.

**New `app/agent/gmail_label_sync.py`** — `sync_gmail_label_dismissals(account, database_url, label)`:
1. Searches Gmail for `label:YouOS/skip` via gog
2. For each match, looks up the pending row by `thread_id`
3. If found in `pending`/`amended`, marks dismissed with `reason='noise'`
4. Removes the label so subsequent syncs don't re-fire

**`run_triage` hook**: label sync runs at the start of every sweep before fetching unread. Failure-isolated — a label-sync error logs and sweep continues.

**New CLI**: `youos sync-labels [--account] [--label]` — on-demand without waiting for next sweep.

**Verified gog shapes** (live):
- `gog gmail search 'label:YouOS/skip' ...` returns `{"threads":[...]}` cleanly even when label doesn't exist
- `gog gmail messages modify <id> ... --remove <label>` confirmed with `--dry-run`

**Failure modes handled**: missing label → empty result; label-removal failure → keep dismissal; missing pending row → skipped; terminal-state row → skipped.

**Tests** — 7 new: clean empty, end-to-end dismiss+remove, skip-no-row, skip-terminal, removal-failure-doesn't-roll-back, invalid-label-edge, `run_triage` calls sync.

**Docs**: `docs/REMOTE_ACCESS.md` "Remote dismissal via Gmail label" section + `docs/USAGE.md` row.

The dismissal flows through the same path as `/triage`'s button — counted in dismissal-feedback aggregate, contributes to `agent.auto_promote_skip_senders`, surfaces on next digest. **The self-tuning loop now works from your phone with zero `/triage` access required.**

## v0.2.0-beta.56 — 2026-05-28

### Daily digest CLI — `youos digest`

Third step in the remote-access series (b54 docs → b55 mobile UI → b56 the remote signal). When you're away from your terminal, `/triage` requires Tailscale and macOS notifications fire on the Mac only. `youos digest` is the poor-man's push notification: a CLI that prints a complete activity summary you can pipe to `mail` via cron for a daily email.

**New `app/agent/digest.py`** — pure formatting on top of existing store helpers (`sweep_aggregate`, `dismissal_stats`, `noise_dismissal_candidates`, `list_pending`, `list_recent_sweeps`). No new DB schema. `DigestData` dataclass + `build_digest()` + `format_digest()` keeps the data layer testable independently from rendering.

**Three output formats**: `text` (default), `html`, `json`.

**What's in the digest**: sweep count + success rate, fetched/hard-skipped/drafted/surfaced totals, pending vs pushed-to-Gmail-Drafts vs dismissed counts, dismissal rate + by-reason breakdown, auto-promoted senders, top 5 dismissed-as-noise senders, clickable Tailscale URL to `/triage` when configured.

**Cron recipe** (in `docs/REMOTE_ACCESS.md`): pipe `youos digest --format html` into `mail` for a daily email.

**Live-tested** on baheros — output rendered cleanly with 8 sweeps / 81 fetched / 75 hard-skipped / 5 drafted in the sample window.

**Tests** (`tests/test_agent_digest.py`) — 5 new: aggregation against seeded state, all 3 format renderers, empty-state edge case.

**Docs**: `docs/REMOTE_ACCESS.md` "Daily digest email" section + `docs/USAGE.md` `youos digest` row.

## v0.2.0-beta.55 — 2026-05-28

### Mobile-responsive `/triage`

Next step in the remote-access series (b54 documented Tailscale; b55 makes the destination usable on a phone). `/triage` was desktop-first by design — the queue is most efficient when inbound + draft sit side-by-side — but with the agent loop running while you're away, review-from-phone matters more than the original assumption.

**Two media-query breakpoints** in `templates/triage.html`:

- **`@media (max-width: 600px)`** — phone-class viewports (iPhone 12-15 etc., 390×844). Repaints the page for touch:
  - Container padding 24→12px
  - Toolbar inputs/selects/buttons stack full-width with 44px min-height (iOS tap-target standard)
  - All input/select font-sizes set to 16px so iOS Safari doesn't auto-zoom on focus
  - Bulk-action buttons full-width
  - Row-actions: every button gets its own row at 44px touch height
  - Dismiss-group wraps: reason selector full-width on one line, "also skip sender" checkbox below, Dismiss button full-width
  - Inbound and draft textareas: smaller heights (240/100px), 16px font (still no zoom)
  - Activity table: horizontally scrollable with momentum (`-webkit-overflow-scrolling: touch`)
  - Agent health card: 2-column tiles instead of auto-fit
  - Help overlay: near-full-screen with scrolling

- **`@media (max-width: 380px)`** — extra-narrow (older / smaller phones)
  - Nav font shrinks
  - Health card tiles collapse to single column
  - Toolbar buttons one-per-row

**Critical detail**: every text-input font-size is explicitly `16px` on mobile. Smaller font-sizes cause iOS Safari to zoom in when the input gains focus — a notoriously bad mobile UX that's easy to ship by accident.

No layout changes for desktop — all rules are inside the `@media` blocks, so anything above 600px is unchanged from b54.

**Testing path** (when on Tailscale-connected phone): set up b54's Tailscale remote-access, open `http://<hostname>:8901/triage`, verify each section behaves. Most browsers expose mobile-emulator devtools (Chrome → DevTools → Device Toolbar → iPhone 14) for desktop testing.

## v0.2.0-beta.54 — 2026-05-28

### Remote access docs + safer `youos status` / `youos doctor` exposure messaging

The remote-access infrastructure already existed — non-loopback bind via `server.host`, PIN auth via `server.pin`, Tailscale hostname via `tailscale.hostname`. What was missing was the *setup story* + safety rails that flag insecure configurations.

**New `docs/REMOTE_ACCESS.md`** — end-to-end Tailscale + PIN setup walkthrough. Covers prerequisites, the 5-step config (find hostname → set PIN → bind to 0.0.0.0 → set Tailscale hostname → restart), what to do on the phone (URL, add-to-home-screen), what's protected (PIN, API tokens, Tailnet identity), what's not yet supported (push notifications, mobile-responsive UI, remote dismissal), and a troubleshooting section.

**`youos status` fixes**:
- Tailscale URL was shown as `https://<hostname>.ts.net` — wrong scheme (no TLS terminator) and missing port. Now shows `http://<hostname>:<port>` matching direct binding.
- New `Remote URL:` line when `server.host` is non-loopback but Tailscale isn't configured.
- Loud `⚠️ server.host is exposed but server.pin is empty` warning when binding without auth.
- Default ("not configured") nudge points at the new docs.

**`youos doctor` warning** for the same insecure-exposure case: `server.host = '0.0.0.0' is exposed (non-loopback) but server.pin is empty. Anyone on your network can reach /triage.` Smoke-tested locally — fires correctly.

**README** — new "Remote access" line under the Autonomous-triage section pointing at `docs/REMOTE_ACCESS.md`.

No code changes to the FastAPI server itself; this is purely surfacing + documenting the existing remote-access capability so users can actually use it without trial-and-error.

## v0.2.0-beta.53 — 2026-05-28

### Process hardening: branch protection + verification checklists in CONTRIBUTING

CI was already running the right checks (ruff + pytest matrix on 3.11/3.12). The reason b41–b48 went red on `main` for days without anyone noticing: **`main` had no branch protection**. Red CI didn't block merges; nothing forced anyone to look at the badge.

**Branch protection now active** on `main` (set via `gh api`):
- Required status checks: `test (3.11)` AND `test (3.12)` must pass before merge
- `strict: false` (don't force rebases on every PR)
- `enforce_admins: false` (Baher can still hot-fix in an emergency)

**`CONTRIBUTING.md` extended** with a "Verification checklists" section capturing the 4 bug classes caught this session. Each comes from a real merged commit that broke real things; each has a cheap detection step:

1. **Code that shells out to an external CLI** → run `<cmd> --help` or schema introspection (`gws schema <method>`) on a real machine, isolate the invocation to one function, name the verification command in a top-of-function comment. (b47/b48.)
2. **Tests that mutate config** → also `monkeypatch.setattr("app.core.config.CONFIG_PATH", ...)` + `load_config.cache_clear()`; check `git diff youos_config.yaml` after running. (b46.)
3. **Tests that exercise model generation** → stub `model_server.is_enabled` to False when fixture-asserting on subprocess calls. (b50.)
4. **Anything touching `sqlite:///` URLs** → use `removeprefix("sqlite:///")`, not `urllib.parse.urlparse(...).path` (which silently absolutizes). (b49.)

The checklist is meant to grow as we learn. Not bureaucracy — every entry corresponds to a real bug class with a known cheap detection step.

## v0.2.0-beta.52 — 2026-05-28

### Audit-surfacing for auto-promoted senders

When `agent.auto_promote_skip_senders` is on and the loop adds a sender to `skip_senders`, the action only lived in stdout logs. Trusting an autonomous behavior needs visibility — this PR routes the promotion list onto the audit row and renders it in `/triage` Recent activity.

**Schema** (`app/db/bootstrap.py`): idempotent ALTER adds `auto_promoted_json TEXT NOT NULL DEFAULT '[]'` to `agent_audit`. Existing DBs migrate on next server start.

**DAL** (`app/agent/store.py`):
- `log_sweep(..., auto_promoted_senders=None)` — new kwarg.
- `_audit_row_to_dict` rehydrates `auto_promoted_json` → `auto_promoted: list[str]`.

**Orchestrator** (`app/agent/triage.py`): the `_maybe_auto_promote_skip_senders` call moved from *after* `log_sweep` to *before*, capturing the return value and passing it as `auto_promoted_senders=`. Failure-isolated — a raise still returns `[]` and the sweep is logged either way.

**`/triage` UI**: new `Auto-promoted` column in Recent activity. Empty (`—`) when nothing was promoted; numeric count with a hover-tooltip listing the senders when something was.

**Tests** — 3 new: DAL roundtrip, null-safety default, end-to-end through `run_triage`.

## v0.2.0-beta.51 — 2026-05-28

### Filter quality: transactional templates no longer false-positive

A real-world QA case from b50 live testing — an "Ali Barber Shop Booking Confirmation" hit score 0.60 (base 0.5 + `imperative verb present` 0.10, exactly at threshold) and got auto-drafted. The drafted reply was a paraphrase of the confirmation itself — wrong response to a transactional acknowledgement, and a waste of the agent's daily budget.

**New detector**: `TRANSACTIONAL_TEMPLATE_PAT` matches confirmation/receipt patterns in subject or body:
- Subject lines: `booking confirmation` · `order confirmation` · `appointment confirmation` · `reservation confirmation` · `receipt for` · `payment (received|confirmation)` · `delivery scheduled` · `order (placed|received|shipped)`
- Body openings: `Your (appointment|booking|order|reservation|payment|purchase|delivery|subscription|trip|flight|hotel) (is|has been) (confirmed|booked|scheduled|received|placed|shipped|processed|ready)`

**Effect** (soft penalty, not hard skip — a "could we reschedule?" reply quoting one of these phrases shouldn't be silenced):
- Subject match: **−0.25**
- Body match (first 500 chars): **−0.20**
- Imperative-verb bonus is **suppressed** when the template detector fires (imperative verbs like "looking forward to see you" are template noise, not requests for action)

Re-classified the Ali Barber row after the fix:
```
score: 0.35 (was 0.60)
needs_reply: False
surface_for_review: True
reasons:
  · transactional template (subject)
  · imperative verb present — suppressed (transactional)
  · short body (41 words)
```

Still visible if you want to act on it, but the agent won't draft for it.

**Tests** (`tests/test_agent_needs_reply.py`) — 4 new:
- Ali Barber row pinned (subject-pattern path, score < 0.6, surface_for_review = True)
- Body-only template phrase ("Your reservation has been confirmed")
- False-positive guard — human reply mentioning "booking" without template phrasing keeps full score
- Order-receipt pattern (Amazon-style)

All 23 needs_reply tests pass.

## v0.2.0-beta.50 — 2026-05-28

### Test isolation: 4 model tests now reliable on dev machines with a warm mlx_lm.server

The 4 "pre-existing" model-test failures I'd been carrying as "not my problem" (`test_model_compare`, `test_model_server`, `test_persona_adapters_phase_3`, `test_stream_local_model`) turned out to share one root cause: each test exercises the **cold subprocess path** of model generation but doesn't disable the **warm-server short-circuit**. On a dev machine with `mlx_lm.server` actually running (which is the normal state — that's the whole point of the warm server), the production code skips the Popen / `_run_subprocess` call, so the test fixtures' captured `cmd` is empty and assertions fail.

The bug is in the **tests**, not the production code: each test should explicitly pin "warm server unavailable" as a precondition. Fixed by adding one line per test:

```python
monkeypatch.setattr("app.core.model_server.is_enabled", lambda: False)
```

(For `test_ensure_running_skipped_under_pytest`, the equivalent is `monkeypatch.setattr(ms, "is_healthy", lambda: False)` — same idea, different surface.)

**Result**: `python -m pytest tests/` now runs **1262 passed, 1 skipped, 0 failed** end-to-end on a dev machine with an active `mlx_lm.server`. CI was already green because GitHub Actions runners don't have a warm server running.

### What this completes

Continues the test-hygiene work from b46 + b49. Three classes of test-fragility caught and fixed this session:

| Class | Where | Fix |
|---|---|---|
| `monkeypatch.setenv` not enough for module-level globals | b46 (`test_agent_routes.py` writing real config) | `monkeypatch.setattr` on the module global + `cache_clear()` |
| `urllib.parse.urlparse` always absolutizes paths | b49 (`youos triage` couldn't open DB) | `removeprefix("sqlite:///")` matches bootstrap |
| **Warm-server short-circuit invalidates Popen tests** | **b50 (this PR)** | **Stub `model_server.is_enabled` per test** |

## v0.2.0-beta.49 — 2026-05-28

### Fix: `youos triage` DB path resolution + CI lint cleanup

**`youos triage` CLI was broken** — running it produced `OperationalError: unable to open database file`. Two agent modules (`app/agent/store.py` + `app/agent/needs_reply.py`) parsed `sqlite:///var/youos.db` via `urllib.parse.urlparse(...).path`, which always returns the path as absolute — `/var/youos.db` instead of the intended relative `var/youos.db`. Bootstrap + all ingestion modules use `removeprefix("sqlite:///")` which preserves relative paths correctly. Aligned both agent files to the bootstrap pattern.

Live-verified by running `youos triage --account drbaher@gmail.com --window 3d --limit 8` after the fix — successfully fetched 8 messages, hard-skipped 6 (GitHub CI, Substack), surfaced 1 borderline, drafted 1 (a false positive worth dismissing as `noise`).

### CI was failing on ruff lint (39 errors from b39–b48 PRs)

CI on `main` had been red since b41-ish; the failure landed in main without anyone noticing. Cleaned up:
- B904 (`raise ... from exc`) — 4 places in `agent_routes.py` + 1 in `cli.py`
- E501 (line too long) — 4 long `help` strings in `feature_flags.py` reformatted as parenthesised concatenation
- E702 (multi-statement semicolons) — 3 cases (one fixture, two test method sentinels)
- F841 (unused variable) — 1 stale `db_url = ...` in `test_push_to_gmail_success_stores_draft_id_and_marks_sent`
- I001 (import sorting) — auto-fixed in 5 files

All 119 agent + clawhub + gmail_write + scheduler + needs_reply tests pass; `ruff check tests/ app/` reports zero issues. CI should go green on the next push.

## v0.2.0-beta.48 — 2026-05-28

### CRITICAL: fix gws `drafts create` call shape — verified against gws schema

Companion fix to b47. The b40 `_gws_create_draft` was a guess. Live-checked `gws schema gmail.users.drafts.create` — the actual interface is fundamentally different.

| What we shipped (broken) | What actually works |
|---|---|
| `gws gmail drafts create` (4 args) | `gws gmail users drafts create` (5 args; "users" subresource) |
| `--user <email>` | `--params '{"userId": "<email>"}'` |
| `--threadId <tid>` (top-level) | `"threadId"` inside the message dict in `--json` body |
| `--format json` | default; flag unnecessary |
| `--raw <b64>` (top-level) | `"raw"` inside the message dict |

`gws` is the official Google Workspace CLI. Its argv convention is `<service> <resource> [<subresource>] <method>` with URL params via `--params` and request body via `--json`. Any user on `ingestion.google_backend=gws` would have hit "unrecognized subcommand" errors on every Push to Gmail Drafts click.

**Live verification path**: `gws schema gmail.users.drafts.create` returns the full schema. A live create wasn't possible on this machine — `gws` isn't authed here (the user's primary backend is gog) — but the schema is now the source of truth.

**Tests** (`tests/test_gmail_write.py`): 2 gws tests rewritten to pin the actual call shape — verify `--params` JSON contains `userId`, `--json` body contains `message.raw` and (optionally) `message.threadId`. Error-path tests unchanged.

---

Push to Gmail Drafts backend matrix is now end-to-end correct:

| Backend | Status | Verified |
|---|---|---|
| **gog** (b47) | Live-verified | 2 real drafts created + deleted on drbaher@gmail.com |
| **gws** (b48) | Schema-verified | Live create deferred until gws auth available |
| **native** (b46) | Schema-verified | googleapiclient call shape matches REST API |

## v0.2.0-beta.47 — 2026-05-28

### CRITICAL: fix gog `drafts create` call shape — Push to Gmail Drafts was broken since b37

Live-verified against `gog` 0.17.0. The b37 `_gog_create_draft` was based on the Google REST API shape — it passed `--raw <base64-rfc822>` + `--thread-id`. **`gog gmail drafts create` doesn't expose `--raw` or `--thread-id` at all.** It takes broken-out fields and threads via the inbound message id:

| What we shipped (broken) | What actually works |
|---|---|
| `--raw <base64-rfc822>` | `--to <addr> --subject <s> --body-file -` (body via stdin) |
| `--thread-id <tid>` | `--reply-to-message-id <mid>` |

Anyone who tried **Push to Gmail Drafts** between b37 and b46 got a CLI error. The mocked test suite passed (we asserted the shape we wrote, not the shape gog wants) — a textbook "tests verified the wrong contract" failure.

**Fixes**:

- `_gog_create_draft` rewritten to use the verified CLI shape. Body goes via stdin (`--body-file -`) so multi-line / shell-hazardous content passes through unmangled.
- `create_draft(...)` signature gains `reply_to_message_id=` alongside `thread_id=` — gog uses the former, gws/native use the latter. `agent_pending_drafts.message_id` is the gog id; `thread_id` stays for the other backends.
- `/api/agent/pending/{id}/push_to_gmail` now passes both ids so each backend gets what it wants.
- Tests updated to pin the actual command shape. Sentinel assertions guard against drift.

**Live verification** (drbaher@gmail.com):
- Created a real draft via the CLI directly → `draftId=r6218207234521709256`, `threadId` matched the inbound's threadId. ✓
- Created another via `app.ingestion.gmail_write.create_draft(backend='gog')` → `draftId=r-8815361813087480813`, threading correct, multi-line body intact. ✓
- Both verification drafts deleted.

**Why this slipped**: the b37 PR explicitly flagged the gog command as "best-effort; needs live verification" and isolated it to one function for easy correction. The discipline paid off — fix was a single-function rewrite. Lesson: mocked tests can't catch a wrong call shape; "verified against the real CLI" check should be in the merge gate for any subprocess-shelling code.

### Other (carried forward in this PR)

- gws backend (b40) — call shape unchanged, but it's also unverified against the actual gws CLI. Plain `--user / --threadId / --format json / --raw` was a guess; needs the same `gws gmail drafts create --help` check before trusting it. Adding to the post-merge follow-up.

## v0.2.0-beta.46 — 2026-05-28

### Test isolation fix (caught: PR #119/120 tests had been writing to the real `youos_config.yaml`)

`app/core/config.py` binds `CONFIG_PATH` at module-import time from `YOUOS_DATA_DIR`. Because `monkeypatch.setenv` in the `authed_client` fixture only fires *after* the module is imported, `set_flag` and the `/api/agent/skip_senders/promote` route were writing to the real user config. Caught when test ordering changed (running `test_gmail_write.py` first) caused promote_skip_senders tests to fail with empty `added: []` — those senders had been leaking in from previous test runs.

Fix: `tests/test_agent_routes.py` fixture now does `monkeypatch.setattr("app.core.config.CONFIG_PATH", tmp_path / "youos_config.yaml")` + `load_config.cache_clear()` so every authed_client test writes to its own tmp config. Cleaned `agent: { skip_senders: ... }` from `youos_config.yaml` (test pollution from b39/b43/b44 runs).

### Phase 2.3: native backend for Push to Gmail Drafts

Closes the backend matrix. `Push to Gmail Drafts` on `/triage` now works on `ingestion.google_backend=native` accounts in addition to `gog` (b37) and `gws` (b40).

**Implementation** (`app/ingestion/gmail_write.py`)

Direct call to Google's REST API via `googleapiclient` — `service.users().drafts().create(userId='me', body={...})`. Same RFC 822 → base64url shape as the CLI backends; the difference is just transport (HTTP vs subprocess).

A new `_NATIVE_WRITE_SCOPES` tuple combines `gmail.readonly` (existing ingestion scope) + `gmail.compose` (write scope). We don't merge `gmail.compose` into the ingestion adapter's `_NATIVE_SCOPES` because read-only users shouldn't be forced into a re-auth — the agent feature is opt-in, and so is the scope expansion.

**Re-auth path**: existing tokens stored at `var/google_tokens/<account>.json` are loaded with the write scopes. If they don't cover `gmail.compose`, the API returns 401/403 and we translate to a clear `GmailWriteError("Native backend draft creation needs the gmail.compose OAuth scope; your current token is read-only. Re-authorize: `youos setup` ...")`.

**Error translation** (parallels gog/gws):

| Failure | Resulting `GmailWriteError` |
|---|---|
| Missing credentials file | "No stored Google credentials for {account} at {path}. Authorize first via `youos setup`." |
| Expired token, no refresh | `_NATIVE_REAUTH_HINT` ("needs gmail.compose scope; re-authorize") |
| HTTP 401 / 403 | Same hint + status code |
| Other API exception | "native drafts.create failed: {exc}" |
| Missing `id` in response | "native drafts.create returned no id; payload=..." |
| `googleapiclient` not installed | "Native backend needs the google extra: pip install youos[google]" |

**Tests** (`tests/test_gmail_write.py`) — 6 new (parallel to the gog/gws suites):

- `test_native_creates_draft_and_extracts_id` — pins the API call shape (userId='me', body.message.raw + body.message.threadId).
- `test_native_skips_thread_id_field_when_none` — no threadId on new-thread drafts.
- `test_native_translates_403_to_reauth_hint` — scope-missing path.
- `test_native_translates_401_to_reauth_hint` — expired-token path.
- `test_native_translates_generic_exception_with_context` — other failures surface the underlying error.
- `test_native_translates_missing_id_to_gmail_write_error` — payload validation.
- `test_native_translates_credentials_runtime_error_to_gmail_write_error` — credentials helper failures become GmailWriteError, not 500s.

19 gmail_write tests total, all pass. Mocks `_native_gmail_service` so the auth + network stack is exercised by call shape, not real OAuth.

**Docs**: `docs/ARCHITECTURE.md` updated — backend matrix now lists all three implementations + their transport.

---

The full Push to Gmail Drafts surface area is shipped:

| Backend | Transport | Scope source | Status |
|---|---|---|---|
| gog | `gog gmail drafts create --raw …` (subprocess) | gog's own auth | b37 |
| gws | `gws gmail drafts create --raw …` (subprocess) | gws's own auth | b40 |
| native | `googleapiclient` → REST | `gmail.compose` on stored token | **b46** |

## v0.2.0-beta.45 — 2026-05-28

### Agent → LoRA training-pair pipeline

This PR completes the symmetric half of the dismissal-feedback story. b39-44 routed `noise`-style signal back into the filter (`agent.skip_senders`). **b45 routes drafting-quality signal back into the LoRA** (`feedback_pairs`).

When the agent drafts something and it's *wrong* — wrong tone, missed the point, made up facts — the right move isn't to dismiss it; it's to **teach the model** what you'd actually have said. New **Save as training pair** button on every draft card does exactly that:

1. Edit the draft textarea to what you'd actually send.
2. Click **Save as training pair**.
3. The `(inbound, agent's draft, your edited reply)` tuple is inserted into `feedback_pairs` (rating defaults to 2).
4. The next nightly LoRA retrain picks it up.

The row **stays in the queue** so you can also Push to Gmail Drafts (send the edited version) or Dismiss separately — these are orthogonal actions.

**New endpoint**: `POST /api/agent/pending/{id}/save_as_feedback_pair` with `{edited_reply, rating?, feedback_note?}`. Goes through the existing `app.api.feedback_routes.feedback_submit` handler in-process so the same edit-distance / edit-category / quality-score / facts-extraction logic runs — the new path and the interactive review queue produce identical training pairs. Surface tier rejected (400) — no draft to compare against. Empty `edited_reply` rejected (422).

**UI** (`/triage`): new **Save as training pair** button between **Copy draft** and **Mark sent manually**. Tooltip: "Edit the draft above to what you'd have said, then click — captures it as a training pair for the next nightly LoRA retrain." Status line surfaces `total_pairs` (running feedback-pairs count) so the user sees momentum: "Saved as training pair — 47 pairs collected."

**Tests** (`tests/test_agent_routes.py`) — 3 new: insertion happy-path (asserts `total_pairs` increments + `edit_distance_pct` is plausible), empty `edited_reply` → 422, surface tier → 400.

---

The full dismissal-feedback story is now symmetric:

| Reason | Routes to | How |
|---|---|---|
| `noise` | `agent.skip_senders` | b43 (one-click) / b44 (auto) |
| `wrong_sender` | (manual triage; user-driven) | b39 + UI checkbox in dismiss |
| **`wrong_content`** | **`feedback_pairs` → LoRA** | **b45 (this PR)** |
| `already_handled` | no action (orthogonal) | — |
| `other` | no action | — |

## v0.2.0-beta.44 — 2026-05-28

### Agent — auto-promote skip_senders at sweep tail (opt-in, off by default)

The b43 PR made promotion one-click. This PR makes it zero-click — *if* the user opts in. With `agent.auto_promote_skip_senders` on, the agent itself promotes any sender dismissed as `noise` 3+ times in the last 30 days to `agent.skip_senders` at the tail of every sweep. The next iteration of the loop already sees the new skip-list — fully self-tuning.

The b39 → b42 → b43 → b44 arc:
- b39 — categorical dismissal reasons (substrate)
- b42 — observability card (visibility)
- b43 — one-click promotion (suggestion → action)
- **b44 — zero-click auto-promotion** (action → habit)

**Threshold**: 3 dismissals (higher than the UI's min_count=2). Auto-action without click should require stronger signal than a user-confirmed promotion.

**Default off**. Even with it on:
- The promoted senders show up in the resulting `agent.skip_senders` value — visible at `/settings` and editable there. Easy to remove anything you didn't want.
- The promotion is logged to the structured logger ("auto-promoted N sender(s) to agent.skip_senders for account=... : ...").
- Already-on-list senders aren't re-added (no duplicate writes).
- If no senders qualify, the flag isn't touched at all.

**Failure isolation**: The auto-promote step runs after the audit-log write, inside its own try/except. A failure there can't crash the sweep or corrupt the audit row.

**New flag** (`app/core/feature_flags.py`): `agent.auto_promote_skip_senders` — bool, default False, with a help string explaining the threshold and reversibility.

**Implementation** (`app/agent/triage.py`): new `_maybe_auto_promote_skip_senders` helper called at the tail of `run_triage`. Mirrors the `/api/agent/skip_senders/promote` route logic so the two paths stay in sync (one for user-initiated, one for auto).

**Tests** (`tests/test_agent_triage.py`) — 3 new: no-op when flag off, promotes qualifying senders (≥3 noise dismissals) when on, skips already-listed senders without writing the flag.

**Docs**: SKILL.md now describes the self-tuning loop end-to-end.

## v0.2.0-beta.43 — 2026-05-28

### Skip-sender promotion — closing the feedback loop

The observability card in b42 *told* the user to extend `agent.skip_senders`; this PR makes it a one-click action. When the user dismisses the same sender as `noise` 2+ times, that sender shows up in the Agent health card with a checkbox — tick the ones to promote, click **Promote selected to skip_senders**, and they're added to the flag. Effective on the next sweep.

This is the natural follow-on to the b39 dismissal-feedback substrate: signal → aggregation → suggestion → one-click action. The user stays in control (no auto-promotion without explicit click) but doesn't have to hand-edit `/settings` anymore.

**New aggregation helper** (`app/agent/store.py`)

`noise_dismissal_candidates(account=None, days=30, min_count=2)` — groups dismissed-as-noise rows by `LOWER(sender_email)`, returns `[{sender_email, count, most_recent, last_subject}]` for any sender meeting the count threshold. Ordered by count DESC then most-recent. Excludes NULL / empty `sender_email` (can't promote what has no address).

**New endpoints**

- `GET /api/agent/skip_sender_candidates?account=&days=30&min_count=2` — the promotion candidates.
- `POST /api/agent/skip_senders/promote` with `{senders: [list]}` — appends to `agent.skip_senders` via the same feature-flag whitelist `/settings` uses. Preserves separator (comma or newline). Idempotent — already-present senders go into `already_present`, not `added`; the return value never duplicates within a single request.

**`/triage` Agent health card**

When candidates exist, a new "Promote to skip-list" section renders after the dismissal-reason breakdown:

```
☑ daily.com/newsletter@daily.com  (3×)  — last: "Q3 roundup"
☑ marketing@blast.com             (2×)  — last: "Special offer!"
☐ events@conference.io            (2×)  — last: "Reminder: tomorrow"

[ Promote selected to skip_senders ]  [ Uncheck all ]
```

Selecting senders and clicking the button calls the promotion endpoint, reports `added N, already on list M`, and reloads the card so promoted entries drop off the candidates list (they'll be hard-skipped on the next sweep, so future dismissals won't accumulate).

**Tests**

- `tests/test_agent_store.py` — 3 new: grouping + min_count filter (different reasons / counts), case-insensitive sender dedup, NULL/empty sender exclusion.
- `tests/test_agent_routes.py` — 3 new: candidates endpoint shape, promote appends + idempotency, empty list rejected.

Test pollution check: the test fixture isolates config writes to `tmp_path` — the user's `youos_config.yaml` is untouched.

## v0.2.0-beta.42 — 2026-05-28

### Agent observability — health card on /triage

The dismissal-feedback PR (b39) shipped the substrate; this one consumes it. New `Agent health` collapsible at the top of `/triage` shows the agent's behavior at a glance over the last 30 days — sweep success rate, throughput, dismissal signal, score distribution — plus rule-based hints that tell you *what to change* when the numbers look off.

**New `GET /api/agent/observability`** — one fetch returns three aggregates + hints:

- `sweep` — counts (sweeps, successful, fetched, kept, surfaced, persisted, avg_duration_ms), success rate, derived `hard_skipped = fetched - kept`.
- `dismissals` — the b39 aggregate (total, dismissed, rate, by_reason).
- `score_histogram` — buckets needs_reply_score across persisted rows into 5 bands (0.0-0.3 / 0.3-0.5 / 0.5-0.7 / 0.7-0.9 / 0.9-1.0). Boundary choices line up with the surface-for-review band.
- `hints` — rule-based interpretations the UI doesn't need to encode. Three rules currently fire:
  - **Noise > 30%** of total persisted (when total ≥ 5) → "raise `agent.threshold` or extend `agent.skip_senders`."
  - **Sweep success rate < 80%** (when sweeps ≥ 3) → "check Recent activity for the actual errors."
  - **≥ 3 `wrong_content` dismissals** → "review-queue these as feedback pairs to retrain the LoRA" (drafting signal, distinct from filter signal).

**New aggregation helpers in `app/agent/store.py`**

- `sweep_aggregate(account=None, days=30)` — derives `hard_skipped` from audit counters since hard-skipped rows aren't persisted (they're filter-stage noise).
- `score_histogram(account=None, days=30)` — five buckets, zero-filled.

**`/triage` UI** — new `Agent health` `<details>` section right above the drafts:

- Four tiles: Sweeps (30d) · Fetched · Drafted · Dismissed (each with a sub-label).
- Yellow hint callouts when any of the three rules fire.
- Horizontal bar chart of the score histogram.
- Dismissal-reason breakdown (zero buckets hidden to keep the list tight).

Refresh-on-demand by changing the account selector; otherwise updates automatically after each `fetchPending()` call (which happens on triage runs and manual refresh).

**Tests**

- `tests/test_agent_store.py` — 4 new: sweep_aggregate sums + success-rate, account filter, empty-table edge case, score_histogram bucketing.
- `tests/test_agent_routes.py` — observability endpoint returns the unified shape with all three aggregates + hints.

---

This completes the agent-triage feature arc (α–ζ + Phase 2.1/2.2 + dismissal-feedback + UX + observability). You can now: enable the loop opt-in, watch it run autonomously, see what it's doing, dismiss with categorical feedback, push surviving drafts to Gmail, and get rule-based guidance when the filter or model drifts off.

## v0.2.0-beta.41 — 2026-05-28

### `/triage` UX upgrades

The triage queue is fine for 3 drafts; it's painful for 15. This PR threads four ergonomic upgrades into the page without changing any backend behavior.

**Keyboard shortcuts** — `j` / `k` move between draft cards (visible focus ring); `p` pushes the focused draft to Gmail Drafts; `d` dismisses it; `e` jumps into the draft editor; `m` marks sent manually; `r` refreshes; `?` opens a help overlay; `Esc` closes the overlay or unblurs a focused textarea. Disabled while an input or textarea has focus — typing `d` into the draft editor won't dismiss the row.

**Bulk actions** — two toolbar buttons:
- **Push all visible** — pushes every visible draft to Gmail Drafts sequentially (confirmation prompt; reports `ok/failed` count when done).
- **Dismiss all surface as noise** — bulk-dismisses every surface-for-review row with `reason='noise'`, feeding the dismissal-feedback aggregate. The single best move when the agent surfaced obvious newsletters or CI mail you don't want to keep seeing.

Both bulk actions operate on the *currently visible* rows, so the new filter bar is your safety control — narrow the filter before bulking.

**Filter bar** — substring filter by sender (matches name + email) and a min-score selector (any / 0.50 / 0.60 / 0.70 / 0.80). Purely visual; doesn't refetch. Status line shows "N drafts · M for review (filtered from total)" so you always see what's hidden.

**Add-to-skip-senders on dismiss** — checkbox next to every Dismiss button: "also skip sender". When ticked, the row's `sender_email` is appended to `agent.skip_senders` via the existing `/api/config/set` endpoint *before* the dismissal POST, so the maintenance lands even if the dismissal itself fails. Idempotent — already-present senders trigger "(already in skip-senders)" feedback. Preserves the existing separator (comma or newline) so you can keep your skip list however you've been formatting it in `/settings`.

**Tests** — `tests/test_agent_routes.py::test_triage_page_includes_ux_upgrades` pins the IDs / sentinel strings so the new HTML elements can't silently vanish.

No schema changes, no API changes — everything client-side on top of existing endpoints (`/api/agent/pending/{id}/{push_to_gmail,dismiss}`, `/api/config/{flags,set}`).

## v0.2.0-beta.40 — 2026-05-28

### Phase 2.2: gws backend for Push to Gmail Drafts

The Phase 2.1 push-to-Gmail-Drafts path (b37) only supported the `gog` backend; calling it on a `gws`-backed account raised `NotImplementedError`. This PR adds the gws path so `/triage`'s **Push to Gmail Drafts** works on both backends.

Implementation mirrors the gog path — RFC 822 message, base64url-encoded `--raw`, isolated to a single `_gws_create_draft` function so if your installed `gws` uses different flag names it's a one-line fix. Tests pin the call shape (`--user`, `--threadId`, `--format json`, `--raw`) so any drift surfaces in one place. Verification path: `gws gmail drafts create --help` on the target machine.

Error translation matches gog: nonzero exit → `GmailWriteError` with stderr; `FileNotFoundError` → "gws CLI not on PATH" message; malformed JSON or missing `id` → distinct errors with payload context.

`native` backend still raises NotImplementedError — needs `gmail.compose` OAuth scope + one-time re-auth; deferred to Phase 2.3.

**Tests** (`tests/test_gmail_write.py`) — 6 new: success + thread-id elision + 3 error paths, identical structure to the gog suite.

## v0.2.0-beta.39 — 2026-05-28

### Agent triage — dismissal-as-feedback loop

When you dismiss a queued draft, the agent now asks why and remembers — so the filter can learn which inboxes/senders to skip without needing you to maintain `agent.skip_senders` by hand. Until now `Dismiss` was a black hole: the row vanished and the filter learned nothing. With this PR every dismissal carries a categorical reason that aggregates into a dismissal-rate metric per account, ready to drive the upcoming observability surface.

**Schema**

- New `dismissal_reason TEXT` column on `agent_pending_drafts` (idempotent ALTER; legacy rows stay NULL and aggregate into the `no_reason` bucket).
- Bounded set of reasons: `noise` (filter shouldn't have drafted) · `wrong_sender` (right kind of mail, wrong person to reply now) · `wrong_content` (draft missed the point — a *drafting* quality signal, not a filter one) · `already_handled` (replied outside YouOS) · `other`.

**DAL** (`app/agent/store.py`)

- `mark_dismissed(row_id, *, reason=None)` — accepts the new reason; unknown values coerced to `'other'` as defence-in-depth.
- New `dismissal_stats(account=None, days=30)` aggregates over a rolling window, returning `{total_persisted, dismissed, dismissal_rate, by_reason}` with the categorical breakdown zero-filled.

**API**

- `POST /api/agent/pending/{id}/dismiss` accepts an optional `{"reason": "..."}` body. Unknown reasons → 400 with the allowed-list. Empty body keeps working (legacy).
- New `GET /api/agent/dismissal_stats?account=&days=30` — returns the aggregate.

**`/triage` UI**

- Each Dismiss button is now flanked by an optional reason selector ("Why? (optional)" → noise / wrong_sender / wrong_content / already_handled / other). Click Dismiss with no selection and behavior is unchanged. Pick one and it's logged alongside the dismissal.

**Tests**

- `tests/test_agent_store.py`: dismissal records reason, coerces unknowns, stats aggregate correctly and filter by account.
- `tests/test_agent_routes.py`: API accepts/rejects reasons, `dismissal_stats` endpoint returns the expected shape.

This PR doesn't yet *use* the dismissal signal anywhere — that's the planned observability + tuning work. It ships the substrate so dismissal data starts accumulating immediately, making downstream PRs meaningfully more useful as soon as you have a few days of data.

## v0.2.0-beta.38 — 2026-05-28

### Documentation refresh — agent triage feature

The autonomous-triage feature shipped across PRs α–ζ and Phase 2.1 was already user-visible (CLI, /triage page, settings), but the **introductory surfaces** — landing page, SKILL.md, README, /about, docs — still described YouOS as draft-only. This PR threads a consistent agent narrative through five surfaces so a first-time visitor lands on a coherent story.

**`SKILL.md`** — new "Autonomous triage (opt-in)" section between "Drafting inside Gmail" and "How it works": enabling commands, the 6-step loop summary, /triage page description, Phase 2.1 Push-to-Gmail-Drafts path, and safety features (never auto-sends, opt-in, audit log, rate-limit, sender skip list, strict-local mode).

**`README.md`** — new "Autonomous triage (opt-in)" section right before "Does it actually sound like you?": same enabling snippet and feature summary as SKILL.md, framed for repo readers.

**`docs/USAGE.md`** — new `youos triage` row in the command table: `Sweep unread inbox, filter, draft survivors; persists to agent_pending_drafts (view at /triage). Background loop opt-in via agent.enabled.`

**`site/index.html` (landing)** — new comparison card in the "vs cloud assistants" block: "You manually paste drafts one by one ↔ Optional autonomous triage — sweeps unread, drafts replies, never auto-sends (opt-in via `agent.enabled`; review queue at `/triage`; push to Gmail Drafts with one click)."

**`templates/about.html` (/about)** — `youos triage` added to the CLI tech-card bullet; new "🤖 Autonomous triage (opt-in)" tech-card detailing the loop, two-tier filter, per-sender skip list, daily-cap, strict-local mode, standing instructions, audit log, and the explicit no-auto-send guarantee.

**`docs/ARCHITECTURE.md`** — new "Autonomous triage (`app/agent/`) — opt-in" component section between Autoresearch and Web UI, with one bullet per module (`inbox_fetch`, `needs_reply`, `triage`, `scheduler`, `store`) plus a paragraph on the /triage page, the Push-to-Gmail-Drafts path, and the seven `agent.*` flags. New tables (`agent_pending_drafts`, `agent_audit`) added to the Database key-tables list.

No code changes; behavior is identical to b37.

## v0.2.0-beta.37 — 2026-05-28

### Agent triage — Phase 2.1 (Push to Gmail Drafts)
The **Mark sent** button used to just stamp a timestamp; you still had to copy-paste the draft into Gmail. Phase 2.1 adds a **Push to Gmail Drafts** button that creates a real Gmail Draft on the original thread via the configured ingestion backend — you open Gmail and finish-and-send from there. The agent never sends; Phase 2 only writes drafts, never ``messages.send``.

### New `app/ingestion/gmail_write.py`
- ``create_draft(account, thread_id, to_email, subject, body, backend=None)`` — backend dispatch on ``ingestion.google_backend``.
- ``GmailDraftResult(draft_id, raw_response)`` — happy-path payload.
- ``GmailWriteError`` — surfaced to the route as HTTP 502 with the underlying message (e.g. "gog returned exit 1: scope not granted").

**`gog` backend implemented** (best-effort): builds an RFC 822 message via ``email.message.EmailMessage``, base64url-encodes it, passes via ``--raw`` to ``gog gmail drafts create --account … --thread-id … --json --no-input``. The exact subcommand and flag names are isolated to ``_gog_create_draft`` so swapping them is a one-line change if your local gog uses a different shape. (When you're back at the terminal, run ``gog gmail drafts --help`` to verify; the tests pin the call shape so any mismatch is caught in one place.)

**`gws` and `native` backends** raise ``NotImplementedError`` with a clear message pointing at Phase 2.2 — native specifically needs ``gmail.compose`` OAuth scope and a one-time re-auth.

### Schema + DAL
- New ``gmail_draft_id TEXT`` column on ``agent_pending_drafts`` (idempotent ALTER for upgrades from pre-Phase-2 instances).
- ``store.mark_sent(...)`` gains an optional ``gmail_draft_id=`` kwarg.

### Endpoint
- New ``POST /api/agent/pending/{id}/push_to_gmail`` — pulls the row, validates it has a draft (tier='draft' + non-empty), reconstructs the reply (uses ``amended_draft`` if user edited, else ``draft``; prepends ``Re:`` to the subject if missing), calls ``gmail_write.create_draft``, and on success marks the row sent + stores the Gmail draft id.
- 501 for unsupported backends, 502 for backend-side failures (so the UI can show the actual error), 400 if the row is surface-tier or has no draft.
- Existing ``mark_sent`` endpoint unchanged — kept as the "I sent it manually outside YouOS" signal.

### `/triage` UI
- New **Push to Gmail Drafts** button (primary) on each draft card; tooltip explains "create a real Gmail Draft on the original thread; then send it yourself from Gmail."
- **Mark sent** rephrased to **Mark sent manually** with a tooltip explaining when to use it.
- Success message shows the Gmail draft id ("Pushed to Gmail Drafts (Gmail draft <id>). Open Gmail to send.") so you can verify what landed.

### Tests
- ``test_gmail_write.py`` (9 new) — backend dispatch (unknown/gws/native), gog happy path with call-shape contract (verifies the exact argv + decodes the RFC822 payload + checks ``To:`` / ``Subject:`` / body landed), thread-id flag omitted when None, error translation for non-zero exit / FileNotFoundError / non-JSON stdout / missing id.
- ``test_agent_routes.py`` (4 new) — push success stores draft id + flips to sent, surface-tier row rejected with 400, NotImplementedError → 501, GmailWriteError → 502 with message preserved.
- 75/75 across agent + gmail-write suites; 1222/1226 full sweep (was 1209 in ζ; +13 here). Same 4 pre-existing MLX failures unrelated.

### Out of scope for 2.1 (deferred to 2.2)
- ``gws`` backend implementation (CLI shape unknown).
- ``native`` backend implementation (needs the ``gmail.compose`` OAuth scope + a one-time re-auth flow).
- Auto-push (never; the user always clicks).

### Verification path when you're back at the terminal
```bash
# Confirm the gog subcommand shape (the only thing I had to guess at):
gog gmail drafts --help
# Then try a single push from /triage. If gog uses different flags (e.g.
# `drafts.create` vs `drafts create`, or different `--raw` semantics),
# the fix is in one function: app/ingestion/gmail_write.py:_gog_create_draft.
```

## v0.2.0-beta.36 — 2026-05-28

### Agent triage — ζ (safety guardrails) — closes Phase 1
Three guardrails on the autonomous loop, all opt-in. **`agent.skip_senders`** (hard-skip a noisy sender or whole domain), **`agent.daily_draft_cap`** (per-UTC-day quota per account, defends against a runaway loop), **`agent.strict_local`** (refuse cloud fallback during background triage only — interactive `/feedback` is unaffected). With ζ shipped, Phase 1 of the autonomous-agent series is complete.

### Changes

**`app/core/feature_flags.py`** — three new flags surface on ``/settings``:
- ``agent.skip_senders`` (text, default empty) — comma-separated emails or ``@domain`` entries
- ``agent.daily_draft_cap`` (int, default 50) — 0 disables; per UTC day, per account
- ``agent.strict_local`` (bool, default False) — interactive paths unaffected

**`app/agent/scheduler.py`** — ``get_agent_config()`` now surfaces all three; ``_parse_skip_senders`` accepts the textarea (comma-separated) form and a list form, normalises to lowercase, dedupes.

**`app/agent/needs_reply.py`** — ``classify(..., skip_senders=...)`` adds a new hard-skip rule that runs FIRST, before list-unsubscribe. Exact emails (``alice@x.com``) and ``@domain`` prefixes both supported.

**`app/agent/store.py`** — ``count_persisted_today(account)`` returns the count of ``agent_pending_drafts`` rows created since UTC midnight for that account.

**`app/agent/triage.py`** — sweep body now:
- pulls all three guardrails once at start (stable across the sweep)
- threads ``skip_senders`` into ``classify_many``
- computes ``cap_remaining`` from ``count_persisted_today`` and decrements per persisted draft; once exhausted, the rest of the messages are recorded as cap-reached skips (no generation, no persistence)
- passes ``strict_local`` into ``DraftRequest``

**`app/generation/service.py`** — new ``DraftRequest.strict_local`` field. When True (and no ``backend_override``), ``fallback_model`` is forced to ``"none"`` for *this draft only*. Interactive ``/feedback`` doesn't set it; only the agent triage path does.

### Tests
- 4 in `test_agent_needs_reply` — skip-list exact match, domain prefix, case-insensitivity, no-match-keeps-original-behaviour
- 3 in `test_agent_triage` — daily cap stops drafting, skip_senders flows through, strict_local lands on DraftRequest
- 3 in `test_agent_scheduler` — `_parse_skip_senders` comma + list forms, dedup, empty
- 59/59 across agent suites; 1209/1213 full sweep (was 1199 in ε; +10 here). Same 4 pre-existing MLX failures.

### How to use
```bash
# Silence a specific sender or whole org:
youos config set agent.skip_senders "ali@noisycorp.com,@autotools.io"

# Cap daily drafts (0 = unlimited):
youos config set agent.daily_draft_cap 50

# Refuse cloud fallback for background sweeps (interactive /feedback unaffected):
youos config set agent.strict_local true
```

### Phase 1 retrospective
The autonomous-agent series shipped as 11 PRs (β.28–b36): an idea ("table β early; do A/B/C/D first to make drafts good enough") that turned into a fully-formed feature with persistence, a web UI, background scheduling, standing instructions, an audit log, and safety guardrails. Every PR ran the same loop: real-inbox QA → file a specific bug → fix → ship → repeat. The agent never auto-sends, always shows its work, and refuses cloud fallback when asked.

### Remaining (Phase 2, separate track)
- ``gmail.compose`` OAuth → real Gmail Drafts on **Mark sent**, so the "I sent it manually" signal becomes an actual Gmail draft on the thread.

## v0.2.0-beta.35 — 2026-05-28

### Agent triage — ε (audit log + "Recent activity")
Every triage sweep — whether triggered by the background scheduler, the API, or the CLI — now writes one row to a new ``agent_audit`` table. The ``/triage`` page surfaces the last 15 sweeps in a collapsible **Recent activity** panel: when, account, trigger, fetched/kept/surfaced/persisted counts, duration, and any per-message errors (hover for details). Trust-building: now that the agent runs autonomously, "what did it do while I was asleep" has a real answer.

**Schema** (``_migrate_agent_audit`` in ``app/db/bootstrap.py``):
- one row per sweep with ``account``, ``trigger`` (``scheduled`` / ``manual`` / ``api``), ``window``, ``threshold``
- counts: ``fetched``, ``kept``, ``surfaced``, ``persisted``
- ``errors_json``: per-message error strings from the sweep
- ``standing_instructions_snapshot`` captured at sweep time (separate from per-draft snapshot already in β.1)
- ``started_at`` / ``finished_at`` / ``duration_ms``
- indexes on ``started_at DESC`` and ``account, started_at DESC``

**DAL** (``app/agent/store.py``):
- ``log_sweep(...)`` — insert one row per sweep (called once at the end of ``run_triage``).
- ``list_recent_sweeps(account=None, limit=20)`` — newest first; rehydrates ``errors_json``.

**Orchestrator** — ``run_triage`` now takes ``trigger="manual"`` (default), brackets the sweep with timing, and writes the audit row on the way out. Audit-log failure is caught + logged at ``warning`` — the agent loop has higher priorities than its own observability, never crash the sweep over a logging glitch. The scheduler passes ``trigger="scheduled"``; ``/api/agent/triage`` passes ``trigger="api"``.

**Audit row written even when ``persist=False``** — ``--dry-run`` doesn't leak inbound data into ``agent_pending_drafts``, but it *does* leave a trace of what was attempted (with ``persisted=0``). So filter-tuning runs are still visible in the activity panel.

**`GET /api/agent/sweeps?account=…&limit=…`** — new endpoint returning the audit rows.

**`/triage` page** — new **Recent activity** ``<details>`` panel:
- Table: When (relative time), Account, Trigger, counts, duration, Errors (count; hover-title shows the messages).
- Rows with errors get a faint red tint so they stand out.
- Refreshes every time the pending list refreshes (after Run triage now, after row actions).

### Tests
- ``test_agent_store.py``: ``log_sweep`` insert + ``list_recent_sweeps`` ordering & rehydration, account filter (2 new).
- ``test_agent_triage.py``: audit row written with counts + trigger on every run; written even on ``persist=False``; per-message errors captured (3 new).
- ``test_agent_routes.py``: ``GET /api/agent/sweeps`` returns the rows with rehydrated ``errors`` (1 new).
- Two fixtures updated to also call ``_migrate_agent_audit``.
- 37/37 across agent suites; 1199/1203 full sweep (was 1193 in δ; +6 here). Same 4 pre-existing MLX failures.

### Remaining
- **ζ** — per-sender opt-out, daily cap, strict-local switch (refuse cloud fallback during triage).
- **Phase 2** — ``gmail.compose`` OAuth → real Gmail Drafts on **Mark sent**.

## v0.2.0-beta.34 — 2026-05-28

### Agent triage — δ (standing instructions)
A free-form text field threaded into every triage draft. Set it to "today I'm OOO; politely decline meetings" and the agent will reflect that in what it drafts. Snapshotted with each persisted row (column was reserved in β.1; now actually written) so a draft made under last week's instructions stays traceable to that exact text.

**Threading**:
- New ``DraftRequest.standing_instructions`` field. Inside ``generate_draft``, the cold-outreach ``DECLINE_NUDGE`` (b27) and the standing instructions are *combined additively* into the same ``extra_constraint`` slot that ``assemble_prompt`` consumes — so both can apply to a single draft when the inbound is a pushy outbound *and* the user is OOO.
- ``run_triage`` accepts ``standing_instructions=...``; when the caller omits it, the orchestrator reads ``agent.standing_instructions`` from config so the background scheduler + the CLI + the API-trigger path all see the same value.
- ``store.upsert_pending`` was already writing whatever the orchestrator handed it; now the orchestrator hands it the active standing-instructions string, and the snapshot column finally has data.

**`/triage` page** gains a collapsible **standing-instructions** banner at the top:
- Summary line shows the first 80 chars when set (teal-coloured); "none" when empty.
- Textarea + **Save** / **Clear** buttons. Save POSTs to ``/api/config/set`` with key ``agent.standing_instructions`` — the same config-write API the rest of ``/settings`` already uses.
- Changes take effect on the next triage run (immediate on a manual **Run triage now**, on the next γ tick for the background scheduler).

**`/settings` page** also surfaces the field as a flag (``type: "text"``). Settings.html gained ``text`` (textarea) and ``int`` (number input) renderers — fixing a pre-existing γ-era bug where ``agent.interval_minutes`` was rendering as a checkbox.

### Tests
- ``tests/test_agent_triage.py`` (3 new): standing-instructions threaded into ``DraftRequest``, snapshotted per persisted row, falls back to config when the caller omits it.
- 25/25 across agent suites; 1193/1197 full sweep (was 1190 in γ; same 4 pre-existing MLX failures unrelated).

### Remaining on the agent roadmap
- **ε** — audit log + "what the agent did today" view on ``/triage``.
- **ζ** — per-sender opt-out, daily cap, strict-local switch (refuses cloud fallback during triage).
- **Phase 2** — ``gmail.compose`` OAuth → real Gmail Drafts on **Mark sent**.

## v0.2.0-beta.33 — 2026-05-28

### Agent triage — γ (background scheduler + macOS notify)
The agent is now actually autonomous — the running ``youos serve`` process sweeps your unread inbox every N minutes by itself and posts a macOS notification when there's something new to review at ``/triage``. Opt-in via ``agent.enabled``; off by default so installing YouOS doesn't quietly start polling.

**`app/agent/scheduler.py`** (new):
- ``get_agent_config()`` — reads ``agent.*`` from ``youos_config.yaml`` (re-read every iteration so a ``youos config set agent.enabled false`` takes effect on the next tick; no restart needed).
- ``_loop(app)`` — the background coroutine. For each ``agent.accounts`` (or fallback to ``user.emails``), call ``run_triage`` in a thread executor and tally ``persisted``. Notify macOS only when the count is > 0 (no Notification Center spam on quiet polls). Sleeps via ``asyncio.wait_for(stop.wait(), …)`` so shutdown is immediate, not "wait the full interval."
- Per-iteration failures (transient gog auth, network blip) are caught + logged at info; the loop keeps running.
- ``_notify_macos(...)`` — best-effort ``osascript display notification``; silently no-ops on non-Darwin or if the call fails. Agent uptime > notification fidelity.
- ``start(app)`` / ``stop(app)`` — lifespan hooks. ``start()`` short-circuits when ``PYTEST_CURRENT_TEST`` is set, so tests can't accidentally launch a real sweep.
- A 60-second floor on the interval prevents an accidental tight-loop config.

**`app/main.py`** — lifespan calls ``scheduler.start(app)`` after the warm-server pre-warm; on shutdown ``scheduler.stop(app)`` sets the event and awaits the task (5s timeout, then cancel). Scheduler failure does NOT block server startup.

**`app/core/feature_flags.py`** — three new flags, surfaced on ``/settings``:
- ``agent.enabled`` (bool, default False)
- ``agent.interval_minutes`` (int, default 15)
- ``agent.notify_macos`` (bool, default True)

### Tests
- ``tests/test_agent_scheduler.py`` (11 new) — config reads + clamping, account resolution (explicit list vs fallback to ``user.emails``), ``osascript`` failure swallowed, loop exit-when-disabled, multi-account sweep + single notification with the correct count, no-notification-on-zero, sweep failure on one account doesn't kill the others, ``start()`` is a no-op under pytest.
- 1190/1194 full sweep (was 1179 in β.2; +11 here). Same 4 pre-existing MLX-integration failures, unrelated.

### How to turn it on
```bash
youos config set agent.enabled true                # opt in
youos config set agent.interval_minutes 15         # default
youos config set agent.notify_macos true           # default
youos config set agent.accounts '["baher@medicus.ai", "drbaher@gmail.com"]'  # optional; falls back to user.emails
# Restart the server (or just start it if not already running)
youos serve
```
Open ``/triage`` to see drafts as they appear.

### What's still missing (δ → Phase 2)
- **δ**: standing-instructions field in ``/settings``, threaded into the generation prompt via the existing ``extra_constraint`` hook.
- **ε**: audit log + a "what the agent did today" view on ``/triage``.
- **ζ**: per-sender opt-out, daily cap, strict-local switch (refuses to use cloud fallback during triage).
- **Phase 2**: ``gmail.compose`` OAuth → real Gmail Drafts on **Mark sent**.

## v0.2.0-beta.32 — 2026-05-28

### Agent triage — β.2 (API + `/triage` page)
Second half of β. The persisted ``agent_pending_drafts`` table is now visible and actionable through the web UI; the agent loop is end-to-end usable in a browser without touching the CLI.

**New `app/api/agent_routes.py`**:
- ``GET /api/agent/pending`` — list pending rows. Optional ``?account=`` / ``?tier=draft|surface`` / ``?status=`` / ``?limit=`` filters; JSON columns rehydrated to lists.
- ``POST /api/agent/pending/{id}/amend`` — save user edits to ``amended_draft``, status → ``amended``.
- ``POST /api/agent/pending/{id}/dismiss`` — status → ``dismissed``, ``dismissed_at`` stamped.
- ``POST /api/agent/pending/{id}/mark_sent`` — status → ``sent``, ``sent_at`` stamped (does NOT push to Gmail — that's Phase 2; this is the "I sent it manually, stop showing it" signal).
- ``POST /api/agent/triage`` — synchronous triage trigger from the UI (``{account, window, limit, threshold, backend}``). Defaults to the first ``user.emails``.
- ``GET /triage`` — page route serving the template.

**New `templates/triage.html`** — full UI:
- Toolbar: account input (persisted to ``localStorage``), window picker (24h/3d/7d/14d), **Run triage now** + **Refresh** buttons, status line.
- **Tier 1 — drafts**: each row is a card with score / cold-outreach / model badges, inbound (left) + editable draft (right), per-row actions (**Save edits** / **Copy draft** / **Mark sent** / **Dismiss**).
- **Tier 2 — surface for review**: collapsed `<details>` panel listing borderline cases that were intentionally not auto-drafted (e.g. the demo-form noreply lead from the medicus QA). Per-row **Dismiss** so the user can clear them.
- Theme-aware (light/dark via the existing `data-theme` / no-flash mechanism), uses the shared design system tokens from `youos.css`.

**Nav link wired into** ``feedback.html``, ``stats.html``, ``settings.html``, ``bookmarklet.html``, ``about.html`` — `/triage` shows up next to **Draft Email** in every chrome.

### Tests
- ``tests/test_agent_routes.py`` (7 new) — list with both tiers + tier filter, amend/dismiss/mark-sent state transitions, 404 on missing id, page renders with expected nav/assets.
- Fixed a test-isolation issue: ``app.state.settings`` is sticky across tests (set once at import time), so the fixture now re-binds it per test to point at the per-test DB.
- 32/32 across agent suites (was 25/25 in β.1; +7 routes); 1179/1183 full sweep (same 4 MLX-integration pre-existing failures, unrelated).

### What you can do now
1. ``youos triage`` (CLI) — persists drafts to the DB.
2. Visit ``/triage`` in the web UI — see them, edit, dismiss, mark sent.
3. The "Run triage now" button on the page triggers a fresh sweep without the CLI.

### What's still missing (γ → Phase 2)
- **γ**: background scheduler in the running server + macOS notification.
- **δ**: standing-instructions field threaded into the prompt.
- **ε**: audit log + a "what the agent did today" view.
- **ζ**: per-sender opt-out + daily cap + strict-local switch.
- **Phase 2**: ``gmail.compose`` OAuth → "Mark sent" pushes to real Gmail Drafts (so you actually send from Gmail, not just clear the queue).

## v0.2.0-beta.31 — 2026-05-28

### Agent triage — β.1 (persistence)
First half of β. Triage results now persist to a new ``agent_pending_drafts`` table, so the loop has memory between runs. Idempotent on the Gmail ``message_id`` — repeated triage runs on the same window don't re-draft the same inbound. The web UI (``/triage``) is the next PR (β.2); for now you inspect via SQL or the CLI summary.

**Schema** (``app/db/bootstrap.py``: new ``_migrate_agent_pending_drafts``):
- ``message_id`` unique → upserts are no-ops on repeat
- inbound snapshot (sender / subject / body / received_at)
- verdict (``needs_reply_score``, ``reasons_json``, ``cold_outreach``, ``tier``)
- draft (``draft``, ``draft_model``, ``draft_repairs_json``, ``standing_instructions_snapshot``)
- lifecycle (``status``: pending/amended/sent/dismissed, ``amended_draft``, ``sent_at``, ``dismissed_at``)
- two indexes (status+tier+created, account+status)

**Two-tier classification** (new ``NeedsReplyVerdict.surface_for_review`` flag): drafts (``tier='draft'``, scored ≥ threshold) and borderline cases (``tier='surface'``, scored 0.30–0.59 with no hard-skip). Tier 2 captures the cases the filter intentionally won't auto-draft but shouldn't silently bury — e.g. the demo-form noreply lead from the b30 medicus QA. Hard-skipped messages (newsletters, automation domains, repo-tag CI mail) are *not* persisted; that's noise.

**`app/agent/store.py`** (new DAL):
- ``upsert_pending(...)`` — INSERT OR IGNORE on message_id; returns row id or None on duplicate.
- ``list_pending(account=None, status='pending', tier=None, limit=100)`` — newest+highest-score first; rehydrates JSON columns to Python lists.
- ``get(row_id)``, ``mark_amended(row_id, amended_draft)``, ``mark_sent(row_id)``, ``mark_dismissed(row_id)``.

**`app/agent/triage.py`** updated: orchestrator now writes both tiers to the table (controlled by ``persist=True`` default). ``TriageResult`` gains ``surfaced`` and ``persisted`` counts.

**CLI ``youos triage``** gains ``--dry-run`` (print only, no DB writes — useful for filter-tuning). Without the flag, drafts are persisted and the operator visits ``/triage`` (β.2) to act. CLI also prints the new "surface for review" tier separately from hard-skipped noise.

### Tests
- ``tests/test_agent_store.py`` (6 new) — upsert idempotency, list ordering + JSON rehydration, tier filter, state transitions (amend/send/dismiss), pending-only filter.
- ``tests/test_agent_triage.py`` (2 new) — persistence (rows land, second run is no-op), `--dry-run` skips persistence.
- 25/25 across agent suites; 1172/1176 full sweep (same 4 pre-existing MLX-integration failures, unrelated).

### Next (β.2)
- ``app/api/agent_routes.py`` — REST surfaces for the UI.
- ``templates/triage.html`` — inbound + draft side-by-side, two-tier surfacing, [Edit] [Copy to Gmail] [Dismiss] [Mark sent].
- Nav link wired into existing pages.

## v0.2.0-beta.30 — 2026-05-28

### Agent triage — further filter tuning from the 14-day medicus sample
The 14-day window on `baher@medicus.ai` surfaced 5 false-positives, all transactional notifications that produced hallucinated drafts (wrong names, wrong topics — random Baher-corpus context plugged in). Three root causes, one fix PR each:

1. **Prior-history boost poisoned for transactional senders.** `youos ingest` had captured Wise / Workspace / Calendar notifications into `reply_pairs`, so `count_for(noreply@wise.com)` returned 6 and `+0.20` lifted pure automation past threshold. **Fix**: suppress the history boost when `NOREPLY_LOCAL_PAT` or `NON_HUMAN_MAILBOX_PAT` already fired — those prior pairs are corpus noise, not real correspondence. Reason still recorded (`"prior history (6) — suppressed (sender is automation)"`) so an operator can see history existed.
2. **Operational-mailbox regex was anchored at `^`.** Google's `workspace-noreply@` / `calendar-notification@` start with `workspace` / `calendar`, so the prefix-anchored regex missed them. **Fix**: match the operational keyword *anywhere* in the local part — `(?:^|[\w-])(?:notifications?|notify|alerts?|automated|billing|support|help|info|hello|admin|team|service|webmaster|postmaster|abuse)(?:[\w-]*)@`. Caught `calendar-notification@` correctly. `workspace-noreply@` lands on `NOREPLY_LOCAL_PAT` instead (the `\bnoreply\b` word boundary catches it after the hyphen) — `noreply` variants were intentionally removed from the operational pattern to avoid double-charging the same case.
3. **Meeting-bot domains missing.** `fred@fireflies.ai` slipped past — Fireflies is a meeting-recording service. **Fix**: added `fireflies.ai`, `otter.ai`, `loom.com`, `calendly.com`, `doodle.com`, `fathom.video`, `krisp.ai`, `grain.com` to `AUTOMATION_DOMAIN_PAT`.

### Tests
Four new regressions in `test_agent_needs_reply` pinning each behavior (workspace-noreply penalty, calendar-notification operational match, fireflies hard-skip, history-suppression-for-transactional). 17/17 in agent suites.

### Expected effect on the 14-day medicus sample
All 5 false-positives from b29 should now skip:
- Payment failure (`workspace-noreply@google.com`): noreply penalty + history suppressed → below threshold ✓
- Fireflies recording (`fred@fireflies.ai`): automation-domain hard skip ✓
- Wise money received (`noreply@wise.com`): noreply + history suppressed → below threshold ✓
- Calendar "no events today" (`calendar-notification@google.com`): operational-mailbox match → below threshold ✓
- Workspace transition announcement (`workspace-noreply@google.com`): same as #1 ✓

Net expected on 20-message sample: 0 drafts. Same as b29 but for the *right* reasons. The honest answer for a corpus where the inbound shape is "automation + newsletters" — actual human conversation is what β's "surface for review" tier is going to need to make visible without auto-drafting.

## v0.2.0-beta.29 — 2026-05-28

### Agent triage — filter tuning from real-inbox feedback
Running ``youos triage`` against the live BaherOS inboxes (medicus.ai + drbaher@gmail.com, 3-day window, 10 messages) surfaced filter quality issues in both directions:

- **Too strict**: `noreply@` was a hard skip, so a genuine demo-form lead from `noreply@medicus.ai` (which is *transactional*, not marketing) got dropped.
- **Too loose**: GitHub `notifications@github.com` mails, Supabase `billing-support@supabase.com` notifications, and CI subjects like `[DrBaher/youos] PR run failed` passed and got bad drafts.

### Changes to `app/agent/needs_reply.py`

**Hard skips tightened** (sender CANNOT be replied-to personally):
- Split `NOREPLY_PAT` → `MAILER_DAEMON_PAT` (bounces / mailer-daemon — never repliable, kept as hard skip).
- `AUTOMATION_DOMAIN_PAT` widened: now matches `@github.com`, `@gitlab.com`, `@bitbucket.org`, `@*.atlassian.net`, `@*.circleci.com`, `@*.travis-ci.{com,org}` on top of the existing `notifications.*` / `*.bounces.*` / `amazonses` / `mailgun` / `sendgrid` / `mailchimp`.
- New `SERVICE_SUBJECT_PAT`: hard-skips subjects starting with `[Org/Repo]` (GitHub/GitLab convention) or matching `(Build|Run|Pipeline|CI|PR) (failed|succeeded|completed|cancelled|started)`.

**Soft penalties added** (transactional content can still surface):
- `noreply@` / `donotreply@` → `−0.20`. Pure marketing `noreply@` is still hard-skipped by the existing List-Unsubscribe rule; this lets a transactional lead-form `noreply@` with strong positive signals (question, imperative, short body) cross the threshold.
- Non-human mailbox prefixes (`billing|support|help|info|hello|alerts|notifications|admin|team|service|webmaster|postmaster|abuse`, including hyphenated variants like `billing-support@`) → `−0.20`. Same logic: usually automation, but a human-tended `support@vendor.com` real conversation can still cross with strong signals.

### Expected effect on the 10 real-inbox samples
- Medicus demo lead (`noreply@medicus.ai`, transactional): score lands just under threshold (no question/imperative) — **acceptable skip**, will be caught by β's "surface for review" tier later. Better than silently false-positiving.
- 4 medicus newsletters: still List-Unsubscribe-skipped ✓
- Supabase `billing-support@`: penalty + low signals → correctly skipped ✓
- 2× GitHub PR/CI emails (`notifications@github.com` + `[DrBaher/youos]` subject): hard-skipped both by domain *and* subject ✓
- 2 gmail newsletters: List-Unsubscribe ✓

Net on a 10-message sample: 0 drafts on a span dominated by automation — the honest answer.

### Tests
Five new in `test_agent_needs_reply` pinning the refined behavior (mailer-daemon hard skip, noreply soft penalty, github.com automation domain, repo-tagged service subject, operational-mailbox penalty). 13/13 in the agent suites.

## v0.2.0-beta.28 — 2026-05-28

### Agent triage — Phase 1 (α): fetch + filter + dry-run CLI
First slice of the autonomous email-assistant loop. **No persistence, no Gmail writes, no auto-send** — Phase 1 is "show me what the agent would do" against the real inbox. Persistence (β), background scheduling (γ), and Gmail-drafts OAuth (Phase 2) follow.

**New `app/agent/` module**:
- **`inbox_fetch.fetch_unread(account, window, limit, backend=None)`** — pulls unread threads via the configured Google backend (`gog`/`gws`/`native`) using the existing adapter; returns the latest message per thread as a normalised `InboxMessage` (sender, subject, body, headers, parsed `sender_email`). The only difference from `youos ingest` is the query: `in:inbox is:unread newer_than:<window>` instead of `in:sent`.
- **`needs_reply.classify(msg, history, threshold)`** — combines hard rules (skip `List-Unsubscribe`, `noreply@`, automation domains, empty body) + lightweight scoring (base 0.5, +0.20 ending question, +0.10 imperative verb, +0.10 short body, +0.20 prior history with this exact sender, +0.10 short-body bonus, −0.20 very long digest, −0.15 cold-outreach flag). Returns a `NeedsReplyVerdict(needs_reply, score, reasons, cold_outreach)`.
- **`needs_reply.SenderHistory`** — cached count of prior reply pairs per inbound author, queried from the active instance's `reply_pairs` table. The b26 sender-history boost re-applied as a needs-reply signal.
- **`triage.run_triage(...)`** — orchestrator. Fetches, classifies, drafts the survivors via the same `generate_draft` path `/feedback` uses (so all our repair/persona/retrieval work flows through). Per-message draft failures are recorded with `error=` rather than killing the sweep.

**New CLI command `youos triage`** — `--account` / `--window` (default `3d`) / `--limit` (default 8) / `--threshold` (default 0.6) / `--backend`. Prints `[score]  flag  subject / from / reasons / model / draft` for each kept inbound, then a `skipped` list with reasons. Always dry-run in Phase 1; persistence + actions come in β.

The cold-outreach detector (b27), sender-email boost (b26), and post-generation repairs (b21–b25) all flow through unchanged — the agent loop *reuses* that work, doesn't duplicate it.

### Tests
- `test_agent_needs_reply` — hard-skip rules (list-unsubscribe, noreply, automation domains, empty body), question + imperative scoring, long-digest drop, cold-outreach flagging, sender-history boost.
- `test_agent_triage` — end-to-end with a mocked Google source: drafts the real inbound, skips the newsletter, records per-message generation errors without crashing the sweep.
- 10/10 in the agent suites; 1157 in the full suite (same 4 pre-existing MLX-integration failures, unrelated).

### Next in the autonomous-agent series
- **β** — `agent_pending_drafts` table + `/triage` web page + persistence.
- **γ** — Background scheduler in the running server + macOS notifications.
- **δ** — Standing-instructions surface threaded into the prompt via `extra_constraint` (already in `assemble_prompt`).
- **ε** — Audit log + observability on the `/triage` page.
- **ζ** — Safety polish: per-sender opt-out, daily cap, strict-local mode.
- **Phase 2** — `gmail.compose` OAuth and real Gmail-Drafts integration.

## v0.2.0-beta.27 — 2026-05-28

### Cold-outreach detection + polite-decline prompt nudge
QA fix #3/3: the LoRA politely accepts pushy outbound sales emails because Baher's training data doesn't include many polite-decline replies (he mostly ignores cold sales). This catches the *inbound* shape so generation nudges the prompt toward declining.

- **`app/core/cold_outreach.py`** — `detect_cold_outbound(subject, body, sender_email)` returns a `ColdOutboundVerdict(is_cold, score, hits)`. Weighted heuristic: subject patterns ("Boost / 10x / 30-min call"), body patterns ("I work with [type] founders" — weighted 2×, "saw your", "can I steal X min", "10x", "portfolio founders"), domain patterns (`@*market*`, `@*growth*`, `@*outreach*`). Threshold = 3 signals.
- **`DECLINE_NUDGE`** — phrased as soft guidance, not a hard rule ("Reply briefly and either politely decline or ask a clarifying question"). The 1.5B LoRA doesn't reliably follow rigid instructions.
- **`assemble_prompt`** gained `extra_constraint` — appended to the persona-constraints block. `generate_draft` populates it with `DECLINE_NUDGE` when the verdict is cold.

### Live evidence (Jess QA case, BaherOS)
- Detector: `is_cold=True, score=9`, 8 hits (2 subject + 5 body + 1 domain). The exact case that motivated this.
- Draft tone shifted from `"I'm happy to schedule a call next week. I'm also happy to share…"` (b26) to `"I'm not sure if I can make it. I'm also on a tight schedule this week…"` (b27). The decline framing landed.

### Honest limit
The LoRA loops on the same phrase 4× under the new constraint ("not sure if I can make it" / "tight schedule" repeated). The cold-outreach part works; the LoRA's tendency to repeat under longer prompts is a separate model-quality issue not addressed here. Real-life this surfaces as a draft for review — exactly what the agent's "draft only, never auto-send" design assumes.

### Tests
Six in `test_cold_outreach`: the Jess case (positive), Alex pricing inquiry (true negative), Sam friend message (true negative), internal teammate quick-chat (true negative — guards against false positive on "quick chat" subjects from `@medicus.ai` peers), high-confidence body-pattern double-weighting, and the `DECLINE_NUDGE` constant. 50/50 across the affected suites.

### QA series complete
This closes the three "still not great" items from the BaherOS review:
- #1 (b25) — strip trailing user-name from exemplars + output.
- #2 (b26) — sender-history boost (exact email > domain).
- #3 (b27) — cold-outreach detection + decline nudge.

The deeper content-semantics issues (the LoRA's small size and Baher's relatively pleasant-and-cooperative corpus) need a retrain on hard cases. Out of scope for this series; a known follow-up.

## v0.2.0-beta.26 — 2026-05-28

### Retrieval: sender-history boost (exact email > domain)
QA fix #2/3: same-domain boosting (`@medicus.ai → @medicus.ai`) over-fires for users with a large in-org corpus — recurring-meeting/check-in pairs get amplified over topic matches. Exact-email match is a much sharper signal: "I've corresponded with *this exact person* before" outranks "this is from someone at the same company."

- New `extract_email()` helper in `app/core/sender.py` — pulls the `local@domain` out of an `"Name <email>"` author string, lowercased.
- New `RetrievalRequest.sender_email_hint` + `RetrievalConfig.sender_email_boost: 0.40` (4× the same-domain boost).
- `_metadata_score` adds `sender_email_boost` when the pair's `inbound_author` exact-matches the hint.
- `generate_draft` populates `sender_email_hint = extract_email(request.sender)` and threads it into the `RetrievalRequest`.
- Live evidence: queries with `sender_email_hint="vanessa@medicus.ai"` (a real recurring correspondent with 259 pairs in BaherOS) surface Vanessa's pairs at `meta=0.40` — the boost fires as designed. Modest leverage vs lexical (~12), but the foundation is in place; weight can be cranked up if the same-email signal needs to outrank topic matches more aggressively.

The same-type / same-domain boosts (`0.15` / `0.10`) stay at the conservative weights — the experiment at `0.20/0.20` regressed the Alex/Stripe case (b24's note).

## v0.2.0-beta.25 — 2026-05-28

### Strip trailing-name artifact from exemplars **and** drafts
QA-driven content-quality fix #1: BaherOS drafts of short technical questions were returning **only the signature block** (`"Baher Al Hakim CEO / Medicus AI w: medicus.ai e: …"`) with no actual answer. Diagnosis confirmed the LoRA was emitting only the signature half because every exemplar in its training + every exemplar in the prompt ended with `… Baher Al Hakim`. `strip_signature` removes the contact-detail block (`CEO / Medicus AI w: …`) but leaves the trailing user-name intact — it's not at line start, so the line-anchored patterns miss it.

**New helpers in `app/generation/service.py`**:
- `_strip_trailing_user_name(text)` — strips the user's name (and any trailing surname tokens like `"Al Hakim"`) when it sits at end-of-text after a sentence-ending punctuation. Lookbehind on `[.,!?]` + `[^.!?]*$` tail means mid-sentence uses ("Baher mentioned the team…") are left alone.
- `strip_exemplar_signature(text)` — `strip_signature` + `_strip_trailing_user_name`, used in both exemplar-formatting sites (`_format_exemplars`, the prompt-builder).
- `_repair_draft` now runs the same two passes on **output**, not just inputs.

### Visible impact on the QA cases
| Case | Before b25 | After b25 |
|---|---|---|
| Friend / Sam | `…Let me know if you want to go. **Baher Al Hakim**` | `…Let me know if you want to go.` |
| Vendor / Jess | `…Let me know what works best for you. **Baher Al Hakim**` | `…Let me know what works best for you.` |
| Work / Alex (pricing) | `…I'll share the monthly volume with you and we can discuss the pricing.` | `…We are currently using 1000 users and we plan to grow to 2000 users in the next 12 months.` (the model now gives concrete numbers — the cleaner exemplars left it more room to lean on retrieved content) |
| Edge / short DB-backup | Local model emits signature-only → Claude fallback (good answer) | Same — the LoRA learned the pattern during training; runtime fixes can't undo that, only retraining can. Falling back to Claude on signature-only is the correct behavior |

### Tests
Four new regressions in `test_draft_repair`: strip-after-punctuation, leave-mid-sentence, combined-strip-helper for the run-on case, and the `_repair_draft` integration. 21/21 in the suite.

### Still to do in this QA series
- **#2 sender-history retrieval boost** — same-exact-email > same-domain, to push down recurring-meeting noise.
- **#3 cold-outbound heuristic + decline nudge** — to stop the LoRA from politely accepting pushy outreach.

## v0.2.0-beta.24 — 2026-05-28

### Retrieval tuning: semantic gets equal voice + wider candidate pool
Two small constants in `app/retrieval/service.py` that the QA inspection identified as the second-order fix after the topic-keyword filter:
- **`semantic_weight: 0.4 → 0.5`** — semantic now blends equally with BM25 in the combined score, so a long inbound's high-frequency template terms can't outrank topic semantics by lexical weight alone.
- **BM25 candidate pool `3× → 5×`** the requested top_k for both `reply_pairs_fts` and `chunks_fts`. The semantic re-ranker now picks from a wider lexical short-list, so topic-relevant pairs that barely outscore intro/template emails on BM25 have a chance to surface.

### Sender-domain boost: experimented, reverted to conservative weights
Bumped `sender_type_boost` and `sender_domain_boost` from `0.15/0.10` to `0.20/0.20`, ran retrieval against the Alex/Stripe inbound (with `sender_type_hint`/`sender_domain_hint` populated as `generate_draft` does), and it **regressed**: same-domain boosting amplified Baher's own-account `@medicus.ai` pairs (recurring meetings/check-ins) over the topic-relevant pricing exemplars. Reverted; left a comment explaining why the boost is the wrong lever for *topic* mismatch (intros vs pricing) — that's what semantic+candidate-pool fix. Boosts remain for the rarer "have-I-talked-to-X" axis.

`sender_type_hint` / `sender_domain_hint` are already wired into `generate_draft` (lines ~1592, 1619); D from the QA plan was already in place.

1136 across the test suite pass (same 4 pre-existing MLX-integration failures, unrelated).

## v0.2.0-beta.23 — 2026-05-28

### Retrieval: topic-keyword filter + user-name stripping
The QA inspection found the misread-direction bug class came from retrieval, not the LoRA: a 200-word inbound about Q3 pricing had its BM25 query drowned by "Hi", "Thanks", "happy to", "could you", and the user's own name "Baher" appearing in every line. The top-5 precedents came back as 4 *intro emails* and zero pricing exemplars, so the LoRA had nothing relevant to draft from.

**Fixes**:
- `app/core/query_expansion.py` gains `extract_topic_keywords()` — for inbounds ≥ 25 words, strips English stopwords + email-template idioms (greeting/closing/"happy to"/"looking forward"/"let me know"/etc.) so BM25 ranks against the words that carry the topic. Defensive: a filler-only inbound falls back to the original text, never empty.
- New `extra_stopwords=` lets callers drop additional terms — the retriever now passes the user's own name tokens from `get_user_names()`. Every inbound has "Hi Baher"; that's pure BM25 noise pulling intros to the top.
- The original `query` is preserved for the semantic re-ranker — only the FTS path is shaped.

### Live impact (BaherOS, Alex/Stripe pricing query)
Top-5 precedents before A:
1. RE: Intro Baher <> Maxime (Otium Venture) — intro
2. Tuesday Meeting — reschedule
3. Re: Intro Baher / Johannes — intro
4. Intro Jeremias / Baher — intro
5. Re: Intro Fabian / Baher — intro

After A (with user-name strip):
1. "15 mins" — meeting reschedule
2. **"Follow-up on HomeWell Demo"** — *"pricing tiers based on volume"*
3. **"RE: Medicus Smart Wellbeing"** — *"Our pricing model is based on monthly active users… volume"*
4. "RE: Medicus AI / ARCHIMED"
5. **"Re: fatsecret Developer Contact"** — *"pricing tiers"*

**Three of the top five are now pricing-relevant** (was 0). The synthetic-inbound drafts didn't dramatically change because the persona refresh (beta.22) already fixed the visible Alex/Jess content, but the structural improvement helps more in cases where the LoRA genuinely leans on precedents.

### Tests
Four new in `test_query_expansion`: short-query passthrough, stopword stripping on long inbound, defensive empty-fallback, and the `extra_stopwords` user-name strip. 14/14 in the suite.

## v0.2.0-beta.22 — 2026-05-28

### Persona pipeline: instance-aware paths + category translation
Two bugs in the persona-analysis pipeline that together made `analyze_persona` useless for instance setups (BaherOS, etc.):

- **`analyze_persona.py` ignored `YOUOS_DATA_DIR`** — it hardcoded `ROOT_DIR / "configs"` for both the analysis JSON and the merge target. So `YOUOS_DATA_DIR=~/YouOS-Instances/baheros python scripts/analyze_persona.py` analyzed the instance's corpus correctly, then wrote findings to the **repo**'s `configs/`, leaving the instance's persona.yaml stale. Now resolves both paths from `get_settings().configs_dir`.
- **`merge_persona_analysis` copied category labels into persona.yaml verbatim.** The analyzer emits *labels* like `"Hi X"` / `"Direct start"` / `"Statement"` (high-level patterns), not renderable phrases. The merge then wrote `closing_patterns.default: "Statement"` and the generator emitted the literal word "Statement" as the closing. New `_translate_category()` maps each known label to its renderable form:
  - Greetings: `Hi X → "Hi {name},"`, `Hey X → "Hey {name},"`, `Hello X → "Hello {name},"`, `Dear X → "Dear {name},"`, `Direct start / Direct answer / Thanks opener → ""`
  - Closings: `Statement / Question / Let me know → ""`, `Thanks → "Thanks,"`
  Unknown labels are *skipped* (rather than copied) — better to leave a field unchanged than to corrupt it.

### Live impact on BaherOS
Applied the merge against the live instance after the fix landed. The corpus (11,758 reply pairs) is **70% no-signoff** ("Statement" dominant) — so the default closing collapsed from `"Best,"` to `""`. Re-running the four QA synthetic inbounds: the Alex/Stripe pricing inquiry now correctly says **"I'm happy with the current pricing and we're not planning to move to the enterprise tier"** (vs the previous misread offering Stripe pricing back at Alex). The persona refresh changed the LoRA's prompt enough to flip the *content*, not just the artifacts — biggest single quality lift in the QA series so far.

### Tests
Five new regressions in `test_persona_analysis_merge`: Statement→empty closing, Direct start→empty greeting, "Hi X"→"Hi {name},", unknown-label-skipped, and the `_translate_category` helper itself. Updated `test_merge_updates_greeting_pattern` to assert the translated phrase, not the buggy literal "Hey X". 14/14 in the suite.

## v0.2.0-beta.21 — 2026-05-28

### Draft-quality repairs: kill the three LoRA artifacts that leaked into output
Running the agent-triage prototype against the live BaherOS instance with synthetic inbounds surfaced three model-output artifacts the existing `_repair_draft` pass *wasn't* catching. These fix them.

- **Run-on inline signature.** The LoRA emits `"Cheers, Baher Al Hakim CEO / Medicus AI w: medicus.ai e: baher@…"` *on a single line*, but the existing signature patterns were line-anchored (`^Cheers,$` with `MULTILINE`) and missed the inline form. Added three inline patterns to `_build_signature_patterns`: role+separator+capital (`CEO / Medicus`), single-letter contact marker followed by URL/email/phone (`w: medicus.ai`), and "Sent from my <device>" inline. Specific enough to avoid eating legitimate prose.
- **Quote-tail hallucination** (`"On 23. Jul 2025 at 10:17 +0200, X <a@b> wrote:"`). New `strip_quote_tail()` truncates from the match start; bounded to {0,160} so it can't over-eat across paragraphs. Wired in **before** the signature pass so the signature regex sees a smaller, cleaner substring.
- **HTML entities** (`&#39;` → `'`, `&amp;` → `&`). New `decode_html_entities()` runs `html.unescape` on the output. Pure decode, no semantic change.

### Defaults flipped: the three artifact-removal repairs are now **on**
Previously all repair flags defaulted False ("behavior-preserving"). The three new artifact-removal repairs (`strip_trailing_signature`, `strip_quote_tail`, `decode_html_entities`) are objectively-correct cleanups — the model emits training-data leakage that the user never wants. They now default **True**. `enforce_greeting_closing` stays opt-in (it *adds* content the model didn't produce; that's a different category). Each fired repair is recorded in `DraftResponse.repairs` so the operator can audit what's been touched.

### Live regression evidence (BaherOS, synthetic inbounds)
Same four cases the QA review caught artifacts on, drafted again with the new repairs on:
- Friend draft: `…Thanks, Baher. On 23. Jul 2025 at 10:17 +0200, … wrote: Hey, I can do that…` → **`…Thanks, Baher.`** (`stripped_quote_tail`)
- Vendor draft: `…I&#39;d love to share… Cheers, Baher Al Hakim CEO / Medicus AI w: medicus.ai e: baher@` → **`…I'd love to share… Cheers, Baher Al Hakim`** (`stripped_trailing_signature`, `decoded_html_entities`)
- Edge case (no sender name): `…Thanks, Baher Al Hakim CEO / Medicus AI w:` → **`…Thanks, Baher Al Hakim`** (`stripped_trailing_signature`); the beta.20 `Hi,` greeting fix stays clean.
- Work draft (already clean): unchanged, `repairs: []`.

Five new regression tests pin each fix (and one end-to-end "all three artifacts in one draft → clean" check).

### Out of scope for this PR
Shallow content semantics from the 1.5B LoRA (e.g., misreading who's offering what to whom, accepting pushy outbounds, occasional self-contradiction) are inherent to the model+corpus and not addressable in a post-process pass. They're a model-quality concern for the autonomous-agent loop, but they're orthogonal to artifact cleanup.

## v0.2.0-beta.20 — 2026-05-28

### QA-review fixes (BaherOS live testing)
A reviewer hit the live BaherOS instance with synthetic inbound and flagged four real issues; these fix them and back the fixes with regression tests.

- **`Hi ,` greeting bug.** When sender-name extraction returned an empty first name, `_resolve_greeting` rendered `"Hi {name},".replace("{name}", "")` → `"Hi ,"` (dangling space before the comma). Fix collapses the leading-space form of the placeholder first, so the result is `"Hi,"` instead. New regression test asserts `" ,"` is impossible for empty/None names across all sender types.
- **Per-draft model badge: always renders.** The badge already existed but was conditional on `data.model_used` — if an older code path forgot to populate it, the badge silently disappeared and the public "always shows which model wrote each draft" claim wasn't strictly true. Now falls back to a clearly-marked `model: unknown` (warn-styled), so the badge is always present.
- **Doctor's `mlx_lm` message** now distinguishes "Python package not importable in this venv" from "global `mlx_lm` binary on PATH". Same failure, but the message no longer reads as contradicting a visible `which mlx_lm`.
- **Landing wording softened** under the comparison card: "Everything stays on your Mac **by default** — cloud fallback is opt-in; set `model.fallback: none` for strict local-only". Honest about the cold-start/fallback path without diluting the headline contrast.

### Items the reviewer flagged that turned out to be already in place
- `scripts/install.sh` exists.
- `compare-models` CLI is wired (`app/cli.py:557` → `scripts/compare_models.py`).
- `ingestion.google_backend` with `gog` / `gws` / `native` is implemented (`app/ingestion/adapters.py:SUPPORTED_BACKENDS`, `app/core/config.py:get_ingestion_google_backend`, surfaced by `youos doctor`).
- `youos corpus --json` doesn't crash with `ModuleNotFoundError: No module named 'scripts'` on `main` — `scripts/__init__.py` resolves the import; the reviewer likely hit an older install.
- `analyze_persona.py` does learn closings from sent emails (`Best,` / `Cheers,` regexes + `closing_patterns` aggregation). Baher's `Best,` closing comes from the formal/default sender-type classification on the test inbounds, not from a missing learning path — re-running `youos persona analyze` against the current corpus will refresh it.

## v0.2.0-beta.19 — 2026-05-28

### Review-driven hardening (OpenClaw review)
- **`/readyz` added; `/healthz` now returns the version.** `/readyz` reports DB resolvability for launchd/health probes. New test pins `/healthz`, `/readyz`, and `/api/config` to the same `get_version()` value — version drift across runtime surfaces is the recurring bug the canonical-version refactor was meant to kill, and now it has a regression test.
- **SKILL.md gains a Safety & impact section near the top** (sensitive Gmail/Docs/WhatsApp ingestion, install runs local code, opt-in launchd/nightly, optional cloud fallback with strict-local instructions). Also a **Naming** line spelling out that `<First>OS` is the user's local instance at `YOUOS_DATA_DIR=~/YouOS-Instances/<you>/`, **not a fork**.
- **`clawhub.json` metadata hygiene**: removed `screenshots` and `demo` fields — they referenced files not in the text-only bundle; the homepage repo already resolves them. New test pins this.
- **Bundle now has a tested launchd installer guarantee.** The plist is built programmatically by `app/core/service.py:build_plist()` (no `deploy/` directory dependency), and a new test runs `prepare_clawhub_release.sh` and asserts the bundle contains `app/core/service.py` with `build_plist`, `launchctl`, `RunAtLoad`, and `KeepAlive`.
- **Two safety regressions tests**: missing DB auto-bootstraps cleanly with the required tables, and an unsafe DB path (Trash) fails fast with a clear `RuntimeError`.

Status of the reviewer's other items: `var/` was already in `.gitignore` (line 26); the canonical version refactor already wired `app.core.version.get_version()` through `settings.py`, `/api/config`, and all UI footers (the cited `0.1.11` / `0.1.10` drift was an earlier snapshot); no `scripts/run_youos.sh` / `scripts/install_youos_launchd.sh` exist because the install path is `youos service install` → `app/core/service.py`, which generates the plist in Python and points `ProgramArguments` directly at `uvicorn` — no shell launcher needed.

## v0.2.0-beta.18 — 2026-05-27

### Light mode for the Gmail extension + bookmarklet (and a regression fix)
Final surface in the light-mode series. The extension's in-Gmail panel (Shadow DOM, injected by `content.js`) and the options page were dark-only; both now follow the OS via `prefers-color-scheme`, with a ☀/☾ toggle in the panel header (persisted in `localStorage`) — same model as the rest of the app. Verified both themes by mounting the real `STYLE`+`MARKUP` in a Shadow-DOM harness. Extension manifests bumped to 0.1.2.

**Regression fix:** beta.17 tokenized all 8 templates, but `login.html` and `draft_popup.html` (the bookmarklet popup) don't link `youos.css`, so their new `var()` tokens were undefined — leaving both pages unstyled. Added the `youos.css` link (and `youos.js` to draft_popup for the toggle); verified the popup renders correctly in both themes.

That completes light mode across all four surfaces: landing (beta.16-era), backend UI (beta.17), and now the extension + bookmarklet.

## v0.2.0-beta.17 — 2026-05-27

### Light mode for the backend UI (system default + persisted toggle)
The app was dark-only. Added a full light theme across all 8 templates, following the same mechanism as the landing: `static/youos.css` now ships a light palette via `@media (prefers-color-scheme: light)` (follows the OS) plus `:root[data-theme="light"|"dark"]` overrides set by a no-flash `<head>` script from `localStorage`, and `static/youos.js` injects a floating ☀/☾ toggle that persists the choice.
- The templates carried ~500 hardcoded hex colors and used **zero** CSS variables. Tokenized them to the existing `youos.css` tokens — both inside `<style>` blocks and inline `style="…"` attributes (including the first-run tour modal, which is built from inline styles) — while deliberately **not** touching color strings inside `<script>` (JS keeps literal colors). Dark mode is preserved (tokens default to the original values).
- `login.html` now loads `youos.js` so it gets the toggle too.
- Verified both themes in a running server via browser render: Draft/Feedback (+ tour modal), Stats, Settings, About, and the Welcome wizard.

Next in the series: the browser extension + bookmarklet.

## v0.2.0-beta.16 — 2026-05-27

### A real logo for YouOS (envelope-flap "Y")
YouOS had no logo — the landing header was a bare ✉️ emoji and the extension icons were placeholders. Designed a proper mark: **an envelope whose flap forms the "Y" of YouOS**, in the brand teal (`#00c4a7`) on dark navy. It fuses the email meaning with the "this becomes *your* OS" idea, and reads cleanly from a 16px favicon up to the hero.
- **SVG source of truth**: `assets/youos-mark.svg` (the mark) + `assets/youos-logo.svg` (horizontal lockup with the wordmark — teal "You" + light "OS").
- **Landing** (`site/index.html`): added a brand lockup at the top of the hero, plus SVG + PNG favicons (`site/youos-mark.svg`, `site/favicon-32.png`).
- **App**: favicon wired into all 8 templates via `/static/youos-mark.svg` (served from the existing `/static` mount).
- **Browser extension**: regenerated `extension/icons/icon{16,48,128}.png` (and the Firefox-build copies) from the mark — real icon instead of the placeholder.
- **README**: replaced the `# YouOS ✉️` emoji heading with the rendered mark.

Each candidate was rendered in a browser and screenshot-verified at favicon through hero sizes before shipping; the chosen "Y" reads as a letterform (narrow flap + extended stem) rather than a plain envelope chevron.

## v0.2.0-beta.15 — 2026-05-26

### Documentation revision (docs/ was stale; README config gaps; cruft removed)
The `docs/` guides predated the standalone decoupling **and** the recent local-by-default work, so they were misleading:
- **Wrong port everywhere.** Docs used `8765`; the server defaults to **`8901`** (`config.py`). Fixed in `docs/USAGE.md`, `docs/OPERATIONS.md`, `docs/demo-script.md`, and the README's "Running a Personal Instance" example.
- **`docs/USAGE.md`** rewritten: first-run is `./scripts/install.sh` (not `pip install -e .`); added `youos doctor`, the extension install path, the readiness gate, `compare-models`, and a full command table (`serve`, `service`, `model server`, `config`, `corpus`, …).
- **`docs/ARCHITECTURE.md`** rewritten: generation now drafts on the **fine-tuned local model by default** (was "local Qwen or Claude CLI fallback"); ingestion documents the `gog`/`gws`/`native` backends; added the warm `mlx_lm.server`, voice-match evaluation, and the readiness gate.
- **`docs/OPERATIONS.md`**: corrected port; added `ingestion.google_backend`, `review.draft_model`, and `model.server` config keys + `youos config`/`youos service`.
- **README Configuration**: documented `review.draft_model` (auto/local/claude), the warm `model.server`, and `ingestion.google_backend`; added a troubleshooting pointer (`youos doctor` + in-UI "How to fix"); stale beta-version label → "latest release".
- **`PUBLISHING.md`**: corrected `clawhub.com` → `clawhub.ai`; documented that the dashboard is the working upload path (the `clawhub publish` CLI times out ~49s server-side).
- **Removed cruft**: `CHANGELOG_FOR_CLAWHUB_0.1.14.md` and `CHANGELOG_SINCE_YESTERDAY.md` — one-off working notes from the v0.1.x era, superseded by this canonical changelog.

`SKILL.md` and `clawhub.json` were already current (updated in beta.12/beta.14) and unchanged here.

## v0.2.0-beta.14 — 2026-05-26

### ClawHub pack is text-only again (fixes upload rejection)
ClawHub rejects non-text files in a skill bundle, so the b13 pack — which had added `screenshots/` and `extension/` (the latter ships PNG icons) — was rejected on upload ("Remove non-text files: …png/.jpg"). Reverted the allowlist to **text-only** (the original set: `app/`, `clawhub.json`, `configs/`, `PRIVACY.md`, `pyproject.toml`, `README.md`, `scripts/`, `SKILL.md`): the registry resolves `clawhub.json`'s screenshot paths from the homepage repo, and the browser extension is installed from the repo's `extension/` folder (SKILL.md updated to say so). Added a **binary guard** to `prepare_clawhub_release.sh` that aborts if any non-text file slips into the bundle. The bundle is now 1.0M / 0 binaries (zip 278K).

## v0.2.0-beta.13 — 2026-05-26

### ClawHub release pack now includes the extension + screenshots
`scripts/prepare_clawhub_release.sh` builds the minimal folder you upload with `clawhub publish`, but its allowlist was missing **`extension/`** (the SKILL.md now tells users to "Load unpacked" that folder — it has to be in the pack) and **`screenshots/`** (referenced by `clawhub.json`). Added both, and the script now strips the generated `extension/firefox-build/` and the dev-only `screenshots/CAPTURE.md` from the bundle. `PUBLISHING.md` updated to match. So `./scripts/prepare_clawhub_release.sh` → `clawhub publish ./` ships a complete, working skill.

## v0.2.0-beta.12 — 2026-05-26

### OpenClaw skill (clawhub.json + SKILL.md) brought up to date
The skill manifest and instructions had drifted from the standalone app:
- **Install now sets up the local model.** Both `clawhub.json` and `SKILL.md` install steps changed from `pip install -e .` (which never installed MLX) to **`./scripts/install.sh`** — so a skill install gets a working on-device model, then `youos setup`.
- **Local-by-default framing.** Descriptions / "How it works" updated from "falls back to Claude automatically" to drafting on your **fine-tuned local model by default** (served warm); the cloud is only a cold-start/fallback. Strict local-only now documented as `review.draft_model: local` + `model.fallback: none`.
- **Gmail = extension.** The "Gmail Bookmarklet" section is now "Drafting inside Gmail" (the browser extension, bookmarklet as fallback).
- **Fixed stale bits.** Wrong `cd ~/Projects/youos` path; `youos ui` → `youos serve`; golden "8 cases" → 10; ingestion backend is now gog **/ gws / native**, not gog-only.
- **New capabilities added** to the command list + How-it-works: `youos compare-models` (voice-match), `youos model server`, `youos service install`, the readiness gate, and `<your>OS` personalization. Refreshed the manifest description/tags.

## v0.2.0-beta.11 — 2026-05-26

### Public landing page: pipeline diagram, tech stack, and FAQ
Brought the in-app About content to the public site (`site/index.html`, youos.drbaher.com), styled to match the landing:
- **Pipeline diagram** added to "How it works" — the clean vertical flow (Corpus → Ingestion → Reply Pairs DB → Retrieval → Draft Generation → Draft Reply) plus the separated "Self-improving loop · nightly" strip.
- **"Under the hood" tech stack** — eight cards (local model + warm serving, fine-tuning, retrieval, storage, backend/backends, Gmail extension, evaluation/voice-match, optional cloud).
- **FAQ** — a seven-question accordion tuned for visitors (privacy, which model drafts, how it learns your style, how to verify it sounds like you, Gmail extension, Apple-Silicon requirement, free/open-source).

Verified the rendering via Peekaboo. Pages redeploys `site/` on merge.

## v0.2.0-beta.10 — 2026-05-26

### About page: refreshed tech stack + new FAQs
- **Tech stack** updated to the current architecture: Model now notes "+ your LoRA, served warm via `mlx_lm.server`; Claude only cold-start/fallback"; **Email access** fixed from "gog CLI" to the pluggable **gog / gws / native** backends; new cards for **Model serving** (warm server, `review.draft_model`, readiness gate), **Evaluation** (voice-match + `youos compare-models`), and **Gmail integration** (MV3 extension + bookmarklet); Draft transparency now lists the per-draft model badge + "Drafting with" row.
- **FAQ** (now 28): updated "is my LoRA helping?" to point at `youos compare-models` / voice-match + the "Drafting with" row, and added five questions for the recent work — which model writes my drafts (local-by-default), why the first draft warms up, why it asks me to wait (readiness gate), how to draft inside Gmail (extension), and what "&lt;your&gt;OS" / BaherOS means.

## v0.2.0-beta.9 — 2026-05-26

### Fix: Activity-card "How to fix" layout + auto-collapse
Two bugs in the v0.2.0-beta.8 troubleshooting on the Activity card: putting the `<details>` inside the flex value cell broke the row's `space-between` (so "Ingestion" and "✕ Failed…" crammed together), and the 5-second activity poll re-rendered the cell, collapsing an expanded tip on its own. Fixed: the failure text stays in the right-aligned value cell, the "How to fix" expander moved to its own full-width row, and it only re-renders when the error actually changes — so an expanded tip stays open across polls.

## v0.2.0-beta.8 — 2026-05-26

### Failures now link how to fix them
On the Stats dashboard, failure messages (the Activity card's ingestion failure and the Pipeline card's error list — e.g. "Gmail ingestion failed", "Autoresearch failed") now show an inline **"How to fix"** expander with an actionable tip, the relevant command, and a "More help →" link. A small failure→fix map covers the common cases (ingestion/backend, autoresearch, fine-tuning, MLX/embeddings) with a `youos doctor` fallback for anything unmapped — so a red error tells you what to do, not just that something broke.

## v0.2.0-beta.7 — 2026-05-26

### Readiness banner: a working "Refresh" + run the benchmark from the UI
The "preparing your voice model" banner's **Refresh** appeared to do nothing — in the *benchmark-pending* phase nothing is running to refresh toward, so it silently re-rendered the same state. Fixes:
- **Refresh now shows progress** — "Checking…" → "✓ Checked" (with a brief minimum so it's always visible), so it never feels dead.
- **New "Run benchmark now" button** (shown in the benchmark-pending phase) actually clears the gate: it triggers a golden eval on the current adapter via the new **`POST /api/benchmark`** (runs in the background; readiness then reports `benchmarking` and the banner auto-polls to `ready`). No more "wait for tonight / run it in the terminal."
- `/api/model/readiness` now reflects a running benchmark *or* fine-tune.

Tests (5) cover the benchmark endpoint (spawn / 409-when-running / 409-when-fine-tuning), readiness reporting `benchmarking` while it runs, and the banner wiring (Refresh progress + benchmark action).

## v0.2.0-beta.6 — 2026-05-26

### Backend-UI sweep — surface the recent work everywhere
Audited the web UI against everything shipped this cycle and closed the gaps:
- **Settings now exposes the drafting controls.** Two flags that governed core behavior weren't in the whitelist (so they were invisible in `/settings` and `youos config`): **`review.draft_model`** (auto / local / claude — which model drafts) and **`model.server.enabled`** (the warm local-model server). Both are now toggleable from Settings and the CLI.
- **Stats: per-model breakdown.** The "Draft Quality by Condition" card now includes a **By model** row (e.g. `qwen2.5-1.5b-lora: 32 · claude: 18`), surfacing the `draft_events.by_model` data alongside the existing "Drafting with" health row — so silent base/cloud drafting is visible in detail.
- **About page refreshed.** The Tools card now leads with the **Gmail extension** (bookmarklet as fallback), mentions the **per-draft model badge / "Drafting with"** row, and adds **`youos compare-models`** to the CLI list; the reply-instruction FAQ points at the extension panel.

(The `/feedback` readiness banner + per-draft badge + loading overlay, the redesigned About diagram, and the extension-first Gmail page were already shipped earlier this cycle.)

## v0.2.0-beta.5 — 2026-05-26

### Cleaner "How it works" diagram on the About page
The flow diagram's side-branches broke the main column's alignment and muddled the feedback loop into the downward flow. Redesigned as a **clean, aligned vertical pipeline** (Corpus → Ingestion → Reply Pairs DB → Retrieval → Draft Generation → Draft Reply, all boxes the same width, with the corpus + generation steps accented and Draft Reply highlighted) plus a visually-separated **"Self-improving loop · nightly"** strip (Your feedback → LoRA fine-tuning → Autoresearch) that clearly notes it feeds back into retrieval & generation. Also refreshed stale content: Ingestion now reads "gog / gws / native backend", and Draft Generation reads "your local Qwen + LoRA, served warm · Claude fallback".

## v0.2.0-beta.4 — 2026-05-26

### Promote the Gmail extension + fix its out-of-box port
The in-app **Gmail page** (`/bookmarklet`, nav relabeled "Bookmarklet" → "Gmail") now leads with the **browser extension** and walks through installing it inline — start the server, open `chrome://extensions`, enable Developer mode, **Load unpacked** (the page injects the exact `extension/` folder path to select), open Gmail. Covers Options (server URL / `youos token-create` for PIN-protected instances) and the Firefox build. The bookmarklet is demoted to a collapsible "no-install fallback."
- **Fixed the extension's default port**: it defaulted to `127.0.0.1:8765` while YouOS serves on `8901`, so it wouldn't connect out-of-box (you'd have to set the URL every install). Now defaults to `8901` across `background.js`, `options.js`, `options.html`, the README, and the regenerated `firefox-build/`. Extension bumped to 0.1.1.

Test pins the page promoting the extension with install steps + the injected folder path.

## v0.2.0-beta.3 — 2026-05-26

### About page corrected + a screenshot capture guide
- **`/about`**: fixed stale file paths (`~/Projects/youos/…` → the real `~/YouOS-Instances/<you>/…` instance paths); reframed the privacy table so **local (your trained Qwen+LoRA, served warm) is the default** drafting path and Claude is the cold-start; added a "becomes _your_ OS" line.
- **`screenshots/CAPTURE.md`**: a recipe for re-shooting the three landing assets (`demo.gif`, `01-draft-reply.png`, `02-stats.png`) so they show the current UI — the per-draft model badge, the "Drafting with" row, and the personalized wordmark. (The existing screenshots predate that UI; they need a manual re-capture on a Mac.)

## v0.2.0-beta.2 — 2026-05-26

### Docs/landing polished for the beta narrative
Brought the public-facing surfaces up to date with the latest work (the model comparison was already surfaced; this adds the rest):
- **README**: beta badge + "during setup it becomes _your_ OS (→ BaherOS)" in the intro; replaced the stale "empty output → Claude fallback" line with **drafts-on-your-local-model-by-default** (warm-served, on-device; Claude only cold-start/fallback) and a **no-silent-failures** bullet (model shown in stats/doctor/per-draft badge + the trained-and-benchmarked readiness gate); added `youos model server` to Usage.
- **Landing page**: hero badge now reads "Public beta", the tagline notes it "becomes _your_ OS", and two new problem/solution cards — "Becomes _your_ OS (BaherOS)" and "Drafts on your local model by default; see which model wrote each draft."

## v0.2.0-beta.1 — 2026-05-26

**First public beta.** A milestone tag over the 0.1.x line — highlights since the project became standalone:

- **Runs standalone** (no OpenClaw required) with a one-command `./scripts/install.sh`, plus **three Google ingestion backends** (`gog` / `gws` / `native`).
- **Local model out of the box** — `install.sh` sets up MLX on Apple Silicon; a fresh install yields a working on-device model.
- **Drafts in your voice, by default** — the local Qwen fine-tuned on your sent mail is now the default drafter on both the Draft Reply tab and the Review Queue, served by a **warm model server** (loaded once, fast), fully on-device. Claude is only the cold-start/fallback.
- **Proven, not assumed** — `youos compare-models` + the voice-match metric measured the fine-tuned local model beating Claude on *sounding like you* (0.80 vs 0.70 on the maintainer's corpus).
- **No silent failures** — the actual drafting model is surfaced in stats, `youos doctor`, and a per-draft badge; a readiness gate asks you to wait until your model is **trained _and_ benchmarked**.
- **Guided onboarding** — the `/welcome` wizard does identity → ingest → **auto-trains your LoRA** → secures → installs the background service, with plain-language explanations throughout.
- **It becomes _your_ OS** — setup personalizes the name to `<First>OS` (e.g. BaherOS).
- Landing page, settings UI, feature-flag CLI, and a launchd background service.

See the 0.1.x entries below for the full per-change history.

## v0.1.67 — 2026-05-26

### YouOS becomes *your* OS — personalized name at setup (BaherOS)
The idea behind YouOS is that it's *yours*. Setup now personalizes the product name from your name: **`set_identity` auto-derives `display_name` as `<First>OS`** (e.g. "Baher Al Hakim" → **BaherOS**, "jane" → "JaneOS", internal casing preserved → "McAvoyOS"). New `derive_os_name()` helper. The onboarding identity step shows a **live preview** ("YouOS becomes BaherOS") as you type and confirms it on save ("welcome to BaherOS"). The derived name flows through the existing `display_name` plumbing, so the app title and UI wordmark show *your* OS everywhere.

Respects custom brands: an explicit `display_name` (via the identity API) always wins, and a later name change only updates a display name that still tracks the old derived value — a custom brand is never clobbered. Empty name falls back to the generic "YouOS". Tests (7) cover the derivation, auto/explicit/clobber/rename cases, and the live-preview wiring.

## v0.1.66 — 2026-05-26

### Local, in-your-voice drafting is now the default (warm server on)
PR 3 of 3 — the warm model server is now enabled by default, making fast local drafting the default everywhere:
- **`model.server.enabled` defaults on.** The server is **pre-warmed on startup** (a background thread in the app lifespan loads the model off the request path, so the first draft isn't slow) and **stopped on shutdown** (no orphaned process). A no-op when mlx_lm is unavailable — generation falls back to the subprocess/Claude.
- **`review.draft_model` now defaults to `auto`** (was `claude`): the batch Review Queue uses your local LoRA when an adapter is trained, else Claude — and with the warm server, batch-on-local is finally fast. Claude's role narrows to cold-start (no adapter yet) and fallback, exactly as intended.
- **Test safety:** `ensure_running()` never spawns the ~3GB server inside the test suite (guarded on `PYTEST_CURRENT_TEST`) — generation falls back as if the server were down.

Net across PRs #70–#72: a trained user drafts in their own voice, on-device, fast, on both the Draft Reply tab and the Review Queue; Claude is only the bootstrap/fallback. Tests (4): enabled-by-default, `auto` default, pre-warm/shutdown wiring, and the pytest spawn-guard.

## v0.1.65 — 2026-05-26

### Generation uses the warm model server (when enabled), with graceful fallback
PR 2 of 3. Both local generation paths now prefer the warm server (v0.1.64) so they skip the ~3s per-draft model reload:
- **`_call_local_model`** routes the common case (global adapter / base) to the server's `complete()`; on any failure it falls back to the `mlx_lm generate` subprocess. It deliberately keeps the subprocess for a per-persona `adapter_path` and for explicit base requests (`use_adapter=False`), since the server loads a single adapter at startup.
- **`/draft/stream`** streams from the server when it's enabled and healthy, and only falls through to the subprocess/Claude paths if it fails *before producing any tokens* (so a mid-stream hiccup never double-streams).
- **Adapter reload:** the server records the adapter it loaded and `ensure_running()` restarts it automatically when the adapter file changes — so a freshly fine-tuned voice model is picked up without a manual restart.

Still gated by `model.server.enabled` (default off) — PR 3 enables it and flips the drafting default to `auto`. Tests (8): server-vs-subprocess routing (used / fallback-on-error / skipped for base + persona + disabled), warm-server streaming, and adapter-change reload.

## v0.1.64 — 2026-05-26

### Warm local-model server (foundation) — load the model once, not per draft
First of three steps toward fast, private, in-your-voice drafting everywhere. New `app/core/model_server.py` wraps `mlx_lm.server` (OpenAI-compatible HTTP): it loads Qwen + the global LoRA adapter **once** and serves generation, so a draft becomes a fast HTTP call instead of a ~3s model reload — which is what makes batch-on-local viable. Provides lifecycle (`ensure_running` with health-polled lazy start, `stop`, `restart` for picking up a freshly trained adapter), a client (`complete` + streaming `stream` parsing the server's `choices[0].text` deltas), and `youos model server {status,start,stop,restart}`.

**Inert for now** — `model.server.enabled` defaults off and nothing auto-starts; the next steps wire the generation paths to prefer it (with graceful fallback to the subprocess/Claude) and flip the drafting default to `auto`. Tests (10) mock all HTTP/subprocess (no real model load): health checks, completion/stream parsing, lazy start + graceful spawn-failure, adapter-arg passing, and the CLI wiring.

## v0.1.63 — 2026-05-26

### Loading animation masks the local model's cold start
Streaming from the local model (v0.1.62) reloads it per draft (~3s before the first token). The Draft Reply tab now shows a **loading overlay** (spinner) over the draft area the moment you hit Generate, and clears it the instant the first token streams in. If the wait runs past ~1.5s it explains itself — "Warming up your local model — the first draft is the slowest, then it stays fast." Covers the streaming, fallback, and error paths.

## v0.1.62 — 2026-05-26

### The Draft Reply tab now streams from your local fine-tuned model
The single-draft streaming path (`/draft/stream`) used the **Claude CLI** directly — so the main "Generate Draft" experience drafted with Claude, not your LoRA, even after the comparison showed the local model wins on voice. Now: **when the local model is ready (mlx_lm on PATH + a trained adapter), streaming runs `mlx_lm generate` with your adapter**, on-device, reporting `model_used: qwen2.5-1.5b-lora`. It falls back to the Claude CLI only when there's no adapter yet (and to non-streaming `generate_draft` on any error).
- Real token streaming preserved: a chunk-based parser reads mlx_lm's stdout (not line-buffered — a short reply is one line and would otherwise arrive all at once) and strips its `=====` framing, withholding only a trailing run that could begin the closing delimiter.
- Trade-off: the local path reloads the model per draft (~3s cold start before the first token) since generation runs as a fresh subprocess; the Claude path had none. A future optimization could keep the model warm.

Tests (4) pin the mlx framing parser (incl. body text containing `=`), local-vs-Claude selection by adapter readiness, and the streamed `model_used`.

## v0.1.61 — 2026-05-26

### Ask users to wait until the voice model is trained AND benchmarked
A new user shouldn't rely on drafts from a half-baked model. New **model-readiness gate** with a phase signal — `not_started → training → benchmarking → benchmark_pending → ready` — where "ready" means the LoRA is **both trained and benchmarked** (a golden eval ran at or after the adapter was trained).
- **The wizard's fine-tune now chains the benchmark**: `/api/finetune` runs export → fine-tune → **golden eval**, so "benchmarked" is reachable during onboarding instead of only via the nightly.
- **Soft "please wait" banner on the drafting page** (`/feedback`): until ready, a dismissible banner shows the current phase and explains drafts use the base model and won't sound like you yet. Drafting still works if you proceed ("Draft anyway").
- **Onboarding's final step** now reports the same phase and asks you to wait before relying on drafts.
- New `GET /api/model/readiness` and `get_model_readiness()` are the shared source of truth.

Tests (8) pin the phase machine (including stale-benchmark = not ready), the wizard chaining the eval, and the banner wiring.

## v0.1.60 — 2026-05-26

### Per-draft model badge — see which model wrote each draft
The review queue now shows a badge on every draft for the model that actually produced it: **✍️ your fine-tuned model** (green), **⚠️ base model (not personalized)** (amber), or **☁️ cloud fallback (not your local model)** (amber). So a draft that ran on the base model or fell back to the cloud is visible at a glance, not silently mistaken for your fine-tuned voice. `model_used` is now returned by both `/feedback/generate` and the `/draft/stream` done-payload (the streaming path uses the Claude CLI, so streamed drafts are correctly labelled `claude`; the non-streaming fallback reports its own model). Completes the three surfaces (stats indicator, doctor warning, per-draft badge) for confirming the LoRA is really in use.

## v0.1.59 — 2026-05-26

### Onboarding now reliably processes your LoRA (and the export can't hang)
Two fixes so a new user actually ends up with a trained voice model instead of silently skipping it:
- **The wizard auto-starts fine-tuning** when you reach the "Learn your voice" step (unless one is already trained or running) — it's no longer a button that's easy to skip. It runs in the background; you keep going through setup. The **final "You're set" step now reports the voice-model status** ("training in progress" / "trained ✓" / "uses base model for now, retrains tonight"), so you can't finish onboarding unaware that drafts aren't yet personalized.
- **The training export no longer hangs on a large corpus.** Near-duplicate dedup is O(n²) over `hybrid_similarity` — fine for a review-queue-sized set, but on a big organic corpus (tens of thousands of pairs) it ran for many minutes and stalled both the wizard's and the nightly's fine-tune. Above a 2,000-pair cap it's now skipped with a note (the cleanup is marginal there; the stall was not). This is what made the manual workaround necessary when training baheros.

Tests: dedup cap returns a large set untouched (no hang), and the wizard markup wires auto-start + the done-step status.

## v0.1.58 — 2026-05-26

### Surface what's *actually* drafting — no more silent LoRA failures
You can now tell, honestly, whether drafts are using your fine-tuned LoRA or silently running on the base model / falling back to the cloud:
- **Stats dashboard** gains a **"Drafting with"** row (System Health) — computed from what *recent drafts actually used* (`draft_events.model_used`), not from whether an adapter file happens to exist. Green when your LoRA is in use; amber with a tooltip when it isn't (base model, cloud fallback, or a mix).
- **`youos doctor`** now warns when drafts will silently run the base model (mlx_lm present but no adapter trained) or can't run locally at all (mlx_lm missing → cloud fallback) — reusing the same reality-based signal.
- **Fixed a false-confidence bug** in the model-status label: it reported `qwen2.5-1.5b-lora` whenever an adapter file existed, even if `mlx_lm` was missing (so the local model couldn't actually run). It's now capability-aware: `lora` / `base` / `claude` reflect adapter + `mlx_lm` reality, and a new `local_available` field is exposed.
- `summarize_draft_events` now includes a **`by_model`** breakdown.
- **Benchmark drafts no longer pollute the signal:** drafts generated with a forced `backend_override` (e.g. `youos compare-models`) are no longer written to `draft_events` — they're not real user drafts and were skewing both the training signal and the new "drafting with" status.

New helper `get_drafting_model_status()` is the shared source of truth (reality first, capability as fallback). Tests (12 new + updated stats tests) pin the classifier, the `by_model` aggregate, the capability-aware label, the doctor warning, and the benchmark-draft logging skip.

## v0.1.57 — 2026-05-26

### Docs: the "does it sound like you?" proof point
Surfaced the measured cross-model result (from v0.1.55's `youos compare-models`, run on the maintainer's ~11,700-email corpus) as a proof point in the **README** and the **landing page** (`site/index.html`): a fine-tuned local Qwen beats Claude on voice-match (**0.80 vs 0.70**), reuses the user's phrasing ~3× more (lexical 0.40 vs 0.13), matches their length (37 vs 81 words), is ~4× faster, and stays on-device — while *base* Qwen with no adapter scores just 0.43, so the personalization is what wins. Both note the numbers are from one corpus and reproducible via `youos compare-models --limit 30 --semantic`.

## v0.1.56 — 2026-05-26

### Fix: the wizard's "Start fine-tuning" silently did nothing for a history-only corpus
Surfaced while running the cross-model comparison (v0.1.55) on a real corpus — training the adapter required two manual workarounds that a normal user would just hit as dead ends:
- **Organic pairs were filtered out of training.** The export's edit-distance floor (`--min-edit-pct`, default 0.05) discarded every *organic* pair — real sent replies have `edit_distance_pct=0` because there was no YouOS draft to diff against. For a fresh user whose only data is historical sent mail, that meant **"No qualifying pairs after filtering"** and an empty train set. Organic pairs (`feedback_pairs.organic=1`) are now **exempt from the edit-distance floor** (it only ever made sense for review-queue pairs that had a draft). Column-detected, so DBs predating the `organic` column still export unchanged.
- **mlx_lm rejected the curriculum metadata line.** `finetune_lora.py` left the leading `{"_curriculum": ...}` annotation line in `train.jsonl`; mlx_lm (≥0.31) treats every line as a training record and aborts on it ("Unsupported data format") — on line 1. It's now **stripped before training** via `strip_curriculum_line()` (the curriculum *ordering* is in the row order, so the benefit is preserved). Idempotent.

Net: `youos finetune` / the wizard's fine-tune button now train a working voice adapter out of the box on a purely historical corpus. Tests (5) pin organic-pair survival (and that non-organic low-edit pairs are still filtered) plus curriculum-line stripping (strip/no-op/idempotent/missing-file). Existing export/finetune tests unchanged (backward-compatible).

## v0.1.55 — 2026-05-26

### Compare the LLM backends on *your own* mail (`youos compare-models`)
- **Answers "how do the models compare?" with data instead of vibes.** New `youos compare-models` (→ `scripts/compare_models.py`, `app/evaluation/model_compare.py`) samples real `(inbound → your reply)` pairs from your corpus, drafts each one under **every available backend** (local MLX+LoRA, Ollama, Claude), and scores each draft against the reply you actually sent — using the v0.1.54 voice-match metric — then prints a side-by-side scorecard **ranked by voice-match** (the metric that decides whether a cloud model's privacy/cost trade is worth it). Reports voice/semantic/lexical/style/length-fit, avg words, and latency per backend.
- **Backend pinning:** `DraftRequest.backend_override` ("mlx"|"ollama"|"claude") forces the engine for a draft regardless of `use_local_model`/config, so each backend is measured as itself.
- **Honesty guard:** generation silently retries empty/failed local drafts on Claude — the comparison detects this via `model_used` and reports a per-backend **`fellbk` count**, so a fallback can't be scored as the pinned model's own output.
- Auto-detects which backends can actually run (mlx_lm on PATH, a reachable Ollama server, the `claude` CLI); `--backends mlx,claude` to subset, `--semantic` to add embedding similarity, `--limit`/`--seed` for sample size/reproducibility, `--json` for raw output. Deterministic sampling so re-runs compare the same messages.
- Tests (12) pin `backend_override` selection (mlx/ollama/claude/default), fell-back detection, voice-ranked aggregation, error counting, the semantic flag, deterministic+filtered reply-pair sampling, and the empty-DB/empty-result paths.

## v0.1.54 — 2026-05-26

### Voice-match metric — measuring whether a draft sounds like *you*
- **The eval harness scored structure (keyword hit-rate, brevity, intent) but never voice** — the one thing YouOS exists to do. New `app/evaluation/voice_match.py` scores a draft against the user's *real* reply to the same message (`reply_pairs.reply_text` / a curated `reference_reply`): a combined `voice_match` plus sub-scores for lexical overlap, length fit, stylometry (sentence/word length, contraction & question/exclaim rates), greeting/closing-habit match, and an **optional semantic** cosine (uses `app.core.embeddings.get_embedding` when injected, degrades gracefully without it). The core is deterministic + dependency-free so it runs in CI. Wired into `evaluate_case`/`run_eval_suite` **additively** — it only computes when a case carries a reference reply and never changes the existing pass/fail. This is the foundation for the upcoming cross-model comparison (does a fine-tuned local Qwen sound more like you than a frontier cloud model?). Golden seeding now stores `reference_reply`/`expected_reply` when present. Tests pin identical→~1, unrelated→low, semantic lift when an embedder is injected, graceful degradation, and the additive wiring.

## v0.1.53 — 2026-05-26

### Installer sets up the MLX local model (no more manual step)
- **MLX — the on-device model engine — isn't bundled with macOS and YouOS wasn't installing it.** It was only a keyword in `pyproject.toml` (not a dep or extra), so after `./scripts/install.sh` the doctor's *required* `mlx_lm` check failed and local drafting silently fell back to cloud/none until the user found `pip install mlx-lm` themselves. Now: a **`youos[mlx]` extra** (`mlx-lm`), and **`install.sh` installs it automatically on Apple Silicon** (gated to arm64 macOS; best-effort so a failure doesn't abort the install; skipped with a note on non-AS). The doctor's hint now points at `pip install -e ".[mlx]"`, and the README notes the installer handles MLX. A fresh Apple-Silicon install now yields a working local model out of the box. Tests pin the extra + the installer's arm64-gated MLX step.

## v0.1.52 — 2026-05-26

### Docs: new-user quick start points to the web wizard + service
- **The README quick start was behind the product** — it sent new users only to the terminal `youos setup`, with no mention of the web onboarding wizard (`/welcome`) or `youos service install` built this cycle. Rewrote it as the real new-user path: clone + `./scripts/install.sh` → `youos service install` (run reliably) → open `/welcome`, with the wizard's 6 steps (identity → connect → corpus → fine-tune → token → keep-it-running) listed. Terminal `youos setup` + manual install moved to a "prefer the terminal?" details block; removed the now-duplicate CLI-steps list.

## v0.1.51 — 2026-05-25

### Stats: live Activity panel for ingestion + fine-tuning
- **The `/stats` dashboard now reports in-progress jobs**, not just results. A new "Activity" card auto-refreshes (polls `GET /api/ingest/status` + `GET /api/finetune/status` every 5s) and shows ingestion (⏳ "Ingesting… N found, M stored" / ✓ last-ingest reply-pair count / ✕ failed) and fine-tuning (⏳ "Fine-tuning…" / ✓ adapter trained / idle). Previously these were visible only while on the wizard's steps; now you can watch a long-running ingest or fine-tune from the dashboard regardless of where it was launched. Reuses the existing status endpoints — no backend change.

## v0.1.50 — 2026-05-25

### Wizard: install the background service in-browser ("Keep it running")
- **New wizard step** between Secure and Done: a plain-language explanation of why a background service matters (runs at login, restarts on crash, survives reboot, localhost-only, no root) and an **Install background service** button → `POST /api/service/install` (the launchd LaunchAgent from v0.1.49), with a live status line via `GET /api/service/status`. Completes the "make every operational step actionable from the wizard" pass. Tests cover the endpoints (install ok / failure→500 / status; `service.install` mocked) and the wizard wiring.

## v0.1.49 — 2026-05-25

### Run YouOS reliably: `youos service` (launchd background service)
- **`youos serve` is foreground-only** — it dies on terminal close, reboot, or crash, which is no way to run a daily-driver. New `youos service install` / `uninstall` / `status` installs the server as a macOS **launchd LaunchAgent** (`com.youos.server`): runs the venv uvicorn at the configured host/port, **RunAtLoad** (start at login) + **KeepAlive** (auto-restart on crash), survives reboot, no root. Logs to `var/server.log`; passes `YOUOS_DATA_DIR` through so the agent serves the right instance. README gains a "Run it reliably" section. Tests pin the plist generation (args / KeepAlive / RunAtLoad / data-dir env) and install/uninstall/status (launchctl + LaunchAgents path mocked). The onboarding wizard will offer this too (next).

## v0.1.48 — 2026-05-25

### Onboarding wizard: plain-language explanations on every step
- **Each step now has a jargon-free "what this means / why it matters" callout** for users who won't know the terms. Welcome explains what YouOS *is* (and that everything stays local); Identity explains why it needs your addresses (to tell your replies from others' in a thread); Connect explains a "backend" is just the tool that reads your mail (read-only, local) with a one-line plain description of gog/gws/native; Build-your-corpus defines "corpus"; Learn-your-voice explains fine-tuning / LoRA in plain terms and that it's optional; Secure explains localhost/PIN/token and that most users can skip it. Content-only.

## v0.1.47 — 2026-05-25

### Wizard: run fine-tune + create API token in-browser
- **"Learn your voice" now runs fine-tuning from the wizard.** A "Start fine-tuning" button → `POST /api/finetune` spawns export + LoRA fine-tune in the background (arg-list, no shell; in-memory guard returns 409 if one's already running), and the step polls `GET /api/finetune/status` → shows "Fine-tuning…" then ✓ when the adapter lands. This **replaces the "Check status" button that appeared to do nothing** (it had re-rendered the same text).
- **"Secure it" now mints an API token from the wizard.** A "Create API token" button → `POST /api/token` (via `add_api_token()`) shows the token once in a copyable field to paste into the Gmail extension. Terminal equivalents (`youos finetune` / `youos token-create`) stay as notes.
- Tests cover the spawn + running-guard (409) + status (running/idle/done) and token minting (subprocess + token creation mocked).

## v0.1.46 — 2026-05-25

### Wizard: run ingestion in-browser with a lookback window
- **The "Build your corpus" step now runs ingestion from the wizard** instead of only printing `youos ingest`. A "How far back" dropdown (6 months / 1 / 2 / 3 / 4 years / Everything) maps to a whitelisted Gmail `newer_than:` filter, and a **Run ingestion** button kicks it off via `POST /api/ingest`, which spawns the ingest script in the background (arg-list, no shell — nothing user-typed reaches the command) and returns immediately. The step then polls `GET /api/ingest/status` (from the `ingest_runs` log) and shows live progress — discovered / stored reply pairs, then ✓ done or the failure. Refuses to double-run (409) while one's in progress; the terminal `youos ingest` stays as a fallback. Tests cover the status reader, lookback validation + query building, the running-guard, and the spawn (subprocess mocked).

## v0.1.45 — 2026-05-25

### Onboarding wizard: make the backend install commands obvious
- **The "Connect Gmail & Docs" step buried the install step in prose.** Each backend's help now shows the actual commands as copyable command blocks (matching the ingest/train steps): `gog` → `pip install gog-cli` + `gog auth login`; `gws` → repo link + `gws auth login`; `native` → `pip install 'youos[google]'` + the OAuth-client note. So it's clear a new user must run something to connect, not just pick from the dropdown.

## v0.1.44 — 2026-05-25

### Web onboarding wizard (4/4)
- **A guided first-run wizard at `/welcome`** mirroring the steps of the terminal `youos setup`: Welcome → Identity → Connect Gmail/Docs → Build corpus → Learn your voice → Secure → Done. It **performs** the config steps in the browser (identity via a new `POST /api/config/identity`; Google backend via `/api/config/set`) and **guides** the operational steps (ingest / fine-tune / auth / PIN) with the exact command plus a live ✓ readiness check against `/api/config` (`corpus_ready` / `adapter_ready`). Feature toggles link to the Settings page (no duplication).
- **First-run entry point:** the draft page's empty state (shown when there's no corpus) now leads with a "Run the setup wizard →" button to `/welcome`, with `youos setup` as the terminal alternative.
- **Why guided, not fully automated:** there are no web endpoints for ingest/fine-tune/OAuth/PIN (only `/trigger-autoresearch`), and building those long-running/shell/OAuth actions as blind web endpoints would be a large, separate effort that duplicates `youos setup`. The wizard drives everything it safely can and points to the one command for the rest. New `set_identity()` write path is validated like the flag whitelist. Verified structurally (serves + wired + 7 steps); visual flow eyeballed on a running instance.

This completes the config UX series: `youos config` CLI (#47) · config-write API (#48) · Settings page (#49) · onboarding wizard (this).

## v0.1.43 — 2026-05-25

### Web Settings page (easy flag toggling, 3/4)
- **A `/settings` page to toggle features in the browser.** Renders the whitelisted feature flags from `GET /api/config/flags` as switches (bool) / selects (choice), saving each change immediately via `POST /api/config/set` with inline saved/error feedback. Added a **Settings** link to the nav across the chrome pages (Draft / Stats / Settings / Bookmarklet / About). Uses the shared design system; same flags as `youos config`. Verified structurally (serves + wired to the API); visual behavior eyeballed on a running instance.

## v0.1.42 — 2026-05-25

### Config-write API (easy flag toggling, 2/4)
- **`GET /api/config/flags`** lists the whitelisted feature flags with their current values (for the settings page / onboarding wizard to render toggles), and **`POST /api/config/set`** `{key, value}` sets one — restricted to the feature-flag whitelist, so it can never write arbitrary config keys. Inherits the app's auth + Origin protections on state-changing requests (when a PIN is configured). Returns `400` on an unknown key or a value that doesn't fit the flag's type. Pinned with API tests (list, unknown-key 400, bad-value 400, valid set).

## v0.1.41 — 2026-05-25

### Feature-flag core + `youos config` CLI (easy flag toggling, 1/4)
- **No more hand-editing YAML to flip a flag.** New `app/core/feature_flags.py` defines a **whitelist** of the session's toggles (`generation.multi_candidate.enabled`, `generation.repair.*`, `generation.log_drafts`, `autoresearch.draft_quality_weighting`, `personas.routing_enabled`, `ingestion.google_backend`) with label/type/default, and `get`/`set`/`list` helpers (dotted paths, bool/choice coercion, persisted via the existing `save_config`). Writes are restricted to the whitelist — the same guard makes the upcoming web config-write path safe.
- **`youos config` CLI:** `youos config list` (all flags + current values), `youos config get <key>`, `youos config set <key> <value>`. This is the foundation shared by the forthcoming web **Settings page** and **onboarding wizard**. Pinned with tests for the core (round-trip, coercion, whitelist guard, sibling-preservation) and the CLI wiring.

## v0.1.40 — 2026-05-25

### UI: stats page surfaces the unused data (rethink, 3/3)
- **The stats dashboard now renders data it was already fetching but dropping.** Two `/stats/data` keys were returned and never shown: the **draft-quality-by-condition** summary (`draft_events`, from v0.1.36) and **per-persona adapter status** (`persona_adapters`). Added a "Draft Quality by Condition" card (drafts logged, off-target-length rate, and counts by length / confidence / sender type / intent) and a "Per-Persona Adapters" card (trained ✓ + pairs used per cohort). Both hide when there's no data.
- **Note:** the `outcome_deltas` "data leak" flagged during the survey turned out to be a false alarm — that section is fully wired (HTML + JS). The remaining unused key, `embedding_coverage_by_table`, is left for now (the overall coverage % is already shown in System Health). Pinned with tests that the panels exist, read the right keys, and that `/stats/data` exposes them.

This completes the UI rethink (1/3 shared design system + version fix · 2/3 drafting flow · 3/3 stats panels). The deeper template/component de-duplication remains an incremental, visually-verified follow-up.

## v0.1.39 — 2026-05-25

### UI: draft flow surfaces the new capabilities (rethink, 2/3)
- **The draft UI now shows what generation produces.** `length_flag`, `repairs`, and the multi-candidate `candidates` were computed but never rendered. The draft page now shows a **length badge** (`on target` / `long` / `short`), a **"repaired"** badge when the post-generation pass made changes, and a **multi-candidate picker** — when several candidates come back, they render as selectable cards (best first, with temperature/score); clicking one swaps it into the draft. Built on the shared design-system classes from 1/3 (`.yos-badge`, `.yos-candidate`).
- **Both draft paths covered.** The streaming `/draft/stream` done-event now carries `length_flag`/`repairs`/`candidates` (populated on the local-model fallback path), and both the streaming and non-streaming handlers call the same renderer. Candidates only appear when `generation.multi_candidate.enabled`; repairs only when the repair flags are on — so the default experience just gains the length badge.
- Pinned with tests that the draft page has the render targets + logic and that the stream done-event carries the fields. (Full SSE/visual behavior is verified on a running instance — the UI can't render in CI.)

## v0.1.38 — 2026-05-25

### UI: shared design-system assets + single-source version (rethink, 1/3)
- **Version now has one source of truth.** It was hardcoded and had drifted across three places — `settings.version` (`0.1.25`), `/api/config` (`0.1.10`), and the UI footers (`YouOS v0.1.10`). New `app/core/version.py:get_version()` resolves it from `pyproject.toml` (repo-based local-first app → accurate without a reinstall), falling back to installed package metadata. `settings.version` and `/api/config` now use it, and the four page footers hydrate the version from `/api/config` (no more hardcoded strings).
- **Shared front-end assets.** Mounted `/static` and added a design-system stylesheet (`static/youos.css` — the dark + teal palette as CSS variables, plus shared component classes for the multi-candidate picker / draft-quality badges coming next) and `static/youos.js` (hydrates the shared chrome from `/api/config`; small helpers). The four chrome templates (feedback, stats, about, bookmarklet) link them. This is the foundation the next two UI PRs build on; the deeper template de-duplication / component split lands incrementally on top (and is verified visually on a running instance — the UI can't be rendered in CI). Pinned with tests that the version is dynamic (not the old hardcoded value), the static assets serve, and every page links them.

## v0.1.37 — 2026-05-25

### Draft-quality-weighted autoresearch objective (closes the draft→tuning loop)
- **Autoresearch can now bias its objective toward the cohorts where real drafts get edited most.** With `autoresearch.draft_quality_weighting: true`, each golden-eval case is importance-weighted by the average edit distance of its sender_type cohort (from the `draft_events` log via `summarize_draft_events`) — benchmark cases already carry the sender_type as their `category`, which is the join key. Cohorts you rewrite heavily count more in the composite, so the optimizer prioritizes config changes that help where drafting actually struggles, instead of treating every cohort equally. The weights are computed **once per run** and applied to both the baseline and every candidate so their composites stay comparable.
- **Why this is the sound integration:** autoresearch scores a *hypothetical mutated config* by re-running the golden eval, but draft-quality history was produced under *past* configs and can't be re-derived per candidate — so it can't be a naive term in the per-candidate score. Importance-weighting the eval cases is the principled way to realign the objective with real-world need. (The model's own drafts remain non-targets; this only reweights the synthetic eval.)
- **Default-off & graceful.** `draft_quality_weighting` defaults `false` (equal weighting — unchanged). Enabled but with no accumulated edit-distance data → empty weights → uniform → still unchanged. Weight = `clamp(1 + 2·edit_distance, 1, 3)`. Pinned with tests for the weight derivation (scaling, clamp, dataless), weighted scoring (failing cohort ↓ / passing cohort ↑ composite; uniform == unweighted; unknown category → weight 1), and the config gate.

## v0.1.36 — 2026-05-25

### Consume the draft_events signal — draft-quality-by-condition
- **The per-draft signal log (`draft_events`, v0.1.33) is now turned into an actionable picture, surfaced in the nightly log and `/stats/data`.** New `summarize_draft_events()` (`app/core/stats.py`) aggregates the log by **condition**: counts per intent / sender_type / confidence / length_flag, the **off-target length rate** (% of length-annotated drafts flagged `long`/`short` — a direct signal that a cohort's target-words are mis-calibrated), and a **best-effort edit-distance-by-condition correlation** (LEFT JOIN to `draft_history` on inbound+draft text, with a `matched` coverage count since that key isn't unique). This tells the self-improvement loop *where* drafting is weak.
- **Why not "train on drafts":** the LoRA target is always the user's edited reply (ground truth); a model's own draft is never a training target (that would just reinforce current behavior). `draft_events`' unique value is the *conditions* a draft was produced under — analysis/observability, and the substrate a future autoresearch objective can optimize. Wired into `scripts/nightly_pipeline.py` (`draft_events_summary` in the run log) and the `/stats/data` API. Read-only and tolerant of an absent/empty table. Pinned with tests for the condition counts, off-target rate (NULL flags excluded), the outcome correlation, and the empty/missing-table paths.

## v0.1.35 — 2026-05-25

### Smarter drafting 4/4 — multi-candidate generation + ranking
- **Optionally generate several drafts and keep the best.** With `generation.multi_candidate.enabled: true`, `generate_draft` produces one local-model draft per configured temperature (`temperatures`, default `[0.3, 0.7, 1.0]`) and returns the highest-scoring one. The deterministic scorer (`_score_candidate`) disqualifies empty/placeholder/signature-only drafts and rewards length-fit (peaking at the persona's target words) plus honoring the persona greeting/closing. The ranked alternatives are surfaced on `DraftResponse.candidates` (draft, model_used, temperature, score) for the review queue.
- **Refactor:** the Phase-3 adapter precedence is now factored into `_local_draft_once`, shared by the single-draft and multi-candidate paths (one source of truth for adapter routing); the greeting/closing are resolved once and reused by both ranking and the repair pass.
- **Default-off** — `enabled` defaults `false`, so drafting makes exactly one model call and `candidates` is empty, identical to before. It's gated because it multiplies model calls (latency/cost); the quality benefit is best assessed live on a real instance. Pinned with tests for the config, usability check, scorer (length-fit / disqualification / greeting-closing credit), ranker ordering, and end-to-end (one call per temperature → best chosen + alternatives surfaced; single call + empty candidates when disabled).

## v0.1.34 — 2026-05-25

### Smarter drafting 3/4 — configurable, adaptive decoding
- **Decoding sampling is now surfaced and adaptive.** Temperature was hardcoded (Ollama `0.7`) or absent (MLX ran with `mlx_lm`'s defaults), and uniform across all intents/confidence. New `generation.decoding` config exposes `temperature` and `top_p`, with an optional per-intent override (`intent_temperature`) and a per-confidence delta (`high_confidence_temperature_delta` / `low_confidence_temperature_delta`) — e.g. drop the temperature when retrieval is high-confidence (favor fidelity) and raise it for creative intents. `_resolve_decoding(intent, confidence)` computes the effective params, which now plumb through the MLX (`--temp`/`--top-p`) and Ollama (`options`) call paths.
- **Surfacing precondition for autoresearch tuning.** Like the retrieval weights before them, these params being in config is what lets the nightly autoresearch loop A/B-tune them against the golden eval (wiring them into the search space is a follow-up).
- **Default unchanged.** With no `generation.decoding` config, `_resolve_decoding` returns `(None, None)`: MLX gets no sampling flags (its prior behavior) and Ollama keeps `0.7` — identical output to before. Pinned with tests for the resolver (base / per-intent / confidence-delta + clamp / malformed) and that the params plumb into the MLX command and Ollama options while the default omits them.

## v0.1.33 — 2026-05-25

### Smarter drafting 2/4 — draft-time signal capture
- **Every generated draft is now logged, not just the ones you give feedback on.** `draft_history` is only written when a reply is saved/edited via the review queue or feedback API; drafts you never act on — and the *signals* a draft was produced with (which exemplars, intent, sender_type, confidence, length flag) — left no trace. New append-only `draft_events` table captures one row per `generate_draft` call: `(inbound, draft, account, sender, sender_type, detected_mode, intent, confidence, confidence_reason, model_used, retrieval_method, exemplar_ids, length_flag, created_at)`. This is the raw material for the nightly to learn from far more than explicit edits (consumption by the fine-tune/autoresearch steps is a follow-up).
- **Default-on but fully fault-isolated.** Gated by `generation.log_drafts` (default `true`; set `false` to opt out). The logger self-heals the table (`CREATE TABLE IF NOT EXISTS`) so it works on a DB that predates it, and it **never raises** — a logging failure can't break drafting (returns `False`, logs a warning). Table added to `schema.sql` and `bootstrap` migrations. Pinned with tests for the write, self-heal, disabled no-op, never-raises, empty-exemplar serialization, and the migration.

## v0.1.32 — 2026-05-25

### Smarter drafting 1/4 — post-generation repair pass
- **Drafts get a final repair/annotation pass before being returned.** Previously the model's output was returned after only an emptiness check. New `_repair_draft()` in `app/generation/service.py` always adds a non-mutating `length_flag` (`ok`/`long`/`short` vs. the persona's target words) to `DraftResponse`, and — when opted in — enforces the persona greeting/closing the model dropped and strips a trailing duplicate signature. (Both `_resolve_greeting`/`_resolve_closing` are injected as a prompt *instruction* today but never enforced; signature-stripping was computed but only used for the emptiness check, never applied to the returned draft.)
- **Default-off, behavior-preserving.** The two mutating repairs are gated behind `generation.repair.enforce_greeting_closing` and `generation.repair.strip_trailing_signature` (both default `false`); the length flag is metadata only. Placeholder/error drafts (`[...]`) are left untouched. Flip the flags on per instance once verified against real drafts. Pinned with tests for length-flag thresholds, greeting/closing detection, each opt-in mutation, no-double-add, and the default-off no-op.

## v0.1.31 — 2026-05-25

### Backend-aware doctor + setup wizard (decoupling from OpenClaw, step 4/5 — complete)
- **The doctor no longer fails non-`gog` users for a missing `gog`.** `youos doctor` (and the setup wizard's dependency check) previously required the OpenClaw `gog` CLI unconditionally — so a `gws` or `native` user, who may not have `gog` installed at all, failed the health check. A new `_google_backend_status()` keys the required dependency on `ingestion.google_backend`: `gog` → the `gog` CLI, `gws` → Google's `gws` CLI, `native` → the `youos[google]` libraries. The doctor and `scripts/setup_wizard.py` both use it; the wizard now shows `Google backend (<backend>)` with backend-specific install hints.
- **Manifest credential notes updated.** `clawhub.json` and `SKILL.md` now describe the Google backend as a choice (gog default / gws / native) rather than gog-only. `gog` stays in `requires.bins` — it remains the default and the OpenClaw-skill install path ships it.
- **Default unchanged** (`gog`), so existing instances see identical doctor behavior. Pinned with tests for each backend × present/absent, that a native user with the extra isn't failed for missing `gog`/`gws`, and the wizard's pass/fail wiring.

This completes the OpenClaw decoupling: YouOS installs standalone (#33), and Gmail/Docs ingestion runs on `gog`, Google's `gws` CLI (#34), or the native Google API (#35) — selectable, with `gog` the default.

## v0.1.30 — 2026-05-25

### `native` ingestion backend — direct Google API, no CLI (decoupling from OpenClaw, step 3/5)
- **Added `NativeSource`** to `app/ingestion/adapters.py`, selectable via `ingestion.google_backend: native`. It talks to the Google API directly (`google-api-python-client` + `google-auth-oauthlib`) — no external CLI at all. Gmail via `users().threads().list()/get()`, Drive via `files().list()/get()`, Docs via `documents().get()`. Because the native client and `gws` both return the raw Google API shape, the native backend **reuses the same shaping** as `gws` (`_normalize_gog_thread_payload`, the Docs text walk, the Drive-query builder, byte truncation) — same mapping, different transport.
- **New `youos[google]` extra** carries the Google libraries. They're imported lazily inside `NativeSource` methods, so the base install and importing `app.ingestion.adapters` never require them; calling a native method without the extra raises a clear `pip install youos[google]` error.
- **Multi-account via per-account OAuth tokens.** Unlike `gws`, `native` is naturally multi-account: tokens are stored per account under the instance dir (`var/google_tokens/<account>.json`, or `ingestion.google_token_dir`), auto-refreshed on expiry. First-run authorization is the interactive `NativeSource.authorize_account()` (OAuth installed-app flow), which reads the client JSON from `ingestion.google_oauth_client_secrets`.
- **Default unchanged** — `gog` remains default; purely additive. Unit-tested via a mocked service object (pagination/cap, thread normalization feeding the existing normalizer, Drive query building, `documents.get` caching across `docs_info`+`docs_cat`, truncation, metadata fields), the deterministic absence-of-extra error path, and token-path resolution. **Live OAuth + ingestion is verified on a real instance** (the container has no browser/Google account).

## v0.1.29 — 2026-05-25

### `gws` ingestion backend — Google's own Workspace CLI (decoupling from OpenClaw, step 2/5)
- **Added `GwsSource`** to `app/ingestion/adapters.py`, selectable via `ingestion.google_backend: gws`. It drives Google's open-source [Workspace CLI](https://github.com/googleworkspace/cli) (`gws <service> <resource> <method> --params '{...}'`, JSON output): Gmail via `users threads list`/`get`, Docs via Drive `files list`/`get` + Docs `documents get`. Because the Gmail normalizer already consumes the raw Gmail-API message shape, the Gmail path is near-identity (the threads.get resource flows straight through `_normalize_gog_thread_payload`); Docs content comes from a structural-element text walk (handles the tabs feature), Docs metadata from Drive's `files.get`.
- **Single-account bridging.** `gws` is single-account per credential (no per-command `--account` like `gog`). The adapter sets `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` per call from an optional `ingestion.gws_credentials` map (`{account: creds_file}`); with no mapping it uses the ambient `gws` login. Same rate-limit backoff and per-call timeout as the `gog` backend.
- **Default unchanged** — `gog` remains the default backend, so this is purely additive. Pinned with fixture-based unit tests (transport, command construction, JSON-envelope unwrapping, credential-env bridging, pagination, the Docs text walk, max-bytes truncation, and that gws Gmail payloads feed the existing normalizer). **Live `gws` ingestion is verified on a real instance** — the container has no authenticated `gws`, and the Discovery-derived command names may need confirmation there.

## v0.1.28 — 2026-05-25

### Standalone distribution (decoupling from OpenClaw, step 5/5)
- **YouOS installs and runs without OpenClaw / clawhub.** Added `scripts/install.sh` — a re-runnable installer that locates a Python ≥ 3.11, creates `.venv`, installs YouOS (with optional extras, e.g. `./scripts/install.sh reranker`), and runs `youos doctor` for an immediate setup readout. The clawhub skill artifacts (`clawhub.json`, `SKILL.md`, `prepare_clawhub_release.sh`) are kept, so YouOS ships **both** standalone and as an OpenClaw skill.
- **PyPI-ready packaging metadata.** `pyproject.toml` now declares `license`, `authors`, `keywords`, trove `classifiers`, and `[project.urls]`.
- **README documents the standalone path and the pluggable Google backend.** Quick start now leads with `./scripts/install.sh` (with a manual fallback) and a new "Google ingestion backend" section explains `ingestion.google_backend` (`gog` available today; `gws` and `native` in progress). Fixed the stale `cd ~/Projects/youos` path in the old quick start.

## v0.1.27 — 2026-05-25

### Pluggable Google Workspace ingestion backend (decoupling from OpenClaw, step 1/5)
- **Gmail/Docs ingestion now fetches through a backend-agnostic seam.** Introduced `app/ingestion/adapters.py` with a `GoogleWorkspaceSource` protocol and a `get_google_source()` factory selected by a new `ingestion.google_backend` config key. The lone implementation today is `GogSource`, a thin delegating wrapper over the existing `_gog_*` helpers — **zero behavior change**; the subprocess transport, rate-limit retry, and gog-shape normalization are untouched. `gmail_threads.py` and `google_docs.py` now resolve the source via the factory instead of calling `_gog_*` directly (6 call sites). This is the foundation for moving off the OpenClaw `gog` CLI: the reserved `gws` (Google's own Workspace CLI) and `native` (direct Google API) backends are recognized by the factory and raise a clear `NotImplementedError` until they land in later steps.
- **Default-safe.** `ingestion.google_backend` defaults to `gog`, and an unrecognized value degrades to `gog` at config-read time (a typo can't break ingestion; the doctor will flag it in a later step). Pinned with tests covering the config accessor, factory selection/override, the not-yet-implemented backends, and `GogSource` delegation.

## v0.1.26 — 2026-05-25

### Nightly embeds reply_pairs, not just chunks
- **The nightly embedding skip-gate was chunks-only.** `should_skip_embeddings` / `_count_null_embeddings` counted unembedded rows in `chunks` alone, so an instance with a fully-embedded (or empty) `chunks` table but a backlog of unembedded `reply_pairs` would skip the embedding step every night — reporting "all documents already indexed" while semantic re-ranking stayed off for `reply_pairs`, the *primary* retrieval target. The gate now sums unembedded rows across both `chunks` and `reply_pairs`: a missing table contributes 0 (pre-ingest), a table that exists but hasn't had the `embedding` column migrated yet contributes its full row count (so the indexer runs and migrates it), otherwise `COUNT(embedding IS NULL)`. Behavior-fixing only — the step can now do work it was already meant to, never less. Added regression tests for the reply_pairs-backlog, both-indexed, and unmigrated-column cases.

## v0.1.25 — 2026-05-25

### Default to the local MLX model; fix nightly auto-feedback import
- **Subject generation no longer goes to the Claude CLI by default.** `generate_subject` was hardcoded to `_call_claude_cli`, which meant every benchmark case in the nightly pipeline paid a 120s subprocess timeout when the CLI wasn't reachable (the silent failure mode on the launchd-driven nightly). It now uses the local MLX model when `mlx_lm` is on PATH, falling back to the Claude CLI only when MLX is unavailable.
- **Draft generation runs the base MLX model when no LoRA adapter is present.** Previously `generate_draft` gated local-model use on `_adapter_available()` and fell over to the cloud whenever the adapter was missing — defeating the point of having MLX installed on instances that haven't finished a fine-tune yet. New `_local_model_available()` (checks for the `mlx_lm` CLI) gates whether to use local generation at all; the LoRA adapter is now an optional enhancement (`use_adapter` becomes effective only when both the caller asks for it *and* the adapter exists on disk). `model_used` is reported as `qwen2.5-1.5b-lora` or `qwen2.5-1.5b-base` accordingly. The Review Queue's stricter "auto = must have adapter" gate is preserved via the existing `_adapter_available()`.
- **Nightly pipeline can now import its sibling scripts.** Running `python3 scripts/nightly_pipeline.py` directly under launchd puts only `scripts/` on `sys.path[0]`, so `from scripts.extract_auto_feedback import …` failed with `No module named 'scripts'` — visible in `var/pipeline_last_run.json` as `"Auto-feedback error: No module named 'scripts'"`. The script now prepends the repo root to `sys.path` before importing.

## v0.1.24 — 2026-05-24

### Retrieval & generation fixes (code-review findings)
- **Exemplar token-budget trimming no longer demotes the best precedent.** When a prompt exceeded the token budget, the trimmer kept whichever exemplars came first in the *cache* order (`_apply_cached_order` moves cached pairs to the front regardless of score), so a high-relevance pair could be dropped in favor of a lower-scoring cached one. Trimming now selects by score; the exemplar cache is for presentation consistency, not selection.
- **Feedback `quality_score` now actually influences exemplar selection.** `_top_exemplar_source_ids` ranks by `metadata["quality_score"]` first, but retrieval never put `quality_score` into the match metadata — so the primary sort key was dead (always `1.0`) in production despite being unit-tested. Both the FTS and legacy reply-pair scorers now surface it; added a regression test guarding the wiring on both paths.
- **`model_fallback: none` is now honored.** With no local model available and fallback explicitly disabled, generation still made a Claude CLI call (the catch-all `else` branch) — defeating strict local-only mode. It now returns an explicit "no model available" draft instead of reaching out to the cloud.
- **Retrieval connection no longer leaks and gets the concurrency PRAGMAs.** The main `retrieve()` query opened a raw `sqlite3.connect(...)` (no `busy_timeout`/WAL, relying on GC to close); switched to the pooled `connect()` helper wrapped in `closing()`.

## v0.1.23 — 2026-05-24

### Ingestion & CLI fixes (code-review findings)
- **Critical: Gmail ingestion was completely dead.** A malformed regex (`+1`, an unescaped quantifier) in `gmail_threads.py` raised `re.PatternError` at import time, so the whole module — and thus all Gmail ingestion — failed to load. Escaped to `\+1`; added an import-regression test covering every ingestion module.
- **WhatsApp pairing dropped messages.** `build_reply_pairs` only paired one inbound with the next reply, silently dropping the earlier messages of a consecutive-inbound run; now accumulates the whole run (matching the Gmail importer). And when no `user.names` are configured it now fails with a clear message instead of misattributing every message as inbound.
- **CLI commands propagate exit codes.** `setup`/`improve`/`ingest`/`finetune`/`eval` ran underlying scripts but always exited 0 even on failure (broke scripting/CI); they now exit non-zero, and `finetune` short-circuits if its export step fails.
- **`note`/`feedback` give a clean error** on a missing database instead of a raw traceback.
- **`gog` calls have timeouts** — Gmail/Docs ingestion can't hang forever on a stalled `gog`.
- Removed the stale 524-line duplicate CLI `scripts/youos_cli.py` (the shipped entrypoint is `app.cli`); repointed its tests to the real CLI (which also fixes a long-standing flaky `test_cli_stats_no_db`).

## v0.1.22 — 2026-05-24

### UI polish (deferred review items)
- Consolidated the two `<style>` blocks in `feedback.html` into one (the second was nested mid-body) — no visual change, just maintainability.
- The Review Queue keyboard-hints line is now clickable to open the shortcuts overlay (discoverable without knowing the `?` keystroke), with brighter contrast; the overlay also closes on backdrop click and Esc.

## v0.1.21 — 2026-05-24

### Backend cleanup (code-review findings)
- Removed the dead, never-mounted `app/api/memory_routes.py` (a stale duplicate of `facts_routes.py`) and a redundant double-`SELECT` in `facts_routes.py`.
- **Review-queue submit is now race-safe** — a check-then-insert could double-insert the same `reply_pair_id` under concurrent submits; replaced with an atomic `INSERT … WHERE NOT EXISTS`, and the route now uses the pooled `connect()` (busy_timeout + WAL).
- **Streaming `claude` subprocess hardened** — passes the prompt via `-p` (so a prompt starting with `-` isn't parsed as a flag) and kills the whole process group on error or client disconnect, so a hung/abandoned generation can't linger.
- **FTS query expansion no longer pollutes ranking** — synonyms are appended bare instead of as `(also: …)`; the literal `also` was being tokenized into every expanded query.
- Exemplar cache is no longer rewritten to the DB on every cache hit (only on a miss or when the selection changes).

## v0.1.20 — 2026-05-24

### Web UI review fixes
- **Security:** History tab now HTML-escapes inbound/draft/snippet content before injecting it (raw corpus email bodies could otherwise execute embedded markup). Sender-note editing builds its textarea via `.value` instead of string-interpolated `innerHTML`.
- **Streaming fidelity:** drafts streamed from the server kept their paragraph breaks — the SSE path was dropping blank lines and the client was adding a stray newline per token.
- **"How was this generated?"** now works from the main Draft tab: `/feedback/generate` returns a `draft_id` (stores a trace) so the explain link renders and resolves.
- **Progress nudge** ("X/10 pairs collected") refreshes after each Draft/Review-Queue submission instead of freezing at the page-load value.
- **Accessibility:** the four main tabs are now keyboard-navigable (roving `tabindex`, `role=tab`/`tablist`/`tabpanel`, `aria-selected`, arrow/Enter/Space + focus ring); raised text contrast on the footer, inactive tabs, placeholders, and empty rating stars; stats dashboard gained `role`s and a no-corpus empty state.
- **Robustness:** the `r` re-generate shortcut checks the response and restores (no longer blanks) the draft on error; the Review Queue won't start a second batch stream while one is in flight; sender-note save and fact-delete failures are now surfaced instead of silently swallowed.
- **Cleanup:** removed orphaned/broken script after `</html>` in `stats.html`, a dead `if (false)` branch, and a duplicate history-load trigger.

## v0.1.19 — 2026-05-24

### Autoresearch reliability (the real `database is locked` fix)
- **`run_eval_suite` now commits after each case** instead of once at the end of the suite. Previously the suite's connection held a single uncommitted write transaction (the per-case `eval_runs` inserts) across the entire loop — keeping the WAL write lock the whole time. Every per-draft write on another connection (e.g. the exemplar cache) then blocked for the busy_timeout and failed with `database is locked`, and no eval results were visible until the suite ended (so autoresearch recorded nothing). With per-case commits the lock is released between cases. Verified end-to-end on a real instance: `eval_runs` grows per case with zero lock errors. Builds on the busy_timeout + WAL hardening in 0.1.18.

## v0.1.18 — 2026-05-24

### Autoresearch reliability (DB concurrency)
- **Fixed `database is locked` under concurrent access.** All SQLite connections in the generation, evaluation, and autoresearch-log paths now go through a shared `app.db.bootstrap.connect()` that sets a 30s `busy_timeout` and enables **WAL** journaling. A single draft opens several connections and the nightly pipeline runs while the web server is live, so contention is normal; previously a momentarily-locked write raised immediately (the exemplar-cache and eval writes failed, blocking autoresearch from recording results). Now writers briefly wait, and WAL lets a writer proceed alongside readers.

## v0.1.17 — 2026-05-24

### Autoresearch reliability
- **Generation can no longer hang the loop.** The `claude`/`mlx_lm` subprocess calls now run in their own session and kill the whole process group on timeout. `subprocess.run(timeout=)` only kills the direct child, so a generation that spawned children (the `claude` Node CLI does) could keep the stdout pipe open and stall far past the 120s timeout — observed as an 8-minute freeze mid-run.
- **One bad case no longer aborts the eval suite.** `run_eval_suite` wraps each case's generation; a failure/timeout is logged and scored as a fail, and the loop continues.
- **benchmark_cases auto-seeds from `configs/benchmarks/golden.yaml`.** `load_benchmark_cases` seeds the table when it's missing or empty, so eval and autoresearch work on a fresh instance instead of crashing on `no such table: benchmark_cases`. `seed_benchmarks.py` now falls back to the same golden source (its old `fixtures/benchmark_cases.yaml` never existed) and targets the active instance's DB.

## v0.1.16 — 2026-05-24

### Autoresearch
- **Autoresearch is now instance-aware.** `run_autoresearch.py` derived its DB and configs from hardcoded repo paths (`ROOT_DIR/var/youos.db`, `ROOT_DIR/configs`) and ignored `YOUOS_DATA_DIR` — so it always optimized the repo's default config against the repo DB and never the instance it was meant to tune. It now resolves both from settings (honoring `YOUOS_DATA_DIR`), with `--db-path` / `--configs-dir` overrides.
- The nightly pipeline's `DEFAULT_DB` is likewise derived from settings, so the autoresearch skip-gate and other DB-dependent steps check the active instance's database.

## v0.1.15 — 2026-05-24

### Security
- **Snapshot path traversal (critical).** `restore_snapshot` now refuses any path outside the managed snapshots directory (previously an arbitrary path could overwrite the live DB with any readable file), and `create_snapshot` validates the `tier` as a single safe path component (previously `tier="../.."` could write a DB copy anywhere). Both the API routes and CLI return clean errors.
- **Server-side session expiry.** `PinAuthMiddleware` now keeps session creation timestamps in memory and rejects/evicts tokens older than `SESSION_MAX_AGE`. Previously only token keys were stored, so a captured token replayed indefinitely until process restart.
- **Exposed-without-PIN warning.** Startup now prints a security warning when the server is reachable beyond localhost (non-loopback `server.host` or Tailscale) while no PIN is configured — in that state the UI and API are unauthenticated.
- **Bounded rate-limiter maps.** The draft and login per-IP limiters now evict stale keys so they can't grow unbounded.

### Fixes
- **Autoresearch composite-weight tuning now takes effect.** Composite weights were cached once at baseline and never reloaded, so the optimizer's weight mutations scored identically and always reverted. Scoring now reads the freshly written config during a run.
- **DB connection leak.** `generate_draft` wraps its shared SQLite connection in `try/finally`; an exception during retrieval/lookup no longer leaks the handle.
- **Startup health check** now watches the real `memory` table (the previous `facts` entry never matched, so a dropped table went undetected).

## v0.1.14 — 2026-03-18

### ClawHub metadata parity
- Aligned `clawhub.json` registry metadata with actual package behavior to remove "instruction-only vs full app" ambiguity for install-time trust review: `packageType: application`, `execution: local-python`, explicit install workflow (`venv` + `pip install -e .`), and credential scope (`gog` required for ingestion; Claude/API optional, only when external fallback is enabled).
- `SKILL.md`: added an explicit safety note that `pip install -e .` executes local package install code and should be reviewed before install.

## v0.1.12–0.1.13 — 2026-03-18

### Data safety & recovery
- **Instance data guardrails** — startup rejects mismatched DB paths and unsafe locations (e.g. Trash); `YOUOS_DATA_DIR` derives the canonical DB path as `YOUOS_DATA_DIR/var/youos.db`.
- **Snapshots** — `youos snapshot-create` / `snapshot-list` / `snapshot-restore` (with confirmation + `--dry-run`), plus `youos health-check` integrity checks (required tables + regression warnings).
- **CI hardening** — resolved Ruff lint failures; `create_app()` tolerates mocked settings without `instance_name`.

## v0.1.11 — 2026-03-18

### Review Queue & quality
- **Bulk actions + keyboard shortcuts** — merged review-queue bulk submit/skip with expanded shortcuts.
- **Sender-type style anchors** — explicit `[STYLE ANCHOR — internal|client|personal]` prompt slot to stabilize first-draft tone by audience.
- **Persistent exemplar cache** — exemplar cache by intent+sender-type (TTL + feedback-triggered invalidation); quickstart default.
- **Edit-reduction metrics** — surfaced in the Stats dashboard.
- Narrowed the low-signal filter so valid training pairs are no longer dropped.

### Release packaging
- Enforced a minimal ClawHub allowlist bundle; added a default release-bundle prep script.

## v0.1.10 — 2026-03-17

### Release metadata
- Version bump to `0.1.10` for re-upload sequencing.

## v0.1.9 — 2026-03-17

### Release metadata
- Version bump to `0.1.9` across app/package/UI metadata for clean resubmission.

## v0.1.7 — 2026-03-17

### Drafting UX
- **Optional reply instruction field** in Draft tab now works in both modes (New email + Reply), so you can steer output with explicit guidance.
- **Bookmarklet popup instruction box** added (`Your instruction (optional)`) and passed through to generation APIs.
- **Compose/Reply parity** — stream and non-stream paths both accept `user_prompt` and `mode` consistently.

### Docs + Website Sync
- Updated README, About page, landing page, and Bookmarklet page to match current UI and workflow.
- Removed stale references to removed Review Queue controls (`Bulk submit ready`, `Skip low-signal`, `Compare`) from public docs.

## v0.1.6 — 2026-03-17

### Review Queue Throughput
- **Bulk submit ready** — one-click submit of all ready, non-low-signal drafts in the current batch (default rating 4)
- **Skip low-signal** — one-click skip of low-signal queue items to keep review flow focused
- **Expanded keyboard shortcuts** — added `b` (bulk submit), `n` (bulk skip), and `?` (shortcut help overlay), alongside existing `j/k/e/1-5`
- **Docs/UI sync** — README, About page, and landing page copy updated to reflect new Review Queue workflow

## v0.1.5 — 2026-03-17

### Smarter Drafting
- **Subject line in prompt** — email subject injected into generation context for more topic-grounded drafts
- **Edit pattern analysis** — human edits categorized on save (greeting change, closing change, tone change, length change, content addition/removal); stored in `edit_categories` column for future training signals
- **Prior reply for standalone emails** — most recent sent reply to the same sender used as few-shot context when no thread history is present
- **Expanded tone hints** — "Shorter", "Formal", "Detail" tone buttons in draft popup; `tone_hint` parameter passed through to generation service

### UX
- **Streak tracking** — consecutive daily review days tracked in `review_streaks` table; streak count returned in Review Queue API and shown in UI
- **Corpus scan for facts** — `/scan-corpus-facts` endpoint scans top 100 reply pairs by quality score and bulk-extracts structured facts
- **Undo / duplicate-prevention in Review Queue** — resubmitting a pair that was already reviewed returns `already_submitted` status instead of creating a duplicate
- **Bookmarklet sender auto-detect** — Gmail bookmarklet extracts sender email from the DOM (thread-level → message-level fallback); normalizes "Name <email>" format

### Data Quality
- **Reply quality filtering** — hard address filters (`no-reply`, `noreply`, `donotreply`, etc.) and content-pattern regex (`_AUTOMATED_CONTENT_PATTERNS`) drop transactional/machine-generated emails before queue selection; minimum 20-char reply length enforced
- **Semantic deduplication** — `deduplicate_corpus.py` detects near-duplicate reply pairs with `hybrid_similarity ≥ 0.90`; keeps higher-quality pair per cluster
- **Organic pair quality gate** — `extract_auto_feedback.py` filters pure-acknowledgment replies (< 10 chars or ACK patterns like "ok", "sure", "thanks") before ingesting organic sent-email pairs; assigns neutral rating=3

### Retrieval
- **Exponential recency decay** — recency score is now continuous (`max(0, 1 − days_old/365)`) rather than a binary cutoff; `recency_boost_days` and `recency_boost_weight` exposed as autoresearch-tunable surfaces
- **Exemplar effectiveness tracking** — `exemplar_reply_chars` and `exemplar_inbound_chars` added as mutable autoresearch surfaces, allowing nightly pipeline to tune prompt-context window for exemplars
- **Language-filtered retrieval** — `language` column on `reply_pairs`; retrieval queries filtered to match detected language of inbound email

### Feedback Loop
- **Weighted LoRA training** — `export_feedback_jsonl.py` now applies curriculum ordering (first 20% sorted quality ASC for warmup), 3× oversampling of 5-star recent pairs, 2× for 4-star; DPO preference-pair export supported via `--dpo` flag
- **Expanded autoresearch surfaces** — composite metric weights (`composite_weight_pass_rate`, `_keyword_hit`, `_confidence`) exposed as tunable surfaces; `_normalize_composite_weights()` enforces sum == 1.0

### Observability
- **Edit distance trend chart** — Stats dashboard shows weekly average `edit_distance_pct` for the last 8 weeks
- **Per-sender-type accuracy** — Stats API returns breakdown by sender type (external_client, personal, internal, automated) with review count, avg edit %, and avg rating
- **System Health card** — Stats API includes `system_health` dict: `corpus_size`, `last_ingestion`, `embedding_coverage` %, `adapter_ready` flag

### Edge Cases
- **Short email fallback** — Review Queue candidate selection enforces ≥ 50-char inbound minimum before quality scoring
- **Forwarded email detection** — Emails containing "---------- Forwarded" header filtered out of the review queue
- **Calendar invite handling** — Organic pair capture skips replies < 10 chars, catching calendar accept/decline responses

### Config
- **Auto-detect gog accounts** — Setup wizard calls `gog auth list --json` and suggests detected accounts; falls back to manual entry if none found
- **Auto-detect internal domains** — Setup wizard scans `reply_author` domains in corpus, excludes user domains and common free-email providers, and suggests top recurring domains (≥ 3 occurrences) as internal domain candidates

## v0.1.4 — 2026-03-17

### Fixes
- **Critical: semantic reranking return type** — reranking function returned wrong type, causing silent failures in semantic result ordering; now returns correctly typed scored pairs

### Performance
- Shared DB connection across retrieval calls — eliminates redundant open/close per query
- Legacy query `LIMIT` applied earlier — reduces candidate set before scoring
- Real embedding batching — embeddings now computed in true batches instead of one-at-a-time loops
- Conditional FTS rebuild — FTS index only rebuilt when content has changed, not on every request
- One-pass token trimming — prompt token budget enforced in a single pass instead of repeated truncation loops

### UX
- **Confidence reason banner** — draft UI now shows a human-readable explanation of *why* a draft received its confidence score (e.g. "3 strong exemplars found", "low retrieval — new topic")
- Structured error responses — API errors now return consistent JSON `{error: ..., detail: ...}` instead of bare strings
- Logged history failures — draft history fetch errors are logged with context instead of silently swallowed

### Code quality
- Fixed 45 bare `except` blocks — all now catch specific exception types with appropriate logging
- Extracted shared scoring logic — duplicate scoring code unified into a single helper used across retrieval and generation
- Named constants — magic numbers (score thresholds, limits, weights) replaced with named module-level constants
- Type hints added throughout retrieval and generation service functions

### Retrieval
- Dynamic semantic scaling — semantic score weight scales with corpus size; small corpora rely more on BM25
- Normalized intent scoring — intent match scores normalized to [0, 1] before blending with retrieval scores
- Lower topic overlap threshold — topic overlap required to boost an exemplar reduced, surfacing more relevant pairs

### Draft
- Relative confidence thresholds for exemplars — exemplar selection now uses mean±σ of retrieval scores rather than hardcoded cutoffs

## v0.1.3 — 2026-03-17

### New features
- **Auto fact extraction** — rule-based extractor (`facts_extractor.py`) parses sender notes and feedback notes on save, automatically creating structured facts in the DB. Uses `finditer` for multi-match per note, negation awareness (skips "not prefers", "never available", etc.), confidence scoring per pattern (0.4–0.9), fact deduplication/merging, and LLM (Claude CLI) fallback when rule extraction returns nothing.
- **15+ fact pattern categories**: communication preferences, dislikes/avoidances, scheduling (meeting days, availability, response time), timezone (abbreviations + IANA), identity (title, company, location, preferred name, reports-to), sign-offs, languages, contact metadata (phone, billing email, CC rules), relationship tags (VIP, decision maker, referred by), and project facts (deadline, budget, renewal date, stakeholders).
- **79 unit tests** for the fact extractor covering all pattern categories, negation, span claiming, LLM fallback, and edge cases.
- **Memory routes** (`/api/memory`) — additional memory endpoints wired into main app.

### Improvements
- Facts auto-extracted whenever a sender note or feedback note is saved — no manual fact entry required for structured notes.
- All BaherOS references in shared/UI code unified to YouOS branding.
- Review Queue UX: emails appear instantly; drafts stream in one by one as they generate.
- Draft popup title updated from BaherOS to YouOS.
- Generation service, config, settings, auth: instance path and security improvements.

## v0.1.2 — 2026-03-16

### New features
- **Facts** — context-aware drafting via `/api/facts` CRUD API. Store facts about contacts, projects, and personal preferences (`contact`, `project`, `user_pref` types); facts are injected into generation prompts automatically
- **Unified codebase** — YouOS is now the canonical name for the product; all internal BaherOS references in shared code replaced with YouOS branding
- **Instance-based data paths** — `YOUOS_DATA_DIR` environment variable controls all instance data (database, configs, adapters); each instance in `instances/` is fully isolated

### Improvements
- `templates/draft_popup.html` title updated from BaherOS to YouOS
- `docs/schema.sql` facts table documented

## v0.1.1 — 2026-03-16

### New features
- WhatsApp export ingestion — `youos ingest --whatsapp <path>` to add WhatsApp chats to your corpus
- `youos doctor` — pre-flight health checker with green/red output (Python, gog, mlx_lm, config, disk, port)
- `youos improve --verbose` — step-by-step Rich progress output for the nightly pipeline
- Thread support in Draft UI — paste a full email thread; YouOS extracts the latest message and uses history as context
- Rate limiting — 10 drafts/min per IP on `/feedback/generate` and `/draft/stream`
- Structured autoresearch run log — `var/autoresearch_runs.jsonl` for reliable benchmark trend tracking
- Pipeline failure log — `var/pipeline_last_run.json` with status, timestamp, and errors visible in Stats dashboard
- `youos export` — backup corpus, adapter, and feedback pairs to a tar.gz archive
- `youos quickstart` — lightweight onramp (3 steps) for users who already have gog configured

### Improvements
- Retrieval candidate pool now ordered by recency (`paired_at DESC`) instead of random
- Mobile-responsive UI — feedback and stats pages stack cleanly at ≤768px
- `retrieval.yaml` defaults tuned: `top_k_reply_pairs=8`, `recency_boost_days=60`
- `youos setup` now runs `youos doctor` as step 0 and bails early on failures
- Autoresearch log moved from project root to `var/autoresearch_log.md`
- `youos stats` CLI unified with web stats via shared query layer
- Session tokens persisted to `var/sessions.json` — survive server restarts

### Fixes
- PRIVACY.md contact URL corrected to `DrBaher/youos`
- `.clawhubignore` added to exclude tests, fixtures, `.venv`, and build artifacts from publish
- `gif-frames/` excluded from git and clawhub publish

## v0.1.0 — 2026-03-16 — Initial Release

### Features

- Gmail corpus ingestion via gog CLI
- Writing style analysis and persona detection
- Draft reply generation (local Qwen + Claude fallback)
- FTS5 + semantic retrieval for finding similar past replies
- Web UI with feedback loop and review queue
- Sender-aware drafting with relationship profiles
- LoRA fine-tuning on user feedback
- Autoresearch for automated config optimization
- Nightly pipeline (ingest, feedback, fine-tune, optimize)
- Gmail bookmarklet for one-click drafting
- PIN-based web UI authentication
- Auto-generated benchmarks from corpus
- CLI interface (`youos setup`, `youos draft`, `youos status`, etc.)
- Teardown command for clean data removal
