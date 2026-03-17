# Gmail Thread Import Spec

BaherOS supports both live Gmail ingestion via `gog` and local JSON thread ingestion.

## Supported Inputs

Use one of these:

- live Gmail fetched via `gog gmail search` and `gog gmail thread get --full`
- a directory containing one or more `.json` files
- a single `.json` file containing one thread
- a single `.json` file containing `{"threads": [...]}`

Each thread must contain a thread id and a `messages` list.

## Preferred Normalized Format

```json
{
  "thread_id": "thread-123",
  "account": "baher@example.com",
  "source": "json_import",
  "subject": "Project follow-up",
  "messages": [
    {
      "id": "msg-1",
      "timestamp": "2026-03-01T09:00:00Z",
      "from_email": "alice@example.com",
      "from_name": "Alice",
      "to": [{"email": "baher@example.com", "name": "Baher"}],
      "body_text": "Can you send the draft?"
    },
    {
      "id": "msg-2",
      "timestamp": "2026-03-01T09:15:00Z",
      "from_email": "baher@example.com",
      "from_name": "Baher",
      "label_ids": ["SENT"],
      "body_text": "Attached. Let me know what you want changed."
    }
  ]
}
```

## Also Accepted

Saved Gmail API `users.threads.get` response JSON is accepted as a local import source. The importer reads:

- `threadId`
- `id`
- `messages[].id`
- `messages[].labelIds`
- `messages[].internalDate`
- `messages[].payload.headers`
- `messages[].payload.body.data`
- `messages[].payload.parts`

If `body_text` is present in a normalized dump, it takes precedence over payload decoding.

## Live Gmail via gog

The live path is deliberately narrow:

1. Run `gog gmail search <query>` for each selected account.
2. Extract thread ids from the search results.
3. Run `gog gmail thread get <threadId> --full` for each matched thread.
4. Normalize each fetched thread into the same internal schema used by local JSON imports.

Recommended usage:

```bash
gog login drbaher@gmail.com
gog login baher@medicus.ai

python3 -m scripts.ingest_gmail_threads --live \
  --query 'in:anywhere newer_than:90d' \
  --max-threads 50 \
  --cache-dir data/gmail_live
```

Honest limitations:

- this is query-based ingestion, not an incremental Gmail sync with history checkpoints
- BaherOS accepts both direct Gmail-style thread payloads and the nested `gog` adapter envelope shape where the thread lives under keys like `thread` or `result`
- if `gog gmail thread get --full` returns `messages` without a thread id, BaherOS uses the requested thread id and records an `ingestion_warnings` entry instead of pretending the payload was complete
- if `gog gmail search` omits a usable thread id entirely, the live run fails with that raw result called out in the error

## Message Classification

A message is treated as Baher-authored when any of these are true:

- `label_ids` or `labelIds` contains `SENT`
- `is_baher` is `true`
- `self_authored` is `true`
- `author_role` or `mailbox_role` is `self`, `me`, or `baher`
- the sender matches `--baher-email` or `--baher-name`
- for live `gog` ingestion, the sender matches the selected account email

All non-self messages with non-empty text are treated as inbound.

The importer preserves enough metadata to support:

- source path: `json_import` or `gog_gmail`
- account email used for live fetches
- thread id and message id
- sender context
- recipient context from direct fields or Gmail headers (`To`, `Cc`, `Bcc`, `Reply-To`)
- label ids and selected Gmail metadata fields

## Reply Pair Extraction

Within each thread, messages are ordered by timestamp and original file order.

- Each inbound message is stored as a `documents` row and a single full-message `chunks` row.
- Consecutive inbound messages accumulate until the next Baher-authored message.
- That next Baher-authored message becomes the reply for one `reply_pairs` row.
- The pair anchors to the latest inbound document in that inbound span.

This means the inbound side can represent either one inbound message or a short inbound span from the same thread.

Reply-pair metadata stores:

- account/source
- inbound message ids
- reply message id
- inbound and reply recipient context
- reply labels
- pair strategy: `messages_since_last_self_authored_message`

## Example Fixture

See [fixtures/gmail/sample_thread.json](/Users/bbot/Projects/baheros/fixtures/gmail/sample_thread.json).
