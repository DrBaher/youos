CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    author TEXT,
    external_uri TEXT,
    thread_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ingestion_run_id TEXT,
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    accounts_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    stored_document_count INTEGER NOT NULL DEFAULT 0,
    stored_chunk_count INTEGER NOT NULL DEFAULT 0,
    stored_reply_pair_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    error_detail TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,
    char_count INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    UNIQUE(document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS reply_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_id INTEGER,
    thread_id TEXT,
    inbound_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    inbound_author TEXT,
    reply_author TEXT,
    paired_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    auto_feedback_processed INTEGER DEFAULT 0,
    quality_score REAL DEFAULT 1.0,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS benchmark_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    expected_properties_json TEXT NOT NULL DEFAULT '{}',
    reference_reply TEXT,
    notes TEXT,
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL UNIQUE,
    benchmark_case_id INTEGER,
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    retrieval_summary_json TEXT,
    generation_output TEXT,
    score_json TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    created_ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (benchmark_case_id) REFERENCES benchmark_cases(id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type);
CREATE INDEX IF NOT EXISTS idx_documents_thread_id ON documents(thread_id);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_source ON ingest_runs(source);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_status ON ingest_runs(status);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_reply_pairs_thread_id ON reply_pairs(thread_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_case_id ON eval_runs(benchmark_case_id);

CREATE TABLE IF NOT EXISTS feedback_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL,
    generated_draft TEXT NOT NULL,
    edited_reply TEXT NOT NULL,
    feedback_note TEXT,
    rating INTEGER,
    used_in_finetune INTEGER DEFAULT 0,
    edit_distance_pct REAL,
    reply_pair_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sender_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    domain TEXT,
    company TEXT,
    sender_type TEXT,
    relationship_note TEXT,
    reply_count INTEGER DEFAULT 0,
    avg_reply_words REAL,
    avg_response_hours REAL,
    first_seen TEXT,
    last_seen TEXT,
    topics_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sender_profiles_email ON sender_profiles(email);
CREATE INDEX IF NOT EXISTS idx_sender_profiles_domain ON sender_profiles(domain);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS reply_pairs_fts USING fts5(
    inbound_text,
    reply_text,
    content='reply_pairs',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS reply_pairs_fts_insert AFTER INSERT ON reply_pairs BEGIN
    INSERT INTO reply_pairs_fts(rowid, inbound_text, reply_text)
    VALUES (new.id, new.inbound_text, new.reply_text);
END;

CREATE TRIGGER IF NOT EXISTS reply_pairs_fts_delete AFTER DELETE ON reply_pairs BEGIN
    INSERT INTO reply_pairs_fts(reply_pairs_fts, rowid, inbound_text, reply_text)
    VALUES ('delete', old.id, old.inbound_text, old.reply_text);
END;

CREATE TRIGGER IF NOT EXISTS reply_pairs_fts_update AFTER UPDATE ON reply_pairs BEGIN
    INSERT INTO reply_pairs_fts(reply_pairs_fts, rowid, inbound_text, reply_text)
    VALUES ('delete', old.id, old.inbound_text, old.reply_text);
    INSERT INTO reply_pairs_fts(rowid, inbound_text, reply_text)
    VALUES (new.id, new.inbound_text, new.reply_text);
END;

CREATE TABLE IF NOT EXISTS draft_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_text TEXT NOT NULL,
    sender TEXT,
    generated_draft TEXT NOT NULL,
    final_reply TEXT,
    edit_distance_pct REAL,
    confidence TEXT,
    model_used TEXT,
    retrieval_method TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
