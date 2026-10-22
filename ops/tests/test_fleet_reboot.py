import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

from fastapi import HTTPException

from ops import fleet_control, fleet_reboot as fr, usb_recovery as ur

P1, P2, P3 = "192.168.50.57:5555", "192.168.50.56:5555", "192.168.50.55:5555"
HW1 = "ce061716abcd1234"


def dev(serial, state="device"):
    return {"serial": serial, "state": state, "usb": ur.is_usb_serial(serial)}


class RebootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"D3M0N_STATE_DIR": self.tmp.name})
        self.env.start()
        fr._records.clear()
        self.inventory = [dev(P1), dev(P2), dev(P3, "offline")]
        self.rebooted = []
        self.shell = {}

        async def inventory():
            return list(self.inventory)

        async def adb_shell(serial, command, timeout=5):
            if command == "getprop ro.serialno":
                return {P1: HW1}.get(serial, "") + "\n"
            value = self.shell.get(serial, "1 10.0")
            if isinstance(value, BaseException):
                raise value
            return value

        async def send_reboot(serial):
            self.rebooted.append(serial)

        async def no_monitor():
            return None

        self.patches = [
            mock.patch.object(fr, "inventory", inventory),
            mock.patch.object(fr, "adb_shell", adb_shell),
            mock.patch.object(fr, "send_reboot", send_reboot),
            mock.patch.object(fr, "_ensure_monitor", no_monitor),
            mock.patch.object(fr, "agent_busy_devices", lambda: {P2}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        fr._records.clear()
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_requires_explicit_confirmation(self):
        with self.assertRaises(HTTPException) as ctx:
            self.run_async(fr.reboot_devices(fr.RebootBody(devices=[P1])))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(self.rebooted, [])

    def test_validation_rejects_empty_bad_and_too_many(self):
        for devices in ([], ["bad serial!"], [f"192.168.2.{i}:5555" for i in range(101)]):
            with self.assertRaises(HTTPException):
                fr._validate(devices)

    def test_skips_busy_offline_and_already_rebooting(self):
        result = self.run_async(fr.reboot_devices(fr.RebootBody(devices=[P1, P2, P3], confirm=True)))
        self.assertEqual(result["started"], [P1])
        self.assertEqual({s["device"]: s["reason"] for s in result["skipped"]}, {P2: "agent_running", P3: "offline"})
        self.assertEqual(self.rebooted, [P1])
        self.assertEqual(result["without_usb"], [P1])
        # The hardware serial is learned before the reboot for later USB recovery.
        self.assertEqual(ur.load_aliases(), {HW1: P1})
        again = self.run_async(fr.reboot_devices(fr.RebootBody(devices=[P1], confirm=True)))
        self.assertEqual(again["skipped"], [{"device": P1, "reason": "already_rebooting"}])
        self.assertEqual(self.rebooted, [P1])

    def test_preflight_reports_usb_attachment(self):
        ur.remember(P1, HW1)
        self.inventory.append(dev(HW1))
        result = self.run_async(fr.reboot_preflight(fr.DevicesBody(devices=[P1, P2])))["devices"]
        self.assertEqual(result[P1], {"online": True, "usb_connected": True, "agent_busy": False, "rebooting": False})
        self.assertFalse(result[P2]["usb_connected"])
        self.assertTrue(result[P2]["agent_busy"])
        reboot = self.run_async(fr.reboot_devices(fr.RebootBody(devices=[P1], confirm=True)))
        self.assertEqual(reboot["without_usb"], [])

    def test_failed_reboot_command_is_reported(self):
        async def failing(serial):
            raise fleet_control.AdbCommandError("device offline")

        with mock.patch.object(fr, "send_reboot", failing):
            result = self.run_async(fr.reboot_devices(fr.RebootBody(devices=[P1], confirm=True)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["started"], [])
        self.assertEqual(fr._records[P1]["state"], "failed")

    def _record(self, serial, age, usb=False):
        fr._records[serial] = {"state": "rebooting", "started_at": time.time() - age, "usb_connected": usb, "message": ""}

    def test_back_online_needs_fresh_uptime_and_boot_completed(self):
        self._record(P1, 40)
        self.shell[P1] = "0 12.0"
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "rebooting")
        self.shell[P1] = "1 14.0"
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "online")
        self.assertEqual(fr._records[P1]["back_after_s"], 40)

    def test_lingering_old_transport_is_not_mistaken_for_back(self):
        self._record(P1, 5)
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "rebooting")  # inside the grace period
        self._record(P1, 100)
        self.shell[P1] = "1 99999.0"
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "failed")
        self.assertIn("did not restart", fr._records[P1]["message"])

    def test_offline_without_usb_becomes_needs_usb_then_recovering_on_usb(self):
        self.inventory = [dev(P2)]
        self._record(P1, 200)
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "needs_usb")
        ur.remember(P1, HW1)
        self.inventory.append(dev(HW1, "unauthorized"))
        self.run_async(fr.monitor_once())
        self.assertEqual(fr._records[P1]["state"], "recovering")
        self.assertIn("Allow USB debugging", fr._records[P1]["message"])
        self.assertTrue(fr._records[P1]["usb_connected"])

    def test_status_is_public_metadata_only(self):
        ur.remember(P1, HW1)
        self._record(P1, 3)
        status = self.run_async(fr.reboot_status())
        self.assertEqual(status["active"], 1)
        self.assertEqual(set(status["devices"][P1]), {"state", "message", "started_at", "elapsed_s", "usb_connected"})
        self.assertNotIn(HW1, repr(status))

    def test_connected_devices_hides_usb_twin_of_known_phone(self):
        ur.remember(P1, HW1)

        async def run_adb(*args, timeout=0):
            return f"List of devices attached\n{P1}\tdevice\n{HW1}\tdevice\nR58NEW\tdevice\n"

        with mock.patch.object(fleet_control, "run_adb", run_adb):
            self.assertEqual(self.run_async(fleet_control.connected_devices()), [P1, "R58NEW"])


if __name__ == "__main__":
    unittest.main()
