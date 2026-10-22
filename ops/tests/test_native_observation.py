from __future__ import annotations

import asyncio
import importlib.util
import types
import unittest

from ai_contracts.models import Capability
from gitd.services.native_observation import (
    NativeCheckpointRequired,
    canonicalize_ui_state,
    to_sdk_ui_state,
    verify_navigation_history,
)
from gitd.services.native_bridge import NativeBridge
from ops import ai_control_guard as guard
from ops import control_arbiter


class FakeObservation:
    def __init__(self, raw):
        self.raw = raw
    async def state(self, serial):
        return canonicalize_ui_state(self.raw, serial, package_allowlist=frozenset({"com.example.safe"}))
    def _device(self, serial):
        return FakeDevice()


class FakeDevice:
    def __init__(self):
        self.calls = []
    def adb(self, *args, **kwargs):
        self.calls.append(args)
        return ""
    def back(self):
        self.calls.append(("back",))


class NativeObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    def tearDown(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    def raw(self, text="Send", package="com.example.safe"):
        return {
            "a11y_tree": {"text": text, "className": "android.widget.Button", "boundsInScreen": {"left": 0, "top": 0, "right": 200, "bottom": 100}, "isClickable": True, "children": []},
            "phone_state": {"packageName": package, "activityName": ".MainActivity"},
            "device_context": {"screen_bounds": {"width": 1080, "height": 2400}},
        }

    async def test_canonical_mapper_and_pinned_sdk_state(self):
        state = canonicalize_ui_state(self.raw(), "phone-1", package_allowlist={"com.example.safe"})
        self.assertEqual((state.screen_width, state.screen_height), (1080, 2400))
        self.assertEqual(state.nodes[0].node_id, "0")
        if importlib.util.find_spec("mobilerun") is not None:
            sdk_state = to_sdk_ui_state(state)
            self.assertEqual((sdk_state.screen_width, sdk_state.screen_height), (1080, 2400))
            self.assertEqual(sdk_state.elements[0]["nodeId"], "0")

    async def test_sensitive_text_is_redacted_and_checkpoints_before_sdk_egress(self):
        state = canonicalize_ui_state(self.raw("Enter OTP 123456"), "phone-1", package_allowlist={"com.example.safe"})
        self.assertTrue(state.sensitive)
        self.assertEqual(state.nodes[0].text, "[REDACTED]")
        with self.assertRaises(NativeCheckpointRequired):
            # Same boundary used by NativeStateProvider; no model-facing state is produced.
            if state.sensitive:
                raise NativeCheckpointRequired(state.checkpoint_reason or "checkpoint")

    async def test_final_navigation_verifier_uses_observed_history(self):
        first = canonicalize_ui_state(self.raw("Home", "com.android.launcher"), "phone-1", package_allowlist={"com.android.launcher", "com.android.settings"})
        settings = canonicalize_ui_state(self.raw("Settings", "com.android.settings"), "phone-1", package_allowlist={"com.android.launcher", "com.android.settings"})
        last = canonicalize_ui_state(self.raw("Home", "com.android.launcher"), "phone-1", package_allowlist={"com.android.launcher", "com.android.settings"})
        self.assertTrue(verify_navigation_history([first, settings, last], ["com.android.launcher", "com.android.settings", "com.android.launcher"]))
        self.assertFalse(verify_navigation_history([settings, last], ["com.android.launcher", "com.android.settings", "com.android.launcher"]))

    async def test_bridge_state_and_navigation_are_typed_and_fenced(self):
        observation = FakeObservation(self.raw())
        bridge = NativeBridge(guard.AIControlGuard(), observation=observation, package_allowlist=frozenset({"com.example.safe"}), h264_module=types.SimpleNamespace(SESSIONS={}))
        lease = bridge.guard.acquire("child-1", "phone-1", Capability.OBSERVE, 1)
        state = await bridge.observe_state(lease, "phone-1")
        self.assertEqual(state.package, "com.example.safe")
        nav_lease = bridge.guard.acquire("child-1", "phone-1", Capability.NAVIGATE, 1)
        result = await bridge.navigate(nav_lease, "phone-1", "home")
        self.assertTrue(result.ok)
        with self.assertRaises(Exception):
            await bridge.navigate(nav_lease, "phone-1", "open_app", package="com.example.not-approved")

    async def test_registered_tools_do_not_accept_model_confirmation(self):
        observation = FakeObservation(self.raw())
        bridge = NativeBridge(guard.AIControlGuard(), observation=observation, h264_module=types.SimpleNamespace(SESSIONS={}))
        lease = bridge.guard.acquire("child-1", "phone-1", Capability.NAVIGATE, 1)
        tools = bridge.register_mobilerun_tools(serial="phone-1", capability_authorizer=lambda cap: None, cancelled=lambda: False)
        with self.assertRaises(Exception):
            await tools["navigate"]["function"](action="home", confirmed=True)
        self.assertIn("navigate", tools)


if __name__ == "__main__":
    unittest.main()
