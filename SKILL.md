---
name: youos
description: >
  YouOS — your personal AI email copilot. Learns your writing style from your Gmail history
  and drafts replies that sound like you. Improves automatically from your feedback.
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
- "generate a draft"
- "reply draft" / "email draft" / "draft reply"
- "compose reply"
- "write a response"
- "email response"
- "how do I usually reply to"
- "reply to this"

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

# Run setup wizard (15 min, mostly ingestion)
youos setup

# Draft a reply
youos draft "paste inbound email here"
youos draft --sender john@company.com "email text"

# Open web UI
youos ui

# Check status
youos status

# Run nightly pipeline manually
youos improve

# Add sender note
youos note john@company.com "integration partner, prefers bullet points"

# View stats
youos stats

# Teardown (remove all data, keep code)
youos teardown
```

## How it works

1. Ingests your sent Gmail history (stays local, never uploaded)
2. Builds a retrieval index of your real past replies
3. When you ask for a draft: retrieves the most similar past replies and generates a new one in your style
4. Every email you review trains the model further
5. Nightly: ingests new emails, fine-tunes the local Qwen model, runs autoresearch to optimize retrieval

## Privacy

All data stays on your machine. No email content is ever sent to a cloud service (except the LLM call for initial drafts before the local model is trained). See PRIVACY.md.
