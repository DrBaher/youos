# YouOS Usage Guide

## First-time setup

```bash
pip install -e .
youos setup
```

The setup wizard configures your email accounts, ingests your sent history, and analyzes your writing style.

## Drafting replies

### Via CLI
```bash
youos draft "paste the inbound email here"
youos draft --sender john@company.com --mode work "email text"
```

### Via Web UI
1. Run `youos serve` to start the server
2. Open http://localhost:8765/feedback
3. Paste the inbound email, click Generate Draft
4. Edit the draft as needed, then Submit Feedback

### Via Gmail Bookmarklet
1. Visit http://localhost:8765/bookmarklet
2. Drag the bookmarklet to your browser bar
3. Open any email in Gmail, click the bookmarklet

## Review Queue

The Review Queue shows real emails from your corpus with auto-generated drafts. Edit and rate each draft to train the model:

1. Open the web UI, click "Review Queue" tab
2. Review each draft — edit to match what you'd actually send
3. Rate 1-5 stars and submit
4. After 10 reviews, the system triggers autoresearch optimization

## Commands

| Command | Description |
|---------|-------------|
| `youos setup` | Run setup wizard |
| `youos status` | Show system status |
| `youos draft "text"` | Generate a draft reply |
| `youos ui` | Open web UI in browser |
| `youos serve` | Start the web server |
| `youos stats` | Print corpus statistics |
| `youos improve` | Run nightly pipeline manually |
| `youos ingest` | Ingest new emails |
| `youos finetune` | Run LoRA fine-tuning |
| `youos eval` | Run benchmark evaluation |
| `youos note email "text"` | Add sender relationship note |
