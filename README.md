# Recall

[![PyPI](https://img.shields.io/pypi/v/szl-recall)](https://pypi.org/project/szl-recall/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/szl-recall)](https://pypi.org/project/szl-recall/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**EU AI Act Article 12 compliant agent audit infrastructure — with persistent memory built in.**

Every tool call logged. Every log tamper-evident. Every record exportable to immutable S3 Object Lock storage.

**PyPI:** [`szl-recall`](https://pypi.org/project/szl-recall/) &nbsp;·&nbsp; **GitHub:** [`sentient-zero-labs/szl-recall`](https://github.com/sentient-zero-labs/szl-recall) &nbsp;·&nbsp; **By:** [Sentient Zero Labs](https://sentientzerolabs.com)

---

## Five-line quickstart

```bash
pip install szl-recall
export ANTHROPIC_API_KEY=sk-ant-...
recall serve --port 8000 &
recall create-token myapp
# → Bearer <token>   (add to MCP client headers)
```

Or with Docker:

```bash
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... \
  -v $(pwd)/data:/data ghcr.io/sentient-zero-labs/szl-recall:latest &
docker run --rm --network host ghcr.io/sentient-zero-labs/szl-recall:latest \
  recall create-token myapp --db /data/recall.db
```

**Add to Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "recall": {
      "url": "http://localhost:8000/mcp",
      "transport": "streamable-http",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

---

## The compliance story

EU AI Act Article 12 requires *"automatic recording over the lifetime of the system"* with tamper-evident logs for high-risk AI systems. Enforcement began August 2026. Enterprise compliance platforms (Credo AI, Fiddler AI) start at €80,000/year. There is nothing developer-accessible between free OSS tools and those platforms.

Recall fills that gap. The compliance features are the product. The memory is included.

**What Recall provides for Article 12:**

| Requirement | Recall feature |
|---|---|
| Automatic logging | `tool_call_records` — every MCP tool call logged with tool name, namespace, duration, tokens, cost |
| Tamper-evident | Hash-chained audit trail: each record includes `SHA256(prev_hash \|\| tool_name \|\| timestamp \|\| inputs_hash)`. Modifying any field breaks every hash from that point forward. |
| Immutable storage | `trigger_audit_export` uploads records to S3/R2 with Object Lock (COMPLIANCE mode, 7-year retention). Immutable even for the bucket owner. |
| Verify integrity | `verify_audit_chain` walks the hash chain and returns a signed integrity report. |
| Export for regulators | `export_compliance_report` returns NDJSON of all records in a date range. |

**What Recall provides for GDPR Article 17 (right to erasure):**

`delete_namespace_data` permanently removes all memories, tasks, operations, and audit records for a namespace in a single call. Revokes all tokens. The erasure itself is audit-logged before deletion. This is a complete erasure path — not a `clear_memories` wrapper.

---

## Why not Hindsight / Mem0 / Zep

Every comparison is based on documented behavior, not speculation.

**vs Hindsight (vectorize.io)**

Hindsight is the closest competitor. Launched May 2026, currently in n8n Cloud review queue.

| | Hindsight | Recall |
|---|---|---|
| Auth | Control-plane UI unprotected by default (Issue #1148) | Mandatory bearer auth — structural, can't be configured away |
| Namespace isolation | Tag-based (configurable, can be bypassed) | ContextVar injection from auth middleware — agent cannot lie about namespace |
| Audit trail | Not present | Every tool call logged with tokens, cost, duration |
| Tamper evidence | Not present | SHA-256 hash chain on every audit record |
| GDPR erasure | Not documented | `delete_namespace_data` — erases across all tables, revokes tokens |
| Self-hosted default | Requires external PostgreSQL | SQLite default — `pip install szl-recall && recall serve` |
| Open source | Closed source | MIT |

**vs Mem0**

Mem0 has strong memory quality and a hosted API. It does not log tool calls, does not have an audit trail, and does not address Article 12 compliance. If you need memory quality benchmarks, Mem0 is the comparison. If you need audit infrastructure, it is not the right tool.

**vs Zep**

Zep Community Edition requires PostgreSQL and does not have an audit log. Zep Cloud is hosted and charges by memory count. Neither version provides Article 12-grade tamper evidence. Recall is the migration target if Zep CE's Postgres requirement is the friction point.

---

## Twelve MCP tools

### Memory tools (7)

**`store_memory`** — Store conversation text. Returns immediately (<10ms). Background extraction with Claude Haiku produces typed memories (`preference`, `fact`, `decision`, `procedure`).

**`search_memories`** — Hybrid BM25+dense retrieval with recency weighting, MMR diversification, score threshold, and token budget trimming.

**`inspect_memories`** — Paginated list of all active memories.

**`delete_memory`** — Permanently delete a memory by ID.

**`get_memory_stats`** — Counts by type + pending extraction queue depth.

**`consolidate_memories`** — Find semantically similar memories in a topic and merge them via LLM. Reduces memory bloat over time. Requires `[embeddings]` extra.

**`delete_namespace_data`** — GDPR Article 17 erasure. Pass `confirm="DELETE MY DATA"` exactly. Irreversible.

### Compliance tools (5)

**`verify_audit_chain`** — Walk the hash chain for the current namespace. Returns:

```json
{
  "status": "ok",
  "records_checked": 142,
  "pre_chain_records": 0,
  "first_record": "...",
  "last_record": "...",
  "broken_at": null,
  "message": "Chain integrity verified across 142 records."
}
```

**`export_compliance_report`** — NDJSON of all audit records in a date range. Used for regulatory inspection exports. Each line includes `prev_hash` and `row_hash` so the chain can be verified offline.

**`get_cost_summary`** — LLM token cost breakdown grouped by session. Pass a `session_id` to see cost for a single n8n execution.

**`trigger_audit_export`** — On-demand S3/R2 Object Lock export of all unexported records. Requires `RECALL_EXPORT_BUCKET` and credentials configured.

**`score_response`** — LLM-judge faithfulness scoring via Claude Haiku. Scores a response against retrieved context (0.0–1.0). Result stored in `eval_scores` table for quality tracking.

---

## Architecture

```
Agent / Claude Desktop / n8n
        │
        Bearer <token>
        │
        ▼
BearerAuthMiddleware  ── hash(token) → api_tokens, injects namespace_ctx
TimeoutMiddleware     ── asyncio.wait_for(30s)
LoggingMiddleware     ── records ToolCallRecord on every tools/call
        │
        ▼
FastMCP (Streamable HTTP)
        │
   Memory tools                        Compliance tools
   store_memory ──► ExtractionWorker   verify_audit_chain
   search_memories   (Haiku, async)    export_compliance_report
   consolidate_memories                get_cost_summary
   delete_namespace_data               trigger_audit_export ──► ExportWorker
                                       score_response           (aioboto3, nightly)
        │
        ▼
SQLite (default) / Postgres (RECALL_DB_URL)
   memories              typed facts + decay scores
   tool_call_records     hash-chained audit log (prev_hash, row_hash)
   eval_scores           score_response quality log
   a2a_tasks             persistent A2A task state
   api_tokens            SHA-256 hashed bearer tokens
   operations            extraction job queue

                                       ▼
                          S3/R2 with Object Lock (WORM)
                          {namespace}/{YYYY-MM-DD}.ndjson
                          COMPLIANCE mode, 7-year retention
```

**Hash chain invariant:**

```
record N:
  prev_hash = row_hash of record N-1 (or SHA256(b"\x00") for namespace genesis)
  row_hash  = SHA256(prev_hash || tool_name || timestamp || inputs_hash)

If any field in any record is modified → every hash from that point breaks
verify_audit_chain detects this on any call
```

**Key invariants:**

- Every DB operation is scoped to `namespace` — injected via `ContextVar` from auth middleware, never passed as a tool argument. The agent cannot lie about its namespace.
- `store_memory` is idempotent: `INSERT OR IGNORE` + `rowcount` check makes concurrent retries safe.
- Active memories are always `WHERE valid_until IS NULL`. Superseded facts remain for audit.
- `ExportWorker` only exports records with `timestamp < today` (complete days only) and marks them `exported_at` to prevent re-export.

---

## Install

```bash
# Core (SQLite + memory + audit)
pip install szl-recall

# With dense vector search (BAAI/bge-small-en-v1.5, ~500MB first run)
pip install "szl-recall[embeddings]"

# With Postgres backend
pip install "szl-recall[postgres]"

# With S3/R2 Object Lock export
pip install "szl-recall[export]"
```

---

## Environment variables

### Required

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Haiku for memory extraction, consolidation, and `score_response` |

### Database

| Variable | Default | Description |
|---|---|---|
| `RECALL_DB_PATH` | `recall.db` | SQLite database path |
| `RECALL_DB_URL` | — | Postgres DSN — overrides SQLite when set |

### Compliance export (S3/R2 Object Lock)

| Variable | Default | Description |
|---|---|---|
| `RECALL_EXPORT_BUCKET` | — | S3/R2 bucket name. Unset = export disabled |
| `RECALL_EXPORT_ENDPOINT_URL` | — | Custom endpoint for Cloudflare R2: `https://<account>.r2.cloudflarestorage.com` |
| `RECALL_EXPORT_AWS_KEY` | — | S3/R2 access key ID |
| `RECALL_EXPORT_AWS_SECRET` | — | S3/R2 secret access key |
| `RECALL_EXPORT_AWS_REGION` | `us-east-1` | AWS region (R2: auto) |
| `RECALL_EXPORT_RETENTION_DAYS` | `2557` | Object Lock retention in days (2557 = 7 years) |

### Tuning

| Variable | Default | Description |
|---|---|---|
| `RECALL_DECAY_LAMBDA` | `0.02` | Decay rate (~35-day half-life) |
| `RECALL_DECAY_JOB_INTERVAL` | `3600` | Seconds between decay scoring runs |

---

## Development

```bash
git clone https://github.com/sentient-zero-labs/szl-recall
cd szl-recall

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest tests/ -q                        # all tests (~22s)
pytest tests/test_audit_chain.py -v     # hash-chain tests
pytest tests/test_export_worker.py -v   # S3 export tests (mocked)
ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_worker.py -v -s  # live extraction (~8s)
```

**Test coverage: 129 tests, 1 skipped (live Postgres)**

| File | What it covers |
|---|---|
| `test_audit_chain.py` | Hash chain: compute, fetch_prev_hash, insert, verify intact, verify broken, pre-chain records |
| `test_export_worker.py` | S3/R2 export: grouping, Object Lock mode, idempotency, skip-today, mark-exported |
| `test_gdpr.py` | GDPR erasure — full erasure, token revocation, operations and A2A task deletion |
| `test_search.py` | Hybrid search, superseded filtering, idempotency, ranking |
| `test_mmr.py` | MMR reranking, diversity, score_threshold, fallback |
| `test_budget.py` | Context budget trimming (`max_tokens`) |
| `test_server.py` | HTTP layer — auth, MCP routing, response shapes, error codes |
| `test_consolidation.py` | Memory consolidation — clustering, LLM merge, dry-run |
| `test_decay.py` | DecayWorker scoring, access-count protection |
| `test_postgres_backend.py` | Backend abstraction, placeholder translation, factory |
| `test_client.py` | SQLite layer — store, get, search, delete, pagination |
| `test_worker.py` | Extraction pipeline — Haiku output, queue→DB path |
| `test_models.py` | MemoryUnit validation and serialization |

---

## CLI reference

```bash
recall serve [--host 0.0.0.0] [--port 8000] [--db recall.db] [--reload]
recall create-token <namespace> [--db recall.db]
recall status [--db recall.db]
```

`<namespace>` can be any string: `alice`, `agent:code-reviewer`, `project:payments`, etc.

---

## Database schema

Seven tables in `recall.db`:

| Table | Purpose |
|---|---|
| `memories` | Core store. Active rows: `valid_until IS NULL` |
| `tool_call_records` | Hash-chained audit log — every tool call with `prev_hash`, `row_hash`, `exported_at` |
| `eval_scores` | `score_response` quality log — score, reasoning, per namespace |
| `operations` | Idempotency + extraction job lifecycle |
| `api_tokens` | SHA-256 hashed bearer tokens per namespace |
| `a2a_tasks` | Persistent A2A task state (survives restarts) |
| `schema_version` | Migration tracking |

---

## Version history

- **v0.4.0** (current): **Compliance Foundation** — hash-chained `tool_call_records` (SHA-256 chain linking every audit record), `verify_audit_chain` tool, `export_compliance_report` NDJSON tool, `get_cost_summary` per-session cost breakdown, `score_response` LLM-judge eval tool, S3/R2 Object Lock `ExportWorker` (nightly + on-demand via `trigger_audit_export`), `eval_scores` table, Docker multi-stage image published to `ghcr.io/sentient-zero-labs/szl-recall`, GitHub Actions publish workflow (multi-platform + PyPI).
- **v0.3.9**: Postgres connection exhaustion fix — `PostgresBackend` now uses a shared pool singleton. Pool closes cleanly on shutdown.
- **v0.3.8**: `BLOB` → `BYTEA` translation in Postgres schema init. `embedding` column now creates correctly.
- **v0.3.7**: Postgres schema init — inline SQL comments containing `;` no longer split `CREATE TABLE` statements.
- **v0.3.5–v0.3.6**: Postgres backend fully wired — `RECALL_DB_URL` routes all server, worker, decay, A2A, and CLI queries through `PostgresBackend`.
- **v0.3.3**: `user_id` → `namespace` across all tables, code, and CLI. Auto-migration on startup. MMR bug fix: hybrid scores used as relevance signal (not raw cosine).
- **v0.3.0–v0.3.2**: MMR diversification, context budget, GDPR erasure, A2A task persistence, Postgres backend abstraction.
- **v0.2**: Hybrid BM25Plus+RRF search, 4-component scoring, structured fact extraction, contradiction detection.
- **v0.1**: SQLite + BM25, 5 MCP tools, async extraction, bearer auth.

---

MIT — built by [Sentient Zero Labs](https://sentientzerolabs.com).
Newsletter: [read.sentientzerolabs.com](https://read.sentientzerolabs.com).
