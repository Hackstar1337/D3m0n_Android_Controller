"""Offline tests for ops.sync_links (control-only SYNC ALL links).

No ADB or phone is used: ``h264.adb`` and the scrcpy subprocess are faked, and a
local TCP server plays the scrcpy control socket so the handshake, the exact
control bytes and teardown are exercised over a real socket.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import ops.fast_control as fc
import ops.sync_links as sl

PORTRAIT = ('  mOverrideDisplayInfo=DisplayInfo{"Built-in Screen", uniqueId "local:0", app 1080 x 2220, '
            'real 1080 x 2220, largest app 2220 x 2148, smallest app 1080 x 1008, mode 1, defaultModeId 1, '
            'rotation 0, density 420 (420.0 x 420.0) dpi}')
LANDSCAPE = PORTRAIT.replace("real 1080 x 2220", "real 2220 x 1080").replace("rotation 0", "rotation 1")


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"[server] INFO: Device: samsung SM-G9500\n")
        self._done = asyncio.Event()
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self._finish(0)

    def kill(self):
        self._finish(-9)

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


class FakeScrcpy:
    """Plays the device side of a scrcpy control-only connection."""

    def __init__(self):
        self.received = bytearray()
        self.connections = 0
        self.server = None
        self.port = 0
        self.writers = []

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        self.connections += 1
        self.writers.append(writer)
        writer.write(b"\x00" + b"SM-G9500".ljust(64, b"\x00"))
        await writer.drain()
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            self.received.extend(chunk)
        writer.close()

    async def drop_clients(self):
        for writer in self.writers:
            writer.close()
        self.writers.clear()

    async def stop(self):
        await self.drop_clients()
        self.server.close()
        await self.server.wait_closed()


async def wait_for(predicate, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class DisplayParsingTests(unittest.TestCase):
    def test_parses_logical_size_and_rotation(self):
        self.assertEqual(sl.parse_display(PORTRAIT), (1080, 2220, 0))
        self.assertEqual(sl.parse_display(LANDSCAPE), (2220, 1080, 1))

    def test_rejects_missing_or_absurd_sizes(self):
        self.assertIsNone(sl.parse_display(""))
        self.assertIsNone(sl.parse_display("mOverrideDisplayInfo=DisplayInfo{app 1080 x 2220}"))
        self.assertIsNone(sl.parse_display("real 10 x 20, rotation 0"))

    def test_command_is_control_only(self):
        args = sl.ControlLink("192.168.50.37:5555").command()
        for flag in ("video=false", "audio=false", "tunnel_forward=true", "clipboard_autosync=false"):
            self.assertIn(flag, args)
        self.assertEqual(args[:3], ["adb", "-s", "192.168.50.37:5555"])
        self.assertIn(sl.h264.VERSION, args)


class LinkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeScrcpy()
        await self.fake.start()
        self.display = PORTRAIT
        self.adb_state = "device"
        self.adb_calls = []
        self.procs = []

        async def fake_adb(device, *args, timeout=10):
            self.adb_calls.append((device,) + args)
            if args[0] == "get-state":
                return self.adb_state
            if args[0] == "shell":
                return self.display
            if args[:2] == ("forward", "tcp:0"):
                return str(self.fake.port)
            return ""

        async def fake_exec(*args, **kwargs):
            proc = FakeProc()
            proc.args = args
            self.procs.append(proc)
            return proc

        self.patches = [
            patch.object(sl.h264, "adb", fake_adb),
            patch.object(sl.asyncio, "create_subprocess_exec", fake_exec),
            patch.object(sl, "SUPERVISE_TICK", 0.02),
            patch.object(sl, "RETRY_FIRST", 0.05),
            patch.object(sl, "DISPLAY_REFRESH", 0.05),
        ]
        for p in self.patches:
            p.start()
        sl.h264.PREPARED.add("dev1")

    async def asyncTearDown(self):
        await sl.shutdown_for_tests()
        for p in reversed(self.patches):
            p.stop()
        sl.h264.PREPARED.discard("dev1")
        await self.fake.stop()

    async def test_link_connects_injects_exact_bytes_and_tears_down(self):
        sl.lease(["dev1"], 30)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(link.control_ready), link.status())
        self.assertIs(sl.link_for("dev1"), link)
        self.assertEqual((link.width, link.height), (1080, 2220))
        await link.inject_touch(sl.h264.ACTION_DOWN, 0.5, 1.0)
        await link.inject_key(3)
        expected = (sl.h264.touch_message(sl.h264.ACTION_DOWN, 540, 2219, 1080, 2220, 1.0)
                    + sl.h264.key_message(sl.h264.ACTION_DOWN, 3) + sl.h264.key_message(sl.h264.ACTION_UP, 3))
        self.assertTrue(await wait_for(lambda: len(self.fake.received) >= len(expected)))
        self.assertEqual(bytes(self.fake.received), expected)
        self.assertEqual(link.events, 3)
        self.assertIn("video=false", self.procs[0].args)
        sl.release()
        self.assertTrue(await wait_for(lambda: link.task.done()))
        self.assertEqual(link.state, "closed")
        self.assertTrue(self.procs[0].terminated)
        self.assertTrue(any(call[1:3] == ("forward", "--remove") for call in self.adb_calls))
        self.assertNotIn("dev1", sl.LINKS)
        self.assertIsNone(sl.link_for("dev1"))

    async def test_rotation_is_tracked_for_coordinates(self):
        sl.lease(["dev1"], 30)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(link.control_ready))
        self.display = LANDSCAPE
        self.assertTrue(await wait_for(lambda: (link.width, link.height) == (2220, 1080)))
        self.assertEqual(link.point(1.0, 0.0), (2219, 0, 2220, 1080))
        self.assertEqual(link.rotation, 1)

    async def test_stale_display_is_not_trusted(self):
        sl.lease(["dev1"], 30)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(link.control_ready))
        with patch.object(sl, "DISPLAY_REFRESH", 3600):
            link.display_at = time.monotonic() - sl.DISPLAY_STALE - 1
            self.assertFalse(link.control_ready())
            with self.assertRaises(sl.h264.ControlUnavailable):
                await link.inject_touch(sl.h264.ACTION_DOWN, 0.1, 0.1)
            self.assertIsNone(sl.link_for("dev1"))

    async def test_dropped_link_reconnects_while_leased(self):
        sl.lease(["dev1"], 30)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(link.control_ready))
        await self.fake.drop_clients()
        self.assertTrue(await wait_for(lambda: link.restarts >= 1 and link.control_ready()), link.status())
        self.assertGreaterEqual(self.fake.connections, 2)
        self.assertEqual(link.failures, 1)

    async def test_offline_phone_retries_and_reports_error(self):
        self.adb_state = "offline"
        sl.lease(["dev1"], 30)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(lambda: link.failures >= 2))
        self.assertFalse(link.control_ready())
        self.assertEqual(link.error, "Wireless ADB is offline")
        self.assertEqual(self.procs, [])
        self.adb_state = "device"
        self.assertTrue(await wait_for(link.control_ready, timeout=6), link.status())

    async def test_lease_expiry_closes_link(self):
        sl.lease(["dev1"], 0.4)
        link = sl.LINKS["dev1"]
        self.assertTrue(await wait_for(link.control_ready))
        self.assertTrue(await wait_for(lambda: link.task.done(), timeout=3))
        self.assertEqual(link.state, "closed")


class LeaseRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def idle_run(link):
            while link.leased():
                await asyncio.sleep(0.01)
            link.stopping = True
            link.state = "closed"
        self.run_patch = patch.object(sl.ControlLink, "run", idle_run)
        self.run_patch.start()

    async def asyncTearDown(self):
        await sl.shutdown_for_tests()
        self.run_patch.stop()

    async def test_lease_extends_and_release_keeps_listed(self):
        sl.lease(["a", "b"], 5)
        first = sl.LINKS["a"]
        sl.lease(["a"], 50)
        self.assertIs(sl.LINKS["a"], first)
        self.assertGreater(first.lease_until - time.monotonic(), 40)
        self.assertEqual(sl.release({"a"}), 1)
        self.assertTrue(await wait_for(lambda: "b" not in sl.LINKS))
        self.assertIn("a", sl.LINKS)

    async def test_link_count_is_capped(self):
        with patch.object(sl, "MAX_LINKS", 3):
            sl.lease([f"p{i}" for i in range(6)], 5)
            self.assertEqual(len(sl.active_links()), 3)

    async def test_route_validates_origin_serials_and_size(self):
        app = FastAPI()
        fc.install_fast_control(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            bad_origin = await client.post("/api/phone/sync/links", json={"devices": ["a"]}, headers={"origin": "https://evil.example"})
            self.assertEqual(bad_origin.status_code, 403)
            self.assertEqual((await client.post("/api/phone/sync/links", json={"devices": ["bad serial!"]})).status_code, 422)
            self.assertEqual((await client.post("/api/phone/sync/links", json={"devices": "a"})).status_code, 422)
            too_many = {"devices": [f"p{i}" for i in range(sl.MAX_LINKS + 1)]}
            self.assertEqual((await client.post("/api/phone/sync/links", json=too_many)).status_code, 422)
            ok = await client.post("/api/phone/sync/links", json={"devices": ["192.168.50.37:5555", "192.168.50.38:5555", "192.168.50.37:5555"]},
                                   headers={"origin": "https://dashboard.example.test"})
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.json()["total"], 2)
            cleared = await client.post("/api/phone/sync/links", json={"devices": []})
            self.assertEqual(cleared.status_code, 200)
        self.assertTrue(await wait_for(lambda: not sl.LINKS))


class FakeVideo:
    def __init__(self):
        self.events = []
    def control_ready(self):
        return True
    async def inject_touch(self, action, x, y, pressure=1.0):
        self.events.append(("touch", action, round(x, 4), round(y, 4)))
    async def inject_key(self, code):
        self.events.append(("key", code))


class FastControlLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fc.reset_state_for_tests()
        self.executor = AsyncMock(return_value=None)
        self.patches = [
            patch.dict(fc.h264.SESSIONS, {}, clear=True),
            patch.object(fc.fleet, "action_executor", self.executor),
            patch.object(fc, "TAP_HOLD", 0),
            patch.object(fc, "SWIPE_STEP", 0.05),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI(); fc.install_fast_control(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await sl.shutdown_for_tests()
        for p in reversed(self.patches):
            p.stop()
        fc.reset_state_for_tests()

    def ready_link(self, serial):
        link = sl.ControlLink(serial)
        link.events_seen = []
        async def inject_touch(action, x, y, pressure=1.0):
            link.events_seen.append(("touch", action, round(x, 4), round(y, 4)))
        link.control_ready = lambda: True
        link.inject_touch = inject_touch
        sl.LINKS[serial] = link
        return link

    async def test_followers_without_stream_use_their_link_not_adb(self):
        leader = FakeVideo(); fc.h264.SESSIONS["dev1"] = leader
        link = self.ready_link("dev2")
        body = {"device": "dev1", "x": 359.5, "y": 639.5, "stream_w": 720, "stream_h": 1280, "sync": {"devices": ["dev2", "dev3"]}}
        response = await self.client.post("/api/phone/tap", json=body)
        result = response.json()
        self.assertEqual(result["via"], "control")
        self.assertEqual(result["sync"]["via"].get("link"), 1, result)
        self.assertEqual(link.events_seen, [("touch", 0, 0.5, 0.5), ("touch", 1, 0.5, 0.5)])
        self.assertEqual(self.executor.await_count, 1)          # only dev3 needed ADB
        status = (await self.client.get("/api/phone/sync/status")).json()
        self.assertEqual(status["devices"]["dev1"]["path"], "stream")
        self.assertEqual(status["devices"]["dev2"]["path"], "link")
        self.assertEqual(status["devices"]["dev2"]["last"]["via"], "link")
        self.assertTrue(status["devices"]["dev2"]["last"]["ok"])
        self.assertEqual(status["devices"]["dev3"]["path"], "adb")

    async def test_leader_without_stream_uses_link(self):
        link = self.ready_link("dev9")
        result = (await self.client.post("/api/phone/tap", json={"device": "dev9", "x": 0, "y": 0, "stream_w": 100, "stream_h": 200})).json()
        self.assertEqual(result["via"], "link")
        self.assertEqual(link.events_seen[0], ("touch", 0, 0.0, 0.0))
        self.assertEqual(fc.STATS["link"], 1)

    def test_follower_wait_scales_with_gesture(self):
        self.assertAlmostEqual(fc.follower_wait({"kind": "tap"}), fc.FOLLOWER_WAIT)
        self.assertAlmostEqual(fc.follower_wait({"kind": "swipe", "duration": 400}), fc.FOLLOWER_WAIT + 0.4)
        self.assertAlmostEqual(fc.follower_wait({"kind": "text"}), fc.TEXT_FOLLOWER_WAIT)


if __name__ == "__main__":
    unittest.main()
