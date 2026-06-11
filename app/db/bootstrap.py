import os
import sqlite3
from pathlib import Path

from app.core.settings import get_settings

SQLITE_BUSY_TIMEOUT_MS = 30000  # wait up to 30s for a lock before erroring


def _secure_db_dir(db_path: Path) -> None:
    """Keep the var/ directory owner-only (0o700) and the DB owner-rw (0o600).
    The DB holds the full mailbox corpus; on a shared host the default umask
    would leave both world-readable. Best-effort — never blocks bootstrap."""
    for target, mode in ((db_path.parent, 0o700), (db_path, 0o600)):
        try:
            if target.exists():
                os.chmod(target, mode)
        except OSError:
            pass


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


def ensure_agent_schema(database_url: str) -> bool:
    """Idempotently bring the agent tables on ``database_url`` up to date.

    This is the self-heal for the silent-failure class where new code expects a
    column an existing instance DB doesn't have yet (e.g. a server that wasn't
    restarted, or was started with an instance-relative path so
    ``bootstrap_database`` couldn't find ``docs/schema.sql``). Unlike
    ``bootstrap_database`` it needs NO schema file — the agent migrations all
    ``CREATE TABLE IF NOT EXISTS`` then ``ALTER TABLE ADD COLUMN`` as needed, so
    they create-or-upgrade on any DB. Cheap + idempotent; safe to call before
    every sweep. Returns True on success.
    """
    db_path = resolve_sqlite_path(database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    _secure_db_dir(db_path)
    try:
        _migrate_agent_pending_drafts(conn)
        _migrate_agent_audit(conn)
        _migrate_triage_precision_history(conn)
        _migrate_agent_actions(conn)
        _migrate_agent_digest_runs(conn)
        _migrate_agent_digest_items(conn)
        conn.commit()
        return True
    finally:
        conn.close()


def bootstrap_database() -> Path:
    settings = get_settings()
    db_path = resolve_sqlite_path(settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_path = settings.configs_dir.parent / "docs" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    connection = sqlite3.connect(db_path)
    _secure_db_dir(db_path)
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
        _migrate_triage_precision_history(connection)
        _migrate_agent_actions(connection)
        _migrate_agent_digest_runs(connection)
        _migrate_agent_digest_items(connection)
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

            -- b189: time-criticality (app/core/urgency.py). ORDERING + VISIBILITY
            -- only; never a send/auto-send/auto-push gate. (Also self-healed via
            -- ALTER below for instances that predate this column.)
            urgency_score REAL NOT NULL DEFAULT 0.0,
            urgency_reasons_json TEXT NOT NULL DEFAULT '[]',

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
    # 'other' (free-text lives in dismissal_note, below).
    if "dismissal_reason" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN dismissal_reason TEXT")
    # Free-text elaboration the UI captures when reason='other' (b206) — the
    # "separate column" the note above anticipated.
    if "dismissal_note" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN dismissal_note TEXT")
    # Raw To/Cc header values (b213) so the queue records who the mail was
    # addressed to — lets "Re-screen queue" retroactively catch CC-only / not-a-
    # direct-recipient drafts, which it otherwise can't (the recipients weren't
    # stored). Populated on new drafts; NULL on pre-b213 rows.
    if "to_recipients" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN to_recipients TEXT")
    if "cc_recipients" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN cc_recipients TEXT")
    # Outreach rows (b232): a NEW outbound draft to a lead-form prospect
    # (rules outreach_draft action) rather than a reply to the inbound's
    # sender. Push composes a fresh message (no thread/In-Reply-To, subject
    # as-is) and outcome capture skips these (the user's send starts a new
    # thread the notification-thread reconciliation can't see).
    if "outreach" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN outreach INTEGER NOT NULL DEFAULT 0")
    # Outcome capture (b224): once we've checked whether the user actually sent a
    # reply on this thread (matching the YouOS draft to the real send), the row
    # is marked so we don't re-check it. ``outcome``: 'sent' (a real reply was
    # found → a training pair was stored) | 'no_send' (no reply after the window
    # → a needs-reply calibration signal). NULL = not yet decided.
    if "outcome_captured" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN outcome_captured INTEGER DEFAULT 0")
    if "outcome" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN outcome TEXT")
    # Long-thread "what changed" summary (opt-in agent.summarize_threads) so a
    # reviewer can catch up on a long thread without reading it.
    if "thread_summary" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN thread_summary TEXT")
    # Per-draft quality score (0–1) — what auto-push/auto-send gate on.
    if "quality_score" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN quality_score REAL")
    # Calibrated P(deserved a reply) for this row's raw score (Phase A2). Stored
    # so auto-send can gate on the calibrated probability, not the raw heuristic.
    if "calibrated_score" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN calibrated_score REAL")
    # Phase B (send frontier): an HONEST send state, kept separate from the
    # overloaded ``status='sent'`` (which means either "user resolved it
    # elsewhere" or "we pushed a Gmail draft"). send_state is explicit:
    #   NULL            — not pushed/sent
    #   'draft_created' — a Gmail DRAFT exists (we pushed it; never sent)
    #   'shadow'        — a real send was simulated (soak mode) but NOT sent
    #   'sent'          — actually sent to the recipient via Gmail
    # sent_message_id is the Gmail *message* id from a real send (distinct from
    # the draft id); actually_sent_at timestamps the real send.
    if "send_state" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN send_state TEXT")
        # Backfill: existing rows that hold a Gmail draft id were 'draft_created'.
        connection.execute(
            "UPDATE agent_pending_drafts SET send_state = 'draft_created' "
            "WHERE gmail_draft_id IS NOT NULL AND send_state IS NULL"
        )
    if "sent_message_id" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN sent_message_id TEXT")
    if "actually_sent_at" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN actually_sent_at TEXT")
    # Phase C (close the loop): marks a terminal row whose outcome has already
    # been mined into feedback_pairs, so the capture pass is idempotent and the
    # agent learns from each of its own drafts exactly once.
    if "feedback_captured" not in _cols:
        connection.execute(
            "ALTER TABLE agent_pending_drafts ADD COLUMN feedback_captured INTEGER NOT NULL DEFAULT 0"
        )
    # Provenance of an amendment: 'user' (a human verbatim edit, a real
    # correction signal) vs 'machine' (a /regenerate re-draft the user never
    # approved). Feedback capture must only treat 'user' edits as gold pairs.
    if "amended_by" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN amended_by TEXT")
    # Phase D: a matched ``hold`` rule excludes this row from auto-push AND
    # auto-send. Persisted (not just in-memory) so a manually-pushed hold row
    # can't later be picked up by the auto-send sweep.
    if "hold" not in _cols:
        connection.execute("ALTER TABLE agent_pending_drafts ADD COLUMN hold INTEGER NOT NULL DEFAULT 0")
    # b189 (urgency ranking): a time-criticality score in [0, 1] computed at
    # triage capture (app/core/urgency.py) — combines the 'urgent' intent label,
    # multilingual deadline markers, high-stakes, and an end-of-body question.
    # ORDERING + VISIBILITY only: list_pending sorts on it (urgency DESC, then
    # needs_reply DESC) and the digest highlights it. It is NEVER a
    # send/auto-send/auto-push gate. DEFAULT 0.0 so pre-b189 rows are simply "not
    # known urgent" and sort below freshly-scored urgent mail without any
    # backfill. urgency_reasons_json mirrors reasons_json: a JSON array of short
    # human-readable strings explaining the score (transparency).
    if "urgency_score" not in _cols:
        connection.execute(
            "ALTER TABLE agent_pending_drafts ADD COLUMN urgency_score REAL NOT NULL DEFAULT 0.0"
        )
    if "urgency_reasons_json" not in _cols:
        connection.execute(
            "ALTER TABLE agent_pending_drafts ADD COLUMN urgency_reasons_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_agent_actions(connection: sqlite3.Connection) -> None:
    """Ledger of mailbox-routing actions the agent took (label / archive / star)
    — the agent-action framework's accountability + undo log. One row per action
    (or would-be action in dry-run). Every action is reversible, so ``undone_at``
    + the add/remove symmetry let the user roll one back.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            message_id TEXT NOT NULL,
            thread_id TEXT,
            sender_email TEXT,
            subject TEXT,
            action_type TEXT NOT NULL,          -- 'label' | 'archive' | 'star' | 'mark_*' | 'forward'
            action_value TEXT,                  -- label name / forward destination (NULL for archive/star/mark_*)
            status TEXT NOT NULL,               -- 'applied' | 'dry_run' | 'error' | 'undone' | 'forwarding' | 'blocked'
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            undone_at TEXT
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_acct ON agent_actions(account, created_at DESC)"
    )
    # Idempotency lookup: has this exact action already been applied to this msg?
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_dedup "
        "ON agent_actions(message_id, action_type, action_value, status)"
    )
    # At-most-once for the OUTBOUND forward action (it sends mail and can't be
    # undone). A partial UNIQUE index makes the 'forwarding' claim atomic and
    # cross-process: only one writer can hold a live forward (forwarding/applied/
    # error) for a given (message, destination), so two concurrent sweeps in
    # SEPARATE processes can't both pass a check-then-insert and double-send.
    # Scoped to forward rows so it never constrains the retryable label actions.
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_actions_forward_claim "
        "ON agent_actions(message_id, action_value) "
        "WHERE action_type = 'forward' AND status IN ('forwarding', 'applied', 'error')"
    )


def _migrate_agent_digest_runs(connection: sqlite3.Connection) -> None:
    """Ledger of scheduled digest-task runs (collect → summarize → send one
    digest email). One row per (digest, account, period) so a digest sends
    AT MOST ONCE per period — the UNIQUE index is the cross-process claim, the
    same pattern that fixed the forward double-send race."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_digest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account TEXT NOT NULL,
            period_key TEXT NOT NULL,           -- 'YYYY-MM-DD' (daily) | 'YYYY-Www' (weekly)
            status TEXT NOT NULL,               -- sending|sent|ready|collected|empty|blocked|error|abandoned
            message_count INTEGER DEFAULT 0,
            sent_message_id TEXT,
            body TEXT,                          -- computed digest (for 'agent'-destination pickup)
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # body / updated_at columns added later — backfill on existing tables.
    cols = {row[1] for row in connection.execute("PRAGMA table_info(agent_digest_runs)").fetchall()}
    if "body" not in cols:
        connection.execute("ALTER TABLE agent_digest_runs ADD COLUMN body TEXT")
    if "updated_at" not in cols:
        # Can't ALTER ADD COLUMN with a non-constant DEFAULT CURRENT_TIMESTAMP;
        # add it nullable then backfill from created_at so reap_stale_digest_runs
        # can age existing rows (b156).
        connection.execute("ALTER TABLE agent_digest_runs ADD COLUMN updated_at TEXT")
        connection.execute("UPDATE agent_digest_runs SET updated_at = created_at WHERE updated_at IS NULL")
    # At-most-once per period: only one writer can claim (name, account, period).
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_runs_period "
        "ON agent_digest_runs(name, account, period_key)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_runs_recent "
        "ON agent_digest_runs(account, created_at DESC)"
    )


def _migrate_agent_digest_items(connection: sqlite3.Connection) -> None:
    """Per-message dedup ledger: one row per (digest, account, message) that has
    been INCLUDED IN A SENT digest, so the same message is never digested twice
    by the same digest (even if a query window overlaps the cadence). The UNIQUE
    index is the dedup key; recording uses INSERT OR IGNORE."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_digest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            account TEXT NOT NULL,
            message_id TEXT NOT NULL,
            period_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_items_dedup "
        "ON agent_digest_items(name, account, message_id)"
    )


def _migrate_triage_precision_history(connection: sqlite3.Connection) -> None:
    """Time series of the draft-decision's precision/recall measured on REAL
    mail (autonomy Phase A2).

    One row per snapshot (run nightly). Ground truth comes from the user's own
    verdicts on queued rows — sent/amended = the message deserved a reply;
    dismissed-as-noise/wrong-sender = it didn't — so the live false-positive
    rate is visible over time to the operator *and* to autoresearch. Read-only
    aggregate; never rewritten.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS triage_precision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT,
            computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            window_days INTEGER NOT NULL,
            precision REAL,
            recall REAL,
            f1 REAL,
            tp INTEGER NOT NULL DEFAULT 0,
            fp INTEGER NOT NULL DEFAULT 0,
            fn INTEGER NOT NULL DEFAULT 0,
            tn INTEGER NOT NULL DEFAULT 0,
            sample_size INTEGER NOT NULL DEFAULT 0,
            excluded INTEGER NOT NULL DEFAULT 0
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_precision_computed "
        "ON triage_precision_history(computed_at DESC)"
    )


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
