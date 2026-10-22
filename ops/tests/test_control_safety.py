"""Focused offline regressions for stream/control safety fixes.

These tests use deterministic fake writers and never invoke ADB, a phone, or a
network service. They exercise the actual FastControl/live-input code paths.
"""
from __future__ import annotations

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import ops.android_h264 as h264
import ops.control_arbiter as arbiter
import ops.fast_control as fc
import ops.live_input as li


class DrainFailureWriter:
    def __init__(self, fail_after: int | None = None):
        self.writes: list[bytes] = []
        self.fail_after = fail_after
        self.attempts = 0
        self.transport = types.SimpleNamespace(get_write_buffer_size=lambda: 0)

    def is_closing(self):
        return False

    def write(self, payload):
        self.attempts += 1
        self.writes.append(bytes(payload))

    async def drain(self):
        if self.fail_after is not None and self.attempts >= self.fail_after:
            raise OSError("drain failed after accepted write")


class SelectiveDrainWriter(DrainFailureWriter):
    def __init__(self, failures):
        super().__init__()
        self.failures = set(failures)

    async def drain(self):
        if self.attempts in self.failures:
            raise OSError("selected drain failure")


class SlowDrainWriter(DrainFailureWriter):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def drain(self):
        self.started.set()
        await self.release.wait()


class SafetySession:
    def __init__(self, writer: DrainFailureWriter | None = None, serial="safe-device"):
        self.writer = writer or DrainFailureWriter()
        self.serial = serial
        self.control_writer = self.writer
        self.width, self.height = 480, 854
        self.control_events = 0
        self.control_error = ""
        self.ready = True

    def control_ready(self):
        return self.ready

    def invalidate_control(self, error="invalidated"):
        self.ready = False
        self.control_error = error

    def point(self, nx, ny):
        return round(nx * (self.width - 1)), round(ny * (self.height - 1)), self.width, self.height

    async def write_control(self, payload, arbiter_token=None):
        try:
            await arbiter.write(self.serial, self.writer, payload, owner=arbiter_token)
        except arbiter.AmbiguousWrite as error:
            raise h264.ControlWriteAmbiguous(str(error)) from error
        self.control_events += 1

    async def write_control_now(self, payload, arbiter_token=None):
        async with arbiter.hold(self.serial, arbiter_token):
            self.writer.write(payload)
        return self.writer

    async def inject_touch(self, action, x, y, pressure=1.0, arbiter_token=None):
        await self.write_control(h264.touch_message(action, *self.point(x, y), pressure), arbiter_token)

    async def inject_key(self, code, arbiter_token=None):
        await self.write_control(h264.key_message(h264.ACTION_DOWN, code), arbiter_token)
        await self.write_control(h264.key_message(h264.ACTION_UP, code), arbiter_token)


class ControlSafetyRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        arbiter.reset_for_tests()
        fc.reset_state_for_tests()

    async def test_accepted_write_then_drain_failure_never_replays_to_adb(self):
        writer = DrainFailureWriter(fail_after=1)
        session = SafetySession(writer)
        adb = AsyncMock()
        with patch.object(fc, "control_session", return_value=session), patch.object(fc, "perform_adb", adb):
            result = await fc.run_on_device("safe-device", {"kind": "tap", "x": 0.2, "y": 0.3})
        self.assertFalse(result["ok"])
        self.assertIn("ambiguous", result["error"])
        self.assertEqual(writer.attempts, 2, "DOWN and the best-effort UP were attempted")
        adb.assert_not_awaited()

    async def test_failure_before_first_write_still_allows_adb_fallback(self):
        session = SafetySession()
        session.control_ready = lambda: False
        adb = AsyncMock()
        with patch.object(fc, "control_session", return_value=session), patch.object(fc, "perform_adb", adb):
            result = await fc.run_on_device("safe-device", {"kind": "key", "code": 4})
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "adb")
        adb.assert_awaited_once()

    async def test_ambiguous_writer_is_invalidated_and_next_action_does_not_reuse_it(self):
        writer = DrainFailureWriter(fail_after=1)
        session = SafetySession(writer)
        adb = AsyncMock()
        with patch.object(fc, "control_session", return_value=session), patch.object(fc, "perform_adb", adb):
            first = await fc.run_on_device("safe-device", {"kind": "tap", "x": 0.2, "y": 0.3})
            second = await fc.run_on_device("safe-device", {"kind": "tap", "x": 0.4, "y": 0.5})
        self.assertFalse(first["ok"])
        self.assertFalse(session.ready)
        self.assertTrue(second["ok"])
        self.assertEqual(second["via"], "adb")
        adb.assert_awaited_once()

    async def test_http_release_truth_distinguishes_confirmed_from_unconfirmed(self):
        confirmed = SafetySession(SelectiveDrainWriter({1}))
        with patch.object(fc, "control_session", return_value=confirmed), patch.object(fc, "perform_adb", AsyncMock()):
            result = await fc.run_on_device("safe-device", {"kind": "tap", "x": 0.2, "y": 0.3})
        self.assertFalse(result["ok"])
        self.assertIn("release confirmed", result["error"])
        self.assertNotIn("release unconfirmed", result["error"])

        unconfirmed = SafetySession(DrainFailureWriter(fail_after=1))
        with patch.object(fc, "control_session", return_value=unconfirmed), patch.object(fc, "perform_adb", AsyncMock()):
            result = await fc.run_on_device("safe-device", {"kind": "tap", "x": 0.2, "y": 0.3})
        self.assertFalse(result["ok"])
        self.assertIn("release unconfirmed", result["error"])

    async def test_failed_live_target_gets_release_attempt_and_truthful_ack(self):
        writer = DrainFailureWriter(fail_after=2)  # DOWN succeeds; MOVE and UP fail.
        session = SafetySession(writer)
        sent = []
        conn = li.LiveConnection(sent.append)
        with patch.dict(fc.h264.SESSIONS, {"safe-device": session}, clear=True), \
             patch.object(fc.sync_links, "link_for", return_value=None):
            await conn.handle_text(json.dumps({"t": "down", "id": 1, "d": "safe-device", "x": 0.1, "y": 0.1}))
            await conn.handle_text(json.dumps({"t": "move", "id": 1, "x": 0.2, "y": 0.2}))
            await conn.handle_text(json.dumps({"t": "up", "id": 1, "x": 0.2, "y": 0.2}))
        up = [message for message in sent if message.get("phase") == "up"][0]
        self.assertFalse(up["ok"])
        self.assertIn("release unconfirmed", up["error"])
        self.assertGreaterEqual(writer.attempts, 3, "final UP was attempted instead of skipped")

    async def test_two_live_gestures_cannot_interleave_on_one_device(self):
        session = SafetySession()
        first, second = li.LiveConnection(lambda _: None), li.LiveConnection(lambda _: None)
        with patch.dict(fc.h264.SESSIONS, {"safe-device": session}, clear=True), \
             patch.object(fc.sync_links, "link_for", return_value=None):
            await first.handle_text(json.dumps({"t": "down", "id": 1, "d": "safe-device", "x": 0.1, "y": 0.1}))
            blocked = asyncio.create_task(second.handle_text(json.dumps(
                {"t": "down", "id": 2, "d": "safe-device", "x": 0.8, "y": 0.8})))
            await asyncio.sleep(0)
            self.assertFalse(blocked.done())
            await first.handle_text(json.dumps({"t": "up", "id": 1, "x": 0.1, "y": 0.1}))
            await asyncio.wait_for(blocked, 1)
            await second.handle_text(json.dumps({"t": "up", "id": 2, "x": 0.8, "y": 0.8}))
        self.assertEqual(len(session.writer.writes), 4)
        self.assertEqual([payload[1] for payload in session.writer.writes], [0, 1, 0, 1])

    async def test_reversed_multi_device_acquisition_terminates(self):
        a, b = SafetySession(serial="A"), SafetySession(serial="B")
        g1 = li.Gesture(1, "A", ["B"], [li.Target("A", a), li.Target("B", b)], [], 0.1, 0.1, 0.0)
        g2 = li.Gesture(2, "B", ["A"], [li.Target("B", b), li.Target("A", a)], [], 0.1, 0.1, 0.0)
        async def acquire_and_release(gesture):
            await gesture.acquire()
            await asyncio.sleep(0)
            await gesture.release()
        await asyncio.wait_for(asyncio.gather(acquire_and_release(g1), acquire_and_release(g2)), 1)

    async def test_distinct_device_writes_start_before_slow_drain(self):
        a, b = SlowDrainWriter(), SlowDrainWriter()
        sa, sb = SafetySession(a, "A"), SafetySession(b, "B")
        targets = [li.Target("A", sa), li.Target("B", sb)]
        task = asyncio.create_task(li.broadcast(targets, lambda _s: b"payload", phase="move"))
        await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait()), 1)
        self.assertEqual(len(a.writes), 1)
        self.assertEqual(len(b.writes), 1)
        a.release.set()
        b.release.set()
        await asyncio.wait_for(task, 1)

    async def test_expired_h264_lock_is_evicted_only_without_replacement_user(self):
        device = "safe-device"
        lock = asyncio.Lock()
        session = h264.Session(device)
        session.task = asyncio.create_task(asyncio.sleep(60))
        h264.SESSIONS[device] = session
        h264.LOCKS[device] = lock
        h264.LOCK_USERS[device] = 0
        with patch.object(h264.asyncio, "sleep", new=AsyncMock()):
            await h264.expire_session(device, session, lock)
        self.assertNotIn(device, h264.SESSIONS)
        self.assertNotIn(device, h264.LOCKS)
        session.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await session.task


if __name__ == "__main__":
    unittest.main()
