# Recall Build Research

**Research Date**: May 2026  
**Purpose**: Concrete technical decisions before writing any Recall code  
**Approach**: One recommendation per topic, with the tradeoff noted

---

## Python Package Design

### Recommendation: src/ layout + hatchling

Use the `src/` layout. It is the 2026 standard for new open-source Python packages.

```
recall/                    ← repo root
├── src/
│   └── recall/            ← actual package
│       ├── __init__.py
│       ├── client.py
│       ├── models.py
│       ├── server.py
│       ├── worker.py
│       ├── logging.py
│       ├── security.py
│       └── db/
│           ├── __init__.py
│           └── schema.sql
├── tests/
│   ├── conftest.py
│   └── test_*.py
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── LICENSE
```

**Why src/ layout**: Prevents accidentally importing the local development version instead of the installed package. Catches import errors before they reach users.

### pyproject.toml (Complete)

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "recall-memory"
version = "0.1.0"
description = "Persistent memory layer for AI agents. Local-first. Inspectable. Framework-agnostic."
readme = "README.md"
license = { text = "MIT" }
authors = [{ name = "Sentient Zero Labs" }]
requires-python = ">=3.11"
keywords = ["ai", "agents", "memory", "mcp", "llm"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Libraries",
]

dependencies = [
    "fastmcp>=3.1",
    "aiosqlite>=0.20",
    "rank-bm25>=0.2",
    "uvicorn>=0.30",
]

[project.optional-dependencies]
postgres = [
    "asyncpg>=0.29",
    "pgvector>=0.3",
]
embeddings = [
    "sentence-transformers>=3.0",
    "torch>=2.1",
]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "mypy>=1.10",
    "ruff>=0.4",
    "httpx>=0.27",   # for test client
]

[project.scripts]
recall = "recall.cli:main"     # `recall server --port 8000`

