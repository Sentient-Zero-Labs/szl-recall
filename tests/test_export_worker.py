"""Export worker tests — S3/R2 Object Lock NDJSON export.

Tests the export logic without hitting a real S3 bucket using unittest.mock.
Covers: grouping by (namespace, date), marking records as exported,
        skipping records already exported, disabled worker behavior.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from recall.db.connection import get_db_path
from recall.export_worker import ExportWorker, _safe_key
from recall.logging import ToolCallRecord, insert_tool_call_record


def _make_record(namespace: str, tool_name: str = "store_memory", ts: datetime | None = None) -> ToolCallRecord:
    return ToolCallRecord(
        id=str(uuid.uuid4()),
        tool_name=tool_name,
        namespace=namespace,
        session_id="ses-export-test",
        inputs_hash="aabb1122",
        status="success",
        error_code=None,
        duration_ms=10,
        llm_tokens_in=5,
        llm_tokens_out=2,
        cost_usd=0.0001,
        timestamp=ts or datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


class TestSafeKey:
    def test_simple_namespace(self):
        assert _safe_key("myapp") == "myapp"

    def test_encodes_slashes(self):
        k = _safe_key("org/team")
        assert "/" not in k.split("/")[0]

    def test_allows_hyphens_and_underscores(self):
        assert _safe_key("my-app_v2") == "my-app_v2"


class TestExportWorkerEnabled:
    def test_disabled_without_bucket(self):
        with patch.dict("os.environ", {}, clear=True):
            w = ExportWorker()
            assert not w.enabled

    def test_disabled_without_credentials(self):
        with patch.dict("os.environ", {"RECALL_EXPORT_BUCKET": "my-bucket"}, clear=True):
            w = ExportWorker()
            assert not w.enabled

    def test_enabled_with_all_config(self):
        env = {
            "RECALL_EXPORT_BUCKET": "my-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env, clear=True):
            w = ExportWorker()
            assert w.enabled

    async def test_export_returns_not_configured_when_disabled(self, client, namespace):
        with patch.dict("os.environ", {}, clear=True):
            w = ExportWorker()
            result = await w.export_pending()
        assert result["errors"] == ["export not configured"]
        assert result["files_uploaded"] == 0


class TestExportPending:
    async def test_skips_records_from_today(self, client, namespace):
        """Records timestamped today should not be exported (incomplete day)."""
        today = datetime.now(timezone.utc)
        rec = _make_record(namespace, ts=today)
        await insert_tool_call_record(rec)

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                result = await w.export_pending()

        # Today's records should not be exported
        assert result["files_uploaded"] == 0
        assert result["records_exported"] == 0
        mock_s3.put_object.assert_not_called()

    async def test_exports_past_records(self, client, namespace):
        """Records with timestamp in the past should be uploaded."""
        past = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        rec = _make_record(namespace, ts=past)
        await insert_tool_call_record(rec)

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                result = await w.export_pending()

        assert result["files_uploaded"] == 1
        assert result["records_exported"] == 1
        mock_s3.put_object.assert_called_once()

        # Verify the uploaded content is valid NDJSON
        call_kwargs = mock_s3.put_object.call_args.kwargs
        body = call_kwargs["Body"].decode()
        uploaded = json.loads(body)
        assert uploaded["id"] == rec.id

    async def test_marks_records_as_exported(self, client, namespace):
        """After export, exported_at should be set on the records."""
        past = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        rec = _make_record(namespace, ts=past)
        await insert_tool_call_record(rec)

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                await w.export_pending()

        async with aiosqlite.connect(get_db_path()) as db:
            rows = await db.execute_fetchall(
                "SELECT exported_at FROM tool_call_records WHERE id = ?", (rec.id,)
            )
        assert rows[0][0] is not None, "exported_at must be set after export"

    async def test_does_not_re_export_already_exported(self, client, namespace):
        """Second export call should upload 0 files (records already marked)."""
        past = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        rec = _make_record(namespace, ts=past)
        await insert_tool_call_record(rec)

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                first = await w.export_pending()
                second = await w.export_pending()

        assert first["files_uploaded"] == 1
        assert second["files_uploaded"] == 0
        assert mock_s3.put_object.call_count == 1

    async def test_groups_same_namespace_same_day_into_one_file(self, client, namespace):
        """Three records in the same namespace and day → 1 NDJSON file."""
        past = datetime(2026, 2, 10, tzinfo=timezone.utc)
        for i in range(3):
            rec = _make_record(
                namespace,
                tool_name=["store_memory", "search_memories", "get_memory_stats"][i],
                ts=past + timedelta(hours=i),
            )
            await insert_tool_call_record(rec)

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                result = await w.export_pending()

        assert result["files_uploaded"] == 1
        assert result["records_exported"] == 3

        call_kwargs = mock_s3.put_object.call_args.kwargs
        body = call_kwargs["Body"].decode()
        lines = [l for l in body.strip().split("\n") if l]
        assert len(lines) == 3

    async def test_uses_object_lock_compliance_mode(self, client, namespace):
        """Uploaded objects must use COMPLIANCE mode and a retention date."""
        past = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        await insert_tool_call_record(_make_record(namespace, ts=past))

        env = {
            "RECALL_EXPORT_BUCKET": "test-bucket",
            "RECALL_EXPORT_AWS_KEY": "key",
            "RECALL_EXPORT_AWS_SECRET": "secret",
            "RECALL_EXPORT_RETENTION_DAYS": "2557",
        }
        with patch.dict("os.environ", env):
            w = ExportWorker()
            mock_s3 = AsyncMock()
            mock_s3.put_object = AsyncMock()

            with patch("aioboto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.client.return_value.__aenter__ = AsyncMock(return_value=mock_s3)
                mock_session.client.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_session_cls.return_value = mock_session

                await w.export_pending()

        call_kwargs = mock_s3.put_object.call_args.kwargs
        assert call_kwargs["ObjectLockMode"] == "COMPLIANCE"
        assert call_kwargs["ObjectLockRetainUntilDate"] is not None
