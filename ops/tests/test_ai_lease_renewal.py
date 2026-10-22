from __future__ import annotations

import unittest

from ai_contracts.models import Capability
from ops import ai_control_guard as g
from ops import control_arbiter


class LeaseRenewalIdentityTests(unittest.TestCase):
    def setUp(self):
        g.reset_for_tests()
        self.guard = g.AIControlGuard(lease_seconds=30)

    def tearDown(self):
        g.reset_for_tests()

    def test_reference_taken_before_heartbeat_renewal_remains_valid(self):
        stale = self.guard.acquire("child-1", "serial-20", Capability.NAVIGATE, 1)
        renewed = self.guard.renew(stale)
        self.assertIsNot(stale, renewed)
        fresh = self.guard.require(stale, serial="serial-20", capability=Capability.NAVIGATE, fence=1)
        self.assertEqual(fresh.lease_id, stale.lease_id)
        self.guard.renew(stale)  # a second heartbeat holding the old object must not fail either

    def test_stop_and_takeover_still_revoke(self):
        lease = self.guard.acquire("child-1", "serial-20", Capability.NAVIGATE, 1)
        self.guard.renew(lease)
        self.guard.stop("child-1")
        with self.assertRaises(g.AIControlDenied):
            self.guard.require(lease, serial="serial-20", capability=Capability.NAVIGATE)

    def test_different_grant_is_rejected(self):
        lease = self.guard.acquire("child-1", "serial-20", Capability.NAVIGATE, 1)
        forged = g.AIOwnership(lease.serial, lease.child_id, lease.fence, lease.capabilities, object(), lease.lease_id, lease.expires_at)
        with self.assertRaises(g.AILeaseRevoked):
            self.guard.require(forged, serial="serial-20")


if __name__ == "__main__":
    unittest.main()
