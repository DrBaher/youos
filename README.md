# YouOS ✉️

> **Your email. Your model. Your style.**

YouOS is a local-first AI email copilot that learns from your sent Gmail history and drafts replies that sound like *you* — not a generic AI. It runs entirely on your Mac. No cloud. No subscriptions. Your data never leaves your machine.

```
Gmail (sent mail)          Your feedback
       │                        │
       ▼                        ▼
  Ingestion pipeline      Review Queue
  (gog CLI + SQLite)      (10 emails/batch)
       │                        │
       ▼                        ▼
  Reply Pairs DB  ──────► LoRA Fine-tuning
  (FTS5 + BM25)           (Qwen, nightly)
       │                        │
       ▼                        ▼
  Draft Generation ◄──── Autoresearch
  (local Qwen MLX)        (80 iterations/night)
       │
       ▼
  Draft Reply ✅
```

**Privacy:** Everything stays local. Your corpus, model, and drafts never leave your Mac.

![YouOS demo](screenshots/demo.gif)

> 🌐 [Landing page](https://dewy-haven-wgpz.here.now/)

## What it does

- Ingests your sent Gmail history, Google Docs, WhatsApp exports — including organic pairs you sent without YouOS
- Learns your writing style — richer persona: bullet point rate, directness score, sentence length, paragraph style; EWMA-weighted toward recent emails
- Persona re-analysis is incremental (recent 90 days × 3 weight), with full weekly refresh; confidence intervals (p25/p75) shown in prompts
- Per-sender-type personas: different voice, length, greeting, and closing for internal, external client, and personal contacts
- Per-account corpus isolation — drafts for work emails draw from work history; personal from personal
- Greets people by first name, closes in your style — greeting and closing injected from persona config per contact type
- Classifies multi-intent (meeting + urgent, etc.), boosts matching exemplars; per-intent reply length calibrated from corpus
- Drafts grounded in score-ranked few-shot exemplars (confidence-annotated, thread-deduplicated); exemplar reply text preserved (600 chars), inbound trimmed (400)
- Prompt token budget enforced — exemplars auto-trimmed if prompt exceeds 2000 tokens
- Confidence thresholds are relative (mean±σ of retrieval scores), not hardcoded
- Subject line + topic-aware retrieval; FTS queries expanded with email vocabulary synonyms
- Same-thread history gets a 2x retrieval boost
- Handles full email threads — paste the whole thread, YouOS focuses on the latest message
- Warns you when confidence is low; explain any draft inline via "How was this generated?"
- Local model empty or signature-only output → automatic Claude fallback
- Subject line generated via smart content analysis — skips greeting/filler lines, extracts the actual topic
- Improves from your feedback via LoRA fine-tuning — quality-filtered, deduplicated, curriculum-ordered, DPO preference pairs supported
- Training export deduplicated by inbound similarity (≥0.95 → keep higher-rated pair)
- Auto-scales training hyperparameters; nightly pipeline skips steps when data is insufficient
- Golden eval runs nightly after fine-tuning — composite score tracked in pipeline log
- Autoresearch benchmarks rotate weekly (seeded re-sample) to prevent overfitting to fixed test cases
- Self-optimizes nightly via autoresearch — configurable composite weights, sender-type boosts, intent signals
- Style drift detection: Stats dashboard flags when your writing patterns shift significantly
- Feedback loop closes: high-rating, low-edit pairs surface higher in future retrievals
- Streak tracking — consecutive daily Review Queue sessions tracked; streak shown in queue UI
- Corpus scan button in Stats — bulk-extracts structured facts from your top reply pairs in one click
- Language-filtered retrieval — retrieval matches language of the inbound email; no cross-language bleed
- Sender profiles track reply-time patterns and topics; notes trigger immediate profile rebuild
- Embedding cache for fast repeated retrieval; corpus health at a glance: `youos corpus`
- Run a golden benchmark anytime (10 curated cases): `youos eval --golden`
- Runs entirely locally on Apple Silicon

## Requirements

- Apple Silicon Mac (M1/M2/M3/M4)
- 8GB+ RAM (16GB recommended)
- Python 3.11+
- [gog CLI](https://github.com/openclaw/gog) configured with your Gmail account(s)
- ~5GB free disk space

## Quick start

```bash
# Clone and install
cd ~/Projects/youos
pip install -e .

# Run the setup wizard
youos setup

# Or run directly
python3 scripts/setup_wizard.py
```

The setup wizard walks you through:
1. Dependency check
2. Gmail account configuration
3. Email corpus ingestion
4. Writing style analysis
5. Optional initial fine-tuning
6. Server setup

## Usage

```bash
# Draft a reply
youos draft "Hi, can we schedule a call next week to discuss the proposal?"

# Draft with sender context
youos draft --sender john@company.com "email text here"

# Open the web UI
youos ui

# Check system status
youos status

# View corpus stats
youos stats

# Full corpus health report (pair count, quality scores, top senders)
youos corpus
youos corpus --json   # raw JSON output

# Add a sender note (immediately rebuilds their profile)
youos note john@company.com "prefers bullet points, decision-maker"

# Submit a feedback pair directly from the terminal
youos feedback --inbound "email text" --reply "your reply" --rating 4
youos feedback --inbound "..." --reply "..." --sender "sarah@co.com" --note "too formal"

# Run nightly pipeline manually (with step-by-step output)
youos improve --verbose

# Check system requirements
youos doctor

# Run golden benchmark evaluation (10 curated cases)
youos eval --golden

# Start the web server
youos serve

# Ingest a WhatsApp export
youos ingest --whatsapp ~/Downloads/WhatsApp-Chat.txt
```

## Facts & Auto-Extraction

YouOS stores contextual facts about your contacts, projects, and preferences to improve draft quality over time. Facts are injected into generation prompts automatically.

**Auto-extraction from notes:** When you add a sender note (`youos note john@co.com "..."`) or submit feedback with a note, YouOS automatically extracts structured facts using a rule-based extractor with LLM fallback.

**How it works:**
- **Rule-based (primary):** `finditer` over 15+ pattern categories; all matches captured per note
- **Negation awareness:** Detects preceding negation words (`not`, `don't`, `never`, etc.) and skips false positives
- **Confidence scoring:** Each pattern carries a base confidence (0.6–0.9); long/noisy captures are downgraded to 0.4
- **Fact merging:** Duplicate facts (same type + key + text) are deduplicated before upsert
- **LLM fallback:** When rule-based extraction returns nothing, the Claude CLI is invoked to extract facts from unstructured text

**Pattern categories supported:**
- Communication preferences: `prefers short replies`, `prefers bullet points`, `prefers formal tone`
- Dislikes / avoidances: `hates X`, `don't like X`, `never CC X`
- Scheduling: `meetings on Mon/Wed`, `available on Fridays`, `responds within 2 hours`, `unavailable on X`
- Timezone: `UTC+5`, `GMT-8`, `EST`, `America/New_York` (IANA-style)
- Identity: `title/role: X`, `works at X`, `based in X`, `preferred name: X`, `reports to X`
- Sign-offs: `signs off with "Best,"`, `use "Cheers" as sign-off`, `signature: X`
- Language: `writes in Spanish`, `speaks French`
- Contact metadata: `phone: X`, `billing email: X`, `always CC X`, `CC their assistant X`
- Relationship tags: `decision maker`, `gatekeeper`, `VIP client`, `key account`, `referred by X`
- Project facts: `deadline: X`, `budget: $X`, `renewal date: X`, `stakeholder: X`

**API endpoints:**

```
GET    /api/facts          — list all facts (optional ?type= filter: contact | project | user_pref)
POST   /api/facts          — create or upsert a fact
DELETE /api/facts/{id}     — delete a fact by id
```

**Example fact types:**
- `contact` — `key: john@acme.com`, `fact: Prefers Tuesday meetings`
- `project` — `key: project_alpha`, `fact: Uses React 18 with TypeScript`
- `user_pref` — `key: sign_off`, `fact: Always close with "Best,"`

Facts are stored locally in the SQLite `memory` table and surfaced via the web UI.

## Web UI

The web UI provides:
- **Draft Reply**: Paste an inbound email (or full thread), generate a draft grounded in your style. A **confidence reason banner** explains *why* the draft received its confidence score (e.g. "3 strong exemplars found", "low retrieval — new topic"). See the full exemplar trace via "How was this generated?"
- **Review Queue**: Emails appear instantly, drafts stream in one by one as they generate. Automated senders filtered by address and content. Configurable batch size (5/10/20) and draft model (`claude`/`local`/`auto`). Bulk actions included (`Bulk submit ready`, `Skip low-signal`) with keyboard shortcuts (`j` submit, `k` skip, `b` bulk submit, `n` bulk skip, `?` help).
- **History**: Past drafts with intent badges, confidence badges, and edit-distance indicators
- **Stats Dashboard**: Corpus health, model status, pipeline status (with skipped steps), style drift indicator, benchmark trends, edit distance trend chart, per-sender-type accuracy breakdown, and **System Health card** (corpus size, last ingestion, embedding coverage, adapter status)
- **Gmail Bookmarklet**: Injects a floating side panel directly into Gmail — auto-detects sender email from the DOM, generate a draft and click "Insert into Gmail" without leaving your inbox. Submit feedback with star rating from the panel

## Architecture

```
app/
  main.py              # FastAPI application
  api/                 # HTTP endpoints
  core/                # Config, embeddings, sender classification
  db/                  # SQLite bootstrap and migrations
  generation/          # Draft generation (local Qwen + Claude fallback)
  ingestion/           # Gmail, Google Docs, WhatsApp importers
  retrieval/           # FTS5 + semantic search
  evaluation/          # Benchmark scoring
  autoresearch/        # Automated config optimization

scripts/               # CLI tools and pipeline scripts
configs/               # Persona, prompts, retrieval settings
templates/             # Web UI (feedback, stats, bookmarklet)
```

## Privacy

All data stays on your machine. No email content is ever sent to a cloud service unless you explicitly use an external LLM for draft generation (configurable). See [PRIVACY.md](PRIVACY.md).

## Configuration

All settings are in `youos_config.yaml`, created by the setup wizard:

```yaml
user:
  name: "Your Name"
  emails: ["you@company.com", "you@gmail.com"]

model:
  base: "Qwen/Qwen2.5-1.5B-Instruct"
  fallback: "claude"  # or "none" for fully local

autoresearch:
  enabled: true
  schedule: "0 1 * * *"
```

## Running a Personal Instance

You can run multiple independent instances from the same codebase by pointing `YOUOS_DATA_DIR` at an instance directory. Each instance has its own database, config files, and user data.

**Instance directory layout:**
```
instances/myname/
├── youos_config.yaml     # user settings (name, emails, pin, etc.)
├── var/
│   └── youos.db          # SQLite database
├── configs/
│   ├── persona.yaml      # writing style
│   ├── prompts.yaml      # prompt templates
│   └── retrieval.yaml    # retrieval settings
├── data/                 # ingested corpus (raw + feedback)
└── models/adapters/      # fine-tuned LoRA adapter (optional)
```

**Start a named instance:**
```bash
YOUOS_DATA_DIR=instances/myname uvicorn app.main:app --host 0.0.0.0 --port 8765
```

When `YOUOS_DATA_DIR` is set, YouOS automatically derives the database URL and configs directory from it. Individual overrides are still possible via `YOUOS_DATABASE_URL` and `YOUOS_CONFIGS_DIR`.

Instance data directories (`instances/*/var/`, `instances/*/data/`, `instances/*/models/`, `instances/*/youos_config.yaml`) are excluded from git.

## License

Open source. See LICENSE for details.
