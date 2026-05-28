# YouOS Architecture

## Overview

YouOS is a retrieval-augmented generation (RAG) system that drafts email replies in the user's personal style. It combines full-text search, semantic embeddings, persona modeling, an on-device fine-tuned model, and continuous learning.

## Components

### Ingestion (`app/ingestion/`)
- **Gmail threads & Google Docs**: via a configurable Google backend (`ingestion.google_backend`) — `gog` CLI (default), Google's `gws` CLI, or the native Google API (`youos[google]` + OAuth); local JSON imports also supported
- **WhatsApp**: export-file parsing
- **Organic pairs**: replies you sent without YouOS are captured as training pairs too

Each source produces:
- `documents` — the raw source material
- `chunks` — retrieval-sized text segments
- `reply_pairs` — inbound message + user's reply pairs

### Retrieval (`app/retrieval/`)
- **FTS5 / BM25 full-text search** via SQLite for lexical matching
- **Semantic reranking** using MLX embeddings (Qwen2.5 mean-pooled, LRU-cached)
- **Metadata scoring**: recency, source type, account isolation, same-thread boost, subject/topic signals, sender type/domain boosts, quality scores
- **Mode detection**: work vs personal signal classification; multi-intent detection

### Generation (`app/generation/`)
- Assembles prompt from: per-mode persona config, retrieved exemplars, sender context, thread history, and relevant facts
- Drafts on the **fine-tuned local model by default** (Qwen + your LoRA). The cloud
  (Claude) is only a cold-start before your model is trained, or an explicit fallback
  (`review.draft_model` / `model.fallback`)
- Returns a draft with confidence score, precedent references, and the model label that
  actually produced it (surfaced as a per-draft badge and in the Stats "Drafting with" row)

### Warm model server (`app/core/model_server.py`)
- Wraps `mlx_lm.server` (OpenAI-compatible HTTP) so the local model + LoRA adapter load
  **once** and stay warm — fast, on-device drafting and streaming without per-draft cold starts
- Reloads automatically when the adapter changes (`model.server`, default enabled)

### Evaluation (`app/evaluation/`)
- **Golden benchmark**: rule-based scoring (keyword hit rate, brevity, mode match, confidence) over curated cases; cases auto-generated from corpus or loaded from fixtures
- **Voice-match** (`voice_match.py`, `model_compare.py`): scores a draft against the reply you actually wrote (lexical / length / style / semantic), powering `youos compare-models` so you can verify which backend sounds most like you
- **Readiness gate**: drafting is held behind a soft banner until the model is trained *and* benchmarked

### Autoresearch (`app/autoresearch/`)
- Iterative config optimization loop
- Mutates retrieval parameters and prompt variants
- Keeps improvements, reverts regressions
- Runs nightly after ingestion and fine-tuning, on a weekly-rotating benchmark sample (prevents overfitting to fixed cases)

### Autonomous triage (`app/agent/`) — opt-in
Background loop that sweeps unread inbox, filters noise, and queues drafts for review. Never auto-sends.

- **`inbox_fetch.py`** — pulls unread messages from each enabled account via the configured Google backend (gog/gws/native), bounded by `agent.window` and `agent.limit`
- **`needs_reply.py`** — two-tier classifier. Hard-skip rules (per-sender skip list → List-Unsubscribe → mailer-daemon → automation domains → CI subjects → empty body) eliminate noise; soft penalties (noreply, operational mailboxes, very long digests, cold outreach) reduce score without rejecting outright. Returns `NeedsReplyVerdict(needs_reply, score, reasons, cold_outreach, surface_for_review)` — borderline scores (0.30–0.59) surface for review without being drafted
- **`triage.py`** — orchestrates a sweep: fetch → classify → draft survivors (threading `agent.standing_instructions` into the prompt via `extra_constraint`, honoring `agent.strict_local` and `agent.daily_draft_cap`) → persist to `agent_pending_drafts` → log one row to `agent_audit`
- **`scheduler.py`** — `_loop(app)` wakes every `agent.interval_minutes`, re-reads config each iteration so flag changes take effect without restart; macOS notification on new drafts. No-ops under `PYTEST_CURRENT_TEST`
- **`store.py`** — pending-draft CRUD (idempotent on `message_id`), daily-cap counter, and sweep audit log

The `/triage` page shows the pending queue (Save edits, Push to Gmail Drafts, Copy, Mark sent, Dismiss with optional categorical reason), surface-for-review collapsibles, and Recent activity. `Push to Gmail Drafts` (`app/ingestion/gmail_write.py`) creates a real Gmail Draft on the original thread via the configured backend — `gog` (Phase 2.1) and `gws` (Phase 2.2) both implemented; `native` raises NotImplementedError until the `gmail.compose` OAuth scope lands (Phase 2.3). Dismissals carry an optional categorical reason (`noise` / `wrong_sender` / `wrong_content` / `already_handled` / `other`) aggregated by `GET /api/agent/dismissal_stats`.

All seven `agent.*` flags (`enabled`, `interval_minutes`, `accounts`, `window`, `limit`, `threshold`, `notify_macos`, `standing_instructions`, `skip_senders`, `daily_draft_cap`, `strict_local`) are toggleable via `/settings` or `youos config set`.

### Web UI (`templates/`)
- Feedback collection with streaming draft generation + cold-start loading overlay
- Review queue for human-in-the-loop training
- Stats dashboard (model status, by-model breakdown, style-drift detection, troubleshooting)
- Settings (`/settings`) for feature flags, About (`/about`), setup wizard (`/welcome`)
- Gmail extension page + bookmarklet fallback

## Data flow

```
Gmail / Docs / WhatsApp ─► Ingestion ─► SQLite DB
   (gog / gws / native)                    │
                                  FTS5 + Embeddings
                                           │
Inbound email ─► Retrieval ─► Generation ─► Draft
                              (local model,    │
                               warm server)    │
                                       User edits draft
                                           │
                                   Feedback pair saved
                                           │
                            Export JSONL ─► LoRA fine-tune
                                           │
                            Autoresearch ─► Config optimization
```

## Database

SQLite with FTS5 virtual tables for full-text search. Schema in `docs/schema.sql`.

Key tables: `documents`, `chunks`, `reply_pairs`, `feedback_pairs`, `benchmark_cases`, `eval_runs`, `sender_profiles`, `ingest_runs`, `facts`, `agent_pending_drafts`, `agent_audit`.
