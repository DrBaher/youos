# YouOS — Path to Autonomous Email Processing

_Generated 2026-05-29 from a 34-agent gap analysis (every gap adversarially verified against the post-b84 code + live baheros behavior). 28 gaps: 9 confirmed, 17 partial (real but partly shipped / mis-stated — corrections folded in), 2 refuted._

## The thesis

The machinery to **act** is nearly there — auto-push is one function call from auto-send. The problem is the agent can't yet be **trusted** to act, for four compounding reasons we can now point to in the code and saw live:

1. **It acts on the wrong signal.** Auto-push gates on the *needs-reply* score (`triage.py:272`), not on whether the *draft is any good*. A perfect "this deserves a reply" verdict + a garbage draft still auto-pushes.
2. **Its drafts are single-shot and un-graded.** In production the `generation` config block is absent, so `multi_candidate` defaults off (`service.py:1310`) — live drafts are greedy single-shot and the voice-match ranking (b72) never runs. There's no per-draft quality score anywhere.
3. **Its precision is uncalibrated and unmeasured on real mail.** The act-decision is an additive heuristic (0.85 ≠ 85% correct); the only learning is a 3-strike per-sender blocklist; the precision harness exists but has never run on real mail. Live proof: it drafted "thanks, I'll check it out" to two newsletters.
4. **The loop trains on the old corpus, not its own mistakes.** 46,973 feedback pairs but ~46,952 are organic rating-3 rows; ~21 are real corrections. The live false positives never become negative signal.

So the path is: **make the act-decision trustworthy (quality + calibration + abstain + verify) _before_ crossing the send boundary, then close the loop so it stays trustworthy unattended.** Crossing to auto-send first would just send the newsletter replies.

## Phase A — Make the act-decision trustworthy (prerequisite for *any* autonomy)

| Gap | Impact | What to build |
|---|---|---|
| **Per-draft quality gate** (send-quality, not needs-reply) | 5 / M | Compute a `quality_score` at generation time (existing `_score_candidate` length/greeting/closing + averaged `voice_match` vs top exemplars — both already implemented, ~0 extra cost) and make auto-push (later auto-send) gate on it. **+ enable `multi_candidate` in production** so voice-ranking actually runs. |
| **Generic-acknowledgment detector** | 3 / S | Flag "thanks for the update / I'll check it out / got it" drafts (no concrete content/commitment) and drive their quality_score to zero. Directly kills the live false positives. |
| **Calibrated needs-reply + first-class abstention** | 5 / L | Calibrate to empirical P(needs-reply) from keep/dismiss/sent history; a narrow conjunctive **ACT** band vs DRAFT/SURFACE; auto-act only above a precision target measured on held-out **real** mail. |
| **LLM adjudication on borderline cases** | 3 / M | The warm model is right there — a constrained `model_server.complete` "is this a personal reply or a broadcast?" veto on borderline scores. Catches newsletters the regex misses. |
| **Self-critique / verify-before-accept** | 5 / L | After generation: cheap deterministic checks (every inbound question addressed? language match? no invented date/email/price absent from the inbound or facts?) then optional model self-check. Gate the action on it. |
| **Fact grounding in the sweep** | 3 / M | The sweep never populates the facts table (`extract_and_save` only runs on manual paths), so the model dodges/invents availability/addresses. Wire fact extraction into `_run_sweep`; add a "state only grounded facts, else ask" prompt rule. |
| **Run precision/recall on real mail + track it over time** | 4 / M | Build the harness corpus from decided `agent_pending_drafts` (kept/sent = positive, dismissed-noise/wrong-content = negative) + one-tap labels in /triage; run nightly; record precision so the false-positive problem is visible to you *and* to autoresearch. |

## Phase B — The send frontier (cross the never-send boundary, safely)

| Gap | Impact | What to build |
|---|---|---|
| **A send path + honest state model** | 5 / M | `send_draft` wrapping `gog gmail drafts send`; distinguish `draft_created` / `send_scheduled` / `actually_sent` (today `status='sent'` is overloaded for both "drafted" and "I sent it elsewhere"); `agent.auto_send.enabled` default **false**; add `--gmail-no-send` to read/draft gog calls as defense-in-depth. |
| **Confidence×stakes escalation** | 5 / M | Make confidence *draft-aware* (penalize empty-output/fallback/low voice-match), map confidence × stakes → auto-act / queue / **ask** / skip; never auto-send when a high-stakes predicate (client, legal, money) matches. |
| **Delayed-send / undo window + shadow soak + kill-switch** | 2 / M | A policy ladder: draft-queue → auto-draft → **shadow-send** (log-only soak, N days) → auto-send-to-known-after-delay. Per-recipient trust counter; `agent.outbound_kill_switch`. |

## Phase C — Keep it trustworthy unattended (close the loop + survive)

