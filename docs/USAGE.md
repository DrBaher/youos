# YouOS Usage Guide

## First-time setup

```bash
./scripts/install.sh      # creates .venv, installs YouOS (+ MLX on Apple Silicon), runs the doctor
source .venv/bin/activate
youos doctor              # verify Python, Google backend, MLX, disk space
youos setup               # or open http://127.0.0.1:8901/welcome in the browser
```

The setup wizard configures your identity (and personalizes the app's name from your
first name — e.g. Baher → BaherOS), your Google ingestion backend, ingests your sent
history, and analyzes your writing style.

## Drafting replies

Drafts run on your **fine-tuned local model by default** (Qwen + your LoRA, served warm).
The cloud is only a cold-start before your model is trained, or an explicit fallback. A
**readiness gate** shows a soft "preparing your voice model" banner until your model is
trained *and* benchmarked — drafting still works meanwhile.

### Via CLI
```bash
youos draft "paste the inbound email here"
youos draft --sender john@company.com --mode work "email text"
```

### Via Web UI
1. Run `youos serve` to start the server
2. Open http://127.0.0.1:8901/feedback
3. Paste the inbound email, click Generate Draft (a per-draft badge shows which model ran)
4. Edit the draft as needed, then Submit Feedback

### Inside Gmail (browser extension)
1. Install the YouOS extension from the repo's `extension/` folder — the web UI's Gmail
   page (`/bookmarklet`) has one-click "Load unpacked" steps
2. Open an email → click the teal ✉ launcher → the panel opens
3. Sender + message auto-detected; add an instruction or pick a tone, click Generate
4. **Insert into Gmail** drops the draft in the reply box; rate 1–5 and Submit feedback

A bookmarklet remains as a no-install fallback (it can break when Gmail changes its markup).

## Review Queue

The Review Queue shows real emails from your corpus with auto-generated drafts. Edit and rate each draft to train the model:

1. Open the web UI, click "Review Queue" tab
2. Review each draft — edit to match what you'd actually send
3. Rate 1-5 stars and submit
4. After 10 reviews, the system triggers autoresearch optimization

## Which model sounds most like you?

```bash
# Drafts your held-out replies under each backend and scores them against what you
# actually wrote (voice-match), so you can verify the local model beats the cloud.
youos compare-models --limit 30 --semantic
```

## Commands

| Command | Description |
|---------|-------------|
| `youos setup` | Run setup wizard |
| `youos doctor` | Check system requirements (Python, Google backend, MLX, disk) |
| `youos status` | Show system status |
| `youos draft "text"` | Generate a draft reply |
| `youos serve` | Start the web server |
| `youos ui` | Open the web UI in your browser |
| `youos service install` | Run the server as a background service (starts at login) |
| `youos stats` | Print corpus statistics |
| `youos corpus` | Full corpus health report (pairs, quality, top senders) |
| `youos improve [--verbose]` | Run nightly pipeline manually |
| `youos ingest` | Ingest new emails (`--whatsapp <export>` for a chat export) |
| `youos finetune` | Run LoRA fine-tuning |
| `youos eval [--golden]` | Run benchmark evaluation |
| `youos compare-models` | Rank backends by how closely each sounds like you |
| `youos model show` / `model set` | Show / choose the drafting backend |
| `youos model server status` | Warm local-model server (loads the model once) |
| `youos config list` / `get` / `set` | View or change feature flags |
| `youos note <email> "text"` | Add a sender relationship note |
| `youos feedback ...` | Submit a feedback pair from the terminal |
