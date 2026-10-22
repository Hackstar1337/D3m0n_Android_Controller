from __future__ import annotations

import asyncio
import unittest

from gitd.services.native_bridge import NativeBridge, NativeInputRejected
from gitd.services.fleet_sdk_broker import _rejected_result


class _Obs:
    def __init__(self, width=1080, height=2220, fail=False):
        self.width, self.height, self.fail = width, height, fail

    async def raw_state(self, serial):
        if self.fail:
            raise RuntimeError("adb unavailable")
        return {"device_context": {"screen_bounds": {"width": self.width, "height": self.height}}}


def _bridge(obs):
    bridge = NativeBridge.__new__(NativeBridge)
    bridge.observation = obs
    return bridge


class InputAdaptationTests(unittest.TestCase):
    def test_pixel_tap_uses_real_screen_bounds(self):
        out = asyncio.run(_bridge(_Obs())._pixel_to_normalized("s", "tap", {"x": 540, "y": 1110}))
        self.assertAlmostEqual(out["x"], 540 / 1079)
        self.assertAlmostEqual(out["y"], 1110 / 2219)

    def test_normalized_values_are_not_rewritten(self):
        self.assertIsNone(asyncio.run(_bridge(_Obs())._pixel_to_normalized("s", "tap", {"x": 0.5, "y": 0.4})))

    def test_never_guesses_when_bounds_unavailable_or_out_of_screen(self):
        self.assertIsNone(asyncio.run(_bridge(_Obs(fail=True))._pixel_to_normalized("s", "tap", {"x": 540, "y": 900})))
        self.assertIsNone(asyncio.run(_bridge(_Obs())._pixel_to_normalized("s", "tap", {"x": 5000, "y": 900})))
        self.assertIsNone(asyncio.run(_bridge(_Obs())._pixel_to_normalized("s", "key", {"key": "HOME"})))

    def test_rejection_result_is_bounded_and_correctable(self):
        out = _rejected_result(NativeInputRejected("tap requires only x and y" + "\x00" * 3))
        self.assertFalse(out["ok"])
        self.assertTrue(out["rejected_before_device_action"])
        self.assertNotIn("\x00", out["error"])
        self.assertIn("normalized", out["error"])

    def test_transient_observation_is_retried_then_bounded(self):
        from gitd.services.fleet_sdk_broker import _is_provider_timeout

        class ReadTimeout(Exception):
            pass
        try:
            try:
                raise ReadTimeout("slow")
            except ReadTimeout as inner:
                raise RuntimeError("gateway") from inner
        except RuntimeError as wrapped:
            self.assertTrue(_is_provider_timeout(wrapped))
        self.assertFalse(_is_provider_timeout(ValueError("bad output")))
        self.assertTrue(_is_provider_timeout(TimeoutError()))
        from gitd.services.cloud_gateway import GatewayResponseError
        self.assertTrue(_is_provider_timeout(GatewayResponseError("Provider request was rejected")))

    def test_transient_observation_retry_bounds(self):
        from gitd.services.native_bridge import NativeBridgeError

        class Flaky:
            def __init__(self, fails):
                self.fails, self.calls = fails, 0

            async def state(self, serial):
                self.calls += 1
                if self.calls <= self.fails:
                    raise RuntimeError("Foreground app unavailable")
                return "state"

        bridge = _bridge(Flaky(2)); bridge._read_ctx = lambda *a, **k: None
        self.assertEqual(asyncio.run(bridge.observe_state(None, "s")), "state")
        self.assertEqual(bridge.observation.calls, 3)
        bridge = _bridge(Flaky(5)); bridge._read_ctx = lambda *a, **k: None
        with self.assertRaises(NativeBridgeError):
            asyncio.run(bridge.observe_state(None, "s"))
        self.assertEqual(bridge.observation.calls, 3)


if __name__ == "__main__":
    unittest.main()
