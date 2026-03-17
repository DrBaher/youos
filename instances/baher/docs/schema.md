# Schema Notes

The initial schema is optimized for a local-first MVP using SQLite. It keeps ingestion, retrieval preparation, supervised reply examples, and evaluation data separated so retrieval and prompting can evolve without reworking the storage model.

## Tables

### `documents`

Stores normalized source documents such as Gmail inbound messages, Google Docs, or future WhatsApp conversations.

### `chunks`

Stores retrieval units derived from `documents`. Embeddings are not yet included because the MVP scaffold does not implement vectorization yet.

### `reply_pairs`

Stores paired examples where an inbound message or message span is linked to a Baher-authored reply. These are a first-class corpus type because they are likely the highest-signal source for style transfer and response drafting.

### `ingest_runs`

Stores one persistent record per ingestion attempt, including the source (`gmail` or `google_docs`), accounts, start/end timestamps, final status, discovery/fetch/store counts, and any error summary/detail needed to inspect failures after the fact.

### `benchmark_cases`

Stores benchmark prompts and expected behaviors for retrieval and generation evaluation.

### `eval_runs`

Stores run metadata and aggregate outputs from evaluation jobs.

## Design Notes

- `source_type` is explicit to support corpus-specific ingestion logic.
- raw content is stored locally for traceability in MVP.
- reply pairs can reference a `document_id` when the pair came from an ingested thread.
- evaluation is persisted early so future autoresearch work can tune retrieval, prompting, and routing against stable cases.
