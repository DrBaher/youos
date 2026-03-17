# BaherOS Master Plan

_Last updated: 2026-03-14_

## Purpose

This file is the living source of truth for BaherOS.

We will update it as the project evolves so we always have one place that answers:
- what BaherOS is
- what is done
- what is in progress
- what is next
- what is deferred
- where Autoresearch fits
- what decisions have already been made

---

## 1. Project Definition

BaherOS is a **local-first Baher-mode copilot** built from Baher-authored and Baher-contextual data.

It is **not** a generic chatbot and **not** a full autonomous identity clone.

The intended product is a system that can:
- retrieve relevant prior writing and precedent
- draft replies in Baher’s style
- summarize in Baher’s style
- reflect Baher’s decision patterns over time
- improve through evaluation and later Autoresearch optimization

---

## 2. Core Product Principles

### 2.1 Local-first
- Corpus storage should remain local whenever possible.
- Raw personal data should not be casually shipped to external services.
- Local retrieval and storage are default.

### 2.2 Retrieval-first
- BaherOS should not rely on “style prompting” alone.
- Retrieval of relevant precedent is a first-class capability.
- Draft generation should sit on top of retrieval, not replace it.

### 2.3 Mode-aware
Baher is not one flat persona.
BaherOS must distinguish between modes such as:
- business / formal
- friendly / personal
- collaborator / work-chat
- analytical / explanatory
- quick admin

### 2.4 Traceable
BaherOS should be able to show:
- what prior examples influenced an output
- which source(s) were used
- how confident the system is

### 2.5 Evaluation before self-optimization
Autoresearch should only begin after:
- ingestion exists
- retrieval works
- a first draft flow exists
- benchmark/eval harness exists

---

## 3. Coding / Repo Decisions

### Locked decisions
- **Repo location:** `~/Projects/baheros`
- **Coding engine:** **Codex**
- **Approach:** incremental, retrieval-first, local-first

### Current project structure
- `app/` — application code
- `configs/` — persona, prompts, retrieval config
- `docs/` — schema and ingestion specs
- `scripts/` — ingestion/bootstrap scripts
- `tests/` — focused tests for ingestion/retrieval

---

## 4. Data Source Plan

## 4.1 Active corpus sources

### A. Gmail
Purpose:
- learn reply behavior
- learn formal vs friendly responses
- learn thread-aware response patterns

Desired value:
- inbound message context
- Baher-authored reply
- thread metadata
- account separation (personal vs work)

### B. Google Docs
Purpose:
- learn long-form authored style
- learn explanation structure
- learn summarization voice
- learn analytical reasoning style

Desired value:
- authored document text
- title / metadata / account / timestamps
- retrieval chunks for long-form context

## 4.2 Deferred corpus sources

### WhatsApp
Status: **deferred**

Reason:
- no clean export path yet
- we do not want to design parser logic against imaginary input

Action:
- revisit only once a real export path exists

## 4.3 Not active for now
- Telegram ingestion
- voice notes
- multimodal memory
- calendar-as-corpus
- broad filesystem ingestion beyond targeted docs

---

## 5. Current Implementation Status

## 5.1 Completed / materially implemented

### Project scaffold
- repo initialized
- Python project structure created
- FastAPI skeleton created
- initial configs created
- SQLite bootstrap path created

### Schema foundation
Current key tables:
- `documents`
- `chunks`
- `reply_pairs`
- `benchmark_cases`
- `eval_runs`

### Gmail ingestion
Implemented:
- local JSON thread import
- live Gmail ingestion path via `gog`
- reply-pair extraction
- SQLite persistence into `documents`, `chunks`, `reply_pairs`
- tests for normalized and Gmail-style payloads

### Google Docs ingestion
Implemented:
- live Google Docs ingestion path via `gog`
- local cached JSON snapshot import path
- SQLite persistence into `documents`, `chunks`
- metadata capture where available
- tests for live adapter and cached snapshot paths

---

## 5.2 In progress

### Gmail ingestion run
A long-range ingestion run was started for approximately the last 10 years across:
- `drbaher@gmail.com`
- `baher@medicus.ai`

Goal:
- build the initial Baher email corpus
- populate reply pairs and document records at meaningful scale

### Google Docs ingestion run
A live Google Docs ingestion run was started for:
- `drbaher@gmail.com`
- `baher@medicus.ai`

Goal:
- ingest authored Google Docs and create retrieval chunks

---

## 5.3 Not implemented yet

### Retrieval
- no full retrieval layer available yet
- no semantic retrieval yet
- no ranking/reranking yet
- no precedent lookup API yet

### Generation
- no draft-reply endpoint yet
- no Baher-style generation flow yet

