# WhatsApp Export Spec

This defines the expected input shape for the first WhatsApp ingestion implementation. Parsing is intentionally not implemented yet because the rules should be derived from real exports, not guessed.

## Expected Source

- WhatsApp chat export as UTF-8 text
- one message event per line
- media handling deferred
- locale-specific timestamp parsing deferred until real fixtures are available

## Example Shape

```text
12/31/25, 9:41 PM - Alice: Message text
12/31/25, 9:45 PM - Baher: Reply text
```

## Initial Parsing Goals

- detect speaker name
- capture raw timestamp string
- capture message body
- group adjacent messages into retrieval-friendly documents or reply-pair candidates

## Explicit Deferrals

- attachments and media captions
- multiline edge cases across export variants
- locale-specific date formats
- system events such as joins, leaves, encryption notices, and deleted messages
