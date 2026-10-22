"""Safe LAN discovery tests: all ADB/network tools are injected fakes."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from ops import lan_discovery as discovery


INTERFACES = [{
    "name": "Wi-Fi", "address": "192.168.50.4", "netmask": "255.255.255.0",
    "cidr": "192.168.50.0/24", "up": True, "private": True,
}]


class LanDiscoveryTests(unittest.TestCase):
    def test_mdns_keeps_pairing_and_dynamic_connect_ports_separate(self):
        output = (
            "List of discovered mdns services\n"
            "192.168.50.8:37123 _adb-tls-pairing._tcp\n"
            "_adb-tls-connect._tcp 192.168.50.8:40443\n"
            "malformed _adb-tls-connect._tcp\n"
        )
        services = discovery.parse_mdns_services(output)
        self.assertEqual(
            {(item["service"], item["port"]) for item in services},
            {("_adb-tls-pairing._tcp", 37123), ("_adb-tls-connect._tcp", 40443)},
        )

    def test_public_and_uncontained_networks_are_rejected(self):
        for cidr in ("8.8.8.0/24", "192.0.2.0/24", "10.0.0.0/8"):
            with self.subTest(cidr=cidr):
                with self.assertRaises(ValueError):
                    discovery.validate_private_cidr(cidr, interfaces=INTERFACES)

    def test_probe_has_no_adb_calls_and_is_bounded(self):
        adb_calls = []
        socket_calls = []

        def fake_socket(address, timeout):
            socket_calls.append((address, timeout))
            if address[0] == "192.168.50.5":
                return SimpleNamespace(close=lambda: None)
            raise OSError("closed")

        result = discovery.probe_private_lan(
            "192.168.50.0/29", [5555], interfaces=INTERFACES,
            max_workers=16, timeout=0.3, total_timeout=15,
            socket_factory=fake_socket,
        )
        self.assertEqual(result["candidates"][0]["host"], "192.168.50.5")
        self.assertTrue(socket_calls)
        self.assertTrue(all(timeout <= 0.3 for _, timeout in socket_calls))
        self.assertEqual(adb_calls, [])

    def test_scan_mdns_only_calls_read_only_metadata_command(self):
        calls = []
        mdns = "192.168.50.8:40443 _adb-tls-connect._tcp"

        def fake_runner(args, timeout):
            calls.append(args)
            return 0, mdns, ""

        result = discovery.scan_candidates(methods=["mdns"], interfaces=INTERFACES, runner=fake_runner)
        self.assertEqual(calls, [["mdns", "services"]])
        self.assertEqual(result["candidates"][0]["port"], 40443)
        self.assertNotIn("pairing_code", result["candidates"][0])

    def test_candidate_store_ttl_and_lease(self):
        now = [100.0]
        store = discovery.CandidateStore(ttl=5, clock=lambda: now[0])
        candidate = store.put({"host": "192.168.50.8", "port": 40443, "service": "_adb-tls-connect._tcp"})
        claimed = store.lease(candidate["candidate_id"])
        self.assertEqual(claimed["host"], "192.168.50.8")
        with self.assertRaises(RuntimeError):
            store.lease(candidate["candidate_id"])
        store.release(candidate["candidate_id"], consume=True)
        self.assertIsNone(store.get(candidate["candidate_id"]))


if __name__ == "__main__":
    unittest.main()
