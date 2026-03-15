# Changelog

## v0.1.0 — Initial Release

### Features

- Gmail corpus ingestion via gog CLI
- Writing style analysis and persona detection
- Draft reply generation (local Qwen + Claude fallback)
- FTS5 + semantic retrieval for finding similar past replies
- Web UI with feedback loop and review queue
- Sender-aware drafting with relationship profiles
- LoRA fine-tuning on user feedback
- Autoresearch for automated config optimization
- Nightly pipeline (ingest, feedback, fine-tune, optimize)
- Gmail bookmarklet for one-click drafting
- PIN-based web UI authentication
- Auto-generated benchmarks from corpus
- CLI interface (`youos setup`, `youos draft`, `youos status`, etc.)
- Teardown command for clean data removal