[tool.hatch.build.targets.wheel]
packages = ["src/recall"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
strict = true
python_version = "3.11"

[tool.ruff]
line-length = 88
target-version = "py311"
```

### Async/Sync Compatibility

Decision: **async-only**. No sync wrapper in v0.1.

Reason: The extraction worker, DB calls, and HTTP server are all async. A sync wrapper adds complexity and hiding.

If a user needs sync: `asyncio.run(memory.add(...))` works from sync code. Document this explicitly.

---

## FastMCP Production Patterns

### Current FastMCP API (v3.1.1)

**Server creation + transport:**
```python
from fastmcp import FastMCP

mcp = FastMCP(
    name="recall",               # simple slug, not a version string
    instructions="...",          # optional: server-level LLM instructions
    # transport set when running, not at creation
)
```

**Running with Streamable HTTP (production):**
```python
if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

**Tool registration (two equivalent forms):**
```python
# Preferred: with parentheses — allows future options
@mcp.tool()
async def store_memory(messages: list[dict], idempotency_key: str) -> dict:
    """One-line docstring for the LLM."""
    ...

# Also works (v2.7+)
@mcp.tool
async def store_memory(...):
    ...
```

**Auth — Simple Bearer Token Pattern (recommended for v0.1):**

Use Starlette middleware rather than FastMCP's BearerAuthProvider (which requires RSA key pairs). Simpler, more controllable for indie use:

```python
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

namespace_ctx: ContextVar[str | None] = ContextVar("namespace", default=None)

class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret_key: str):
        super().__init__(app)
        self.secret_key = secret_key
    
    async def dispatch(self, request, call_next):
        if request.url.path in ("/health", "/docs"):
            return await call_next(request)
        
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        
        if not token:
            return JSONResponse({"error": "Missing authorization"}, status_code=401)
        
        namespace = validate_token(token, self.secret_key)
        if not namespace:
            return JSONResponse({"error": "Invalid token"}, status_code=401)
        
        namespace_ctx.set(namespace)
        return await call_next(request)

# Add to FastMCP's underlying Starlette app
app = mcp.get_asgi_app()
app.add_middleware(BearerAuthMiddleware, secret_key=os.environ["RECALL_SECRET"])
```

**Upgrade path**: When users need multi-tenant or enterprise auth, switch to FastMCP's `BearerAuthProvider` with RSA/JWT. Same tool code, different auth layer.

**OpenTelemetry (FastMCP 3.0+ built-in):**
```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)
# FastMCP picks this up automatically — every tool call gets a span
```

### Concurrent Request Safety

FastMCP uses asyncio — all tools run on the event loop. `ContextVar` is safe for per-request state because asyncio creates a new context copy per task. **This is safe.**

Do NOT use module-level globals for per-request state. Use `ContextVar`.

---

## SQLite for Production (aiosqlite)

### Recommended Configuration

```python
import aiosqlite
import asyncio

_db: aiosqlite.Connection | None = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect("recall.db", check_same_thread=False)
        await _configure_db(_db)
    return _db

async def _configure_db(db: aiosqlite.Connection):
    pragmas = [
        "PRAGMA journal_mode = WAL",          # concurrent reads + writes
        "PRAGMA synchronous = NORMAL",         # safe but faster than FULL
        "PRAGMA cache_size = -65536",          # 64MB page cache
        "PRAGMA mmap_size = 268435456",        # 256MB memory-mapped I/O
        "PRAGMA temp_store = MEMORY",          # temp tables in memory
        "PRAGMA foreign_keys = ON",            # enforce FK constraints
    ]
    for pragma in pragmas:
        await db.execute(pragma)
    await db.commit()
    db.row_factory = aiosqlite.Row
```

**WAL mode is critical**: Without WAL, concurrent reads block writes. With WAL, readers never block writers.

### Single-Writer Limitation

SQLite allows one writer at a time. For Recall's v0.1 use case (single user session, one extraction worker), this is not a problem.

**When to migrate to Postgres**:
- Multiple concurrent users (>5 simultaneous active users writing)
- Write volume >30/second sustained
- When pgvector embeddings are needed (SQLite has no vector index)
- Multi-process deployment (SQLite WAL works across processes but not across machines)

### Migration Path

Use raw SQL for schema operations in v0.1 (don't add Alembic as a dependency yet). Provide a migration guide in the README for SQLite → Postgres.

---

## Async Background Worker

### Pattern: asyncio.Queue (Recommended for v0.1)

```python
import asyncio
from dataclasses import dataclass

@dataclass
class ExtractionJob:
    namespace: str
    messages: list[dict]
    idempotency_key: str
    retries: int = 0

_extraction_queue: asyncio.Queue[ExtractionJob] = asyncio.Queue(maxsize=5000)
MAX_RETRIES = 3

async def enqueue_extraction(job: ExtractionJob) -> None:
    try:
        _extraction_queue.put_nowait(job)
    except asyncio.QueueFull:
        raise RuntimeError("Extraction queue full — server overloaded")

async def extraction_worker() -> None:
    while True:
        job = await _extraction_queue.get()
        try:
            await _extract_and_store(job)
        except Exception as e:
            if job.retries < MAX_RETRIES:
                job.retries += 1
                await _extraction_queue.put(job)
                logger.warning(f"Extraction retry {job.retries}/{MAX_RETRIES}", 
                               extra={"job_id": job.idempotency_key})
            else:
                logger.error(f"Extraction failed after {MAX_RETRIES} retries", 
                            extra={"job_id": job.idempotency_key, "error": str(e)})
        finally:
            _extraction_queue.task_done()
```

**Explicit limitation to document**: Queue is in-memory. Server restart loses queued jobs. For v0.1, this is acceptable — the user can re-submit. Document it clearly. v0.2+ can add durable queue.

**For scheduled jobs** (pruning, decay recomputation): Use `APScheduler` with `AsyncScheduler`:

```python
from apscheduler import AsyncScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncScheduler()

@scheduler.scheduled_job(CronTrigger(hour=2, minute=0))  # 2am daily
async def prune_stale_memories():
    await decay_and_prune()
```

---

## Embeddings (When Added in Memory Series)

### Recommended Model: all-MiniLM-L6-v2

- Size: ~90MB (one-time download, cached by sentence-transformers)
- Speed: ~14ms per sentence on CPU
- Dim: 384 dimensions (pgvector default works fine)
- Quality: Strong for short texts (memories are 1-3 sentences)
- License: Apache 2.0

```python
from sentence_transformers import SentenceTransformer

# Load once, reuse (expensive initialization)
_model: SentenceTransformer | None = None

def get_embedding_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model

def embed(text: str) -> list[float]:
    model = get_embedding_model()
    return model.encode(text, normalize_embeddings=True).tolist()
```

**First-run note**: First call downloads the model (~90MB). Document this. Can be pre-downloaded with `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"`.

### pgvector Setup

```sql
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE memories ADD COLUMN embedding vector(384);

-- HNSW index: faster queries, more memory
CREATE INDEX ON memories USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Query
SELECT id, text, 1 - (embedding <=> $1::vector) AS similarity
FROM memories
WHERE namespace = $2
ORDER BY embedding <=> $1::vector
LIMIT 20;
```

**HNSW vs IVFFlat**: HNSW is better for Recall's use case — small per-user datasets (100-10000 vectors), high-recall required. IVFFlat needs large datasets to beat HNSW.

---

## Hybrid Search (BM25 + Dense + RRF)

### Implementation

```python
from rank_bm25 import BM25Okapi
from typing import TypeVar

T = TypeVar("T")

def reciprocal_rank_fusion(
    result_lists: list[list[str]],  # ranked doc IDs from each retriever
    k: int = 60,                     # Cormack et al. SIGIR 2009 standard
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for results in result_lists:
        for rank, doc_id in enumerate(results, start=1):
            scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


async def hybrid_search(
    namespace: str,
    query: str,
    limit: int = 10,
    threshold: float = 0.65,
) -> list[Memory]:
    memories = await db_get_memories(namespace)
    if not memories:
        return []
    
    # BM25 (always available — no embeddings needed)
    corpus = [m.text.split() for m in memories]
    bm25 = BM25Okapi(corpus)
    bm25_scores = bm25.get_scores(query.split())
    bm25_ranked = [memories[i].id for i in bm25_scores.argsort()[::-1][:limit*3]]
    
    # Dense (optional — only when embeddings exist)
    dense_ranked = []
    if has_embeddings(memories):
        dense_ranked = await vector_search(namespace, query, limit=limit*3)
    
    # Fuse
    lists = [bm25_ranked]
    if dense_ranked:
        lists.append([m.id for m in dense_ranked])
    
    fused = reciprocal_rank_fusion(lists)
    
    # Threshold gate — if best score is low, return nothing
    # (prevents irrelevant injection)
    if fused and fused[0][1] < threshold:
        return []
    
    top_ids = {doc_id for doc_id, _ in fused[:limit]}
    return [m for m in memories if m.id in top_ids]
```

**v0.1 (Tools series)**: BM25 only (`dense_ranked = []`). Ships functional without embeddings.  
**v0.2 (Memory series)**: Full hybrid when pgvector is configured.

---

## Admin UI (htmx)

### Pattern: Static Files Served from Python Package

```python
from importlib.resources import files
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import tempfile, shutil

def create_admin_app() -> FastAPI:
    app = FastAPI()
    
    # Copy static files to temp dir (importlib.resources gives us Path-like objects)
    static_dir = files("recall").joinpath("static")
    app.mount("/admin", StaticFiles(directory=static_dir, html=True), name="admin")
    
    @app.get("/admin/api/memories")
    async def get_memories(namespace: str, limit: int = 50, offset: int = 0):
        ...
    
    return app
```

**Package structure for UI files:**
```
src/recall/
├── static/
│   ├── admin/
│   │   ├── index.html       ← single HTML file, htmx loaded from CDN
│   │   └── styles.css       ← minimal CSS
│   └── ...
```

**htmx approach**: Load htmx from CDN in index.html (20KB, no build step). Table of memories with inline edit via htmx PATCH. Delete button via htmx DELETE. No React, no Node.

```html
<!-- admin/index.html — the entire admin UI -->
<!DOCTYPE html>
<html>
<head>
  <script src="https://unpkg.com/htmx.org@2.0/dist/htmx.min.js"></script>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div id="memories" 
       hx-get="/admin/api/memories" 
       hx-trigger="load"
       hx-swap="innerHTML">
    Loading...
  </div>
</body>
</html>
```

**Examples of Python tools that ship UI without Node.js**: mkdocs (Python docs), coverage.py (HTML reports), Jupyter (Flask-based), FastAPI's Swagger UI (bundled).

---

## Testing Strategy

### pytest-asyncio Configuration (Modern Pattern)

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"   # all async test functions run automatically
```

No `@pytest.mark.asyncio` needed on each test. Any `async def test_*` runs automatically.

### Testing FastMCP Tools

Use FastMCP's test client (available since v2.x):

```python
import pytest
from fastmcp import Client
from recall.server import mcp

@pytest.fixture
async def client():
    async with Client(mcp) as c:
        yield c

async def test_store_memory_idempotency(client):
    # First call
    result1 = await client.call_tool(
        "store_memory",
        {"messages": [...], "idempotency_key": "test-key-001"}
    )
    assert result1["status"] == "accepted"
    
    # Second call with same key — must be idempotent
    result2 = await client.call_tool(
        "store_memory", 
        {"messages": [...], "idempotency_key": "test-key-001"}
    )
    assert result2["status"] == "duplicate"
```

### Testing SQLite vs Postgres Parity

Use `pytest.fixture` with scope="session" and parameterize on DB backend:

```python
@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    if request.param == "sqlite":
        return await get_sqlite_db(tmp_path / "test.db")
    else:
        pytest.importorskip("asyncpg")  # skip if not installed
        return await get_postgres_db(os.environ.get("TEST_DATABASE_URL"))
```

Run SQLite tests always. Run Postgres tests only when `TEST_DATABASE_URL` is set.

---

## CI/CD + PyPI

### GitHub Actions (Minimal but Complete)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python-version }}" }
      - run: pip install -e ".[dev]"
      - run: pytest
      - run: mypy src/

  publish:
    needs: test
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    environment: pypi
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install build
      - run: python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          password: ${{ secrets.PYPI_API_TOKEN }}
```

**Publish flow**: Tag a commit `v0.1.0` → CI runs tests → publishes to PyPI. No manual steps.

### First PyPI Publish Checklist

```
[ ] Package name "recall-memory" is available on PyPI (check pypi.org)
[ ] README renders correctly on PyPI (test with `twine check dist/*`)
[ ] Version is set to 0.1.0 in pyproject.toml
[ ] LICENSE file is present and referenced in pyproject.toml
[ ] Tests pass on Python 3.11 and 3.12
[ ] `python -m build` produces clean wheel and sdist
[ ] Manual test: `pip install dist/recall_memory-0.1.0-py3-none-any.whl` in clean venv
```

---

## CHANGELOG Format

Use Keep a Changelog (keepachangelog.com):

```markdown
# Changelog

## [Unreleased]

## [0.1.0] - 2026-05-XX
### Added
- FastMCP server with 5 tools (store_memory, search_memories, inspect_memories, delete_memory, get_memory_stats)
- SQLite storage with WAL mode
- Bearer token authentication middleware
- BM25 keyword search
- Tool call logging (ToolCallRecord)
- Security validation on startup

[Unreleased]: https://github.com/sentientzerolabs/recall/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sentientzerolabs/recall/releases/tag/v0.1.0
```
