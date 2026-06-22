"""Database connection management for Recall (SQLite + Postgres)."""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

_DB_PATH: Path = Path(os.environ.get("RECALL_DB_PATH", "recall.db"))
_SCHEMA_PATH: Path = Path(__file__).parent / "schema.sql"

# Columns added in schema v2 — applied via ALTER TABLE for existing DBs
_V2_COLUMNS = [
    ("entity", "TEXT"),
    ("attribute", "TEXT"),
    ("value", "TEXT"),
    ("valid_from", "TEXT"),
    ("valid_until", "TEXT"),
    ("session_id", "TEXT"),
    ("agent_id", "TEXT"),
    ("linked_ids", "TEXT"),
]


def set_db_path(path: str | Path) -> None:
    global _DB_PATH
    _DB_PATH = Path(path)


async def _migrate_v2(db: aiosqlite.Connection) -> None:
    """Add v2 structured fact columns to an existing memories table."""
    for col, coltype in _V2_COLUMNS:
        try:
            await db.execute(f"ALTER TABLE memories ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # column already exists — safe to ignore
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_entity_attr "
            "ON memories(namespace, entity, attribute) WHERE valid_until IS NULL"
        )
    except Exception:
        pass
    await db.commit()


async def _migrate_v3(db: aiosqlite.Connection) -> None:
    """Create a2a_tasks table for persistent A2A task storage (v3)."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS a2a_tasks (
            id          TEXT PRIMARY KEY,
            namespace     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'submitted',
            input       TEXT NOT NULL,
            output      TEXT,
            message     TEXT,
            pending     TEXT,
            resolution  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_a2a_user ON a2a_tasks(namespace)"
    )
    await db.commit()


_V5_COLUMNS = [
    ("tool_call_records", "prev_hash",   "TEXT"),
    ("tool_call_records", "row_hash",    "TEXT"),
    ("tool_call_records", "exported_at", "TEXT"),
]


async def _migrate_v5(db: aiosqlite.Connection) -> None:
    """Add hash-chain columns to tool_call_records (v5 — Article 12 tamper evidence)."""
    for table, col, coltype in _V5_COLUMNS:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # column already exists
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_chain "
            "ON tool_call_records(namespace, timestamp, id)"
        )
    except Exception:
        pass
    await db.commit()


async def _migrate_v4(db: aiosqlite.Connection) -> None:
    """Rename user_id → namespace across all tables (v4 — SQLite 3.25+ RENAME COLUMN)."""
    renames = [
        ("memories",          "user_id", "namespace"),
        ("operations",        "user_id", "namespace"),
        ("api_tokens",        "user_id", "namespace"),
        ("tool_call_records", "user_id", "namespace"),
        ("a2a_tasks",         "user_id", "namespace"),
    ]
    for table, old_col, new_col in renames:
        try:
            # Check if old column still exists before renaming
            info = await db.execute_fetchall(f"PRAGMA table_info({table})")
            cols = [row[1] for row in info]
            if old_col in cols and new_col not in cols:
                await db.execute(
                    f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}"
                )
        except Exception:
            pass  # already renamed or table doesn't exist
    await db.commit()


async def _migrate_v6(db: aiosqlite.Connection) -> None:
    """Create eval_scores table for score_response LLM-judge tool (v6)."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS eval_scores (
            id          TEXT PRIMARY KEY,
            namespace   TEXT NOT NULL,
            session_id  TEXT,
            query       TEXT,
            response    TEXT,
            score       REAL,
            reasoning   TEXT,
            timestamp   TEXT NOT NULL
        )"""
    )
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_scores_namespace "
            "ON eval_scores(namespace, timestamp)"
        )
    except Exception:
        pass
    await db.commit()


async def _init_sqlite() -> None:
    schema = _SCHEMA_PATH.read_text()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.executescript(schema)

        # WAL mode for concurrent reads + writes without blocking
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.commit()

        await _migrate_v2(db)
        await _migrate_v3(db)
        await _migrate_v4(db)
        await _migrate_v5(db)
        await _migrate_v6(db)


async def _init_postgres() -> None:
    """Create tables for a Postgres backend. Schema.sql is translated at execution time.

    Runs each DDL statement individually (no executescript equivalent in asyncpg).
    Idempotent — uses CREATE TABLE/INDEX IF NOT EXISTS + ON CONFLICT DO NOTHING.
    Skips SQLite-specific migrations; Postgres deployments start with the current schema.
    """
    from recall.db.backend import get_backend
    schema_sql = _SCHEMA_PATH.read_text()
    # Strip inline -- comments from each line before splitting by ; to avoid false
    # splits on semicolons that appear inside SQL comments (e.g. "-- valid; see docs")
    stripped_lines = []
    for line in schema_sql.splitlines():
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        stripped_lines.append(line)
    schema_clean = "\n".join(stripped_lines)
    async with get_backend() as db:
        for stmt in schema_clean.split(";"):
            clean = stmt.strip()
            if not clean:
                continue
            await db.execute(clean)
        await db.commit()

    # Apply v5 hash-chain columns to existing Postgres deployments.
    # Safe no-op on fresh deployments (schema.sql already includes the columns).
    async with get_backend() as db:
        for _table, col, coltype in _V5_COLUMNS:
            try:
                await db.execute(
                    f"ALTER TABLE tool_call_records ADD COLUMN IF NOT EXISTS {col} {coltype}"
                )
            except Exception:
                pass
        await db.commit()


async def init_db() -> None:
    """Create tables and apply schema. Idempotent — safe to call on every startup."""
    if os.environ.get("RECALL_DB_URL"):
        await _init_postgres()
    else:
        await _init_sqlite()


def get_db_path() -> Path:
    return _DB_PATH
