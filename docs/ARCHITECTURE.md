# YouOS Architecture

## Overview

YouOS is a retrieval-augmented generation (RAG) system that drafts email replies in the user's personal style. It combines full-text search, semantic embeddings, persona modeling, and continuous learning.

## Components

### Ingestion (`app/ingestion/`)
- **Gmail threads**: Live ingestion via `gog` CLI or local JSON imports
- **Google Docs**: Live via `gog` or cached snapshots
- **WhatsApp**: Export file parsing (stubbed)

Each source produces:
- `documents` — the raw source material
- `chunks` — retrieval-sized text segments
- `reply_pairs` — inbound message + user's reply pairs

### Retrieval (`app/retrieval/`)
- **FTS5 full-text search** via SQLite for lexical matching
- **Semantic reranking** using MLX embeddings (Qwen2.5 mean-pooled)
- **Metadata scoring**: recency, source type, account, sender type/domain boosts
- **Mode detection**: work vs personal signal classification

### Generation (`app/generation/`)
- Assembles prompt from: persona config, retrieved exemplars, sender context, thread history
- Calls either local Qwen model (with LoRA adapter) or Claude CLI as fallback
- Returns draft with confidence score and precedent references

### Evaluation (`app/evaluation/`)
- Rule-based scoring: keyword hit rate, brevity, mode match, confidence
- Benchmark cases auto-generated from corpus or loaded from fixtures

### Autoresearch (`app/autoresearch/`)
- Iterative config optimization loop
- Mutates retrieval parameters and prompt variants
- Keeps improvements, reverts regressions
- Runs nightly after ingestion and fine-tuning

### Web UI (`templates/`)
- Feedback collection with draft generation
- Review queue for human-in-the-loop training
- Stats dashboard
- Gmail bookmarklet

## Data flow

```
Gmail → gog CLI → Ingestion → SQLite DB
                                  ↓
                            FTS5 + Embeddings
                                  ↓
Inbound email → Retrieval → Generation → Draft
                                  ↓
                          User edits draft
                                  ↓
                          Feedback pair saved
                                  ↓
                    Export JSONL → LoRA fine-tune
                                  ↓
                    Autoresearch → Config optimization
```

## Database

SQLite with FTS5 virtual tables for full-text search. Schema in `docs/schema.sql`.

Key tables: `documents`, `chunks`, `reply_pairs`, `feedback_pairs`, `benchmark_cases`, `eval_runs`, `sender_profiles`, `ingest_runs`.