| Gap | Impact | What to build |
|---|---|---|
| **Capture real draft-outcome feedback** | 3 / M | Mine the queue lifecycle: dismissed → negative pair; pushed-and-sent-unchanged → strong positive; sent-after-heavy-edit → correction pair (generated vs final). So it learns from *its own* mistakes, not just the old corpus. |
| **Validate / harden the golden-eval gate** | 3 / M | The b66 fix routes real drafts through the warm path; confirm on the next nightly and **fail loud** if >half the cases return empty (today an all-empty eval scores 0.0 → the b75 regression gate can't reject). |
| **Auto-recovery + proactive alerting** | 3 / S | On ingestion/gog-auth failure: classify via `_gog_auth_warning`, attempt a bounded token refresh, else **alert** (notify/webhook) — not just a WARN in a log. Fire an alert when a sweep's cloud-fallback/empty-output rate spikes. Add a pipeline watchdog (last-successful-run age). |

## Phase D — Whole-inbox + the human-agent contract

| Gap | Impact | What to build |
|---|---|---|
| **Generalize auto_push → an agent-action framework** | 2 / M | An `agent_actions` table + dispatcher applying uniform dry-run/whitelist/floor/daily-cap/**undo** per action type — so auto-archive / label / forward reuse the same guardrails (as *drafts* first, inside never-act). |
| **Richer policy grammar + corrections→policy** | 3 / M | `agent.rules` can't express content ("anything mentioning legal/$") or recipient-class predicates, or `hold`/`ask` actions. Add them; extend the existing skip-sender corrections-proposer to suggest these rules from your dismissal/edit patterns (propose-then-confirm). |
| **Approval-by-reply + daily accountability report** | 2–3 / M | A `needs_decision` state + single-use decision token + inbound approve/reject/edit endpoint (Telegram buttons) so the human handles exceptions without opening /triage. A scheduled "here's what I did / what needs you" report (the webhook + digest are the building blocks). |

## Already shipped (don't rebuild — b66–b84)
Idempotent push + atomic claim, per-account sweep lock, failed-sweep audit + alerts, heartbeat, DB busy_timeout/WAL; new-content-only classifier + German/transactional fix + VIP routing + thread context + voice-match *ranking* (when multi_candidate on); tiered **auto-push** (dry-run, whitelist, floor, min-pairs, cap); follow-ups; rules engine; webhook push; calendar proposals; long-thread summaries; adapter regression gate; autoresearch fixed end-to-end; triage precision/recall *harness* (fixture-based).

## The single highest-leverage next step
**The per-draft quality gate (Phase A #1).** Everything downstream — auto-send, whole-inbox actions, the human contract — depends on the agent *knowing when its own output is good enough to act*. And it's mostly wiring together pieces we already built (`voice_match` + `_score_candidate` + a generic-ack detector), then making auto-push depend on it instead of the needs-reply score.

## Status — Phases A–D shipped (b85–b99, 2026-05-29)

- **Phase A (trust) — done.** b85 per-draft quality gate; b86 borderline LLM adjudication (broadcast veto); b87 real-mail precision harness + nightly snapshot; b88 needs-reply score calibration (isotonic, dormant until data); b89 verify-before-accept (invented email/link/language → collapse quality); b90 fact grounding (`[GROUNDING]` prompt rule + sweep fact harvest).
- **Phase B (send frontier, hard-gated off) — done.** b91 send path + honest `send_state` + kill-switch; b92 confidence×stakes escalation; b93 autonomous auto-send ladder (delay window + per-recipient trust + shadow-default). Default config still never sends.
- **Phase C (close the loop) — done.** b94 queue-lifecycle feedback capture; b95 golden-eval degeneracy guard; b97 proactive alerting (failure classification + sweep-health spikes, macOS + webhook).
- **b96 audit fixes.** A 30-agent adversarial review of A+B confirmed 11 real bugs — all fixed (incl. the feedback manual-send positive, a `begin_send` TOCTOU, a daily-send cap, and a stale-`sending` reaper).
- **Phase D (whole-inbox + human contract) — partial.** b98 richer policy grammar (`subject_contains`/`body_contains` + a `hold` action that drafts but never auto-acts); b99 daily accountability report surfaces auto-sent/shadow-sent.

**Deliberately deferred (with rationale):**
- *Generalize auto_push → an agent_actions framework (archive/label/forward).* YAGNI for now — YouOS performs no inbox action besides drafting, so a dispatcher for actions that don't exist would be premature abstraction. The guardrail pattern (dry-run/whitelist/floor/cap/undo) is already proven in auto-push and can be extracted when a second action type lands.
- *Approval-by-reply (Telegram decision tokens).* Depends on inbound-webhook handling in the orchestrator layer (Hermes/OpenClaw), not in YouOS core; build it where the chat surface lives.
