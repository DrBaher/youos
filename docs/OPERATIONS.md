# YouOS Operations Guide

## Starting the server

```bash
youos serve
# or
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## Nightly pipeline

The nightly pipeline runs these steps in sequence:
1. Gmail ingestion (last 48 hours of sent mail)
2. Auto-feedback extraction (compare drafts to actual replies)
3. Export feedback JSONL
4. LoRA fine-tuning (if enough unused pairs)
5. Autoresearch optimization

Run manually:
```bash
youos improve
# or
python3 scripts/nightly_pipeline.py
```

Run autoresearch only:
```bash
python3 scripts/nightly_pipeline.py --autoresearch-only
```

## Manual operations

### Re-ingest emails
```bash
python3 scripts/ingest_gmail_threads.py --live --account you@company.com --query "in:sent after:2025/01/01"
```

### Rebuild embeddings
```bash
python3 scripts/index_embeddings.py
python3 scripts/index_embeddings.py --table reply_pairs --limit 500
```

### Rebuild sender profiles
```bash
python3 scripts/build_sender_profiles.py
```

### Check ingestion health
```bash
python3 scripts/report_ingestion_health.py
```

### Re-analyze persona
```bash
python3 scripts/analyze_persona.py
```

## Database

Location: `var/youos.db`

Bootstrap/migrate:
```bash
python3 scripts/bootstrap_db.py
```

## Configuration

All settings in `youos_config.yaml`. Key sections:

- `user`: name, emails, display name
- `server`: host, port
- `model`: base model, adapter path, fallback
- `autoresearch`: enabled, iterations, schedule

Retrieval tuning: `configs/retrieval/defaults.yaml`
Persona: `configs/persona.yaml`
Prompts: `configs/prompts.yaml`
