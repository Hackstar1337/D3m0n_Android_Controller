"""Offline tests for ops.live_input (real-time touch + key fan-out).

No phone, ADB or scrcpy process is used: H.264 sessions are fakes whose control
socket records every byte written, and the ADB executor is a mock.
"""
from __future__ import annotations

import asyncio
import json
import struct
import types
import unittest
from unittest.mock import AsyncMock, patch

import ops.fast_control as fc
import ops.live_input as li

TOUCH = struct.Struct("!BBqiiHHHii")


class FakeWriter:
    def __init__(self):
        self.writes = []          # (loop_time, bytes)
        self.fail = False
        self.closing = False
        self.buffered = 0
        self.drains = 0
        self.transport = types.SimpleNamespace(get_write_buffer_size=lambda: self.buffered)

    def write(self, payload):
        if self.fail:
            raise OSError("broken pipe")
        self.writes.append((asyncio.get_running_loop().time(), bytes(payload)))

    def is_closing(self):
        return self.closing

    async def drain(self):
        self.drains += 1
        self.buffered = 0


class FakeSession:
    """Mimics the control surface of ops.android_h264.Session."""

    def __init__(self, width=480, height=854):
        self.control_writer = FakeWriter()
        self.writer = "VIDEO-SOCKET-MUST-NOT-BE-USED"
        self.width, self.height = width, height
        self.control_events = 0
        self.control_error = ""
        self.ready = True

    def control_ready(self):
        return self.ready

    def point(self, nx, ny):
        w, h = self.width, self.height
        return int(round(nx * (w - 1))), int(round(ny * (h - 1))), w, h

    def touches(self):
        out = []
        for _, data in self.control_writer.writes:
            if data[0] == 2:
                _type, action, _pid, x, y, w, h, pressure, _b, _bb = TOUCH.unpack(data)
                out.append((action, x, y, w, h, pressure))
        return out


class LiveInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fc.reset_state_for_tests()
        li.reset_state_for_tests()
        self.sent = []
        self.run = AsyncMock(return_value={"ok": True})
        self.patches = [
            patch.dict(fc.h264.SESSIONS, {}, clear=True),
            patch.object(fc.sync_links, "link_for", return_value=None),
            patch.object(fc, "run_on_device", self.run),
        ]
        for p in self.patches:
            p.start()
        self.conn = li.LiveConnection(self.sent.append)

    async def asyncTearDown(self):
        # Match the real WebSocket route lifecycle, even after an assertion failure.
        await self.conn.close()
        for p in reversed(self.patches):
            p.stop()
        fc.reset_state_for_tests()
        li.reset_state_for_tests()

    def add(self, serial, **kwargs):
        session = FakeSession(**kwargs)
        fc.h264.SESSIONS[serial] = session
        return session

    async def msg(self, **message):
        await self.conn.handle_text(json.dumps(message))

    def acks(self, phase=None):
        return [m for m in self.sent if m.get("t") == "ack" and (phase is None or m.get("phase") == phase)]

    def errors(self):
        return [m for m in self.sent if m.get("t") == "error"]

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    async def test_live_gesture_reaches_every_phone_in_the_same_turn(self):
        lead = self.add("dev1")
        f2 = self.add("dev2")
        f3 = self.add("dev3", width=720, height=1280)
        await self.msg(t="down", id=7, d="dev1", s=["dev2", "dev3", "dev4"], x=0.1, y=0.2)
        await self.msg(t="move", id=7, x=0.3, y=0.5)
        await self.msg(t="move", id=7, x=0.6, y=0.8)
        await self.msg(t="up", id=7, x=0.6, y=0.8)
        await self.settle()
        for session in (lead, f2, f3):
            self.assertEqual([t[0] for t in session.touches()], [0, 2, 2, 1])
            self.assertEqual(session.touches()[-1][5], 0, "UP carries zero pressure")
        # Same normalized point, proportional per-phone pixels.
        self.assertEqual(lead.touches()[0][1:3], (round(0.1 * 479), round(0.2 * 853)))
        self.assertEqual(f3.touches()[0][1:5], (round(0.1 * 719), round(0.2 * 1279), 720, 1280))
        # Lockstep: DOWN reached all control sockets in the same event-loop turn
        # (no await between writes), so their spread is far below a millisecond.
        downs = [s.control_writer.writes[0][0] for s in (lead, f2, f3)]
        self.assertLess(max(downs) - min(downs), 0.001)
        self.assertEqual(lead.writer, "VIDEO-SOCKET-MUST-NOT-BE-USED")
        down = self.acks("down")[0]
        self.assertEqual((down["ok"], down["via"], down["live"], down["adb"]), (True, "control", 3, 1))
        up = self.acks("up")[0]
        self.assertTrue(up["ok"])
        self.assertEqual(up["sync"]["targets"], 3)
        self.assertEqual(up["sync"]["ok"], 2)
        self.assertEqual(up["sync"]["pending"], 1)
        self.assertEqual(up["sync"]["via"], {"control": 2})
        self.assertTrue(up["sync"]["live"])
        # The ADB-only phone receives the completed path once, on release.
        self.run.assert_awaited_once()
        serial, gesture = self.run.await_args.args
        self.assertEqual(serial, "dev4")
        self.assertEqual(gesture["kind"], "swipe")
        self.assertEqual((gesture["x1"], gesture["y1"], gesture["x2"], gesture["y2"]), (0.1, 0.2, 0.6, 0.8))
        self.assertEqual(gesture["duration"], fc.SWIPE_MIN_MS)
        self.assertTrue(fc.LAST_RESULTS["dev2"]["ok"])
        self.assertEqual(fc.LAST_RESULTS["dev2"]["action"], "touch")
        self.assertEqual(fc.STATS["sync_requests"], 1)
        self.assertEqual(f2.control_events, 4)

    async def test_adb_replay_distinguishes_tap_and_long_press(self):
        self.add("dev1")
        await self.msg(t="down", id=1, d="dev1", s=["dev9"], x=0.5, y=0.5)
        await self.msg(t="up", id=1, x=0.505, y=0.5)
        await self.settle()
        self.assertEqual(self.run.await_args.args, ("dev9", {"kind": "tap", "x": 0.505, "y": 0.5}))
        await self.msg(t="down", id=2, d="dev1", s=["dev9"], x=0.5, y=0.5)
        self.conn.gesture.started -= 0.8
        await self.msg(t="up", id=2, x=0.5, y=0.5)
        await self.settle()
        serial, gesture = self.run.await_args.args
        self.assertEqual(gesture["kind"], "swipe")
        self.assertGreaterEqual(gesture["duration"], 790)
        self.assertEqual((gesture["x1"], gesture["x2"]), (0.5, 0.5))

    async def test_leader_without_instant_path_replays_through_adb(self):
        await self.msg(t="down", id=3, d="dev5", x=0.2, y=0.2)
        await self.msg(t="up", id=3, x=0.2, y=0.2)
        await self.settle()
        self.assertEqual(self.acks("down")[0]["via"], "adb")
        up = self.acks("up")[0]
        self.assertTrue(up["ok"]); self.assertTrue(up["pending"]); self.assertNotIn("sync", up)
        self.run.assert_awaited_once()

    async def test_stale_ids_ignored_and_new_touch_releases_old_one(self):
        lead = self.add("dev1")
        await self.msg(t="down", id=1, d="dev1", s=["dev9"], x=0.1, y=0.1)
        await self.msg(t="move", id=99, x=0.9, y=0.9)
        await self.msg(t="up", id=99, x=0.9, y=0.9)
        self.assertEqual([t[0] for t in lead.touches()], [0])
        await self.msg(t="down", id=2, d="dev1", x=0.4, y=0.4)
        self.assertEqual([t[0] for t in lead.touches()], [0, 1, 0], "old finger lifted before the new touch")
        released = self.acks("up")[0]
        self.assertTrue(released["cancelled"]); self.assertEqual(released["reason"], "superseded")
        self.run.assert_not_awaited()  # a cancelled touch is never replayed through ADB

    async def test_idle_and_disconnect_never_leave_a_finger_down(self):
        lead = self.add("dev1")
        await self.msg(t="down", id=1, d="dev1", x=0.3, y=0.3)
        await self.msg(t="hold", id=1)
        await self.conn.watchdog_tick()
        self.assertEqual([t[0] for t in lead.touches()], [0], "hold keeps the touch alive")
        self.conn.gesture.touched -= li.IDLE_RELEASE + 1
        await self.conn.watchdog_tick()
        self.assertEqual([t[0] for t in lead.touches()], [0, 1])
        self.assertEqual([m for m in self.sent if m.get("t") == "released"][0]["reason"], "idle")
        await self.msg(t="down", id=2, d="dev1", x=0.3, y=0.3)
        self.conn.gesture.started -= li.MAX_HOLD + 1
        await self.conn.watchdog_tick()
        self.assertEqual([t[0] for t in lead.touches()], [0, 1, 0, 1])
        await self.msg(t="down", id=3, d="dev1", x=0.3, y=0.3)
        await self.conn.close()
        self.assertEqual([t[0] for t in lead.touches()], [0, 1, 0, 1, 0, 1])
        self.assertEqual(li.STATS["released_by_server"], 3)

    async def test_lost_follower_is_reported_not_replayed(self):
        self.add("dev1")
        f2 = self.add("dev2")
        await self.msg(t="down", id=1, d="dev1", s=["dev2"], x=0.1, y=0.1)
        f2.control_writer.fail = True
        await self.msg(t="move", id=1, x=0.2, y=0.2)
        await self.msg(t="up", id=1, x=0.2, y=0.2)
        up = self.acks("up")[0]
        self.assertTrue(up["ok"])
        self.assertEqual(up["sync"]["failed"], [{"device": "dev2", "error": "Control channel lost during the gesture; release unconfirmed"}])
        self.assertFalse(fc.LAST_RESULTS["dev2"]["ok"])
        self.assertEqual(f2.control_error, "broken pipe")
        self.run.assert_not_awaited()

    async def test_cancelled_finish_releases_native_ownership(self):
        self.add("dev1")
        await self.msg(t="down", id=1, d="dev1", x=0.1, y=0.1)
        gesture = self.conn.gesture
        with patch.object(li, "broadcast", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.conn._finish(gesture, gesture.last, False, "")
        self.assertEqual(gesture.leases, {})
        # Cancellation cannot leave an orphan lease blocking the next input.
        await asyncio.wait_for(self.msg(t="down", id=2, d="dev1", x=0.1, y=0.1), 0.25)

    async def test_unready_follower_at_down_falls_back_to_adb(self):
        self.add("dev1")
        f2 = self.add("dev2"); f2.ready = False
        await self.msg(t="down", id=1, d="dev1", s=["dev2"], x=0.1, y=0.1)
        await self.msg(t="up", id=1, x=0.1, y=0.1)
        await self.settle()
        self.assertEqual(f2.touches(), [])
        self.assertEqual(self.run.await_args.args[0], "dev2")

    async def test_keys_are_one_write_per_phone_and_adb_for_others(self):
        lead = self.add("dev1")
        await self.msg(t="key", id=5, d="dev1", s=["dev9"], k="BACK")
        await self.settle()
        payload = lead.control_writer.writes[0][1]
        self.assertEqual(payload, fc.h264.key_message(0, 4) + fc.h264.key_message(1, 4))
        self.assertEqual(len(lead.control_writer.writes), 1)
        self.run.assert_awaited_once_with("dev9", {"kind": "key", "code": 4})
        ack = self.acks("key")[0]
        self.assertTrue(ack["ok"]); self.assertEqual(ack["sync"]["pending"], 1)
        await self.msg(t="key", id=6, d="dev1", k="NOT_A_KEY")
        self.assertEqual(self.errors()[-1]["error"], "Unsupported key")

    async def test_adb_backlog_is_bounded(self):
        self.add("dev1")
        gate = asyncio.Event()
        async def slow(*_args):
            await gate.wait()
            return {"ok": True}
        self.run.side_effect = slow
        with patch.object(fc, "MAX_FOLLOWER_BACKLOG", 2):
            for gid in range(3):
                await self.msg(t="key", id=gid, d="dev1", s=["dev9"], k="HOME")
            last = self.acks("key")[-1]
            self.assertEqual(last["sync"]["failed"][0]["device"], "dev9")
            self.assertIn("catching up", last["sync"]["failed"][0]["error"])
        gate.set()
        for _ in range(50):
            if not fc._background:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(fc._adb_backlog, {})

    async def test_validation_errors_do_not_touch_phones(self):
        lead = self.add("dev1")
        bad = [
            {"t": "down", "id": 1, "d": "bad serial!", "x": 0.1, "y": 0.1},
            {"t": "down", "id": 1, "d": "dev1", "s": [f"d{i}" for i in range(101)], "x": 0.1, "y": 0.1},
            {"t": "down", "id": 1, "d": "dev1", "s": "all", "x": 0.1, "y": 0.1},
            {"t": "down", "id": 1, "d": "dev1", "x": "NaN", "y": 0.1},
            {"t": "down", "id": 1, "d": "dev1", "x": 2, "y": 0.1},
            {"t": "down", "id": 1, "d": "dev1", "x": True, "y": 0.1},
            {"t": "down", "d": "dev1", "x": 0.1, "y": 0.1},
            {"t": "launch", "id": 1},
        ]
        for message in bad:
            await self.msg(**message)
        await self.conn.handle_text("[1,2]")
        await self.conn.handle_text("{not json")
        await self.conn.handle_text(json.dumps({"t": "ping", "pad": "x" * li.MAX_MESSAGE_BYTES}))
        self.assertEqual(len(self.errors()), len(bad) + 3)
        self.assertEqual(lead.control_writer.writes, [])
        self.assertIsNone(self.conn.gesture)

    async def test_rate_limit_drops_moves_and_rejects_others(self):
        self.add("dev1")
        with patch.object(li, "MAX_MESSAGES_PER_SECOND", 5):
            for i in range(5):
                await self.msg(t="ping", id=i)
            await self.msg(t="move", id=1, x=0.1, y=0.1)
            await self.msg(t="ping", id=9)
        self.assertEqual(len([m for m in self.sent if m.get("t") == "pong"]), 5)
        self.assertEqual(self.errors()[-1]["error"], "Too many input messages")

    async def test_backlogged_socket_is_drained(self):
        lead = self.add("dev1")
        lead.control_writer.buffered = li.FLUSH_LIMIT + 1
        await self.msg(t="down", id=1, d="dev1", x=0.5, y=0.5)
        self.assertEqual(lead.control_writer.drains, 1)
        await self.msg(t="up", id=1, x=0.5, y=0.5)


class LiveInputRouteTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except Exception as error:  # pragma: no cover - depends on venv
            self.skipTest(f"TestClient unavailable: {error}")
        fc.reset_state_for_tests(); li.reset_state_for_tests()
        app = FastAPI()
        app.include_router(li.router)
        self.client = TestClient(app)
        self.session = FakeSession()
        self.patches = [patch.dict(fc.h264.SESSIONS, {"dev1": self.session}, clear=True),
                        patch.object(fc.sync_links, "link_for", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(getattr(self, "patches", [])):
            p.stop()

    def test_install_fast_control_serves_live_routes(self):
        # FastAPI 0.150 may keep included routers nested, so check behaviour.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        fc.install_fast_control(app)
        client = TestClient(app)
        status = client.get("/api/phone/input/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["protocol"], li.PROTOCOL)
        with client.websocket_connect("/api/phone/input/ws") as ws:
            self.assertEqual(json.loads(ws.receive_text())["t"], "hello")

    def test_untrusted_origin_is_refused(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/phone/input/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_text()

    def test_hello_ping_and_touch_round_trip(self):
        with self.client.websocket_connect("/api/phone/input/ws", headers={"origin": "https://dashboard.example.test"}) as ws:
            hello = json.loads(ws.receive_text())
            self.assertEqual((hello["t"], hello["v"]), ("hello", li.PROTOCOL))
            ws.send_text(json.dumps({"t": "ping", "id": 1}))
            self.assertEqual(json.loads(ws.receive_text()), {"t": "pong", "id": 1})
            ws.send_text(json.dumps({"t": "down", "id": 2, "d": "dev1", "x": 0.5, "y": 0.5}))
            self.assertEqual(json.loads(ws.receive_text())["phase"], "down")
            ws.send_text(json.dumps({"t": "move", "id": 2, "x": 0.6, "y": 0.6}))
        # Closing the socket mid-touch must lift the finger.
        for _ in range(100):
            if [t[0] for t in self.session.touches()][-1:] == [1]:
                break
            import time as _time; _time.sleep(0.01)
        self.assertEqual([t[0] for t in self.session.touches()], [0, 2, 1])
        status = self.client.get("/api/phone/input/status").json()
        self.assertEqual(status["stats"]["open"], 0)


if __name__ == "__main__":
    unittest.main()
