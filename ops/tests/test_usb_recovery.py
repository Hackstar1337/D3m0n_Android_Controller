import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops import usb_recovery as ur

HW = "ce061716abcd1234"
PEER = "192.168.50.57:5555"


def done(stdout="", code=0):
    return subprocess.CompletedProcess(["adb"], code, stdout, "")


class FakeAdb:
    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(tuple(args))
        value = self.responses.get(tuple(args), done())
        if isinstance(value, BaseException):
            raise value
        return value

    def called(self, *args):
        return tuple(args) in self.calls


class UsbRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "aliases.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_devices(self):
        out = (
            "List of devices attached\n"
            f"{PEER}      device product:x model:SM_G9500 transport_id:1\n"
            f"{HW}       device usb:1-1 product:x model:SM_G9500\n"
            "R58M123456     unauthorized usb:1-2 transport_id:3\n"
            "emulator-5554  device\n\n"
        )
        devices = ur.parse_devices(out)
        self.assertEqual([d["serial"] for d in devices], [PEER, HW, "R58M123456", "emulator-5554"])
        self.assertEqual([d["usb"] for d in devices], [False, True, True, False])
        self.assertEqual(devices[2]["state"], "unauthorized")

    def test_alias_file_is_private_and_filter_uses_it(self):
        self.assertFalse(ur.is_fleet_alias(HW, self.path))
        self.assertTrue(ur.remember(PEER, HW, self.path))
        self.assertTrue(ur.is_fleet_alias(HW, self.path))
        self.assertFalse(ur.is_fleet_alias(PEER, self.path))
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        # One hardware serial per peer: a swapped handset replaces the old mapping.
        ur.remember(PEER, "ff00aa11bb22", self.path)
        self.assertEqual(ur.load_aliases(self.path), {"ff00aa11bb22": PEER})

    def test_corrupt_or_invalid_alias_file_is_ignored(self):
        self.path.write_text("{not json")
        self.assertEqual(ur.load_aliases(self.path), {})
        self.path.write_text(json.dumps({"bad serial!": {"peer": PEER}, HW: {"peer": "8.8.8.8:5555"}}))
        self.assertEqual(ur.load_aliases(self.path), {})

    def test_refresh_learns_only_unmapped_online_wifi_phones(self):
        ur.remember("192.168.50.37:5555", "aaaa1111", self.path)
        devices = [
            {"serial": "192.168.50.37:5555", "state": "device", "usb": False},
            {"serial": PEER, "state": "device", "usb": False},
            {"serial": "192.168.50.38:5555", "state": "offline", "usb": False},
            {"serial": HW, "state": "device", "usb": True},
        ]
        adb = FakeAdb({("-s", PEER, "shell", "getprop", "ro.serialno"): done(HW + "\n")})
        self.assertEqual(ur.refresh_aliases(devices, adb, self.path), 1)
        self.assertEqual(adb.calls, [("-s", PEER, "shell", "getprop", "ro.serialno")])
        self.assertEqual(ur.load_aliases(self.path)[HW], PEER)

    def _recovery_adb(self, port="", ip="192.168.50.57"):
        target = ip + ":5555"
        return FakeAdb({
            ("-s", HW, "shell", "getprop", "service.adb.tcp.port"): done(port + "\n"),
            ("-s", HW, "shell", "ip", "-4", "addr", "show", "wlan0"): done(f"3: wlan0: <UP>\n    inet {ip}/24 brd 192.168.50.255 scope global wlan0\n"),
            ("-s", target, "get-state"): done("device\n"),
        })

    def test_known_usb_phone_is_switched_back_and_reconnected(self):
        ur.remember(PEER, HW, self.path)
        adb = self._recovery_adb()
        results = ur.recover_usb([{"serial": HW, "state": "device", "usb": True}], adb, self.path, sleep=lambda s: None)
        self.assertEqual(results, [{"peer": PEER, "state": "usb_recovered", "switched": True}])
        self.assertTrue(adb.called("-s", HW, "tcpip", "5555"))
        self.assertTrue(adb.called("connect", PEER))
        self.assertFalse(any(c[:1] == ("kill-server",) for c in adb.calls))

    def test_port_already_tcp_skips_adbd_restart(self):
        ur.remember(PEER, HW, self.path)
        adb = self._recovery_adb(port="5555")
        results = ur.recover_usb([{"serial": HW, "state": "device", "usb": True}], adb, self.path, sleep=lambda s: None)
        self.assertEqual(results[0]["state"], "usb_recovered")
        self.assertFalse(results[0]["switched"])
        self.assertFalse(adb.called("-s", HW, "tcpip", "5555"))

    def test_phone_already_online_by_wifi_is_never_touched(self):
        ur.remember(PEER, HW, self.path)
        adb = FakeAdb()
        devices = [{"serial": PEER, "state": "device", "usb": False}, {"serial": HW, "state": "device", "usb": True}]
        self.assertEqual(ur.recover_usb(devices, adb, self.path), [{"peer": PEER, "state": "usb_alias_online"}])
        self.assertEqual(adb.calls, [])

    def test_unknown_and_unauthorized_usb_phones_are_only_reported(self):
        ur.remember(PEER, HW, self.path)
        adb = FakeAdb()
        devices = [{"serial": "R58M999", "state": "device", "usb": True}, {"serial": HW, "state": "unauthorized", "usb": True}]
        self.assertEqual(ur.recover_usb(devices, adb, self.path), [{"peer": None, "state": "usb_unknown"}, {"peer": PEER, "state": "usb_unauthorized"}])
        self.assertEqual(adb.calls, [])

    def test_waiting_for_wifi_and_timeouts_retry_next_pass(self):
        ur.remember(PEER, HW, self.path)
        adb = FakeAdb({("-s", HW, "shell", "getprop", "service.adb.tcp.port"): done("5555\n")})
        self.assertEqual(ur.recover_usb([{"serial": HW, "state": "device", "usb": True}], adb, self.path)[0]["state"], "usb_waiting_wifi")
        slow = FakeAdb({("-s", HW, "shell", "getprop", "service.adb.tcp.port"): subprocess.TimeoutExpired("adb", 4)})
        self.assertEqual(ur.recover_usb([{"serial": HW, "state": "device", "usb": True}], slow, self.path)[0]["state"], "usb_retry_later")

    def test_changed_wifi_address_is_reported_and_remembered(self):
        ur.remember(PEER, HW, self.path)
        adb = self._recovery_adb(port="5555", ip="192.168.50.90")
        item = ur.recover_usb([{"serial": HW, "state": "device", "usb": True}], adb, self.path, sleep=lambda s: None)[0]
        self.assertEqual(item["new_peer"], "192.168.50.90:5555")
        self.assertEqual(ur.load_aliases(self.path)[HW], "192.168.50.90:5555")

    def test_recoveries_per_pass_are_capped(self):
        devices = []
        for i in range(6):
            hw = f"aaaa{i}000"
            ur.remember(f"192.168.50.{60 + i}:5555", hw, self.path)
            devices.append({"serial": hw, "state": "device", "usb": True})
        results = ur.recover_usb(devices, FakeAdb(), self.path, sleep=lambda s: None, limit=4)
        self.assertEqual(sum(r["state"] == "usb_retry_later" for r in results), 2)

    def test_cycle_result_never_contains_hardware_serials(self):
        ur.remember(PEER, HW, self.path)
        adb = self._recovery_adb()
        adb.responses[("devices", "-l")] = done(f"List of devices attached\n{HW} device usb:1-1\nR58M999 device usb:1-2\n")
        result = ur.cycle(adb, self.path, sleep=lambda s: None)
        self.assertEqual(result["usb_counts"], {"usb_recovered": 1, "usb_unknown": 1})
        text = json.dumps(result)
        self.assertNotIn(HW, text)
        self.assertNotIn("R58M999", text)


if __name__ == "__main__":
    unittest.main()
