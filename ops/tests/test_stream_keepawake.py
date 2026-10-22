"""Offline tests for the deployed ops.android_h264 keep-awake behavior."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = Path(__file__).with_name("android_h264.py")
if not MODULE_PATH.exists():
    MODULE_PATH = Path(__file__).resolve().parents[1] / "android_h264.py"

# The test is intentionally offline; provide only the decorator surface needed
# to import the router when the workstation has no FastAPI installation.
try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    class _Router:
        def __init__(self, *args, **kwargs):
            pass

        def _route(self, *args, **kwargs):
            return lambda function: function

        get = _route
        post = _route
        websocket = _route

    fastapi_stub = types.ModuleType("fastapi")
    fastapi_stub.APIRouter = _Router
    fastapi_stub.Body = lambda default=...: default
    fastapi_stub.WebSocket = object
    sys.modules["fastapi"] = fastapi_stub

# Load the worktree artifact under the same qualified name used by deployment.
# This does not create an ops package on disk and never invokes adb.
if "ops" not in sys.modules:
    ops_package = types.ModuleType("ops")
    # Permit the actual stdlib-only control arbiter import; ADB is still mocked.
    ops_package.__path__ = [str(MODULE_PATH.parent)]
    sys.modules["ops"] = ops_package
_spec = importlib.util.spec_from_file_location("ops.android_h264", MODULE_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Unable to load {MODULE_PATH}")
_module = importlib.util.module_from_spec(_spec)
sys.modules["ops.android_h264"] = _module
_spec.loader.exec_module(_module)
deployed = importlib.import_module("ops.android_h264")


class StreamKeepAwakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = deployed.Session("pixel-01")
        self.adb = AsyncMock(return_value="")
        self.interval = patch.object(deployed, "KEEP_AWAKE_INTERVAL", 0.02)
        self.offset = patch.object(deployed, "keep_awake_offset", return_value=0)
        self.adb_patch = patch.object(deployed, "adb", self.adb)
        self.interval.start()
        self.offset.start()
        self.adb_patch.start()

    async def asyncTearDown(self):
        task = self.session.keep_awake_task
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.adb_patch.stop()
        self.offset.stop()
        self.interval.stop()

    async def test_wake_is_viewer_bound_and_uses_no_persistent_settings(self):
        self.session.subscribe()
        task = asyncio.create_task(self.session._keep_awake_loop())
        await asyncio.sleep(0.055)
        self.assertGreaterEqual(self.adb.await_count, 1)
        for call in self.adb.await_args_list:
            args = call.args
            self.assertEqual(args[1:5], ("shell", "input", "keyevent", "KEYCODE_WAKEUP"))
            self.assertNotIn("settings", args)
            self.assertNotIn("stay_on_while_plugged_in", args)

        self.session.clients.clear()
        self.adb.reset_mock()
        await asyncio.sleep(0.045)
        self.assertEqual(self.adb.await_count, 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_adb_errors_are_nonfatal_and_retry_on_next_interval(self):
        first = True

        async def transient_failure(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise RuntimeError("temporary adb failure")
            return ""

        self.adb.side_effect = transient_failure
        self.session.subscribe()
        task = asyncio.create_task(self.session._keep_awake_loop())
        await asyncio.sleep(0.055)
        self.assertGreaterEqual(self.adb.await_count, 2)
        self.assertEqual(self.session.keep_awake_errors, 1)
        self.assertEqual(self.session.error, "")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_run_finally_cancels_and_awaits_shared_task(self):
        with patch.object(deployed.Session, "command", side_effect=RuntimeError("stop before adb")):
            await self.session.run()
        self.assertIsNotNone(self.session.keep_awake_task)
        self.assertTrue(self.session.keep_awake_task.done())
        self.assertTrue(self.session.keep_awake_task.cancelled())
        self.adb.assert_not_awaited()

    def test_command_remains_under_the_safe_255_byte_budget(self):
        command = self.session.command()
        self.assertLess(len(" ".join(command[5:]).encode()), 255)
        self.assertNotIn("settings", command)
        self.assertFalse(any("mjpeg" in item.lower() for item in command))

    def test_status_exposes_read_only_keep_awake_metadata(self):
        status = self.session.status()
        self.assertEqual(status["keep_awake_interval"], deployed.KEEP_AWAKE_INTERVAL)
        self.assertFalse(status["keep_awake_active"])
        self.assertFalse(status["keep_awake_task_running"])
        self.assertEqual(status["keep_awake_errors"], 0)

    def test_device_offset_is_stable_and_bounded(self):
        first = deployed.keep_awake_offset("pixel-01")
        self.assertEqual(first, deployed.keep_awake_offset("pixel-01"))
        self.assertGreaterEqual(first, 0)
        self.assertLess(first, deployed.KEEP_AWAKE_INTERVAL)


if __name__ == "__main__":
    unittest.main()
