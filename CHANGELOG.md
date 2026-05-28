# Changelog

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
