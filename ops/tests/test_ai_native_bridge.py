"""Offline contract tests for the Mobilerun native bridge and AI ownership guard."""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from ai_contracts.models import Capability
from gitd.services.native_bridge import NativeBridge
from gitd.services.native_bridge_tools import NativeBridgeError
from ops import ai_control_guard as guard
from ops import control_arbiter


class FakeFleet:
    @staticmethod
    def _validate_action(action, params):
        if action == "tap" and set(params) == {"x", "y"}:
            return {"x": float(params["x"]), "y": float(params["y"])}
        if action == "key" and set(params) == {"key"}:
            return dict(params)
        if action == "text" and set(params) == {"text"}:
            return dict(params)
        raise ValueError("invalid action")

    async def connected_devices(self):
        return ["phone-1", "phone-2"]

    async def list_groups(self):
        return {"groups": []}


class FakeFast:
    def __init__(self):
        self.calls = []

    @staticmethod
    def keycode_for(value):
        return {"HOME": 3}.get(value)

    async def run_on_device(self, serial, gesture, owner_token=None):
        self.calls.append((serial, gesture, owner_token))
        return {"ok": True, "via": "fake"}


class BridgeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    async def asyncTearDown(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    def lease(self, g, cap, serial="phone-1", child="child-1", fence=1):
        return g.acquire(child, serial, cap, fence)

    async def test_manual_write_rejected_and_delegated_input_allowed(self):
        g = guard.AIControlGuard()
        fake_fast = FakeFast()
        bridge = NativeBridge(g, fleet_module=FakeFleet(), fast_module=fake_fast,
                              h264_module=types.SimpleNamespace(SESSIONS={}))
        lease = self.lease(g, Capability.INPUT)
        with self.assertRaises(control_arbiter.AIOwnershipError):
            async with control_arbiter.acquired("phone-1"):
                pass
        result = await bridge.input(lease, "phone-1", "tap", {"x": .2, "y": .3})
        self.assertTrue(result.ok)
        self.assertEqual(fake_fast.calls[0][0], "phone-1")
        self.assertIs(fake_fast.calls[0][2], lease.owner_token)

    async def test_target_drift_and_stale_fence_rejected(self):
        g = guard.AIControlGuard()
        lease = self.lease(g, Capability.INPUT)
        bridge = NativeBridge(g, fleet_module=FakeFleet(), fast_module=FakeFast())
        with self.assertRaises(NativeBridgeError):
            await bridge.input(lease, "phone-2", "tap", {"x": .1, "y": .1})
        with self.assertRaises(guard.AIControlDenied):
            g.require(lease, serial="phone-1", fence=2, capability=Capability.INPUT)

    async def test_stop_and_takeover_fence_old_worker(self):
        g = guard.AIControlGuard()
        lease = self.lease(g, Capability.INPUT)
        g.stop("child-1")
        with self.assertRaises(guard.AIControlDenied):
            g.require(lease, capability=Capability.INPUT)
        # A fresh child can take the device only after the owner explicitly
        # releases/takes over the old device.
        g.takeover("phone-1")
        fresh = g.acquire("child-2", "phone-1", Capability.INPUT, 2)
        self.assertEqual(fresh.child_id, "child-2")

    async def test_twenty_device_isolation(self):
        g = guard.AIControlGuard()
        leases = [self.lease(g, Capability.INPUT, f"phone-{i}", f"child-{i}", 1) for i in range(20)]
        self.assertEqual(len({id(item.owner_token) for item in leases}), 20)
        with self.assertRaises(control_arbiter.AIOwnershipError):
            control_arbiter.assert_write_allowed("phone-7", None)
        control_arbiter.assert_write_allowed("phone-19", leases[19].owner_token)

    async def test_ambiguous_writer_is_not_replayed(self):
        class Writer:
            def __init__(self): self.writes = 0
            def is_closing(self): return False
            def write(self, payload): self.writes += 1
            async def drain(self): raise OSError("drain unavailable")

        writer = Writer()
        with self.assertRaises(control_arbiter.AmbiguousWrite):
            await control_arbiter.write("phone-1", writer, b"one")
        self.assertEqual(writer.writes, 1)

    async def test_capability_accumulation_requires_same_fence(self):
        g = guard.AIControlGuard()
        first = self.lease(g, Capability.INPUT)
        second = g.acquire("child-1", "phone-1", Capability.OBSERVE, 1)
        self.assertIs(first.owner_token, second.owner_token)
        self.assertIn(Capability.OBSERVE, second.capabilities)
        with self.assertRaises(guard.AILeaseConflict):
            g.acquire("child-1", "phone-1", Capability.APP_STATE, 2)


if __name__ == "__main__":
    unittest.main()
