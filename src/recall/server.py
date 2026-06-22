"""Recall MCP server — all 6 tools, auth middleware, 30s timeout."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import anthropic
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from recall.db.backend import close_pg_pool, get_backend
from recall.db.connection import init_db
from recall.decay import DecayWorker
from recall.export_worker import ExportWorker
from recall.logging import SENTINEL_HASH, LoggingMiddleware, compute_row_hash
from recall.security import hash_token, validate_tool_descriptions
from recall.worker import ExtractionWorker

logger = logging.getLogger(__name__)

namespace_ctx: ContextVar[str] = ContextVar("namespace", default="")
extraction_worker: ExtractionWorker | None = None
decay_worker: DecayWorker | None = None
export_worker: ExportWorker | None = None

_VALID_MEMORY_TYPES = frozenset({"preference", "fact", "decision", "procedure"})

_MERGE_PROMPT = """\
Given these related memories about the same topic, produce ONE canonical memory that \
captures all distinct information. Be specific. Preserve concrete details. Do not generalize.

Memories:
{memories}

Return ONLY a JSON object: {{"text": "...", "type": "preference|fact|decision|procedure", "importance": 0.0-1.0}}"""


# ── Middleware ────────────────────────────────────────────────────────────────

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Agent Card is publicly discoverable — no auth required
        if request.url.path.startswith("/.well-known"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        namespace = await _validate_token(token)
        if not namespace:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        namespace_ctx.set(namespace)
        return await call_next(request)


class TimeoutMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        try:
            return await asyncio.wait_for(call_next(request), timeout=30.0)
        except asyncio.TimeoutError:
            return JSONResponse(
                {
                    "status": "error",
                    "error": "Tool call exceeded 30s limit.",
                    "code": "TOOL_TIMEOUT",
                },
                status_code=504,
            )


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    global extraction_worker, decay_worker, export_worker
    await init_db()
    _validate_server_descriptions()
    extraction_worker = ExtractionWorker()
    await extraction_worker.start()
    decay_worker = DecayWorker()
    await decay_worker.start()
    export_worker = ExportWorker()
    await export_worker.start()
    await _recover_orphaned_operations()
    yield
    if extraction_worker:
        await extraction_worker.stop()
    if decay_worker:
        await decay_worker.stop()
    if export_worker:
        await export_worker.stop()
    await close_pg_pool()


async def _recover_orphaned_operations() -> None:
    """Mark any queued/processing operations left over from a prior crash as failed."""
    async with get_backend() as db:
        rowcount = await db.execute(
            "UPDATE operations SET status = 'failed', updated_at = datetime('now') "
            "WHERE status IN ('queued', 'processing')",
        )
        await db.commit()
    if rowcount:
        logger.warning(
            "startup_orphan_recovery",
            extra={"count": rowcount},
        )


def _validate_server_descriptions() -> None:
    """Validate all tool descriptions against poisoning patterns. Fail fast."""
    tool_functions = [
        store_memory, search_memories, inspect_memories,
        delete_memory, get_memory_stats, consolidate_memories,
        verify_audit_chain, export_compliance_report, get_cost_summary,
        trigger_audit_export, score_response,
    ]
    descriptions = {
        f.__name__: (f.__doc__ or "").split("\n")[0].strip()
        for f in tool_functions
    }
    validate_tool_descriptions(descriptions)


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP("recall", lifespan=_lifespan)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def store_memory(
    text: str,
    topic: str,
    idempotency_key: str = "",
    session_id: str = "",
    agent_id: str = "",
) -> dict:
    """Store conversation messages for background memory extraction.
    Returns immediately — extraction runs async. Safe to retry with the same key."""
    namespace = namespace_ctx.get()
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    job_id = str(uuid.uuid4())
    async with get_backend() as db:
        rowcount = await db.execute(
            "INSERT OR IGNORE INTO operations (id, idempotency_key, namespace, status) "
            "VALUES (?,?,?,'queued')",
            (job_id, idempotency_key, namespace),
        )
        await db.commit()
        if rowcount == 0:
            return {"status": "ok", "data": {"queued": False, "cached": True}, "error": None}

    if extraction_worker:
        await extraction_worker.enqueue({
            "job_id": job_id,
            "namespace": namespace,
            "text": text,
            "topic": topic,
            "session_id": session_id,
            "agent_id": agent_id,
        })

    return {"status": "ok", "data": {"queued": True, "job_id": job_id}, "error": None}


@mcp.tool()
async def search_memories(
    query: str,
    limit: int = 20,
    recency_weight: float = 0.3,
    mmr_lambda: float = 0.5,
    score_threshold: float = 0.0,
    max_tokens: int | None = None,
) -> dict:
    """Search memories using hybrid retrieval (~200-500ms).

    recency_weight 0-1 upweights recent results.
    mmr_lambda 0-1: 0=max diversity, 1=pure relevance (default 0.5).
    score_threshold: drop candidates below this hybrid score (default 0.0 = keep all).
    max_tokens: trim results to fit within this token budget (None = no limit).
    """
    if not 0.0 <= recency_weight <= 1.0:
        return {"status": "error", "error": "recency_weight must be between 0.0 and 1.0.", "code": "INVALID_PARAM"}
    if not 0.0 <= mmr_lambda <= 1.0:
        return {"status": "error", "error": "mmr_lambda must be between 0.0 and 1.0.", "code": "INVALID_PARAM"}
    namespace = namespace_ctx.get()
    results = await _hybrid_search(
        namespace, query, min(limit, 50), recency_weight, mmr_lambda, score_threshold
    )
    if max_tokens is not None:
        results = _apply_budget(results, max_tokens)
    return {"status": "ok", "data": {"results": results, "total": len(results)}, "error": None}


@mcp.tool()
async def inspect_memories(limit: int = 20, offset: int = 0) -> dict:
    """List stored memories with pagination. Default 20 per page, max 50."""
    limit = min(limit, 50)
    namespace = namespace_ctx.get()

    async with get_backend() as db:
        rows = await db.fetch_all(
            "SELECT id, text, topic, importance, type, created_at "
            "FROM memories WHERE namespace = ? AND valid_until IS NULL "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (namespace, limit, offset),
        )
        count = await db.fetch_all(
            "SELECT COUNT(*) FROM memories WHERE namespace = ? AND valid_until IS NULL",
            (namespace,),
        )
        total = count[0][0]

    memories = [
        {
            "id": r[0],
            "text": r[1],
            "topic": r[2],
            "importance": r[3],
            "type": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "data": {
            "memories": memories,
            "total": total,
            "has_more": offset + limit < total,
            "next_offset": offset + limit if offset + limit < total else None,
        },
        "error": None,
    }


@mcp.tool()
async def delete_memory(memory_id: str) -> dict:
    """Permanently delete a memory by ID. This action cannot be undone."""
    namespace = namespace_ctx.get()

    async with get_backend() as db:
        rowcount = await db.execute(
            "DELETE FROM memories WHERE id = ? AND namespace = ?",
            (memory_id, namespace),
        )
        await db.commit()
        if rowcount == 0:
            return {
                "status": "error",
                "error": (
                    f"Memory '{memory_id}' not found. "
                    "Use inspect_memories to list valid IDs."
                ),
                "code": "MEMORY_NOT_FOUND",
            }

    return {"status": "ok", "data": {"deleted": memory_id}, "error": None}


@mcp.tool()
async def get_memory_stats() -> dict:
    """Return memory counts and storage stats for the current user. Fast health check."""
    namespace = namespace_ctx.get()

    async with get_backend() as db:
        by_type = await db.fetch_all(
            "SELECT type, COUNT(*) FROM memories "
            "WHERE namespace = ? AND valid_until IS NULL GROUP BY type",
            (namespace,),
        )
        pending = await db.fetch_all(
            "SELECT COUNT(*) FROM operations WHERE namespace = ? AND status = 'queued'",
            (namespace,),
        )

    return {
        "status": "ok",
        "data": {
            "by_type": dict(by_type),
            "total": sum(r[1] for r in by_type),
            "pending_extractions": pending[0][0],
        },
        "error": None,
    }


@mcp.tool()
async def consolidate_memories(
    topic: str,
    similarity_threshold: float = 0.85,
    dry_run: bool = False,
) -> dict:
    """Find semantically similar memories in a topic and merge them into canonical facts.
    Requires embeddings extra: pip install 'szl-recall[embeddings]'. Returns a diff."""
    namespace = namespace_ctx.get()

    # Check embeddings available before any DB work
    from recall.embeddings import embed
    probe = embed(["test"])
    if probe is None:
        return {
            "status": "error",
            "error": "consolidate_memories requires embeddings: pip install 'szl-recall[embeddings]'",
            "code": "EMBEDDINGS_REQUIRED",
        }

    # Fetch all active memories for this topic
    async with get_backend() as db:
        rows = await db.fetch_all(
            "SELECT id, text, type, importance "
            "FROM memories WHERE namespace = ? AND topic = ? AND valid_until IS NULL "
            "ORDER BY created_at DESC",
            (namespace, topic),
        )

    if not rows:
        return {
            "status": "ok",
            "data": {
                "groups_found": 0, "memories_consolidated": 0,
                "memories_created": 0, "deleted_ids": [], "created": [], "dry_run": dry_run,
            },
            "error": None,
        }

    memories = [{"id": r[0], "text": r[1], "type": r[2], "importance": r[3]} for r in rows]

    # Compute embeddings for all fetched memories
    texts = [m["text"] for m in memories]
    vecs = embed(texts)
    if vecs is None:
        return {
            "status": "error",
            "error": "consolidate_memories requires embeddings: pip install 'szl-recall[embeddings]'",
            "code": "EMBEDDINGS_REQUIRED",
        }
    for i, m in enumerate(memories):
        m["_vec"] = vecs[i]

    # Group by cosine similarity — only merge groups with 2+ members
    groups = _greedy_cluster(memories, similarity_threshold)
    merge_groups = [g for g in groups if len(g) >= 2]

    if not merge_groups:
        return {
            "status": "ok",
            "data": {
                "groups_found": 0, "memories_consolidated": 0,
                "memories_created": 0, "deleted_ids": [], "created": [], "dry_run": dry_run,
            },
            "error": None,
        }

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "status": "error",
            "error": "ANTHROPIC_API_KEY not set.",
            "code": "NO_API_KEY",
        }

    llm_client = anthropic.AsyncAnthropic(api_key=api_key)
    deleted_ids: list[str] = []
    created_memories: list[dict] = []

    try:
        for group in merge_groups:
            merged = await _llm_merge(llm_client, group, topic)
            if merged:
                deleted_ids.extend(m["id"] for m in group)
                created_memories.append(merged)
    finally:
        await llm_client.close()

    if dry_run:
        return {
            "status": "ok",
            "data": {
                "groups_found": len(merge_groups),
                "memories_consolidated": len(deleted_ids),
                "memories_created": len(created_memories),
                "deleted_ids": deleted_ids,
                "created": [{"text": m["text"], "type": m["type"], "importance": m["importance"]} for m in created_memories],
                "dry_run": True,
            },
            "error": None,
        }

    # Persist: insert canonical memories, supersede originals
    now = datetime.now(timezone.utc).isoformat()
    persisted: list[dict] = []
    async with get_backend() as db:
        for merged in created_memories:
            new_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO memories (id, namespace, text, type, topic, importance, created_at, valid_from) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (new_id, namespace, merged["text"], merged["type"], topic, merged["importance"], now, now),
            )
            persisted.append({"id": new_id, "text": merged["text"], "type": merged["type"], "topic": topic})
        for del_id in deleted_ids:
            await db.execute(
                "UPDATE memories SET valid_until = ? WHERE id = ? AND namespace = ?",
                (now, del_id, namespace),
            )
        await db.commit()

    return {
        "status": "ok",
        "data": {
            "groups_found": len(merge_groups),
            "memories_consolidated": len(deleted_ids),
            "memories_created": len(persisted),
            "deleted_ids": deleted_ids,
            "created": persisted,
            "dry_run": False,
        },
        "error": None,
    }


@mcp.tool()
async def delete_namespace_data(confirm: str) -> dict:
    """Permanently delete ALL data for the current user. Irreversible.

    Pass confirm='DELETE MY DATA' exactly to proceed.
    Deletes: all memories, A2A tasks, pending operations, and tool-call records.
    Revokes (does not delete) API tokens so the user knows their auth is gone.
    """
    if confirm != "DELETE MY DATA":
        return {
            "status": "error",
            "error": "Pass confirm='DELETE MY DATA' exactly to proceed.",
            "code": "CONFIRM_REQUIRED",
        }
    namespace = namespace_ctx.get()
    async with get_backend() as db:
        await db.execute("DELETE FROM memories WHERE namespace = ?", (namespace,))
        await db.execute("DELETE FROM a2a_tasks WHERE namespace = ?", (namespace,))
        await db.execute("DELETE FROM operations WHERE namespace = ?", (namespace,))
        await db.execute(
            "DELETE FROM tool_call_records WHERE namespace = ? AND tool_name != 'delete_namespace_data'",
            (namespace,),
        )
        tokens_revoked = await db.execute(
            "UPDATE api_tokens SET revoked = 1 WHERE namespace = ?", (namespace,)
        )
        await db.commit()
    return {
        "status": "ok",
        "data": {"tokens_revoked": tokens_revoked},
        "error": None,
    }


@mcp.tool()
async def verify_audit_chain(
    start_id: str = "",
    end_id: str = "",
) -> dict:
    """Verify the tamper-evident hash chain for the current namespace's audit records.

    Returns status 'ok' if the chain is intact, 'broken' if any record was modified.
    Optionally restrict verification to a range with start_id / end_id.
    """
    namespace = namespace_ctx.get()

    async with get_backend() as db:
        rows = await db.fetch_all(
            "SELECT id, tool_name, timestamp, inputs_hash, prev_hash, row_hash "
            "FROM tool_call_records WHERE namespace = ? "
            "ORDER BY timestamp ASC, id ASC",
            (namespace,),
        )

    # Filter to start/end range if specified
    if start_id or end_id:
        ids = [r[0] for r in rows]
        s = ids.index(start_id) if start_id and start_id in ids else 0
        e = ids.index(end_id) + 1 if end_id and end_id in ids else len(rows)
        rows = rows[s:e]

    pre_chain = len([r for r in rows if r[5] is None])
    chained = [r for r in rows if r[5] is not None]

    if not chained:
        return {
            "status": "ok",
            "data": {
                "status": "ok",
                "records_checked": 0,
                "pre_chain_records": pre_chain,
                "first_record": None,
                "last_record": None,
                "broken_at": None,
                "message": "No hash-chained records yet. Records predate v0.4.",
            },
            "error": None,
        }

    expected_prev = SENTINEL_HASH
    broken_at: str | None = None
    checked = 0

    for row_id, tool_name, timestamp, inputs_hash, prev_hash, row_hash in chained:
        if prev_hash != expected_prev:
            broken_at = row_id
            break
        expected_hash = compute_row_hash(prev_hash, tool_name, timestamp, inputs_hash)
        if row_hash != expected_hash:
            broken_at = row_id
            break
        expected_prev = row_hash
        checked += 1

    result_status = "broken" if broken_at else "ok"
    return {
        "status": "ok",
        "data": {
            "status": result_status,
            "records_checked": checked,
            "pre_chain_records": pre_chain,
            "first_record": chained[0][0],
            "last_record": chained[-1][0],
            "broken_at": broken_at,
            "message": (
                f"Chain integrity verified across {checked} records."
                if result_status == "ok"
                else f"Chain broken at record {broken_at}. Records after this point are unverifiable."
            ),
        },
        "error": None,
    }


@mcp.tool()
async def export_compliance_report(
    from_date: str = "",
    to_date: str = "",
) -> dict:
    """Export audit records as NDJSON for the current namespace.

    from_date / to_date: ISO8601 date strings (e.g. '2026-01-01'). Empty = no bound.
    Returns NDJSON of all tool_call_records in the range — suitable for regulatory inspection.
    """
    namespace = namespace_ctx.get()

    conditions = ["namespace = ?"]
    params: list = [namespace]
    if from_date:
        conditions.append("timestamp >= ?")
        params.append(from_date)
    if to_date:
        tail = to_date + "T23:59:59" if "T" not in to_date else to_date
        conditions.append("timestamp <= ?")
        params.append(tail)

    where = " AND ".join(conditions)
    async with get_backend() as db:
        rows = await db.fetch_all(
            f"SELECT id, tool_name, session_id, inputs_hash, status, error_code, "
            f"duration_ms, llm_tokens_in, llm_tokens_out, cost_usd, timestamp, prev_hash, row_hash "
            f"FROM tool_call_records WHERE {where} ORDER BY timestamp ASC, id ASC",
            tuple(params),
        )

    records = [
        {
            "id": r[0], "tool_name": r[1], "namespace": namespace,
            "session_id": r[2], "inputs_hash": r[3], "status": r[4],
            "error_code": r[5], "duration_ms": r[6],
            "llm_tokens_in": r[7], "llm_tokens_out": r[8], "cost_usd": r[9],
            "timestamp": r[10], "prev_hash": r[11], "row_hash": r[12],
        }
        for r in rows
    ]
    ndjson = "\n".join(json.dumps(rec) for rec in records)

    return {
        "status": "ok",
        "data": {
            "record_count": len(records),
            "from_date": from_date or None,
            "to_date": to_date or None,
            "ndjson": ndjson,
        },
        "error": None,
    }


@mcp.tool()
async def get_cost_summary(
    session_id: str = "",
    from_date: str = "",
    to_date: str = "",
) -> dict:
    """Return LLM cost breakdown grouped by session.

    Pass session_id to filter to a single n8n workflow execution.
    from_date / to_date: ISO8601 date strings (e.g. '2026-01-01'). Empty = no bound.
    """
    namespace = namespace_ctx.get()

    conditions = ["namespace = ?"]
    params: list = [namespace]
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if from_date:
        conditions.append("timestamp >= ?")
        params.append(from_date)
    if to_date:
        tail = to_date + "T23:59:59" if "T" not in to_date else to_date
        conditions.append("timestamp <= ?")
        params.append(tail)

    where = " AND ".join(conditions)
    async with get_backend() as db:
        rows = await db.fetch_all(
            f"SELECT session_id, tool_name, llm_tokens_in, llm_tokens_out, cost_usd, timestamp "
            f"FROM tool_call_records WHERE {where} ORDER BY timestamp ASC",
            tuple(params),
        )

    sessions: dict[str, dict] = {}
    for sid_raw, tool_name, tokens_in, tokens_out, cost, ts in rows:
        sid = sid_raw or "unknown"
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "tool_calls": 0,
                "llm_tokens_in": 0,
                "llm_tokens_out": 0,
                "cost_usd": 0.0,
                "first_call": ts,
                "last_call": ts,
                "tools_used": [],
            }
        s = sessions[sid]
        s["tool_calls"] += 1
        s["llm_tokens_in"] += tokens_in or 0
        s["llm_tokens_out"] += tokens_out or 0
        s["cost_usd"] = round(s["cost_usd"] + (cost or 0.0), 6)
        s["last_call"] = ts
        if tool_name not in s["tools_used"]:
            s["tools_used"].append(tool_name)

    session_list = sorted(sessions.values(), key=lambda x: x["first_call"])
    return {
        "status": "ok",
        "data": {
            "sessions": session_list,
            "total_cost_usd": round(sum(s["cost_usd"] for s in session_list), 6),
            "total_tool_calls": sum(s["tool_calls"] for s in session_list),
        },
        "error": None,
    }


@mcp.tool()
async def trigger_audit_export() -> dict:
    """Trigger an immediate export of unexported audit records to S3/R2 Object Lock.

    Requires RECALL_EXPORT_BUCKET, RECALL_EXPORT_AWS_KEY, RECALL_EXPORT_AWS_SECRET.
    Returns a summary of files uploaded and records exported.
    """
    if export_worker is None or not export_worker.enabled:
        return {
            "status": "error",
            "error": (
                "Export not configured. "
                "Set RECALL_EXPORT_BUCKET, RECALL_EXPORT_AWS_KEY, RECALL_EXPORT_AWS_SECRET."
            ),
            "code": "EXPORT_NOT_CONFIGURED",
        }
    summary = await export_worker.export_pending()
    return {"status": "ok", "data": summary, "error": None}


_SCORE_PROMPT = """\
You are an evaluator. Given a query, a response, and optional retrieved context, \
score how faithfully the response answers the query using ONLY information in the context.

