"""Hash-chain audit trail tests — v0.4 Article 12 compliance.

Covers:
  - compute_row_hash() determinism
  - _fetch_prev_hash() sentinel and chained cases
  - insert_tool_call_record() stores correct prev_hash / row_hash
  - verify_audit_chain returns ok on intact chain
  - verify_audit_chain detects a modified row
  - verify_audit_chain handles pre-chain records (NULL row_hash)
  - verify_audit_chain on empty namespace
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import aiosqlite
import pytest

from recall.db.connection import get_db_path
from recall.logging import (
    SENTINEL_HASH,
    ToolCallRecord,
    _fetch_prev_hash,
    compute_row_hash,
    insert_tool_call_record,
)
from recall.server import namespace_ctx, verify_audit_chain


def _make_record(namespace: str, tool_name: str = "store_memory") -> ToolCallRecord:
    return ToolCallRecord(
        id=str(uuid.uuid4()),
        tool_name=tool_name,
        namespace=namespace,
        session_id="ses-test",
        inputs_hash="aabbccdd11223344",
        status="success",
        error_code=None,
        duration_ms=42,
        llm_tokens_in=10,
        llm_tokens_out=5,
        cost_usd=0.000015,
        timestamp=datetime.now(timezone.utc),
    )


class TestComputeRowHash:
    def test_deterministic(self):
        h1 = compute_row_hash("prev", "store_memory", "2026-01-01T00:00:00", "aabb")
        h2 = compute_row_hash("prev", "store_memory", "2026-01-01T00:00:00", "aabb")
        assert h1 == h2

    def test_different_fields_different_hash(self):
        base = compute_row_hash(SENTINEL_HASH, "store_memory", "2026-01-01T00:00:00", "aabb")
        changed = compute_row_hash(SENTINEL_HASH, "delete_memory", "2026-01-01T00:00:00", "aabb")
        assert base != changed

    def test_sentinel_known_value(self):
        sentinel = hashlib.sha256(b"\x00").hexdigest()
        assert SENTINEL_HASH == sentinel
        assert len(SENTINEL_HASH) == 64


class TestFetchPrevHash:
    async def test_returns_sentinel_for_empty_namespace(self, client, namespace):
        result = await _fetch_prev_hash(namespace)
        assert result == SENTINEL_HASH

    async def test_returns_row_hash_of_last_record(self, client, namespace):
        rec = _make_record(namespace)
        await insert_tool_call_record(rec)
        result = await _fetch_prev_hash(namespace)
        # Should be the row_hash of the record we just inserted (not SENTINEL)
        assert result != SENTINEL_HASH
        assert len(result) == 64

    async def test_isolates_by_namespace(self, client):
        ns_a = f"ns-a-{uuid.uuid4()}"
        ns_b = f"ns-b-{uuid.uuid4()}"
        rec_a = _make_record(ns_a)
        await insert_tool_call_record(rec_a)
        # namespace B has no records — should still get sentinel
        result = await _fetch_prev_hash(ns_b)
        assert result == SENTINEL_HASH


class TestInsertToolCallRecord:
    async def test_first_record_uses_sentinel(self, client, namespace):
        rec = _make_record(namespace)
        await insert_tool_call_record(rec)

        async with aiosqlite.connect(get_db_path()) as db:
            rows = await db.execute_fetchall(
                "SELECT prev_hash, row_hash FROM tool_call_records WHERE id = ?", (rec.id,)
            )

        assert len(rows) == 1
        prev_hash, row_hash = rows[0]
        assert prev_hash == SENTINEL_HASH
        expected = compute_row_hash(SENTINEL_HASH, rec.tool_name, rec.timestamp.isoformat(), rec.inputs_hash)
        assert row_hash == expected

    async def test_second_record_links_to_first(self, client, namespace):
        rec1 = _make_record(namespace, "store_memory")
        rec2 = _make_record(namespace, "search_memories")
        await insert_tool_call_record(rec1)
        await insert_tool_call_record(rec2)

        async with aiosqlite.connect(get_db_path()) as db:
            rows = await db.execute_fetchall(
                "SELECT id, prev_hash, row_hash FROM tool_call_records "
                "WHERE namespace = ? ORDER BY timestamp ASC, id ASC",
                (namespace,),
            )

        assert len(rows) == 2
        _id1, prev1, hash1 = rows[0]
        _id2, prev2, hash2 = rows[1]

        assert prev1 == SENTINEL_HASH
        assert prev2 == hash1  # second record's prev_hash must equal first record's row_hash


class TestVerifyAuditChain:
    async def test_ok_on_empty_namespace(self, client, namespace):
        token = namespace_ctx.set(namespace)
        try:
            result = await verify_audit_chain()
        finally:
            namespace_ctx.reset(token)

        assert result["status"] == "ok"
        assert result["data"]["records_checked"] == 0

    async def test_ok_on_intact_chain(self, client, namespace):
        for tool in ["store_memory", "search_memories", "get_memory_stats"]:
            await insert_tool_call_record(_make_record(namespace, tool))

        token = namespace_ctx.set(namespace)
        try:
            result = await verify_audit_chain()
        finally:
            namespace_ctx.reset(token)

        assert result["status"] == "ok"
        assert result["data"]["status"] == "ok"
        assert result["data"]["records_checked"] == 3
        assert result["data"]["broken_at"] is None

    async def test_detects_modified_row_hash(self, client, namespace):
        await insert_tool_call_record(_make_record(namespace))
        await insert_tool_call_record(_make_record(namespace))

        # Tamper: overwrite row_hash on the first record
        async with aiosqlite.connect(get_db_path()) as db:
            row = await db.execute_fetchall(
                "SELECT id FROM tool_call_records WHERE namespace = ? "
                "ORDER BY timestamp ASC, id ASC LIMIT 1",
                (namespace,),
            )
            tampered_id = row[0][0]
            await db.execute(
                "UPDATE tool_call_records SET row_hash = 'deadbeef' WHERE id = ?",
                (tampered_id,),
            )
            await db.commit()

        token = namespace_ctx.set(namespace)
        try:
            result = await verify_audit_chain()
        finally:
            namespace_ctx.reset(token)

        assert result["data"]["status"] == "broken"
        assert result["data"]["broken_at"] == tampered_id

    async def test_detects_modified_tool_name(self, client, namespace):
        rec = _make_record(namespace, "store_memory")
        await insert_tool_call_record(rec)

        # Tamper: change tool_name — hash will no longer match
        async with aiosqlite.connect(get_db_path()) as db:
            await db.execute(
                "UPDATE tool_call_records SET tool_name = 'evil_tool' WHERE id = ?",
                (rec.id,),
            )
            await db.commit()

        token = namespace_ctx.set(namespace)
        try:
            result = await verify_audit_chain()
        finally:
            namespace_ctx.reset(token)

        assert result["data"]["status"] == "broken"
        assert result["data"]["broken_at"] == rec.id

    async def test_pre_chain_records_reported_not_checked(self, client, namespace):
        # Insert a legacy record (NULL row_hash) to simulate pre-v0.4 data
        async with aiosqlite.connect(get_db_path()) as db:
            await db.execute(
                "INSERT INTO tool_call_records "
                "(id, tool_name, namespace, session_id, inputs_hash, status, duration_ms, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), "store_memory", namespace, "old-session",
                    "oldhash", "success", 10, "2025-01-01T00:00:00",
                ),
            )
            await db.commit()

        # Also insert a current chained record
        await insert_tool_call_record(_make_record(namespace))

        token = namespace_ctx.set(namespace)
        try:
            result = await verify_audit_chain()
        finally:
            namespace_ctx.reset(token)

        assert result["data"]["status"] == "ok"
        assert result["data"]["pre_chain_records"] == 1
        assert result["data"]["records_checked"] == 1
