import time
import unittest

from ops.ai_owner_grants import OwnerGrantStore


class OwnerGrantStoreTests(unittest.TestCase):
    def test_grant_is_single_use_and_child_scoped(self):
        store = OwnerGrantStore()
        store.issue("child-a", "reboot", "reboot")
        self.assertFalse(store.consume("child-b", "reboot", "reboot"))
        self.assertTrue(store.consume("child-a", "reboot", "reboot"))
        self.assertFalse(store.consume("child-a", "reboot", "reboot"))

    def test_operation_must_match_capability(self):
        store = OwnerGrantStore()
        with self.assertRaises(ValueError):
            store.issue("child-a", "reboot", "install")
        with self.assertRaises(ValueError):
            store.issue("child-a", "navigate", "tap")
        store.issue("child-a", "apk_install", "install")
        self.assertFalse(store.consume("child-a", "apk_install", "push"))
        self.assertTrue(store.consume("child-a", "apk_install", "install"))

    def test_expired_grant_is_rejected(self):
        store = OwnerGrantStore()
        store.issue("child-a", "file_write", "push", ttl=30)
        key = ("child-a", "file_write", "push")
        store._grants[key] = time.monotonic() - 1
        self.assertFalse(store.consume("child-a", "file_write", "push"))

    def test_revoke_child(self):
        store = OwnerGrantStore()
        store.issue("child-a", "file_write", "pull")
        store.revoke_child("child-a")
        self.assertEqual(store.pending("child-a"), [])


class ReconcileBodyTests(unittest.TestCase):
    def test_grant_requires_retry_and_both_fields(self):
        from pydantic import ValidationError
        from ops.ai_fleet_api import ReconcileBody
        ok = ReconcileBody(child_id="c1", outcome="retry", grant_capability="reboot", grant_operation="reboot")
        self.assertEqual(ok.grant_capability, "reboot")
        with self.assertRaises(ValidationError):
            ReconcileBody(child_id="c1", outcome="succeeded", grant_capability="reboot", grant_operation="reboot")
        with self.assertRaises(ValidationError):
            ReconcileBody(child_id="c1", outcome="retry", grant_capability="reboot")
        plain = ReconcileBody(child_id="c1", outcome="failed", summary="checked")
        self.assertIsNone(plain.grant_capability)


if __name__ == "__main__":
    unittest.main()
