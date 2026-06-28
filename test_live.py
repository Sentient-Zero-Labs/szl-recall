"""
Live integration test — run this while `recall serve` is running.

Usage:
    recall serve --port 8000 &
    recall create-token myapp --db recall.db
    RECALL_TOKEN=<token> python test_live.py

Tests every v0.4 tool via the real HTTP MCP endpoint.
"""

import asyncio
import json
import os
import sys
import time

try:
    import httpx
except ImportError:
    print("pip install httpx")
    sys.exit(1)

BASE_URL = os.environ.get("RECALL_URL", "http://localhost:8000")
TOKEN = os.environ.get("RECALL_TOKEN", "")

if not TOKEN:
    print("Set RECALL_TOKEN=<your bearer token>")
    print("Get one with: recall create-token myapp --db recall.db")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

SESSION_ID: str = ""


def _init_session(client: httpx.Client) -> None:
    global SESSION_ID
    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "id": 0,
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test_live", "version": "1.0"},
        },
    }
    r = client.post(f"{BASE_URL}/mcp", json=payload, headers=HEADERS, timeout=10)
    r.raise_for_status()
    SESSION_ID = r.headers.get("mcp-session-id", "")
    if not SESSION_ID:
        print("  ✗  Could not obtain MCP session ID")
        sys.exit(1)


def call(client: httpx.Client, tool: str, **args) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": int(time.time() * 1000),
        "params": {"name": tool, "arguments": args},
    }
    headers = {**HEADERS, "Mcp-Session-Id": SESSION_ID}
    r = client.post(f"{BASE_URL}/mcp", json=payload, headers=headers, timeout=30)
    r.raise_for_status()

    # FastMCP Streamable HTTP returns SSE — parse the data lines
    full_text = r.text
    for line in full_text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    outer = json.loads(data)
                    # Unwrap: {"result": {"content": [{"text": "{...}"}]}}
                    content = outer.get("result", {}).get("content", [])
                    if content:
                        return json.loads(content[0]["text"])
                    return outer
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
    return {"raw": full_text[:200]}


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def ok(label: str, value: object = "") -> None:
    print(f"  ✓  {label}" + (f": {value}" if value != "" else ""))


def fail(label: str, detail: object = "") -> None:
    print(f"  ✗  {label}" + (f": {detail}" if detail != "" else ""))
    sys.exit(1)


with httpx.Client() as client:

    # ── Health check ─────────────────────────────────────────────────────
    section("1. Health check")
    r = client.get(f"{BASE_URL}/.well-known/agent-card.json", timeout=5)
    if r.status_code == 200:
        ok("Server is up", BASE_URL)
    else:
        fail("Server not responding", r.status_code)

    _init_session(client)
    ok(f"MCP session: {SESSION_ID[:16]}...")

    # ── Store memories ───────────────────────────────────────────────────
    section("2. store_memory — write 3 facts")
    for text, topic in [
        ("I prefer Python for backend services", "engineering"),
        ("The team decided to use FastAPI for the new service", "engineering"),
        ("I work at Acme Corp on the payments team", "personal"),
    ]:
        r = call(client, "store_memory", text=text, topic=topic, session_id="test-session-1")
        if r.get("status") == "ok":
            ok(f"Queued: {text[:50]}")
        else:
            fail("store_memory failed", r)

    print("  (waiting 8s for extraction worker to process...)")
    time.sleep(8)

    # ── Stats ────────────────────────────────────────────────────────────
    section("3. get_memory_stats")
    r = call(client, "get_memory_stats")
    if r.get("status") == "ok":
        data = r["data"]
        ok(f"Total memories: {data['total']}")
        ok(f"By type: {data['by_type']}")
        ok(f"Pending extractions: {data['pending_extractions']}")
    else:
        fail("get_memory_stats failed", r)

    # ── Search ────────────────────────────────────────────────────────────
    section("4. search_memories — 'what language does the user prefer?'")
    r = call(client, "search_memories", query="what language does the user prefer for backend?", limit=5)
    if r.get("status") == "ok":
        results = r["data"]["results"]
        ok(f"Got {len(results)} results")
        for m in results[:3]:
            ok(f"  [{m.get('type','?')}] {m.get('text','')[:70]}")
    else:
        fail("search_memories failed", r)

    # ── Inspect ───────────────────────────────────────────────────────────
    section("5. inspect_memories — list all")
    r = call(client, "inspect_memories", limit=10)
    if r.get("status") == "ok":
        ok(f"Total in DB: {r['data']['total']}")
    else:
        fail("inspect_memories failed", r)

    # ── Audit chain ───────────────────────────────────────────────────────
    section("6. verify_audit_chain — Article 12 tamper check")
    r = call(client, "verify_audit_chain")
    if r.get("status") == "ok":
        d = r["data"]
        status = d.get("status", "?")
        if status == "ok":
            ok(f"Chain intact: {d.get('records_checked', 0)} records verified")
            ok(f"Pre-chain records (pre-v0.4): {d.get('pre_chain_records', 0)}")
        elif status == "broken":
            fail("Chain BROKEN", f"at record {d.get('broken_at')}")
        else:
            ok(f"No hash-chained records yet (all pre-v0.4): {d.get('message','')}")
    else:
        fail("verify_audit_chain failed", r)

    # ── Compliance report ─────────────────────────────────────────────────
    section("7. export_compliance_report — full NDJSON dump")
    r = call(client, "export_compliance_report")
    if r.get("status") == "ok":
        d = r["data"]
        count = d.get("record_count", 0)
        ok(f"Records exported: {count}")
        if d.get("ndjson"):
            first = json.loads(d["ndjson"].splitlines()[0])
            ok(f"First record tool: {first.get('tool_name')}")
            ok(f"Hash-chained: {'row_hash' in first and first['row_hash'] is not None}")
    else:
        fail("export_compliance_report failed", r)

    # ── Cost summary ──────────────────────────────────────────────────────
    section("8. get_cost_summary — LLM spend per session")
    r = call(client, "get_cost_summary")
    if r.get("status") == "ok":
        d = r["data"]
        ok(f"Total tool calls tracked: {d.get('total_tool_calls', 0)}")
        ok(f"Total cost so far: ${d.get('total_cost_usd', 0):.6f}")
        for s in d.get("sessions", [])[:3]:
            ok(f"  Session {s['session_id'][:20]}: {s['tool_calls']} calls, ${s['cost_usd']:.6f}")
    else:
        fail("get_cost_summary failed", r)

    # ── Export worker ─────────────────────────────────────────────────────
    section("9. trigger_audit_export")
    r = call(client, "trigger_audit_export")
    if r.get("status") == "ok":
        ok("Export ran", r["data"])
    elif r.get("code") == "EXPORT_NOT_CONFIGURED":
        ok("Export not configured (expected — no S3 bucket set)")
    else:
        fail("trigger_audit_export failed", r)

    # ── Done ──────────────────────────────────────────────────────────────
    print(f"\n{'─' * 60}")
    print("  All checks passed. Recall v0.4 is working end-to-end.")
    print(f"{'─' * 60}\n")