### Evaluation
- no benchmark runner yet
- no draft quality scoring yet
- no retrieval scoring yet

### Autoresearch
- intentionally not introduced yet

---

## 6. Near-Term Roadmap

## Phase A — Finish corpus ingestion baseline

### Objectives
- complete Gmail ingestion run
- complete Google Docs ingestion run
- verify DB population quality
- inspect sample rows manually

### Success criteria
- meaningful number of `documents`
- meaningful number of `reply_pairs`
- Gmail and Docs both represented
- account metadata preserved
- no obviously broken normalization patterns at scale

### Deliverables
- ingestion commands documented
- cached raw payloads stored locally where configured
- ingestion quality notes

---

## Phase B — Build retrieval layer

### Objectives
- implement precedent lookup across:
  - `documents`
  - `chunks`
  - `reply_pairs`
- support lexical + metadata-aware retrieval first
- avoid pretending embeddings exist before they do

### Required filtering / scoping
- source type
- account / source account
- reply pairs vs authored docs
- possibly mode hints later

### Deliverables
- retrieval service module
- API route and/or CLI query tool
- docs for how to query the corpus

### Success criteria
A user can ask for things like:
- similar past replies
- relevant authored docs
- examples from work vs personal account

---

## Phase C — First useful product flow

### First target feature
**Draft a reply in Baher style using retrieved precedent**

### Input
- inbound message/email
- optional mode
- optional audience hint

### Output
- draft reply
- precedent snippets used
- source references
- optional confidence / rationale

### Success criteria
- produces useful draft output
- grounded in retrieved precedent
- clearly better than generic prompting without retrieval

---

## Phase D — Benchmark and evaluation

### Objectives
Create a first benchmark set covering:
- business/formal replies
- friendly/personal replies
- summary tasks
- explanation tasks
- maybe later decision tasks

### Evaluation dimensions
- tone fit
- brevity fit
- retrieval usefulness
- similarity to Baher’s historical reply patterns
- usefulness after human editing

### Success criteria
- benchmark cases stored in DB / fixtures
- repeatable evaluation run path exists
- we can compare configuration changes against a baseline

---

## Phase E — Autoresearch introduction

Autoresearch should begin **only after** Phases A–D are in place.

### First Autoresearch targets
1. retrieval weighting
2. source prioritization
3. prompt template tuning
4. response-length defaults
5. mode routing

### What Autoresearch should NOT touch initially
- raw corpus ingestion
- schema design
- safety boundaries
- identity constraints
- access rules

### Principle
Autoresearch should optimize a bounded system, not invent the system.

---

## 7. Detailed Task List

## 7.1 Corpus / ingestion

### Gmail
- [ ] confirm long-range ingest completed successfully
- [ ] record counts of documents / reply_pairs
- [ ] inspect quality of sampled reply pairs
- [ ] identify parsing issues (quoted text, signatures, odd thread cases)
- [ ] decide whether to widen/narrow ingest windows after first pass

### Google Docs
- [ ] confirm live ingest completed successfully
- [ ] record counts of docs / chunks
- [ ] inspect quality of chunking
- [ ] decide whether to restrict docs by owner, recency, or folder/query

### Deferred: WhatsApp
- [ ] wait for real export path
- [ ] do nothing speculative until real sample exists

---

## 7.2 Retrieval
- [ ] implement lexical retrieval over `documents`, `chunks`, `reply_pairs`
- [ ] add source/account filters
- [ ] add retrieval CLI and/or API route
- [ ] verify retrieval results on real corpus
- [ ] design retrieval interface to allow future embedding layer

---

## 7.3 Generation
- [ ] define draft endpoint contract
- [ ] connect retrieval into prompt assembly
- [ ] generate first Baher-style grounded draft
- [ ] expose precedent snippets in output

---

## 7.4 Evaluation
- [ ] define benchmark format
- [ ] create first benchmark cases
- [ ] define scoring dimensions
- [ ] add evaluation runner
- [ ] save evaluation results in DB

---

## 7.5 Autoresearch
- [ ] define config surfaces that are safe to mutate
- [ ] define baseline scorecard
- [ ] run first retrieval/prompt optimization loop only after evaluation exists

---

## 8. Proposed Milestone Sequence

## Milestone 1 — Corpus foundation
Done when:
- Gmail ingestion works
- Google Docs ingestion works
- corpus tables contain real data

## Milestone 2 — Retrieval foundation
Done when:
- BaherOS can find relevant past replies/docs on request

## Milestone 3 — Draft engine v1
Done when:
- BaherOS can draft grounded replies using retrieved precedent

## Milestone 4 — Evaluation harness
Done when:
- changes can be compared against a baseline with repeatable scoring

