import sqlite3
from pathlib import Path

from app.core.settings import get_settings

SQLITE_BUSY_TIMEOUT_MS = 30000  # wait up to 30s for a lock before erroring


def resolve_sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// URLs are supported by the bootstrap script.")
    return Path(database_url.removeprefix(prefix))


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection tuned for concurrent access.

    The generation path opens several connections per draft and the nightly
    pipeline runs while the web server is live, so lock contention is normal.
    WAL lets a writer proceed alongside readers, and a generous busy_timeout
    makes a momentarily-locked write wait instead of immediately raising
    'database is locked'.
    """
    conn = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def bootstrap_database() -> Path:
    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_path = settings.configs_dir.parent / "docs" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema_sql)
        _migrate_feedback_pairs(connection)
        _migrate_reply_pairs(connection)
        _migrate_sender_profiles(connection)
        _migrate_memory(connection)
        _migrate_review_streaks(connection)
        _migrate_exemplar_cache(connection)
        _migrate_draft_events(connection)
        _migrate_agent_pending_drafts(connection)
        _migrate_agent_audit(connection)
        _populate_fts(connection)
        connection.commit()
    finally:
        connection.close()

    return db_path


def _migrate_feedback_pairs(connection: sqlite3.Connection) -> None:
    """Add missing columns if needed (migration for existing DBs)."""
    cols = {row[1] for row in connection.execute("PRAGMA table_info(feedback_pairs)").fetchall()}
    if "edit_distance_pct" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN edit_distance_pct REAL")
    if "reply_pair_id" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN reply_pair_id INTEGER")
    if "organic" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN organic BOOLEAN DEFAULT 0")
    if "edit_categories" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN edit_categories TEXT")
    if "precedents_used" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN precedents_used TEXT")
    # `sender_type` is the persona-routing axis added in Phase 1 of the
    # per-persona adapters work. NULL on rows that predate this column (the
    # backfill script `scripts/backfill_feedback_sender_type.py` derives it
    # from the linked reply_pair's inbound_author for the historical pairs;
    # NULL is still legal after backfill for rows whose reply_pair_id is
    # None, which is treated as "unknown" for cohort purposes).
    if "sender_type" not in cols:
        connection.execute("ALTER TABLE feedback_pairs ADD COLUMN sender_type TEXT")


def _migrate_reply_pairs(connection: sqlite3.Connection) -> None:
    """Add quality_score and language columns to reply_pairs if missing."""
    cols = {row[1] for row in connection.execute("PRAGMA table_info(reply_pairs)").fetchall()}
    if "quality_score" not in cols:
        connection.execute("ALTER TABLE reply_pairs ADD COLUMN quality_score REAL DEFAULT 1.0")
    if "language" not in cols:
        connection.execute("ALTER TABLE reply_pairs ADD COLUMN language TEXT")


def _migrate_sender_profiles(connection: sqlite3.Connection) -> None:
    """Add avg_response_hours column to sender_profiles if missing."""
    try:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(sender_profiles)").fetchall()}
    except Exception:
        return
    if "avg_response_hours" not in cols:
        connection.execute("ALTER TABLE sender_profiles ADD COLUMN avg_response_hours REAL")


def _migrate_memory(connection: sqlite3.Connection) -> None:
    """Create memory table if it doesn't exist (migration for existing DBs)."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            key TEXT NOT NULL,
            fact TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.8,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(type, key, fact)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory(type)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_key ON memory(key)")
    # Add confidence column to existing memory tables that predate this migration
    cols = {row[1] for row in connection.execute("PRAGMA table_info(memory)").fetchall()}
    if "confidence" not in cols:
        connection.execute("ALTER TABLE memory ADD COLUMN confidence REAL NOT NULL DEFAULT 0.8")


def _migrate_review_streaks(connection: sqlite3.Connection) -> None:
    """Create review_streaks table if it doesn't exist."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS review_streaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            review_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_review_streaks_date ON review_streaks(date)")


def _populate_fts(connection: sqlite3.Connection) -> None:
    """Rebuild FTS5 indexes from the source tables only if data has changed."""
    # Check if rebuild is needed by comparing rowcount in source vs FTS shadow tables
    # Use a lightweight metadata table to track last rebuild counts
    connection.execute("""
        CREATE TABLE IF NOT EXISTS _fts_rebuild_meta (
            table_name TEXT PRIMARY KEY,
            last_rowcount INTEGER NOT NULL DEFAULT 0
        )
    """)

    needs_rebuild = False
    for source_table, _fts_table in [("chunks", "chunks_fts"), ("reply_pairs", "reply_pairs_fts")]:
        try:
            current_count = connection.execute(f"SELECT COUNT(*) FROM {source_table}").fetchone()[0]
            meta_row = connection.execute(
                "SELECT last_rowcount FROM _fts_rebuild_meta WHERE table_name = ?", (source_table,)
            ).fetchone()
            last_count = meta_row[0] if meta_row else -1
            if current_count != last_count:
                needs_rebuild = True
                break
        except Exception:
            needs_rebuild = True
            break

    if not needs_rebuild:
        return

    connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    connection.execute("INSERT INTO reply_pairs_fts(reply_pairs_fts) VALUES ('rebuild')")

    # Update metadata
    for source_table in ("chunks", "reply_pairs"):
        try:
            current_count = connection.execute(f"SELECT COUNT(*) FROM {source_table}").fetchone()[0]
            connection.execute(
                "INSERT OR REPLACE INTO _fts_rebuild_meta (table_name, last_rowcount) VALUES (?, ?)",
                (source_table, current_count),
            )
        except Exception:
            pass


def _migrate_exemplar_cache(connection: sqlite3.Connection) -> None:
    """Create persistent exemplar cache table if it doesn't exist."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS exemplar_cache (
            cache_key TEXT PRIMARY KEY,
            source_ids_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_exemplar_cache_updated ON exemplar_cache(updated_at)")


