# Changelog

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