## Milestone 5 — Autoresearch v1
Done when:
- retrieval/prompt config can be optimized safely against benchmarks

---

## 9. Product Risks / Failure Modes

### 9.1 Persona soup
Risk:
- blending work emails, friendly replies, and docs into one flattened average style

Mitigation:
- preserve source/account metadata
- introduce mode-aware retrieval and prompting

### 9.2 Retrieval failure
Risk:
- wrong precedent leads to polished nonsense

Mitigation:
- treat retrieval as a first-class system
- benchmark retrieval before heavy generation optimization

### 9.3 Over-optimization too early
Risk:
- Autoresearch optimizes accidental behavior before the system is stable

Mitigation:
- no Autoresearch before benchmarks/evals exist

### 9.4 Data messiness
Risk:
- quoted threads, signatures, malformed docs, and source quirks degrade quality

Mitigation:
- sample and inspect real corpus data early
- fix ingestion quality before chasing cleverness

### 9.5 Scope creep
Risk:
- adding too many source types before first useful output exists

Mitigation:
- keep WhatsApp deferred
- focus on Gmail + Docs + retrieval + draft flow first

---

## 10. Working Principles for the Next Few Days

1. **Finish the ingestion runs and inspect the actual corpus** before making grand claims.
2. **Prioritize retrieval next** — corpus without retrieval is just organized nostalgia.
3. **Keep WhatsApp out of active planning** until a real export exists.
4. **Do not introduce Autoresearch early.**
5. **Aim for a usable first draft flow quickly**, not theoretical completeness.
6. **Update this file whenever a phase meaningfully changes.**

---

## 11. Immediate Next Actions

### Right now
- [ ] check status/results of long Gmail ingestion run
- [ ] check status/results of Google Docs ingestion run
- [ ] record actual counts and quality notes here
- [ ] proceed to retrieval implementation

### Next build phase after ingestion verification
- [ ] implement retrieval service
- [ ] implement precedent lookup API/CLI
- [ ] validate retrieval against real corpus examples

---

## 12. Changelog for This Plan

### 2026-03-14
- Created initial living master plan.
- Locked active corpus sources to Gmail + Google Docs.
- Deferred WhatsApp until a real export path exists.
- Positioned Autoresearch after retrieval + draft flow + eval harness.
- Set retrieval as the next major build phase after ingestion verification.

---

## Phase F — Feedback Loop + Local Fine-Tuning (added 2026-03-15)

### Vision
BaherOS generates drafts daily. Baher reviews, edits, and optionally annotates them.
Those edits become fine-tuning signal for a local Qwen model via LoRA (MLX).
Autoresearch tunes the fine-tuning hyperparams against the benchmark suite.

### Hardware confirmed
- Mac mini M4, 16GB unified memory
- MLX 0.30.5, Metal GPU available
- LoRA fine-tuning: ~5 it/sec, ~3.4GB peak mem, adapter saves to safetensors
- Model: Qwen2.5-1.5B-Instruct (downloaded to HF cache)

### Components to build

#### F1. Feedback capture UI
- Simple local web app (FastAPI + minimal HTML)
- Input: paste inbound email
- Output: BaherOS draft (current retrieval+Claude pipeline)
- Action: edit draft inline, add optional note, submit
- Stored in: `feedback_pairs` table in var/baheros.db

#### F2. Feedback schema (add to schema.sql)
```sql
CREATE TABLE IF NOT EXISTS feedback_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL,
    generated_draft TEXT NOT NULL,
    edited_reply TEXT NOT NULL,
    feedback_note TEXT,
    rating INTEGER,  -- 1-5, optional
    used_in_finetune INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

#### F3. JSONL exporter
- `scripts/export_feedback_jsonl.py`
- Converts feedback_pairs → MLX chat format JSONL
- Filters: only unused pairs, or all, or since date

#### F4. LoRA fine-tuning script
- `scripts/finetune_lora.py`
- Uses mlx_lm.lora on exported JSONL
- Saves adapter to `models/adapters/latest/`
- Marks used feedback_pairs as used_in_finetune=1
- Configurable: iters, batch_size, num_layers, lr

#### F5. Generation updated
- When adapter exists: use Qwen + LoRA adapter for generation
- Fall back to Claude CLI if adapter not present
- Keep same DraftResponse interface

#### F6. Autoresearch program.md updated
- Mutable surfaces now include LoRA hyperparams
- Fitness: benchmark composite score using local model
- Agent runs overnight, keeps best LoRA config

### Phase B (retrieval reinforcement, after F is stable)
- Promote high-rated feedback pairs (rating >= 4) into corpus as gold exemplars
- Boost in retrieval ranking over raw Gmail corpus
- Double signal: fine-tuned model + retrieval weighted toward gold examples
