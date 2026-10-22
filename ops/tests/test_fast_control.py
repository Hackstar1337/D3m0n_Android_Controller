"""Offline tests for ops.fast_control (instant input + SYNC ALL).

No ADB subprocess is started: the upstream phone router, the fleet executor and
H.264 sessions are replaced with fakes.
"""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

import ops.fast_control as fc


class FakeSession:
    def __init__(self, ready=True, fail_after=None):
        self.ready = ready; self.events = []; self.fail_after = fail_after
        self.control_enabled = True; self.profile = "grid"; self.width = 480; self.height = 854
        self.control_events = 0; self.control_error = ""; self.clients = {}
    def control_ready(self):
        return self.ready
    async def inject_touch(self, action, x, y, pressure=1.0):
        if self.fail_after is not None and len(self.events) >= self.fail_after:
            raise fc.h264.ControlUnavailable("socket closed")
        self.events.append(("touch", action, round(x, 4), round(y, 4)))
    async def inject_key(self, code):
        if self.fail_after is not None and len(self.events) >= self.fail_after:
            raise fc.h264.ControlUnavailable("socket closed")
        self.events.append(("key", code))


class FakeUpstream:
    def __init__(self):
        self.calls = []; self.raise_error = None
    def _record(self, name, data):
        self.calls.append((name, dict(data)))
        if self.raise_error:
            raise self.raise_error
        return {"ok": True, "device": data.get("device"), "platform": "android"}
    def api_phone_tap(self, data):
        return self._record("tap", data)
    def api_phone_swipe(self, data):
        return self._record("swipe", data)
    def api_phone_key(self, data):
        return self._record("key", data)
    async def phone_input(self, request, data):
        return self._record("input", data)


class FastControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fc.reset_state_for_tests()
        self.upstream = FakeUpstream()
        self.sessions = {}
        self.executor = AsyncMock(return_value=None)
        self.run_adb = AsyncMock(return_value="")
        self.patches = [
            patch.object(fc, "_upstream", return_value=self.upstream),
            patch.dict(fc.h264.SESSIONS, self.sessions, clear=True),
            patch.object(fc.fleet, "action_executor", self.executor),
            patch.object(fc.fleet, "run_adb", self.run_adb),
            patch.object(fc, "TAP_HOLD", 0),
            patch.object(fc, "SWIPE_STEP", 0.05),
        ]
        for p in self.patches:
            p.start()
        app = FastAPI(); fc.install_fast_control(app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        fc.reset_state_for_tests()

    def add(self, serial, **kwargs):
        session = FakeSession(**kwargs)
        fc.h264.SESSIONS[serial] = session
        return session

    async def post(self, path, body, status=200):
        response = await self.client.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def test_routes_are_installed_ahead_of_upstream(self):
        app = FastAPI()
        @app.post("/api/phone/tap")
        async def upstream_tap():
            return {"upstream": True}
        fc.install_fast_control(app)
        # FastAPI 0.150 may keep included routers nested, so check behaviour:
        # the fast route must answer before the previously registered upstream.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            result = (await client.post("/api/phone/tap", json={"device": "d", "x": 1, "y": 1})).json()
        self.assertNotIn("upstream", result)
        self.assertEqual(result.get("via"), "adb")

    async def test_tap_uses_control_channel_with_exact_frame_mapping(self):
        leader = self.add("dev1")
        result = await self.post("/api/phone/tap", {"device": "dev1", "x": 359.5, "y": 1279, "stream_w": 720, "stream_h": 1280})
        self.assertEqual(result, {"ok": True, "device": "dev1", "platform": "android", "via": "control"})
        self.assertEqual(leader.events, [("touch", 0, 0.5, 1.0), ("touch", 1, 0.5, 1.0)])
        self.assertEqual(self.upstream.calls, [])

    async def test_falls_back_to_upstream_without_control_or_frame_size(self):
        self.add("dev1", ready=False)
        await self.post("/api/phone/tap", {"device": "dev1", "x": 10, "y": 10, "stream_w": 720, "stream_h": 1280})
        await self.post("/api/phone/tap", {"device": "dev2", "x": 10, "y": 10})            # no stream size
        await self.post("/api/phone/tap", {"device": "ios:abc", "x": 1, "y": 1, "stream_w": 2, "stream_h": 2})
        result = await self.post("/api/phone/key", {"device": "dev3", "key": "KEYCODE_NOT_REAL"})
        self.assertEqual([c[0] for c in self.upstream.calls], ["tap", "tap", "tap", "key"])
        self.assertEqual(result["via"], "adb")

    async def test_control_failure_before_any_event_falls_back(self):
        self.add("dev1", fail_after=0)
        result = await self.post("/api/phone/key", {"device": "dev1", "key": "BACK"})
        self.assertEqual(result["via"], "adb")
        self.assertEqual(self.upstream.calls[0][0], "key")

    async def test_mid_gesture_loss_is_reported_not_replayed(self):
        self.add("dev1", fail_after=2)
        result = await self.post("/api/phone/swipe", {"device": "dev1", "x1": 0, "y1": 0, "x2": 100, "y2": 100,
                                                      "stream_w": 101, "stream_h": 101, "duration": 200})
        self.assertFalse(result["ok"])
        self.assertIn("lost", result["error"])
        self.assertEqual(self.upstream.calls, [])

    async def test_swipe_is_interpolated_and_clamped(self):
        leader = self.add("dev1")
        await self.post("/api/phone/swipe", {"device": "dev1", "x1": 0, "y1": 0, "x2": 100, "y2": 50,
                                             "stream_w": 101, "stream_h": 101, "duration": 20})
        actions = [e[1] for e in leader.events]
        self.assertEqual(actions[0], 0); self.assertEqual(actions[-1], 1)
        self.assertTrue(all(a == 2 for a in actions[1:-1]))
        self.assertEqual(leader.events[-2][2:], (1.0, 0.5))
        self.assertEqual(fc.swipe_gesture({"x1": 0, "y1": 0, "x2": 1, "y2": 1, "stream_w": 2, "stream_h": 2, "duration": 9999})["duration"], 1500)
        self.assertEqual(fc.swipe_gesture({"x1": 0, "y1": 0, "x2": 1, "y2": 1, "stream_w": 2, "stream_h": 2, "duration": 1})["duration"], 100)

    async def test_sync_all_mirrors_tap_to_every_follower(self):
        leader = self.add("dev1"); follower = self.add("dev2")
        body = {"device": "dev1", "x": 0, "y": 639.5, "stream_w": 720, "stream_h": 1280,
                "sync": {"devices": ["dev1", "dev2", "dev3", "dev3"]}}
        result = await self.post("/api/phone/tap", body)
        self.assertTrue(result["ok"])
        self.assertEqual(result["sync"]["targets"], 2)
        self.assertEqual(result["sync"]["ok"], 2)
        self.assertEqual(result["sync"]["via"], {"control": 1, "adb": 1})
        self.assertEqual(follower.events, leader.events)
        self.executor.assert_awaited_once_with("dev3", "tap", {"x": 0.0, "y": 0.5})
        self.assertEqual(fc.STATS["sync_requests"], 1)

    async def test_sync_keys_text_and_failures(self):
        self.executor.side_effect = [RuntimeError("device offline\nstack")]
        result = await self.post("/api/phone/key", {"device": "dev1", "key": "KEYCODE_BACK", "sync": {"devices": ["dev2"]}})
        self.run_adb.assert_awaited_once()
        self.assertEqual(self.run_adb.await_args.args[-1], "4")
        self.assertEqual(result["sync"]["ok"], 1)
        # Follower failures are reported per device without failing the leader.
        result = await self.post("/api/phone/tap", {"device": "dev1", "x": 1, "y": 1, "stream_w": 3, "stream_h": 3,
                                                    "sync": {"devices": ["dev4"]}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["sync"]["failed"], [{"device": "dev4", "error": "device offline"}])
        self.executor.side_effect = None
        result = await self.post("/api/phone/input", {"device": "dev1", "action": "text", "text": "héllo",
                                                      "sync": {"devices": ["dev5"]}})
        self.assertEqual(self.upstream.calls[-1], ("input", {"device": "dev1", "action": "text", "text": "héllo"}))
        self.executor.assert_awaited_with("dev5", "text", {"text": "héllo"})
        self.assertEqual(result["sync"]["via"], {"adb": 1})

    async def test_sync_validation_and_unsupported_actions(self):
        await self.post("/api/phone/tap", {"device": "d", "x": 1, "y": 1, "sync": {"devices": ["bad serial!"]}}, status=422)
        await self.post("/api/phone/tap", {"device": "d", "x": 1, "y": 1, "sync": {"devices": [f"d{i}" for i in range(101)]}}, status=422)
        await self.post("/api/phone/tap", {"device": "d", "x": 1, "y": 1, "sync": "all"}, status=422)
        await self.post("/api/phone/tap", {"device": "d", "x": 1, "y": 1, "sync": {"devices": ["ios:x"]}}, status=422)
        result = await self.post("/api/phone/input", {"device": "d", "action": "launch", "sync": {"devices": ["e"]}})
        self.assertEqual(result["sync"]["action"], "unsupported")
        self.assertEqual(result["sync"]["failed"][0]["device"], "e")
        self.assertNotIn("sync", self.upstream.calls[-1][1])

    async def test_cross_site_sync_is_denied(self):
        response = await self.client.post("/api/phone/tap", json={"device": "d", "x": 1, "y": 1, "sync": {"devices": ["e"]}},
                                          headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    async def test_leader_error_still_reports_after_followers_ran(self):
        self.upstream.raise_error = HTTPException(status_code=500, detail="boom")
        await self.post("/api/phone/tap", {"device": "dev1", "x": 1, "y": 1, "stream_w": 3, "stream_h": 3,
                                           "sync": {"devices": ["dev2"]}}, status=500)
        self.executor.assert_awaited_once()

    async def test_slow_followers_do_not_block_the_response(self):
        gate = asyncio.Event()
        async def slow(*args, **kwargs):
            await gate.wait()
        self.executor.side_effect = slow
        with patch.object(fc, "FOLLOWER_WAIT", 0.05):
            result = await self.post("/api/phone/tap", {"device": "dev1", "x": 1, "y": 1, "stream_w": 3, "stream_h": 3,
                                                        "sync": {"devices": ["dev2", "dev3"]}})
        self.assertEqual(result["sync"]["pending"], 2)
        gate.set()
        for _ in range(100):
            if not fc._background:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(fc._background), 0)

    async def test_status_route(self):
        self.add("dev1"); self.add("dev2", ready=False)
        status = (await self.client.get("/api/phone/control/status")).json()
        self.assertEqual(status["ready"], 1)
        self.assertEqual(set(status["devices"]), {"dev1", "dev2"})
        self.assertEqual(status["max_sync_devices"], fc.MAX_SYNC_DEVICES)

    def test_keycodes(self):
        self.assertEqual(fc.keycode_for("KEYCODE_APP_SWITCH"), 187)
        self.assertEqual(fc.keycode_for("home"), 3)
        self.assertEqual(fc.keycode_for(26), 26)
        self.assertEqual(fc.keycode_for("24"), 24)
        for bad in (True, 0, 999, "NOPE", None, 1.5):
            self.assertIsNone(fc.keycode_for(bad))


if __name__ == "__main__":
    unittest.main()
