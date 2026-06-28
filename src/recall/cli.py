"""Recall CLI — `recall serve`, `recall status`, `recall create-token`."""

from __future__ import annotations

import argparse
import asyncio
import sys


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed — env must be set externally


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(
        prog="recall",
        description="Recall — persistent memory layer for AI agents.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # serve
    serve = subparsers.add_parser("serve", help="Start the Recall MCP server.")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=None, help="Path to SQLite database. Overrides RECALL_DB_PATH env var.")
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")

    # status
    status = subparsers.add_parser("status", help="Show database stats.")
    status.add_argument("--db", default="recall.db")

    # create-token
    ct = subparsers.add_parser("create-token", help="Generate an API token for a namespace (user, agent, project, etc.).")
    ct.add_argument("namespace", help="Namespace to associate with this token — can be a user, agent, project, or any string.")
    ct.add_argument("--db", default="recall.db", help="Path to SQLite database.")

    args = parser.parse_args()

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "status":
        asyncio.run(_cmd_status(args))
    elif args.command == "create-token":
        asyncio.run(_cmd_create_token(args))


def _cmd_serve(args: argparse.Namespace) -> None:
    import os
    from recall.db.connection import set_db_path

    # --db flag > RECALL_DB_PATH env var > default "recall.db"
    # set_db_path updates the already-imported module-level variable so the
    # module-level import race (connection.py reads env at import time) is bypassed.
    db_path = args.db or os.environ.get("RECALL_DB_PATH", "recall.db")
    set_db_path(db_path)
    os.environ["RECALL_DB_PATH"] = db_path  # keep env consistent for any subprocesses

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is required to serve. Install with: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    from recall.server import create_app
    app = create_app()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


async def _cmd_create_token(args: argparse.Namespace) -> None:
    import os
    import secrets
    import uuid
    from pathlib import Path

    from recall.security import hash_token
    from recall.db.backend import get_backend

    if not os.environ.get("RECALL_DB_URL"):
        # SQLite path — verify DB exists before connecting
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"Database not found: {db_path}")
            print("Run 'recall serve' first to initialize the database, then create a token.")
            sys.exit(1)
        os.environ.setdefault("RECALL_DB_PATH", str(db_path))
        from recall.db.connection import set_db_path
        set_db_path(db_path)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    token_id = str(uuid.uuid4())

    async with get_backend() as db:
        await db.execute(
            "INSERT INTO api_tokens (id, token_hash, namespace, revoked) VALUES (?, ?, ?, 0)",
            (token_id, token_hash, args.namespace),
        )
        await db.commit()

    print(f"Token created for namespace: {args.namespace}")
    print(f"\n  Bearer {raw_token}\n")
    print("Store this token securely — it will not be shown again.")
    print("Add it to your MCP client as: Authorization: Bearer <token>")


async def _cmd_status(args: argparse.Namespace) -> None:
    import os
    from pathlib import Path

    from recall.db.backend import get_backend

    if not os.environ.get("RECALL_DB_URL"):
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"Database not found: {db_path}")
            print("Run 'recall serve' first to initialize the database.")
            return
        from recall.db.connection import set_db_path
        set_db_path(db_path)

    async with get_backend() as db:
        rows = await db.fetch_all("SELECT COUNT(*) FROM memories")
        total = rows[0][0]
        by_type = await db.fetch_all(
            "SELECT type, COUNT(*) FROM memories GROUP BY type"
        )
        pending = await db.fetch_all(
            "SELECT COUNT(*) FROM operations WHERE status = 'queued'"
        )

    if os.environ.get("RECALL_DB_URL"):
        print(f"Database: {os.environ['RECALL_DB_URL']}")
    else:
        print(f"Database: {args.db}")
    print(f"Total memories: {total}")
    for mem_type, count in by_type:
        print(f"  {mem_type}: {count}")
    print(f"Pending extractions: {pending[0][0]}")
