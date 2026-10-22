"""Offline timing tests for the HTTP Sync path in ops.fast_control.

Fake control sessions record the event-loop time of every injected event, so
lockstep start, swipe duration and response latency are measured without any
phone, ADB or scrcpy process.
"""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import ops.fast_control as fc


class TimedSession:
    def __init__(self, delay=0.0):
        self.events = []
        self.delay = delay
        self.control_enabled = True; self.profile = "grid"; self.width = 480; self.height = 854
        self.control_events = 0; self.control_error = ""; self.clients = {}

    def control_ready(self):
        return True

    async def inject_touch(self, action, x, y, pressure=1.0):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.events.append((action, time.perf_counter()))

    async def inject_key(self, code):
        self.events.append(("key", time.perf_counter()))


class FakeUpstream:
    def api_phone_tap(self, data):
        return {"ok": True, "device": data.get("device")}
    api_phone_swipe = api_phone_key = api_phone_tap


class SyncLockstepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fc.reset_state_for_tests()
        self.executor = AsyncMock(return_value=None)
        self.patches = [
            patch.object(fc, "_upstream", return_value=FakeUpstream()),
            patch.dict(fc.h264.SESSIONS, {}, clear=True),
            patch.object(fc.sync_links, "link_for", return_value=None),
            patch.object(fc.fleet, "action_executor", self.executor),
            patch.object(fc.fleet, "run_adb", AsyncMock(return_value="")),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI(); fc.install_fast_control(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        for _ in range(100):
            if not fc._background:
                break
            await asyncio.sleep(0.01)
        fc.reset_state_for_tests()

    def add(self, serial, **kwargs):
        session = TimedSession(**kwargs)
        fc.h264.SESSIONS[serial] = session
        return session

    async def post(self, path, body):
        response = await self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_swipe_starts_together_and_keeps_its_duration(self):
        # IsolatedAsyncioTestCase defaults to debugger instrumentation. Measure
        # these unchanged millisecond deadlines in production's non-debug mode;
        # slow-callback trace collection otherwise dominates mocked socket writes.
        asyncio.get_running_loop().set_debug(False)
        sessions = [self.add(f"dev{i}") for i in range(1, 11)]
        body = {"device": "dev1", "x1": 0, "y1": 0, "x2": 100, "y2": 100, "stream_w": 101, "stream_h": 101,
                "duration": 300, "sync": {"devices": [f"dev{i}" for i in range(2, 11)]}}
        result = await self.post("/api/phone/swipe", body)
        self.assertEqual(result["sync"]["ok"], 9)
        downs = [s.events[0][1] for s in sessions]
        ups = [s.events[-1][1] for s in sessions]
        self.assertLess(max(downs) - min(downs), 0.003, "all phones touch down together")
        self.assertLess(max(ups) - min(ups), 0.006, "all phones lift together")
        for s in sessions:
            self.assertEqual(s.events[0][0], 0); self.assertEqual(s.events[-1][0], 1)
            span = s.events[-1][1] - s.events[0][1]
            self.assertGreaterEqual(span, 0.295)
            self.assertLess(span, 0.34, "absolute deadlines keep the swipe close to 300 ms")

    async def test_slow_adb_followers_do_not_hold_the_response(self):
        self.add("dev1"); self.add("dev2")
        async def slow(*_args, **_kwargs):
            await asyncio.sleep(0.6)
        self.executor.side_effect = slow
        started = time.perf_counter()
        result = await self.post("/api/phone/tap", {"device": "dev1", "x": 1, "y": 1, "stream_w": 3, "stream_h": 3,
                                                    "sync": {"devices": ["dev2", "dev3", "dev4"]}})
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.25, f"response took {elapsed:.3f}s")
        self.assertEqual(result["sync"]["ok"], 1)
        self.assertEqual(result["sync"]["pending"], 2)
        self.assertEqual(result["sync"]["via"], {"control": 1})

    async def test_adb_follower_backlog_is_bounded(self):
        self.add("dev1")
        gate = asyncio.Event()
        async def blocked(*_args, **_kwargs):
            await gate.wait()
        self.executor.side_effect = blocked
        with patch.object(fc, "MAX_FOLLOWER_BACKLOG", 2), patch.object(fc, "ADB_FOLLOWER_WAIT", 0.0):
            for _ in range(3):
                result = await self.post("/api/phone/tap", {"device": "dev1", "x": 1, "y": 1, "stream_w": 3,
                                                            "stream_h": 3, "sync": {"devices": ["dev9"]}})
        self.assertEqual(result["sync"]["failed"][0]["device"], "dev9")
        self.assertIn("catching up", result["sync"]["failed"][0]["error"])
        gate.set()
        for _ in range(100):
            if not fc._background:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(fc._adb_backlog, {})


if __name__ == "__main__":
    unittest.main()
