import sqlite3
from pathlib import Path

from app.core.settings import get_settings


def resolve_sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// URLs are supported by the bootstrap script.")
    return Path(database_url.removeprefix(prefix))


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


def _migrate_reply_pairs(connection: sqlite3.Connection) -> None:
    """Add quality_score column to reply_pairs if missing."""
    cols = {row[1] for row in connection.execute("PRAGMA table_info(reply_pairs)").fetchall()}
    if "quality_score" not in cols:
        connection.execute("ALTER TABLE reply_pairs ADD COLUMN quality_score REAL DEFAULT 1.0")


def _migrate_sender_profiles(connection: sqlite3.Connection) -> None:
    """Add avg_response_hours column to sender_profiles if missing."""
    try:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(sender_profiles)").fetchall()}
    except Exception:
        return
    if "avg_response_hours" not in cols:
        connection.execute("ALTER TABLE sender_profiles ADD COLUMN avg_response_hours REAL")


def _populate_fts(connection: sqlite3.Connection) -> None:
    """Rebuild FTS5 indexes from the source tables."""
    connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    connection.execute("INSERT INTO reply_pairs_fts(reply_pairs_fts) VALUES ('rebuild')")
