# Google Docs Ingestion

BaherOS ingests authored Google Docs into the shared local-first corpus schema:

- one `documents` row per Doc
- one or more `chunks` rows per Doc for retrieval

This implementation is intentionally latest-content only. It does not ingest revision history.

## Live `gog` Commands

BaherOS uses only the following real `gog` commands:

- `gog drive search <query>`
- `gog docs info <docId>`
- `gog drive get <docId>`
- `gog docs cat <docId>`

Discovery happens through Drive because `gog docs` does not provide a list command in the installed CLI.

## Current Live Limitation

Live Docs ingestion is currently blocked when the Google Drive API is disabled for the `gog`
project/environment. BaherOS now reports that failure explicitly, but it cannot work around it:

- `gog drive search` fails before discovery can begin
- BaherOS does not fake a successful live import in this case
- the practical fallback is replaying cached BaherOS snapshot JSON until Drive API access is enabled

## Exact Usage

Authorize the target accounts:

```bash
gog login drbaher@gmail.com
gog login baher@medicus.ai
```

Ingest Docs discovered from Drive:

```bash
python3 -m scripts.ingest_google_docs --live \
  --query "mimeType = 'application/vnd.google-apps.document' and trashed = false" \
  --max-docs 50 \
  --cache-dir data/google_docs_live
```

Ingest only owned Docs for one account:

```bash
python3 -m scripts.ingest_google_docs --live \
  --account drbaher@gmail.com \
  --query "mimeType = 'application/vnd.google-apps.document' and 'me' in owners and trashed = false" \
  --max-docs 25 \
  --cache-dir data/google_docs_live
```

Ingest explicit Doc ids:

```bash
python3 -m scripts.ingest_google_docs --live \
  --account drbaher@gmail.com \
  --doc-id 1AbCdEfGhIjKlMn \
  --doc-id 9ZyXwVuTsRqPoNm
```

Replay cached BaherOS snapshot JSON:

```bash
python3 -m scripts.ingest_google_docs data/google_docs_live
```

## Stored Metadata

BaherOS stores the following when available from `gog`:

- `account_email`
- `source`
- `doc_id`
- `title`
- `created_at`
- `updated_at`
- `author` / owner
- external Doc URL

Additional adapter payloads are preserved in `metadata_json`:

- `docs_info`
- `drive_file`
- `search_result`
- `query`
- `fetched_at`

## Adapter Notes

The adapter merges metadata from:

- `gog docs info` for Docs-specific fields such as title when present
- `gog drive get` for Drive file metadata such as owners, modified time, and web URL when present

If a field is absent from both command outputs, BaherOS leaves it unset and records the gap in `metadata_json.missing_fields`.

## Deferred

Not implemented in this phase:

- revision history ingestion
- incremental sync checkpoints
- Drive discovery pagination beyond `gog drive search --max <n>`
- deletion reconciliation
- arbitrary third-party local export formats

WhatsApp is the next obvious ingestion module after Gmail and Docs.
