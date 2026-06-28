"""S3/R2 WORM export worker — nightly NDJSON export of tool_call_records.

Uploads unexported audit records to an S3-compatible bucket configured for
immutable retention, satisfying EU AI Act Article 12's tamper-evident storage
requirement.

For Cloudflare R2: apply bucket-level lock rules via wrangler before use —
  wrangler r2 bucket lock add <bucket> --name 7-year-compliance --retention-days 2557
R2 does not support S3 per-object lock headers (returns NotImplemented).

For AWS S3: enable Object Lock on the bucket and optionally set a default
retention policy. Per-object lock can be set here if needed.

Configuration (env vars):
  RECALL_EXPORT_BUCKET         Required. S3/R2 bucket name. Unset = export disabled.
  RECALL_EXPORT_ENDPOINT_URL   Required for Cloudflare R2.
                                R2: https://<account_id>.r2.cloudflarestorage.com
  RECALL_EXPORT_AWS_KEY        AWS/R2 access key ID.
  RECALL_EXPORT_AWS_SECRET     AWS/R2 secret access key.
  RECALL_EXPORT_AWS_REGION     AWS region (default: us-east-1). R2: ignored.

S3 key format:
  {namespace}/{YYYY-MM-DD}.ndjson
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone

from recall.db.backend import get_backend

logger = logging.getLogger(__name__)

_EXPORT_INTERVAL_SECONDS = 86_400  # 24h


def _seconds_until_next_midnight() -> float:
    """Seconds until the next UTC midnight (when we run the export)."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


def _safe_key(namespace: str) -> str:
    """URL-encode namespace for use in an S3 key."""
    return urllib.parse.quote(namespace, safe="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class ExportWorker:
    """Nightly export of audit records to S3/R2 Object Lock.

    Disabled automatically when RECALL_EXPORT_BUCKET is not set.
    Call export_pending() directly for on-demand exports.
    """

    def __init__(self) -> None:
        self._bucket = os.environ.get("RECALL_EXPORT_BUCKET", "")
        self._endpoint_url = os.environ.get("RECALL_EXPORT_ENDPOINT_URL")
        self._aws_key = os.environ.get("RECALL_EXPORT_AWS_KEY")
        self._aws_secret = os.environ.get("RECALL_EXPORT_AWS_SECRET")
        self._region = os.environ.get("RECALL_EXPORT_AWS_REGION", "us-east-1")
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._bucket and self._aws_key and self._aws_secret)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("export_worker_disabled: RECALL_EXPORT_BUCKET not configured")
            return
        self._task = asyncio.create_task(self._loop(), name="export-worker")
        logger.info("export_worker_started", extra={"bucket": self._bucket})

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            delay = _seconds_until_next_midnight()
            logger.debug("export_worker_sleep", extra={"seconds": delay})
            await asyncio.sleep(delay)
            try:
                summary = await self.export_pending()
                logger.info("export_worker_nightly_done", extra=summary)
            except Exception as exc:
                logger.error("export_worker_nightly_failed", extra={"error": str(exc)})

    async def export_pending(self) -> dict:
        """Export all unexported records (timestamp < today UTC).

        Returns:
          {"files_uploaded": N, "records_exported": N, "errors": [...]}
        """
        if not self.enabled:
            return {"files_uploaded": 0, "records_exported": 0, "errors": ["export not configured"]}

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Fetch unexported records that are at least 1 day old (complete days only)
        async with get_backend() as db:
            rows = await db.fetch_all(
                "SELECT id, tool_name, namespace, session_id, inputs_hash, status, "
                "error_code, duration_ms, llm_tokens_in, llm_tokens_out, cost_usd, "
                "timestamp, prev_hash, row_hash "
                "FROM tool_call_records "
                "WHERE exported_at IS NULL AND timestamp < ? "
                "ORDER BY namespace ASC, timestamp ASC",
                (today,),
            )

        if not rows:
            return {"files_uploaded": 0, "records_exported": 0, "errors": []}

        # Group by (namespace, date)
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            ns = row[2]
            ts = row[11]
            date = ts[:10] if ts else "unknown"  # "YYYY-MM-DD" prefix of ISO8601
            key = (ns, date)
            if key not in groups:
                groups[key] = []
            groups[key].append({
                "id": row[0], "tool_name": row[1], "namespace": row[2],
                "session_id": row[3], "inputs_hash": row[4], "status": row[5],
                "error_code": row[6], "duration_ms": row[7],
                "llm_tokens_in": row[8], "llm_tokens_out": row[9],
                "cost_usd": row[10], "timestamp": row[11],
                "prev_hash": row[12], "row_hash": row[13],
            })

        files_uploaded = 0
        records_exported = 0
        errors: list[str] = []

        try:
            import aioboto3
        except ImportError:
            return {
                "files_uploaded": 0,
                "records_exported": 0,
                "errors": ["aioboto3 not installed: pip install 'szl-recall[export]'"],
            }

        session = aioboto3.Session(
            aws_access_key_id=self._aws_key,
            aws_secret_access_key=self._aws_secret,
            region_name=self._region,
        )

        async with session.client(
            "s3",
            endpoint_url=self._endpoint_url,
        ) as s3:
            for (ns, date), records in groups.items():
                s3_key = f"{_safe_key(ns)}/{date}.ndjson"
                ndjson = "\n".join(json.dumps(r, default=str) for r in records)
                record_ids = [r["id"] for r in records]

                try:
                    await s3.put_object(
                        Bucket=self._bucket,
                        Key=s3_key,
                        Body=ndjson.encode(),
                        ContentType="application/x-ndjson",
                    )
                except Exception as exc:
                    err = f"{ns}/{date}: {exc}"
                    errors.append(err)
                    logger.error("export_upload_failed", extra={"key": s3_key, "error": str(exc)})
                    continue

                # Mark rows as exported
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    async with get_backend() as db:
                        for rid in record_ids:
                            await db.execute(
                                "UPDATE tool_call_records SET exported_at = ? WHERE id = ?",
                                (now_iso, rid),
                            )
                        await db.commit()
                except Exception as exc:
                    logger.error(
                        "export_mark_failed",
                        extra={"key": s3_key, "error": str(exc)},
                    )
                    errors.append(f"{ns}/{date} mark failed: {exc}")
                    continue

                files_uploaded += 1
                records_exported += len(records)
                logger.info(
                    "export_file_uploaded",
                    extra={"key": s3_key, "records": len(records)},
                )

        return {
            "files_uploaded": files_uploaded,
            "records_exported": records_exported,
            "errors": errors,
        }
