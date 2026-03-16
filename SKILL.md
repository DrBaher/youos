---
name: youos
description: >
  YouOS — your personal AI email copilot. Learns your writing style from your Gmail history,
  Google Docs, and WhatsApp exports, then drafts replies that sound like you. Improves
  automatically from your feedback via nightly LoRA fine-tuning and autoresearch.
  Runs entirely locally on Apple Silicon. Use when: drafting email replies, reviewing
  how you've handled similar situations before, or setting up a self-improving personal
  communication assistant.
homepage: https://github.com/DrBaher/youos
version: 0.1.0
metadata:
  openclaw:
    emoji: "\u2709\uFE0F"
    requires:
      anyBins: ["gog", "python3"]
      env: []
---

# YouOS — Personal Email Copilot

YouOS drafts email replies in your style, grounded in your real past replies.

## Trigger phrases

- "draft a reply to this email"
- "write this email for me"
- "how would I respond to this"
- "what would I say to"
- "help me reply"
- "draft in my style"
- "youos"
- "my email copilot"
- "email copilot"
- "my copilot"
- "generate a draft"
- "reply draft" / "email draft" / "draft reply"
- "compose reply"
- "write a response"
- "email response"
- "how do I usually reply to"
- "reply to this"
- "help me write"
- "write an email"
- "compose a response"
- "email assistant"
- "my writing style"
- "train on my emails"

## Requirements

- Apple Silicon Mac (M1/M2/M3/M4) with 8GB+ RAM (16GB recommended)
- Python 3.11+
- [gog CLI](https://github.com/openclaw/gog) configured with your Gmail account(s)
- ~5GB free disk space

## Quick start

```bash
# Install
cd ~/Projects/youos
pip install -e .

# Check system requirements (Python, gog CLI, disk space, etc.)
youos doctor

# Run setup wizard (15 min, mostly ingestion)
youos setup

# Draft a reply
youos draft "paste inbound email here"
youos draft --sender john@company.com "email text"

# Open web UI
youos ui

# Check status
youos status

# Run nightly pipeline manually (add --verbose for step-by-step output)
youos improve
youos improve --verbose

# Run golden benchmark evaluation (8 curated test cases)
youos eval --golden

# Full corpus health report (pairs, quality scores, top senders)
youos corpus
youos corpus --json

# Ingest a WhatsApp chat export (optional — augments your corpus)
youos ingest --whatsapp ~/Downloads/WhatsApp-Chat.txt

# Add sender note (immediately rebuilds their profile)
youos note john@company.com "integration partner, prefers bullet points"

# Submit a feedback pair directly from the terminal
youos feedback --inbound "email text" --reply "your reply" --rating 4

# View stats
youos stats

# Teardown (remove all data, keep code)
youos teardown
```

## How it works

1. Ingests Gmail, Google Docs, WhatsApp exports — plus organic pairs from emails you sent without YouOS
2. Builds a retrieval index — BM25 + query expansion + semantic (LRU-cached) + multi-intent + per-account isolation + same-thread 2× + subject + topic signals + sender-type boosts + quality scores + relative confidence thresholds
3. When you ask for a draft: detects multi-intent, retrieves score-ranked thread-deduplicated exemplars (reply preserved 600 chars, inbound trimmed 400), prompt token budget enforced; generates using per-mode persona with first-name greeting; local model empty output falls back to Claude
4. Every email you review trains the model further — curriculum-ordered, quality-filtered, training pairs deduplicated by similarity, DPO pairs supported; nightly pipeline skips steps when data insufficient
5. Nightly: ingests + organic pairs, incremental persona re-analysis (90-day weighted, EWMA avg words, p25/p75 confidence intervals), fine-tunes (with golden eval check), runs autoresearch on rotating benchmark sample
6. Autoresearch benchmarks rotate weekly (seeded re-sample) — prevents overfitting to fixed test cases; golden eval composite tracked in pipeline log
7. Style drift detection: Stats dashboard flags when your writing patterns shift significantly
8. Your best-rated, least-edited replies surface higher in future retrievals via quality scoring
9. Sender profiles track reply-time patterns and topics; `youos note` immediately rebuilds that contact's profile
10. Submit feedback from terminal: `youos feedback --inbound "..." --reply "..." --rating 4`
11. Setup wizard asks for internal domains — accurate sender classification from day one

## Privacy

All data stays on your machine. No email content is ever sent to a cloud service (except the LLM call for initial drafts before the local model is trained). See PRIVACY.md.
