---
name: youos
description: >
  YouOS — local-first personal email copilot that learns your writing style from Gmail,
  Google Docs, and WhatsApp exports, then drafts replies in your voice. Use for drafting
  replies, reviewing how you usually respond, and running a self-improving personal
  communication workflow.
metadata:
  openclaw:
    requires:
      bins: ["python3", "gog"]
      platform: darwin
      arch: arm64
      minRam: "8GB"
    install:
      - kind: instructions
        label: "Manual install required"
        steps:
          - "python3 -m venv .venv"
          - "source .venv/bin/activate"
          - "pip install -e ."
    credentials:
      required:
        - "gog must be authenticated for Gmail/Docs access"
      optional:
        - "Claude CLI/API credentials only if model.fallback uses external provider"
    privacy:
      localStorage:
        - "SQLite DB under YOUOS_DATA_DIR/var/youos.db (or local var/youos.db)"
      networkEgress:
        - "None by default for local mode"
        - "Optional outbound requests when external fallback is enabled"
---

# YouOS — Personal Email Copilot

YouOS is a full local Python app (not an instruction-only snippet). It drafts email replies in your style, grounded in your real past replies.

## Install and runtime model

- Install is **manual** via pip (`pip install -e .`) in a Python 3.11+ environment
- Note: `pip install -e .` executes local package install code from this repository; review source before installing
- Requires **both**:
  - `python3` (3.11+)
  - `gog` CLI authenticated to the Gmail account(s) you want to ingest
- Optional runtime path override: `YOUOS_DATA_DIR`

## Credentials and configuration

- Required: `gog` authentication for Gmail/Docs ingestion
- Optional: Claude CLI/API credentials only if using external fallback generation
- Recommended for local-only privacy: set `model.fallback: none`

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
- Run the UI locally by default (do not expose publicly unless intentionally secured)

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

## Gmail Bookmarklet

Install from the Bookmarklet page in the web UI. Once installed:
- Click bookmarklet on any Gmail thread → floating panel opens on the right
- Click **Generate Draft** → draft appears in the panel
- Click **Insert into Gmail** → draft injected into compose window
- Rate the draft with stars and submit feedback — all without leaving Gmail
- Click the bookmarklet again to close the panel

## How it works

1. Ingests Gmail, Google Docs, WhatsApp exports — plus organic pairs from emails you sent without YouOS
2. Builds a retrieval index — BM25 + query expansion + semantic (LRU-cached) + multi-intent + per-account isolation + same-thread 2× + subject + topic signals + sender-type boosts + quality scores + relative confidence thresholds
3. When you ask for a draft: detects multi-intent, retrieves score-ranked thread-deduplicated exemplars (reply preserved 600 chars, inbound trimmed 400), prompt token budget enforced; generates using per-mode persona with first-name greeting; local model empty/signature-only output falls back to Claude automatically
4. Every email you review trains the model further — curriculum-ordered, quality-filtered, training pairs deduplicated by similarity, DPO pairs supported; nightly pipeline skips steps when data insufficient
5. Nightly: ingests + organic pairs, incremental persona re-analysis (90-day weighted, EWMA avg words, p25/p75 confidence intervals), fine-tunes (with golden eval check), runs autoresearch on rotating benchmark sample
6. Autoresearch benchmarks rotate weekly (seeded re-sample) — prevents overfitting to fixed test cases; golden eval composite tracked in pipeline log
7. Style drift detection: Stats dashboard flags when your writing patterns shift significantly
8. Your best-rated, least-edited replies surface higher in future retrievals via quality scoring
9. Sender profiles track reply-time patterns and topics; `youos note` immediately rebuilds that contact's profile
10. Submit feedback from terminal: `youos feedback --inbound "..." --reply "..." --rating 4`
11. Setup wizard asks for internal domains — accurate sender classification from day one
12. Facts store (`/api/facts`) — save context about contacts, projects, and preferences; facts are injected into generation prompts automatically for context-aware drafts
13. Auto fact extraction — sender notes and feedback notes are parsed automatically on save using 15+ rule patterns (preferences, timezone, schedule, sign-offs, roles, relationships, project metadata); negation-aware with confidence scoring; LLM (Claude CLI) fallback for unstructured notes; fact deduplication/merging on upsert

## Security & privacy notes

- Gmail ingestion uses your local `gog` authentication; review connected accounts before ingestion
- External LLM fallback is optional; if enabled (`model.fallback: claude`), inbound email/context can be sent to Claude for generation
- For strict local-only operation, set `model.fallback: none` in `youos_config.yaml`
- Data location defaults to local project paths (or `YOUOS_DATA_DIR` if set)
- Review `PRIVACY.md` before first ingestion/deployment

## Provenance

- Source/homepage: https://github.com/DrBaher/youos
- This skill bundles a full local Python app and is intended for explicit local install/review before use.
