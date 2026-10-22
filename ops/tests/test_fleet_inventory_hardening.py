"""Focused offline regressions for fleet reconnect and inventory identity safety."""
import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


def load_wireless_reconnect():
    from ops import wireless_reconnect

    return wireless_reconnect


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def mappings(self):
        return self

    def all(self):
        return []


class _InventoryDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((str(query), params))
        if "SELECT wifi_ip" in str(query):
            return _Rows(self.rows)
        return _Rows()

    def commit(self):
        return None


class _AdbOutput:
    def __init__(self, output):
        self.output = output

    def check_output(self, args, timeout):
        return self.output.encode()


def _extract_function(name):
    source = (ROOT / "gitd" / "routers" / "phone.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    return function


class FleetInventoryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.reconnect = load_wireless_reconnect()
        self.reconnect.PENDING.clear()
        self.reconnect.LAST_ATTEMPT.clear()
        self.reconnect.ADDRESS_LOCKS.clear()

    def test_canonical_private_ipv4_validator_rejects_unsafe_or_malformed_peers(self):
        accepted = "192.168.50.37:5555"
        rejected = (
            "8.8.8.8:5555",       # public
            "127.0.0.1:5555",     # loopback
            "169.254.1.2:5555",   # link-local
            "0.0.0.0:5555",       # unspecified
            "224.0.0.1:5555",     # multicast
            "172.15.0.1:5555",    # outside RFC1918
            "192.168.50.37:0",     # invalid port
            "192.168.50.37:65536",
            "192.168.50.37:5555; id",
            "192.168.001.37:5555",
        )
        self.assertTrue(self.reconnect.valid_address(accepted))
        for address in rejected:
            with self.subTest(address=address):
                self.assertFalse(self.reconnect.valid_address(address))
                self.assertFalse(self.reconnect.schedule_reconnect(address))

    def test_rejected_peer_never_reaches_adb_worker(self):
        with patch.object(self.reconnect, "_run") as run:
            self.assertFalse(self.reconnect.ensure_online("8.8.8.8:5555"))
            self.assertFalse(self.reconnect.ensure_online("127.0.0.1:5555"))
            run.assert_not_called()

    def test_inventory_reconnect_uses_same_validator_before_scheduling(self):
        function = _extract_function("_try_wifi_reconnect")
        namespace = {
            "Session": object,
            "_last_wifi_reconnect": 0,
            "_time": Mock(time=Mock(return_value=100)),
            "subprocess": _AdbOutput("List of devices attached\n"),
            "text": lambda value: value,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), "phone.py", "exec"), namespace)
        rows = [
            ("8.8.8.8", 5555),
            ("127.0.0.1", 5555),
            ("169.254.1.2", 5555),
            ("0.0.0.0", 5555),
            ("224.0.0.1", 5555),
            ("192.168.50.37", 5555),
        ]
        db = _InventoryDb(rows)
        with patch.object(self.reconnect, "schedule_reconnect") as schedule:
            namespace["_try_wifi_reconnect"](db)
        schedule.assert_called_once_with("192.168.50.37:5555")

    def test_inventory_keeps_distinct_same_model_and_filters_known_usb_alias(self):
        import ops.usb_recovery as recovery

        with tempfile.TemporaryDirectory() as state:
            previous_state = os.environ.get("D3M0N_STATE_DIR")
            os.environ["D3M0N_STATE_DIR"] = state
            try:
                recovery._alias_cache.clear()
                alias_path = Path(state) / "adb-usb-aliases.json"
                recovery.remember("192.168.50.39:5555", "USBA", alias_path)
                output = (
                    "List of devices attached\n"
                    "USBX device product:x model:SM_G9500\n"
                    "192.168.50.38:5555 device product:x model:SM_G9500\n"
                    "USBA device product:x model:SM_G9600\n"
                    "192.168.50.39:5555 device product:x model:SM_G9600\n"
                )
                function = _extract_function("phone_devices")
                namespace = {
                    "Session": object,
                    "Depends": lambda value: value,
                    "get_db": object(),
                    "subprocess": _AdbOutput(output),
                    "_try_wifi_reconnect": lambda db: None,
                    "list_configured_ios_devices": lambda deep_probe=False: [],
                    "text": lambda value: value,
                }
                exec(compile(ast.Module(body=[function], type_ignores=[]), "phone.py", "exec"), namespace)
                result = namespace["phone_devices"](db=_InventoryDb())
                serials = [device["serial"] for device in result["devices"]]
                self.assertEqual(serials, ["USBX", "192.168.50.38:5555", "192.168.50.39:5555"])
            finally:
                if previous_state is None:
                    os.environ.pop("D3M0N_STATE_DIR", None)
                else:
                    os.environ["D3M0N_STATE_DIR"] = previous_state
                recovery._alias_cache.clear()


if __name__ == "__main__":
    unittest.main()
