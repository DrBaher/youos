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

# View stats
youos stats

# Teardown (remove all data, keep code)
youos teardown
```

## How it works

1. Ingests your sent Gmail history, Google Docs, and WhatsApp exports (all stays local, never uploaded)
2. Builds a retrieval index of your real past replies — BM25 + semantic embeddings (LRU-cached) + subject-line signal + sender-type boosts + quality scores from your feedback
3. When you ask for a draft: retrieves the most similar past replies, assembles up to 5 few-shot exemplars, generates a reply in your style — supports full email threads; shows a low-confidence warning when precedents are weak; subject line extracted using rule-based fallback (no Claude required)
4. Every email you review trains the model further — quality-filtered, temporally split, auto-calibrating threshold; hyperparameters auto-scale with corpus size; persona config updated nightly from corpus patterns
5. Nightly: ingests new emails, re-analyzes persona, fine-tunes the local Qwen model, runs autoresearch (80 iterations) to optimize retrieval weights, sender boosts, prompt variants, and composite score weights
6. Your best-rated, least-edited replies automatically surface higher in future retrievals via quality scoring
7. Sender profiles track avg reply-time patterns; `youos note` immediately rebuilds that contact's profile

## Privacy

All data stays on your machine. No email content is ever sent to a cloud service (except the LLM call for initial drafts before the local model is trained). See PRIVACY.md.