def _migrate_draft_events(connection: sqlite3.Connection) -> None:
    """Create the append-only draft-event signal log if it doesn't exist.

    One row per generated draft (not just ones the user gives feedback on),
    capturing the exemplar ids / intent / sender_type / confidence the draft
    was produced with — richer training signal for the nightly than
    feedback-only `draft_history`.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS draft_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_text TEXT NOT NULL,
            generated_draft TEXT NOT NULL,
            account_email TEXT,
            sender TEXT,
            sender_type TEXT,
            detected_mode TEXT,
            intent TEXT,
            confidence TEXT,
            confidence_reason TEXT,
            model_used TEXT,
            retrieval_method TEXT,
            exemplar_ids TEXT NOT NULL DEFAULT '[]',
            length_flag TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_draft_events_created ON draft_events(created_at)")


def _migrate_agent_pending_drafts(connection: sqlite3.Connection) -> None:
    """Persistence for the autonomous-agent loop's triage results.

    One row per *inbound* the agent processed — both drafts the user should
    review (``tier='draft'``) and skipped-but-borderline cases the UI
    surfaces collapsed for visibility (``tier='surface'``). Hard-skipped
    inbounds (newsletters / automation domains / etc.) aren't stored;
    they're noise. ``message_id`` is unique so repeated triage runs are
    idempotent — the same unread thread won't be drafted twice.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_pending_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- inbound identification (dedup on message_id)
            message_id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            account TEXT NOT NULL,

            -- inbound content (kept so the UI can show it without re-fetching)
            sender TEXT,
            sender_email TEXT,
            subject TEXT,
            body TEXT,
            received_at TEXT,

            -- needs-reply verdict
            needs_reply_score REAL NOT NULL,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            cold_outreach INTEGER NOT NULL DEFAULT 0,
            tier TEXT NOT NULL,                          -- 'draft' | 'surface'

            -- the draft (NULL for tier='surface')
            draft TEXT,
            draft_model TEXT,
            draft_repairs_json TEXT NOT NULL DEFAULT '[]',
            standing_instructions_snapshot TEXT,

            -- lifecycle
            status TEXT NOT NULL DEFAULT 'pending',      -- 'pending' | 'amended' | 'sent' | 'dismissed'
            amended_draft TEXT,
            sent_at TEXT,
            dismissed_at TEXT,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_pending_drafts_status "
        "ON agent_pending_drafts(status, tier, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_pending_drafts_account "
        "ON agent_pending_drafts(account, status)"
    )
    # Phase 2.1: gmail_draft_id is set when "Push to Gmail Drafts" succeeds.
    # Added as an ALTER for idempotent upgrades from pre-Phase-2 instances.
    _cols = {row[1] for row in connection.execute("PRAGMA table_info(agent_pending_drafts)").fetchall()}
    if "gmail_draft_id" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN gmail_draft_id TEXT")
    # Phase 2.2 (dismissal-as-feedback): why the user dismissed a queued row.
    # Categorical hint we use to tune the needs_reply scorer over time —
    # 'noise' (filter let through what we shouldn't have drafted),
    # 'wrong_sender' (right type of mail but the wrong person to reply now),
    # 'wrong_content' (draft missed the point — drafting-quality signal),
    # 'already_handled' (we replied outside YouOS — orthogonal to the filter),
    # 'other' (free-text in a separate column if we ever add one).
    if "dismissal_reason" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN dismissal_reason TEXT")
    # Long-thread "what changed" summary (opt-in agent.summarize_threads) so a
    # reviewer can catch up on a long thread without reading it.
    if "thread_summary" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN thread_summary TEXT")


def _migrate_agent_audit(connection: sqlite3.Connection) -> None:
    """Audit log for the autonomous-agent loop (ε).

    One row per triage *sweep* — not per draft. Records what was attempted,
    by whom (``trigger``), against which account, with what timing and what
    failed. Drives the "what did the agent do" panel on /triage so the user
    can trust an autonomous process running on their inbox.

    Append-only; nothing here is ever rewritten. ``errors_json`` is a list
    of per-message error strings so a transient gog auth failure on one
    inbound is visible without polluting the sweep-level counters.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            trigger TEXT NOT NULL,             -- 'scheduled' | 'manual' | 'api'
            window TEXT,
            threshold REAL,
            fetched INTEGER NOT NULL DEFAULT 0,
            kept INTEGER NOT NULL DEFAULT 0,
            surfaced INTEGER NOT NULL DEFAULT 0,
            persisted INTEGER NOT NULL DEFAULT 0,
            errors_json TEXT NOT NULL DEFAULT '[]',
            standing_instructions_snapshot TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            duration_ms INTEGER
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_audit_started "
        "ON agent_audit(started_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_audit_account "
        "ON agent_audit(account, started_at DESC)"
    )
    # b52: ``auto_promoted_json`` captures senders the loop auto-added to
    # ``agent.skip_senders`` at the tail of this sweep (when
    # ``agent.auto_promote_skip_senders`` is on). Surfaces in
    # /triage Recent activity so the user can trust an autonomous action.
    _audit_cols = {row[1] for row in connection.execute("PRAGMA table_info(agent_audit)").fetchall()}
    if "auto_promoted_json" not in _audit_cols:
        connection.execute("ALTER TABLE agent_audit ADD COLUMN auto_promoted_json TEXT NOT NULL DEFAULT '[]'")
