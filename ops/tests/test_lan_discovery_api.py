"""API contract tests do not start a server or invoke real ADB/network tools."""
from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from ops.lan_discovery_api import EnrollBody, ScanBody, discovery_enroll, discovery_scan
    from fastapi import HTTPException
except ImportError:  # pragma: no cover - dependency-light environments skip API imports
    EnrollBody = ScanBody = discovery_enroll = discovery_scan = None
    HTTPException = Exception


@unittest.skipIf(discovery_scan is None, "FastAPI/Pydantic dependencies are not installed")
class LanDiscoveryApiTests(unittest.TestCase):
    def test_probe_requires_explicit_confirmation(self):
        with self.assertRaises(HTTPException) as raised:
            discovery_scan(ScanBody(methods=["probe"], cidr="192.168.50.0/29"))
        self.assertEqual(raised.exception.status_code, 400)

    @patch("ops.lan_discovery_api.lan_discovery.scan_candidates")
    @patch("ops.lan_discovery_api._interfaces", return_value=[{"name": "Wi-Fi", "cidr": "192.168.50.0/24", "up": True}])
    def test_confirmed_scan_is_delegated_without_adb_connect(self, _interfaces, scan):
        scan.return_value = {"ok": True, "candidates": []}
        result = discovery_scan(ScanBody(methods=["probe"], interface="Wi-Fi", cidr="192.168.50.0/29", confirmed=True))
        self.assertTrue(result["ok"])
        self.assertEqual(scan.call_args.kwargs["methods"], {"probe"})

    @patch("ops.lan_discovery_api.lan_discovery.enroll_candidate", return_value={
        "ok": True, "candidate_id": "issued-candidate", "event": "enrolled",
        "enrolled": {"device_serial": "192.168.50.8:40443"},
    })
    def test_enroll_accepts_only_issued_candidate_and_does_not_return_code(self, enroll):
        result = discovery_enroll(EnrollBody(candidate_id="issued-candidate", action="pair", pairing_code="123456"))
        self.assertEqual(result["event"], "enrolled")
        self.assertNotIn("123456", str(result))


if __name__ == "__main__":
    unittest.main()
