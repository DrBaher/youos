# Changelog

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
