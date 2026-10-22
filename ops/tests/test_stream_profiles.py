"""Offline tests for fleet-scale stream profiles and the scrcpy control channel."""
from __future__ import annotations

import asyncio
import json
import struct
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import ops.android_h264 as h264


class FakeWriter:
    def __init__(self):
        self.data = bytearray(); self.closed = False
    def write(self, payload):
        self.data.extend(payload)
    async def drain(self):
        return None
    def is_closing(self):
        return self.closed
    def close(self):
        self.closed = True
    async def wait_closed(self):
        return None


class ControlEncodingTests(unittest.TestCase):
    def test_touch_message_matches_scrcpy_v5_layout(self):
        payload = h264.touch_message(h264.ACTION_DOWN, 100, 200, 480, 854, 1.0)
        self.assertEqual(len(payload), 32)
        fields = struct.unpack("!BBqiiHHHii", payload)
        self.assertEqual(fields, (2, 0, -2, 100, 200, 480, 854, 0xFFFF, 0, 0))
        self.assertEqual(struct.unpack("!BBqiiHHHii", h264.touch_message(h264.ACTION_UP, 1, 2, 3, 4, 0.0))[7], 0)

    def test_key_message_matches_scrcpy_v5_layout(self):
        payload = h264.key_message(h264.ACTION_UP, 4)
        self.assertEqual(len(payload), 14)
        self.assertEqual(struct.unpack("!BBiii", payload), (0, 1, 4, 0, 0))

    def test_commands_stay_samsung_safe_for_every_profile(self):
        for control in (True, False):
            for profile, values in h264.PROFILES.items():
                s = h264.Session("192.168.100.100:55555")
                s.profile = profile; s.control_enabled = control
                command = s.command()
                self.assertLess(len(" ".join(command[5:]).encode()), 255)
                self.assertIn(f"max_size={values['max_size']}", command)
                self.assertIn(f"max_fps={values['fps']}", command)
                self.assertIn(f"video_bit_rate={values['bit_rate']}", command)
                self.assertEqual("control=false" in command, not control)
                self.assertFalse(any("mjpeg" in item.lower() for item in command))

    def test_default_profile_is_the_historical_hd_stream(self):
        self.assertEqual(h264.DEFAULT_PROFILE, "hd")
        self.assertEqual((h264.MAX_SIZE, h264.FPS, h264.BIT_RATE), (720, 24, 2000000))
        self.assertLess(h264.PROFILES["grid"]["bit_rate"], h264.PROFILES["hd"]["bit_rate"])

    def test_control_disable_window(self):
        with patch.dict(h264.CONTROL_DISABLED, {"dev": time.time() + 60}, clear=True):
            self.assertFalse(h264.control_allowed("dev"))
            self.assertTrue(h264.control_allowed("other"))
        with patch.dict(h264.CONTROL_DISABLED, {"dev": time.time() - 1}, clear=True):
            self.assertTrue(h264.control_allowed("dev"))


class ProfileAndViewerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = h264.Session("dev-1")

    async def asyncTearDown(self):
        for task in (self.s.task, self.s.profile_task):
            if task is not None and not task.done():
                task.cancel()
                with self._suppress():
                    await task

    class _suppress:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return exc[0] is not None and issubclass(exc[0], (asyncio.CancelledError, Exception))

    async def test_highest_requested_profile_wins(self):
        q0 = self.s.subscribe("wall")
        self.assertEqual(self.s.desired_profile(), "wall")
        q1 = self.s.subscribe("grid")
        self.assertEqual(self.s.desired_profile(), "grid")
        q2 = self.s.subscribe()
        self.assertEqual(self.s.client_profiles[q2], "hd")
        self.assertEqual(self.s.desired_profile(), "hd")
        self.s.unsubscribe(q2)
        self.assertEqual(self.s.desired_profile(), "grid")
        self.s.unsubscribe(q1); self.s.unsubscribe(q0)
        self.assertEqual(self.s.desired_profile(), self.s.profile)
        self.assertEqual(self.s.subscribe("bogus") and self.s.desired_profile(), "hd")

    def test_profile_keyframe_intervals(self):
        self.s.profile = "hd"
        self.assertIn("video_codec_options=i-frame-interval=1,repeat-previous-frame-after:long=200000", self.s.command())
        self.s.profile = "wall"
        self.assertIn("video_codec_options=i-frame-interval=2,repeat-previous-frame-after:long=200000", self.s.command())

    async def test_profile_change_restarts_encoder_in_place(self):
        self.s.task = asyncio.create_task(asyncio.sleep(30))
        self.s.writer = FakeWriter()
        with patch.object(h264, "PROFILE_DOWNGRADE_DELAY", 0.01), patch.object(h264, "PROFILE_MIN_RESTART_GAP", 0):
            q = self.s.subscribe("hd")
            self.assertIsNone(self.s.profile_task)  # already hd
            self.s.set_client_profile(q, "grid")
            await asyncio.sleep(0.05)
            self.assertTrue(self.s.restart_requested)
            self.assertTrue(self.s.writer.closed)
            # A quick return to hd before the downgrade fires cancels it.
            self.s.restart_requested = False; self.s.writer = FakeWriter()
            self.s.set_client_profile(q, "hd"); self.s.set_client_profile(q, "grid"); self.s.set_client_profile(q, "hd")
            await asyncio.sleep(0.05)
            self.assertFalse(self.s.restart_requested)

    async def test_run_restarts_and_keeps_viewers(self):
        calls = []
        async def encode_once():
            calls.append(self.s.profile)
            if len(calls) == 1:
                self.s.client_profiles[q] = "grid"
                return "restart"
            raise RuntimeError("stop")
        q = self.s.subscribe("hd")
        self.s.clients[q] = False
        with patch.object(self.s, "_encode_once", encode_once), patch.object(h264, "adb", AsyncMock(return_value="")), \
             patch.object(h264, "keep_awake_offset", return_value=0):
            await self.s.run()
        self.assertEqual(calls, ["hd", "grid"])
        self.assertEqual(self.s.restarts, 1)
        self.assertEqual(self.s.error, "stop")
        self.assertIn(q, self.s.clients)  # viewer stayed subscribed through the restart
        self.assertEqual(q.get_nowait()["status"], "encoder error")

    async def test_key_only_viewers_receive_keyframes_only(self):
        full = self.s.subscribe(); hidden = self.s.subscribe("grid")
        with patch.object(h264, "KEY_ONLY_INTERVAL", 0.0):
            self.s.set_key_only(hidden, True)
            self.s.broadcast(b"K1", key=True)
            self.s.broadcast(b"D1")
            self.assertEqual([full.get_nowait(), full.get_nowait()], [b"K1", b"D1"])
            self.assertEqual(hidden.get_nowait(), b"K1")
            self.assertTrue(hidden.empty())
        # Hidden viewers are throttled to one refresh keyframe per interval.
        with patch.object(h264, "KEY_ONLY_INTERVAL", 60.0):
            self.s.broadcast(b"K1b", key=True)
            self.assertEqual(full.get_nowait(), b"K1b")
            self.assertTrue(hidden.empty())
        # Becoming visible shows the newest keyframe now; deltas wait for the next keyframe.
        self.s.set_key_only(hidden, False)
        self.assertEqual(hidden.get_nowait(), b"K1b")
        self.s.broadcast(b"D2")
        self.assertTrue(hidden.empty())
        self.s.broadcast(b"K2", key=True); self.s.broadcast(b"D3")
        self.assertEqual([hidden.get_nowait(), hidden.get_nowait()], [b"K2", b"D3"])
        self.s.unsubscribe(hidden)
        self.assertNotIn(hidden, self.s.key_sent)

    async def test_viewer_messages_are_bounded_and_validated(self):
        q = self.s.subscribe()
        h264.viewer_message(self.s, q, json.dumps({"profile": "grid", "mode": "key"}))
        self.assertEqual(self.s.client_profiles[q], "grid")
        self.assertIn(q, self.s.key_only)
        for text in ("not json", "[]", json.dumps({"profile": "4k"}), json.dumps({"mode": "x"}), "{" + " " * 300 + "}"):
            h264.viewer_message(self.s, q, text)
        self.assertEqual(self.s.client_profiles[q], "grid")
        h264.viewer_message(self.s, q, json.dumps({"mode": "full"}))
        self.assertNotIn(q, self.s.key_only)

    async def test_control_injection_maps_normalized_points(self):
        self.assertFalse(self.s.control_ready())
        with self.assertRaises(h264.ControlUnavailable):
            await self.s.inject_touch(h264.ACTION_DOWN, 0.5, 0.5)
        self.s.task = asyncio.create_task(asyncio.sleep(30))
        self.s.width, self.s.height = 480, 854
        writer = FakeWriter(); self.s.control_writer = writer
        self.assertTrue(self.s.control_ready())
        await self.s.inject_touch(h264.ACTION_DOWN, 1.0, 0.0)
        await self.s.inject_touch(h264.ACTION_UP, 2.0, -1.0, 0.0)
        fields = struct.unpack("!BBqiiHHHii", bytes(writer.data[:32]))
        self.assertEqual(fields[3:7], (479, 0, 480, 854))
        clamped = struct.unpack("!BBqiiHHHii", bytes(writer.data[32:64]))
        self.assertEqual(clamped[3:5], (479, 0))
        await self.s.inject_key(4)
        self.assertEqual(len(writer.data), 64 + 28)
        self.assertEqual(self.s.control_events, 4)
        self.s.restart_requested = True
        self.assertFalse(self.s.control_ready())

    async def test_status_reports_profile_and_control(self):
        q = self.s.subscribe("grid"); self.s.set_key_only(q, True)
        status = self.s.status()
        self.assertEqual(status["viewer_profiles"], {"hd": 0, "grid": 1, "wall": 0})
        self.assertEqual(status["key_only_clients"], 1)
        self.assertEqual(status["max_size"], h264.PROFILES[status["profile"]]["max_size"])
        for key in ("control", "control_enabled", "control_error", "restarts", "control_events"):
            self.assertIn(key, status)
        settings = (await h264.stream_status())["settings"]
        self.assertEqual(settings["default_profile"], "hd")
        self.assertIn("grid", settings["profiles"])


if __name__ == "__main__":
    unittest.main()
