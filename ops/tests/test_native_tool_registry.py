"""Isolated native tool registry mapping and checkpoint tests."""
from __future__ import annotations

import types
import unittest

from ai_contracts.models import Capability
from gitd.services.native_bridge import NativeBridge
from gitd.services.native_bridge_tools import NativeBridgeError, NativeConfirmationRequired
from gitd.services.native_observation import NativeCheckpointRequired
from ops import ai_control_guard as guard
from ops import control_arbiter


class FakeFleet:
    @staticmethod
    async def connected_devices():
        return ["phone-1"]

    @staticmethod
    async def list_groups():
        return {"groups": [{"id": "g1"}]}


class FakeStore:
    @staticmethod
    def get_public(serial):
        return {"serial": serial, "applied_mode": "normal"}


class FakeProxy:
    store = FakeStore()

    @staticmethod
    async def make_driver():
        return object()

    @staticmethod
    async def _run_one(job, serial, driver):
        return {"ok": True, "status": "tested", "verified_device": True}


class FakeSession:
    @staticmethod
    def status():
        return {"running": True, "control": True, "transport": "fake"}


class FakeReboot:
    class usb_recovery:
        @staticmethod
        def usb_states_by_peer(devices):
            return {"phone-1": "usb_recovered"}

    @staticmethod
    async def _preflight(serials):
        return {serial: {"online": True, "agent_busy": False, "rebooting": False} for serial in serials}

    @staticmethod
    async def inventory():
        return [{"serial": "phone-1", "state": "device"}]


class FakeSync:
    @staticmethod
    def links_summary():
        return {"links": {"phone-1": {"ready": True, "state": "ready"}}}


class NativeToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    async def asyncTearDown(self):
        guard.reset_for_tests()
        control_arbiter.reset_for_tests()

    def _bridge(self, g):
        return NativeBridge(
            g,
            fleet_module=FakeFleet(),
            reboot_module=FakeReboot(),
            proxy_module=FakeProxy(),
            sync_module=FakeSync(),
            h264_module=types.SimpleNamespace(SESSIONS={"phone-1": FakeSession()}),
            observation=types.SimpleNamespace(),
        )

    def _authorize_all(self, g):
        capabilities = (
            Capability.INVENTORY, Capability.GROUP_READ, Capability.PROXY_READ,
            Capability.PROXY_TEST, Capability.REBOOT_PREFLIGHT, Capability.RECOVERY_STATUS,
            Capability.SYNC_STATUS, Capability.STREAM_STATUS,
        )
        for capability in capabilities:
            g.acquire("child-1", "phone-1", capability, 1)

    async def test_registry_maps_concrete_read_only_native_methods(self):
        g = guard.AIControlGuard()
        self._authorize_all(g)
        bridge = self._bridge(g)
        specs = bridge.register_mobilerun_tools(
            serial="phone-1", capability_authorizer=lambda capability: None, cancelled=lambda: False,
            allowed_serials={"phone-1"},
        )
        expected = {
            "inventory": Capability.INVENTORY.value,
            "group_read": Capability.GROUP_READ.value,
            "proxy_read": Capability.PROXY_READ.value,
            "proxy_test": Capability.PROXY_TEST.value,
            "reboot_preflight": Capability.REBOOT_PREFLIGHT.value,
            "recovery_status": Capability.RECOVERY_STATUS.value,
            "sync_status": Capability.SYNC_STATUS.value,
            "stream_status": Capability.STREAM_STATUS.value,
        }
        for name, capability in expected.items():
            self.assertIn(name, specs)
            self.assertEqual(specs[name]["capability"], capability)
            self.assertFalse(specs[name]["mutation"])
            self.assertFalse(specs[name]["parameters"].get("additionalProperties", True))
            self.assertTrue(callable(specs[name]["function"]))
        self.assertTrue((await specs["inventory"]["function"]())["ok"])
        self.assertTrue((await specs["group_read"]["function"]())["ok"])
        self.assertTrue((await specs["proxy_read"]["function"]())["ok"])
        self.assertTrue((await specs["proxy_test"]["function"]())["ok"])
        self.assertTrue((await specs["reboot_preflight"]["function"]())["ok"])
        self.assertTrue((await specs["recovery_status"]["function"]())["ok"])
        self.assertTrue((await specs["sync_status"]["function"]())["ok"])
        self.assertTrue((await specs["stream_status"]["function"]())["ok"])

    async def test_mutations_are_closed_and_checkpointed_without_private_grant(self):
        g = guard.AIControlGuard()
        g.acquire("child-1", "phone-1", Capability.GROUP_WRITE, 1)
        bridge = self._bridge(g)
        specs = bridge.register_mobilerun_tools(
            serial="phone-1", capability_authorizer=lambda capability: None, cancelled=lambda: False,
            allowed_serials={"phone-1"},
        )
        self.assertTrue(specs["group_write"]["mutation"])
        self.assertTrue(specs["file_transfer"]["mutation"])
        self.assertTrue(specs["apk_install"]["mutation"])
        self.assertTrue(specs["reboot"]["mutation"])
        for name in ("group_write", "file_transfer", "apk_install", "reboot"):
            self.assertFalse(specs[name]["parameters"].get("additionalProperties", True))
        with self.assertRaises(NativeConfirmationRequired):
            await specs["group_write"]["function"](operation="create", name="safe", devices=["phone-1"])
        with self.assertRaises(NativeConfirmationRequired):
            await specs["reboot"]["function"]()
        with self.assertRaises(NativeBridgeError):
            await specs["group_write"]["function"](operation="create", devices=["phone-1"], confirmed=True)

    async def test_group_write_defaults_fail_closed_and_rejects_out_of_scope_members(self):
        g = guard.AIControlGuard()
        g.acquire("child-1", "phone-1", Capability.GROUP_WRITE, 1)
        bridge = self._bridge(g)
        no_scope = bridge.register_mobilerun_tools(
            serial="phone-1", capability_authorizer=lambda capability: None, cancelled=lambda: False,
        )
        with self.assertRaises(NativeCheckpointRequired):
            await no_scope["group_write"]["function"](operation="delete", group_id="g1")

        scoped = bridge.register_mobilerun_tools(
            serial="phone-1", capability_authorizer=lambda capability: None, cancelled=lambda: False,
            allowed_serials={"phone-1"}, confirmation_grant=lambda capability, operation: True,
        )
        with self.assertRaises(NativeCheckpointRequired):
            await scoped["group_write"]["function"](operation="create", name="unsafe", devices=["phone-2"])


if __name__ == "__main__":
    unittest.main()