Query: {query}
Response: {response}
Context: {context}

Return ONLY valid JSON: {{"score": 0.0-1.0, "reasoning": "one sentence", "issues": []}}
Score 1.0 = fully faithful, 0.0 = completely hallucinated."""


@mcp.tool()
async def score_response(
    query: str,
    response: str,
    context_used: str = "",
    session_id: str = "",
) -> dict:
    """Score a response for faithfulness to retrieved context using Claude Haiku as judge.

    Returns a score (0.0-1.0), reasoning, and a list of faithfulness issues found.
    Useful in n8n workflows to measure memory retrieval quality per execution.
    Requires ANTHROPIC_API_KEY.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"status": "error", "error": "ANTHROPIC_API_KEY not set.", "code": "NO_API_KEY"}

    namespace = namespace_ctx.get()
    prompt = _SCORE_PROMPT.format(
        query=query,
        response=response,
        context=context_used or "(none provided)",
    )

    llm_client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        msg = await llm_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
    except (json.JSONDecodeError, ValueError, IndexError) as exc:
        return {"status": "error", "error": f"LLM returned unparseable output: {exc}", "code": "PARSE_ERROR"}
    finally:
        await llm_client.close()

    score = max(0.0, min(1.0, float(result.get("score", 0.0))))
    reasoning = str(result.get("reasoning", ""))
    issues: list[str] = [str(i) for i in result.get("issues", [])]
    now = datetime.now(timezone.utc).isoformat()

    async with get_backend() as db:
        await db.execute(
            "INSERT INTO eval_scores (id, namespace, session_id, query, response, score, reasoning, timestamp) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), namespace, session_id or "", query, response, score, reasoning, now),
        )
        await db.commit()

    return {
        "status": "ok",
        "data": {"score": score, "reasoning": reasoning, "issues": issues},
        "error": None,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _validate_token(token: str) -> str | None:
    if not token:
        return None
    token_hash = hash_token(token)
    async with get_backend() as db:
        rows = await db.fetch_all(
            "SELECT namespace FROM api_tokens WHERE token_hash = ? AND revoked = 0",
            (token_hash,),
        )
    return rows[0][0] if rows else None


def _parse_ts(ts: str) -> datetime:
    """Parse ISO8601 timestamp from SQLite, ensuring UTC timezone."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bm25_ranks(query: str, texts: list[str]) -> list[int] | None:
    """Return per-document BM25 rank (1-based). None if rank_bm25 not installed.

    Uses BM25Plus over BM25Okapi: BM25Okapi IDF collapses to 0 with N=2 when a term
    appears in exactly one document (log(1.5/1.5)=0), causing all scores to be 0.
    BM25Plus adds a lower-bound delta that keeps scores positive for small corpora.
    """
    try:
        from rank_bm25 import BM25Plus
        tokenized = [t.lower().split() for t in texts]
        bm25 = BM25Plus(tokenized)
        scores = list(bm25.get_scores(query.lower().split()))
        # rank = position in descending score order (0-based → 1-based)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        ranks = [0] * len(scores)
        for rank, idx in enumerate(order):
            ranks[idx] = rank + 1  # 1-based
        # Docs with zero BM25Plus score have no query term overlap — push to worst rank
        for i, s in enumerate(scores):
            if s == 0:
                ranks[i] = len(scores) + 1
        return ranks
    except ImportError:
        return None


def _dense_ranks(query: str, texts: list[str]) -> list[int] | None:
    """Return per-document cosine similarity rank (1-based). None if model unavailable."""
    from recall.embeddings import cosine_scores, embed, embed_query
    q_vec = embed_query(query)
    if q_vec is None:
        return None
    doc_vecs = embed(texts)
    if doc_vecs is None:
        return None
    sims = cosine_scores(q_vec, doc_vecs)
    order = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)
    ranks = [0] * len(sims)
    for rank, idx in enumerate(order):
        ranks[idx] = rank + 1
    return ranks


def _mmr_rerank(
    candidates: list[dict],
    query_vec: "np.ndarray | None",
    mmr_lambda: float,
    limit: int,
    hybrid_scores: "list[float] | None" = None,
) -> list[dict]:
    """Max-Marginal Relevance reranking for diversity.

    Iteratively selects the candidate that maximises:
      mmr_lambda * relevance(doc) - (1 - mmr_lambda) * max(embed_sim(doc, selected))

    relevance(doc) = hybrid_scores[i] when provided, else cosine similarity to query.
    Using hybrid_scores means mmr_lambda=1.0 preserves the full decay/recency/importance
    ordering from the hybrid ranker, rather than collapsing to pure embedding similarity.

    Falls back to candidates[:limit] when embeddings are unavailable.
    """
    if query_vec is None:
        return candidates[:limit]

    from recall.embeddings import embed
    import numpy as np

    doc_vecs = embed([m["text"] for m in candidates])
    if doc_vecs is None:
        return candidates[:limit]

    # Relevance signal: normalised hybrid scores when available, otherwise cosine similarity
    if hybrid_scores is not None:
        max_s = max(hybrid_scores) or 1.0
        rel_scores = [s / max_s for s in hybrid_scores]
    else:
        rel_scores = (doc_vecs @ query_vec).tolist()

    selected: list[int] = []
    remaining = list(range(len(candidates)))

    while len(selected) < limit and remaining:
        best_i, best_score = None, float("-inf")
        for i in remaining:
            redundancy = float(np.max(doc_vecs[selected] @ doc_vecs[i])) if selected else 0.0
            mmr_score = mmr_lambda * rel_scores[i] - (1.0 - mmr_lambda) * redundancy
            if mmr_score > best_score:
                best_score, best_i = mmr_score, i
        if best_i is None:
            break
        selected.append(best_i)
        remaining.remove(best_i)

    return [candidates[i] for i in selected]


def _rrf_fuse(
    bm25_ranks: list[int] | None,
    dense_ranks: list[int] | None,
    k: int = 60,
) -> list[float]:
    """Reciprocal Rank Fusion. Returns a score per document (higher = better match)."""
    n = len(bm25_ranks or dense_ranks or [])
    if n == 0:
        return []
    scores = [0.0] * n
    for ranks in (bm25_ranks, dense_ranks):
        if ranks is None:
            continue
        for i, r in enumerate(ranks):
            if r <= n:  # r == n+1 means zero BM25 signal — skip
                scores[i] += 1.0 / (k + r)
    return scores


def _greedy_cluster(memories: list[dict], threshold: float) -> list[list[dict]]:
    """Group memories by cosine similarity using greedy assignment.

    Each unassigned memory starts a new cluster. Subsequent memories join the
    cluster of the first memory they exceed `threshold` similarity with.
    Vectors must be pre-computed and stored as m['_vec'].
    """
    import numpy as np

    if len(memories) <= 1:
        return [[m] for m in memories]

    vecs = np.stack([m["_vec"] for m in memories])
    sim_matrix = vecs @ vecs.T  # cosine similarity (vectors already L2-normalized)

    assigned = [False] * len(memories)
    groups: list[list[dict]] = []

    for i in range(len(memories)):
        if assigned[i]:
            continue
        group = [memories[i]]
        assigned[i] = True
        for j in range(i + 1, len(memories)):
            if not assigned[j] and float(sim_matrix[i, j]) >= threshold:
                group.append(memories[j])
                assigned[j] = True
        groups.append(group)

    return groups


async def _llm_merge(
    client: anthropic.AsyncAnthropic, group: list[dict], topic: str
) -> dict | None:
    """Call Claude Haiku to merge a group of similar memories into one canonical memory."""
    memories_str = "\n".join(f"{i + 1}. {m['text']}" for i, m in enumerate(group))
    prompt = _MERGE_PROMPT.format(memories=memories_str)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        merged = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("consolidation_merge_parse_failed", extra={"topic": topic, "raw": raw[:100]})
        return None

    mem_type = merged.get("type", "fact")
    if mem_type not in _VALID_MEMORY_TYPES:
        mem_type = "fact"
    text = str(merged.get("text", "")).strip()
    if not text:
        return None

    return {
        "text": text,
        "type": mem_type,
        "importance": max(0.0, min(1.0, float(merged.get("importance", 0.5)))),
    }


def _estimate_tokens(text: str) -> int:
    """Estimate token count using the ~4 chars/token GPT-family heuristic."""
    return max(1, len(text) // 4)


def _apply_budget(memories: list[dict], max_tokens: int) -> list[dict]:
    """Greedily trim the ranked result list to fit within a token budget.

    Always returns at least one memory even if it exceeds the budget.
    """
    kept: list[dict] = []
    used = 0
    for m in memories:
        cost = _estimate_tokens(m.get("text", ""))
        if kept and used + cost > max_tokens:
            break
        kept.append(m)
        used += cost
    return kept


async def _hybrid_search(
    namespace: str,
    query: str,
    limit: int,
    recency_weight: float = 0.3,
    mmr_lambda: float = 0.5,
    score_threshold: float = 0.0,
) -> list[dict]:
    """BM25 + dense RRF(k=60) hybrid retrieval with MMR diversification.

    Components (Park et al. 2023 + MemoryBank):
      w_rrf      · RRF(BM25, cosine)
      w_recency  · exp(-Δt / (1 + access_count))   — recency with strength modifier
      w_import   · importance * decay_score          — decay-adjusted importance
      w_strength · log(1+ac) / log(1+max_ac)        — access frequency (strength)

    Weights interpolate linearly with recency_weight (always sum to 1.0):
      recency_weight=0 → (w_rrf=0.70, w_recency=0.00, w_import=0.20, w_strength=0.10)
      recency_weight=1 → (w_rrf=0.40, w_recency=0.40, w_import=0.10, w_strength=0.10)

    After scoring, applies score_threshold filtering then MMR reranking for diversity.
    Updates access_count and last_accessed for returned memories.
    """
    async with get_backend() as db:
        rows = await db.fetch_all(
            "SELECT id, text, topic, importance, type, created_at, access_count, decay_score "
            "FROM memories WHERE namespace = ? AND valid_until IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (namespace, limit * 5),
        )

    if not rows:
        return []

    memories = [
        {
            "id": r[0], "text": r[1], "topic": r[2], "importance": r[3],
            "type": r[4], "created_at": r[5], "access_count": r[6] or 0,
            "decay_score": r[7],
        }
        for r in rows
    ]
    texts = [m["text"] for m in memories]

    bm25_r = _bm25_ranks(query, texts)
    dense_r = _dense_ranks(query, texts)
    rrf = _rrf_fuse(bm25_r, dense_r, k=60)

    now = datetime.now(timezone.utc)
    max_ac = max((m["access_count"] for m in memories), default=1) or 1

    w_rrf      = 0.70 - 0.30 * recency_weight
    w_recency  = 0.40 * recency_weight
    w_import   = 0.20 - 0.10 * recency_weight
    w_strength = 0.10

    scored: list[tuple[float, dict]] = []
    for i, m in enumerate(memories):
        if rrf[i] == 0:
            continue  # no BM25 or dense signal — suppress from results
        age_days = (now - _parse_ts(m["created_at"])).total_seconds() / 86400
        ac = m["access_count"]
        recency = math.exp(-age_days / (1 + ac))
        strength = math.log(1 + ac) / math.log(1 + max_ac)
        decay = m["decay_score"]
        effective_importance = (m.get("importance") or 0.5) * (decay if decay is not None else 1.0)
        score = (
            w_rrf      * rrf[i]
            + w_recency  * recency
            + w_import   * effective_importance
            + w_strength * strength
        )
        scored.append((score, m))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Pre-MMR pool: top limit*3 candidates, filtered by score_threshold
    pre_mmr_scored = [(s, m) for s, m in scored[: limit * 3] if s >= score_threshold]
    pre_mmr = [m for _, m in pre_mmr_scored]
    pre_mmr_scores = [s for s, _ in pre_mmr_scored]

    # MMR diversification — embed_query is fast (model already warm from _dense_ranks)
    # Pass hybrid scores so mmr_lambda=1.0 preserves full decay/recency ordering.
    from recall.embeddings import embed_query as _eq
    results = _mmr_rerank(pre_mmr, _eq(query), mmr_lambda, limit, hybrid_scores=pre_mmr_scores)

    # Update access tracking for returned memories
    if results:
        returned_ids = [m["id"] for m in results]
        try:
            async with get_backend() as db:
                for mid in returned_ids:
                    await db.execute(
                        "UPDATE memories SET access_count = access_count + 1, "
                        "last_accessed = datetime('now') WHERE id = ?",
                        (mid,),
                    )
                await db.commit()
        except Exception as exc:
            logger.warning("access_count_update_failed", extra={"error": str(exc)})

    return results


# ── ASGI app factory ──────────────────────────────────────────────────────────

def create_app():
    """Return the Starlette app with all middleware and A2A routes wired. Used by uvicorn."""
    from recall.a2a import create_a2a_router, create_well_known_router

    app = mcp.http_app(transport="streamable-http")
    app.mount("/a2a", create_a2a_router(namespace_ctx))
    app.mount("/.well-known", create_well_known_router())
    # Starlette applies middleware in reverse order — LoggingMiddleware runs outermost (sees full duration)
    app.add_middleware(LoggingMiddleware, namespace_ctx=namespace_ctx)
    app.add_middleware(TimeoutMiddleware)
    app.add_middleware(BearerAuthMiddleware)
    return app
