-- Recall SQLite schema
-- Version: 5 (v0.4 — hash-chained audit trail for Article 12 compliance)
-- Apply with: aiosqlite execute_script() on first startup
-- Existing DBs: _migrate_v5() in connection.py adds hash-chain columns via ALTER TABLE

-- ------------------------------------------------------------------ --
-- Schema version tracking                                             --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version (version, applied_at, description)
VALUES (1, datetime('now'), 'Initial schema — memories + tool_call_records');

-- ------------------------------------------------------------------ --
-- Core memory store                                                   --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    namespace         TEXT NOT NULL,
    text            TEXT NOT NULL,
    type            TEXT NOT NULL,          -- preference|fact|decision|procedure
    topic           TEXT,
    importance      REAL DEFAULT 0.5,
    confidence      REAL DEFAULT 0.8,
    source_session  TEXT,
    created_at      TEXT NOT NULL,
    last_accessed   TEXT,
    access_count    INTEGER DEFAULT 0,
    decay_score     REAL,
    superseded_by   TEXT,                   -- FK to memories.id that superseded this
    embedding       BLOB,                   -- L2-normalized float32 vector (BAAI/bge-small-en-v1.5)
    -- v2: structured fact fields
    entity          TEXT,                   -- subject: "user", "project-x", "tool-y"
    attribute       TEXT,                   -- property: "preferred_language", "works_at"
    value           TEXT,                   -- value: "Python", "Acme Corp"
    valid_from      TEXT,                   -- ISO8601 — when fact became true (default: created_at)
    valid_until     TEXT,                   -- ISO8601 — NULL means still active; set on contradiction
    session_id      TEXT,                   -- conversation that produced this memory
    agent_id        TEXT,                   -- which agent stored it
    linked_ids      TEXT                    -- JSON array ["id1","id2"] of related memories
);

-- ------------------------------------------------------------------ --
-- Observability: tool call audit log                                  --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS tool_call_records (
    id              TEXT PRIMARY KEY,
    tool_name       TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    inputs_hash     TEXT NOT NULL,          -- sha256 of canonicalised inputs
    status          TEXT NOT NULL,          -- success|error|timeout
    error_code      TEXT,
    duration_ms     INTEGER NOT NULL,
    llm_tokens_in   INTEGER DEFAULT 0,
    llm_tokens_out  INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0.0,
    timestamp       TEXT NOT NULL,
    prev_hash       TEXT,                      -- row_hash of previous record in this namespace
    row_hash        TEXT,                      -- SHA256(prev_hash||tool_name||timestamp||inputs_hash)
    exported_at     TEXT                       -- ISO8601 set when exported to S3 Object Lock
);

-- ------------------------------------------------------------------ --
-- Indexes                                                              --
-- ------------------------------------------------------------------ --

CREATE INDEX IF NOT EXISTS idx_memories_user
    ON memories(namespace);

CREATE INDEX IF NOT EXISTS idx_memories_user_type
    ON memories(namespace, type);

CREATE INDEX IF NOT EXISTS idx_memories_created
    ON memories(created_at);

CREATE INDEX IF NOT EXISTS idx_memories_entity_attr
    ON memories(namespace, entity, attribute)
    WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_tool_calls_user_session
    ON tool_call_records(namespace, session_id);

CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp
    ON tool_call_records(timestamp);

CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name
    ON tool_call_records(tool_name);

-- idx_tool_calls_chain is created in _migrate_v5() (after _migrate_v4 renames user_id→namespace)

-- ------------------------------------------------------------------ --
-- Idempotency + extraction job tracking                               --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    namespace         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued', -- queued|processing|complete|failed
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_operations_user
    ON operations(namespace);

CREATE INDEX IF NOT EXISTS idx_operations_status
    ON operations(status);

-- ------------------------------------------------------------------ --
-- API token store                                                     --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS api_tokens (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT UNIQUE NOT NULL,   -- sha256 of the raw token
    namespace     TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_hash
    ON api_tokens(token_hash);

-- ------------------------------------------------------------------ --
-- A2A task store (v3: persistent — was in-memory dict in v0.1)        --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS a2a_tasks (
    id          TEXT PRIMARY KEY,
    namespace     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'submitted',  -- submitted|working|input-required|completed|failed
    input       TEXT NOT NULL,                      -- JSON: {text, topic}
    output      TEXT,                               -- JSON blob, NULL until completed
    message     TEXT,                               -- JSON blob: contradiction payload
    pending     TEXT,                               -- JSON blob: _pending_memories list
    resolution  TEXT,                               -- keep_existing|keep_new|keep_both
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_a2a_user ON a2a_tasks(namespace);

-- ------------------------------------------------------------------ --
-- Eval scores (v6 — score_response LLM-judge tool)                   --
-- ------------------------------------------------------------------ --

CREATE TABLE IF NOT EXISTS eval_scores (
    id          TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    session_id  TEXT,
    query       TEXT,
    response    TEXT,
    score       REAL,
    reasoning   TEXT,
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eval_scores_namespace
    ON eval_scores(namespace, timestamp);
