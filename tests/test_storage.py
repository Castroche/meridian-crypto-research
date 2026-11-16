from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app.main import app
from backend.app.reporting import build_report
from backend.app.storage import StorageUnavailable
from backend.app.storage_blob import BlobReportStore
from backend.app.storage_local import LocalFileReportStore
from tests.test_pipeline import snapshot


class StorageArchitectureTests(unittest.TestCase):
    def test_preview_authorization_is_limited_to_same_origin_library_edits(self):
        from backend.app.config import Settings
        async def check():
            origin = "http://127.0.0.1:8116"
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 19000))
            async with httpx.AsyncClient(transport=transport, base_url=origin,
                                         cookies={"preview": "session"}) as client:
                with patch.object(app.state, "library_preview", (origin, "preview", "session"), create=True):
                    url = "/api/reports/nonexistent/saved?saved=false"
                    self.assertEqual((await client.patch(url)).status_code, 403)
                    self.assertEqual((await client.patch(url, headers={"Origin":"http://other.example"})).status_code, 403)
                    self.assertEqual((await client.patch(url, headers={"Origin":origin})).status_code, 404)
                    self.assertEqual((await client.post("/api/admin/reconcile", headers={"Origin":origin})).status_code, 503)
                self.assertEqual((await client.patch(url, headers={"Origin":origin})).status_code, 503)
        with tempfile.TemporaryDirectory() as root, patch("backend.app.main.store", LocalFileReportStore(Path(root))), patch("backend.app.main.settings", Settings(admin_token=None)):
            asyncio.run(check())

    def test_local_report_crud_and_export_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalFileReportStore(root)
            report = build_report(
                [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")],
                [],
                datetime.now(timezone.utc),
            )

            store.save_report(report)

            self.assertEqual(store.get_report(report.id).id, report.id)
            self.assertEqual(store.list_reports()[0]["id"], report.id)
            self.assertTrue((root / "reports" / "BTC" / f"{report.id}.json").is_file())
            self.assertTrue((root / "exports" / f"{report.id}.md").is_file())
            self.assertTrue(store.delete_report(report.id))
            self.assertIsNone(store.get_report(report.id))

    def test_job_create_update_and_list(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalFileReportStore(Path(directory))
            job = store.save_job({"id": "job-1", "status": "running", "step": "collect", "progress": 10})
            self.assertEqual(set(("id", "status", "step", "progress", "created_at", "updated_at", "error")) - set(job), set())
            updated = store.update_job("job-1", {"status": "succeeded", "step": "complete", "progress": 100})
            self.assertEqual(updated["progress"], 100)
            self.assertEqual(store.get_job("job-1")["status"], "succeeded")
            self.assertEqual(store.list_jobs()[0]["id"], "job-1")

    def test_cache_has_expiration_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalFileReportStore(root)
            store.save_cache("provider", "resource", {"value": 1}, ttl_seconds=60)
            text = (root / "cache" / "provider" / "resource.json").read_text(encoding="utf-8")
            self.assertIn('"expires_at"', text)
            self.assertEqual(store.get_cache("provider", "resource"), {"value": 1})

    def test_blob_unavailable_does_not_break_import_or_health(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLOB_STORE_ID", None)
            blob_store = BlobReportStore(None)
            with self.assertRaises(StorageUnavailable):
                blob_store.list_reports()

        async def request_health():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/health")
            return response

        response = asyncio.run(request_health())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "research"})

        environment = os.environ.copy()
        environment["VERCEL"] = "1"
        environment.pop("BLOB_READ_WRITE_TOKEN", None)
        imported = subprocess.run(
            [sys.executable, "-c", "from api.index import app; print(app.title)"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)

    def test_blob_oidc_request_retries_transient_transport_errors(self):
        store = BlobReportStore(None)
        request = httpx.Request("PUT", "https://vercel.com/api/blob")
        success = httpx.Response(200, request=request, json={"ok": True})
        failures = [httpx.ConnectError("temporary", request=request), httpx.ConnectError("temporary", request=request)]
        with (
            patch.object(store, "_oidc_credentials", return_value=("oidc", "store-id")),
            patch("backend.app.storage_blob.httpx.request", side_effect=[*failures, success]) as call,
            patch("backend.app.storage_blob.time.sleep"),
        ):
            response = store._oidc_api_request("PUT", params={"pathname": "research/test.json"}, content=b"{}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_count, 3)

    def test_blob_oidc_uses_store_id_and_request_token(self):
        response = httpx.Response(
            200,
            json={"blobs": [{"pathname": "reports/BTC/report-1.json"}], "hasMore": False},
            request=httpx.Request("GET", "https://vercel.com/api/blob"),
        )
        with (
            patch.dict(os.environ, {"BLOB_STORE_ID": "store_example"}),
            patch.object(
                BlobReportStore,
                "_oidc_credentials",
                return_value=("oidc-token", "example"),
            ),
            patch("backend.app.storage_blob.httpx.request", return_value=response) as request,
        ):
            paths = BlobReportStore(None)._list_paths("reports/")

        self.assertEqual(paths, ["reports/BTC/report-1.json"])
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer oidc-token")
        self.assertEqual(headers["x-vercel-blob-store-id"], "example")
        self.assertEqual(headers["x-api-version"], "12")

    def test_blob_blocked_store_has_actionable_error(self):
        response = httpx.Response(
            403,
            text="Your store is blocked",
            request=httpx.Request("GET", "https://example.private.blob.vercel-storage.com/reports/one.json"),
        )
        store = BlobReportStore(None)
        with (
            patch.object(store, "_oidc_credentials", return_value=("oidc-token", "example")),
            patch("backend.app.storage_blob.httpx.get", return_value=response),
        ):
            with self.assertRaisesRegex(StorageUnavailable, "usage allowance was exhausted"):
                store._get_bytes("reports/one.json")

        api_response = httpx.Response(
            403,
            text="Your store is blocked",
            request=httpx.Request("GET", "https://vercel.com/api/blob"),
        )
        with (
            patch.object(store, "_oidc_credentials", return_value=("oidc-token", "example")),
            patch("backend.app.storage_blob.httpx.request", return_value=api_response),
        ):
            with self.assertRaisesRegex(StorageUnavailable, "usage allowance was exhausted"):
                store._oidc_api_request("GET")

    def test_report_scans_are_reused_within_one_request_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalFileReportStore(Path(directory))
            report = build_report(
                [snapshot("BTC"), snapshot("ETH"), snapshot("SOL")],
                [],
                datetime.now(timezone.utc),
            )
            store.save_report(report)
            with patch.object(store, "_list_paths", wraps=store._list_paths) as listing:
                self.assertEqual(store.list_reports()[0]["id"], report.id)
                self.assertEqual(store.get_report(report.id).id, report.id)
                self.assertEqual(listing.call_count, 1)

    def test_storage_failure_is_structured_503(self):
        async def request_reports():
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with patch("backend.app.main.store", BlobReportStore(None)):
                    return await client.get("/api/reports")

        response = asyncio.run(request_reports())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "STORAGE_UNAVAILABLE")

    def test_cron_and_runtime_have_no_database_dependency(self):
        root = Path(__file__).resolve().parents[1]
        main_source = (root / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        factory_source = (root / "backend" / "app" / "storage_factory.py").read_text(encoding="utf-8")
        self.assertIn("return await service.reconcile", main_source)
        self.assertIn("BlobReportStore", factory_source)
        self.assertIn("LocalFileReportStore", factory_source)


if __name__ == "__main__":
    unittest.main()
