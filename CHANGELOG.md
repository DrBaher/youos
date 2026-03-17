# Changelog

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
