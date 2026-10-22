"""Unit tests for fleet_files; all device work is mocked and local-only."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from ops import fleet_files as files


class FleetFilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.db_path = self.state / "fleet.sqlite3"
        self.state_patch = patch.object(files.fleet, "_state_db_path", return_value=self.db_path)
        self.state_patch.start()
        files.reset_state_for_tests()

    def tearDown(self) -> None:
        files.reset_state_for_tests()
        self.state_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def upload(name: str, content: bytes) -> UploadFile:
        return UploadFile(file=io.BytesIO(content), filename=name)

    async def test_group_and_direct_targets_are_deduplicated_and_empty_rejected(self) -> None:
        groups = [{"id": "g1", "devices": ["phone-a", "phone-b"]}]
        with patch.object(files.fleet, "list_groups", new=AsyncMock(return_value={"groups": groups})):
            self.assertEqual(["phone-a", "phone-b"], await files._resolve_targets({"devices": ["phone-a"], "group_ids": ["g1"], "all": False}))
            with self.assertRaises(HTTPException):
                await files._resolve_targets({"devices": [], "group_ids": [], "all": False})

    async def test_remote_path_traversal_and_server_id_are_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            files._validate_remote_path("/sdcard/../data", destination=False)
        with self.assertRaises(HTTPException):
            files._validate_remote_path("/data/local/tmp/secret", destination=False)
        with self.assertRaises(HTTPException):
            files._meta_path("../etc/passwd")

    async def test_upload_apk_validation_and_private_artifact(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"manifest")
        result = await files.upload_file(self.upload("trusted.apk", payload.getvalue()))
        staged = result["file"]
        self.assertTrue(staged["is_apk"])
        import os
        if os.name == "nt":
            from ops.windows_security import owner_only
            self.assertTrue(owner_only(files._blob_path(staged["id"])), "staged file must have an owner-only protected ACL")
        else:
            self.assertEqual(0o600, files._blob_path(staged["id"]).stat().st_mode & 0o777)
        not_apk = await files.upload_file(self.upload("bundle.apks", b"not a standalone apk"))
        self.assertFalse(not_apk["file"]["is_apk"])
        with self.assertRaises(HTTPException):
            await files.upload_file(self.upload("..\\escape.apk", b"x"))

    async def test_upload_file_limit_removes_partial_artifact(self) -> None:
        with patch.object(files, "MAX_FILE_SIZE", 4), patch.object(files, "MAX_STAGED_BYTES", 4):
            with self.assertRaises(HTTPException) as raised:
                await files.upload_file(self.upload("large.bin", b"12345"))
        self.assertEqual(413, raised.exception.status_code)
        self.assertEqual([], list((files._state_root() / "blobs").glob("*")))

    async def test_install_requires_standalone_apk_and_explicit_trust(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"x")
        staged = (await files.upload_file(self.upload("a.apk", payload.getvalue())))['file']
        with patch.object(files.fleet, "list_groups", new=AsyncMock(return_value={"groups": []})):
            body = files.FileJobBody(targets={"devices": ["phone-a"]}, operation="install", file_id=staged["id"])
            with self.assertRaises(HTTPException):
                await files.create_file_job(body)
            body.confirm_install = True
            with patch.object(files, "_install_one", new=AsyncMock(return_value=(1, None))):
                created = await files.create_file_job(body)
                task = files._job_tasks[created["job"]["id"]]
                await task
        final = await files.get_job(created["job"]["id"])
        self.assertEqual("completed", final["job"]["status"])

    async def test_install_argv_is_direct_and_replace_only_adds_r(self) -> None:
        captured: list[tuple[str, ...]] = []

        async def fake_run(*argv: str, timeout: float) -> str:
            captured.append(argv)
            return "Success"

        item = {"id": "a" * 32, "name": "safe.apk", "size": 10}
        source = self.state / "safe.apk"
        source.write_bytes(b"fixture")
        with patch.object(files.fleet, "run_adb", new=fake_run):
            with patch.object(files, "_trusted_blob_path", return_value=source):
                await files._install_one("phone-a", item, True)
        self.assertEqual(("adb", "-s", "phone-a", "install", "-r"), captured[0][:-1])
        self.assertTrue(captured[0][-1].endswith(".apk"))
        self.assertFalse(Path(captured[0][-1]).exists())
        self.assertNotIn("-d", captured[0])
        self.assertNotIn("-g", captured[0])

    async def test_partial_device_failures_are_isolated_and_truthful(self) -> None:
        files._transfer_sem = asyncio.Semaphore(files.TRANSFER_CONCURRENCY)
        files._active_jobs = 1
        job = {"id": "b" * 32, "operation": "push", "status": "queued", "devices": ["a", "b"], "created_at": files._utc_now(), "updated_at": files._utc_now(), "progress": {"done": 0, "total": 2}, "results": [{"device": "a", "status": "queued"}, {"device": "b", "status": "queued"}], "remote_path": "/sdcard/Download"}
        item = {"id": "c" * 32, "name": "x.bin", "size": 3}

        async def fake_push(serial: str, file: dict, path: str, replace: bool):
            if serial == "b":
                raise RuntimeError("offline")
            return 3, None

        with patch.object(files, "_push_one", new=fake_push):
            await files._run_job(job, item)
        self.assertEqual("failed", job["status"])
        self.assertEqual(["completed", "failed"], [entry["status"] for entry in job["results"]])
        self.assertTrue(job["results"][0]["ok"])
        self.assertFalse(job["results"][1]["ok"])

    async def test_download_lookup_uses_metadata_not_client_path(self) -> None:
        download_id = "d" * 32
        stored = files._state_root() / "downloads" / f"{download_id}.bin"
        stored.write_bytes(b"download")
        files._private(stored)
        files._atomic_json(files._download_meta_path(download_id), {"id": download_id, "path": str(stored), "filename": "phone file.bin", "created_at_epoch": 9999999999})
        with self.assertRaises(HTTPException):
            await files.download_file("../etc/passwd")
        response = await files.download_file(download_id)
        self.assertEqual("application/octet-stream", response.media_type)
        from urllib.parse import unquote
        self.assertIn("phone file.bin", unquote(response.headers["content-disposition"]))

    async def test_restart_marks_running_job_interrupted(self) -> None:
        job_id = "e" * 32
        files._atomic_json(files._job_path(job_id), {"id": job_id, "operation": "pull", "status": "running", "devices": ["phone-a"], "created_at": files._utc_now(), "updated_at": files._utc_now(), "progress": {"done": 0, "total": 1}, "results": [{"device": "phone-a", "status": "running"}]})
        files._restore_jobs()
        restored = files._load_job(job_id)
        self.assertEqual("interrupted", restored["status"])
        self.assertEqual("failed", restored["results"][0]["status"])

    async def test_origin_dependency_rejects_cross_site(self) -> None:
        scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"origin", b"https://evil.example"), (b"host", b"localhost")], "query_string": b"", "scheme": "http", "server": ("localhost", 80), "client": ("127.0.0.1", 1)}
        with self.assertRaises(HTTPException) as raised:
            await files.fleet._same_origin(Request(scope))
        self.assertEqual(403, raised.exception.status_code)

    async def test_symlink_escape_is_rejected_before_mkdir(self) -> None:
        runner = AsyncMock(return_value="/data/private")
        with patch.object(files.fleet, "run_adb", new=runner):
            with self.assertRaises(RuntimeError):
                await files._push_one("phone-a", {"id":"a" * 32,"name":"x.txt","size":1}, "/sdcard/link/new", False)
        self.assertEqual(runner.await_count, 1)
        self.assertNotIn("mkdir", " ".join(runner.await_args.args))

    async def test_install_requires_positive_success_and_cleans_link(self) -> None:
        source = self.state / "source.apk"
        source.write_bytes(b"fixture")
        item = {"id":"a" * 32,"name":"source.apk","size":7}
        for output in ("Failure [INVALID_APK]", ""):
            with patch.object(files.fleet, "run_adb", new=AsyncMock(return_value=output)), patch.object(files, "_trusted_blob_path", return_value=source):
                with self.assertRaises(RuntimeError):
                    await files._install_one("phone-a", item, False)
        self.assertFalse(list((files._state_root()/"blobs").glob("*.apk")))

    async def test_cancel_active_job_keeps_truthful_terminal_state(self) -> None:
        entered = asyncio.Event()
        files._ensure_runtime()
        files._active_jobs = 1
        job = {"id":"f" * 32,"operation":"push","status":"queued","devices":["phone-a"],"created_at":files._utc_now(),"updated_at":files._utc_now(),"progress":{"done":0,"total":1},"results":[{"device":"phone-a","status":"queued"}],"remote_path":"/sdcard/Download"}
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.sleep(3600)
        with patch.object(files,"_push_one",new=blocked):
            task = asyncio.create_task(files._run_job(job,{"id":"a"*32,"name":"x.txt","size":1}))
            files._job_tasks[job["id"]] = task
            await asyncio.wait_for(entered.wait(),1)
            await files.cancel_job(job["id"])
            await task
        final = files._load_job(job["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["results"][0]["status"], "cancelled")
        self.assertEqual(final["progress"]["done"], 1)

    async def test_active_job_limit_rejects_new_work(self) -> None:
        with patch.object(files, "MAX_ACTIVE_JOBS", 0), patch.object(files.fleet,"list_groups",new=AsyncMock(return_value={"groups":[]})):
            with self.assertRaises(HTTPException) as raised:
                await files.create_file_job(files.FileJobBody(targets={"devices":["phone-a"]},operation="pull",remote_path="/sdcard/Download/x.txt"))
        self.assertEqual(raised.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
