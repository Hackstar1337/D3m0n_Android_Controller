import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ops.ai_fleet_settings import (
    EndpointPolicyError,
    ProfileExpiredError,
    ProfileStore,
    ProfileValidationError,
)


def fake_resolver(host, port):
    return ["93.184.216.34"]


class FleetProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProfileStore(self.temp.name, resolver=fake_resolver, approved_generic_origins=("https://api.example.test/v1",))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, profile_id, provider="openai", roles=()):
        endpoint = "https://api.openai.com/v1" if provider == "openai" else "https://openrouter.ai/api/v1"
        return self.store.create_profile(profile_id, provider, "test-model", endpoint, "secret-value", roles=roles)

    def test_encrypted_namespace_has_no_plaintext_secret_and_public_view_is_metadata_only(self):
        public = self.create("planner", roles=("planner",))
        self.assertTrue(public.configured)
        self.assertNotIn("secret", public.as_dict())
        encrypted = Path(self.temp.name) / "profiles.enc"
        self.assertTrue(encrypted.exists())
        self.assertNotIn(b"secret-value", encrypted.read_bytes())
        if os.name == "posix":
            self.assertEqual((Path(self.temp.name) / "profiles.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(self.temp.name).stat().st_mode & 0o777, 0o700)
        self.assertNotIn("secret-value", json.dumps(self.store.public_catalog()))

    def test_no_environment_secret_fallback_and_clear_revoke_expiry_fail_closed(self):
        self.create("p")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-be-read"}, clear=False):
            self.assertEqual(self.store.get_runtime("p")["secret"], "secret-value")
        self.store.clear_secret("p")
        with self.assertRaises(ProfileValidationError):
            self.store.get_runtime("p")
        self.create("p", roles=("planner",))
        self.store.revoke_profile("p")
        with self.assertRaises(Exception):
            self.store.get_runtime("p")
        expired_store = ProfileStore(tempfile.mkdtemp(), resolver=fake_resolver)
        try:
            with self.assertRaises(ProfileValidationError):
                expired_store.create_profile("expired", "openai", "m", "https://api.openai.com/v1", "s", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        finally:
            import shutil
            shutil.rmtree(expired_store.state_dir, ignore_errors=True)

    def test_official_and_generic_endpoint_ssrf_policy(self):
        self.create("ok")
        with self.assertRaises(EndpointPolicyError):
            self.store.create_profile("evil", "openai", "m", "https://evil.example/v1", "s")
        with self.assertRaises(EndpointPolicyError):
            self.store.create_profile("private", "generic", "m", "https://127.0.0.1/v1", "s",)
        generic = self.store.create_profile("generic", "generic", "m", "https://api.example.test/v1", "s")
        self.assertEqual(generic.endpoint_origin, "https://api.example.test/v1")
        with self.assertRaises(EndpointPolicyError):
            self.store.create_profile("userinfo", "openai", "m", "https://u:p@api.openai.com/v1", "s")

    def test_freeze_requires_three_distinct_active_roles_and_is_versioned(self):
        self.create("planner", roles=("planner",))
        self.create("executor", roles=("executor",))
        self.create("verifier", roles=("verifier",))
        freeze = self.store.freeze({"planner": "planner", "executor": "executor", "verifier": "verifier"})
        self.assertGreaterEqual(freeze.version, 1)
        self.assertEqual(freeze.role_bindings["verifier"], "verifier")
        self.store.create_profile("planner", "openai", "changed-after-freeze", "https://api.openai.com/v1", "new-secret", roles=("planner",))
        self.assertEqual(self.store.latest_freeze().profiles[0].model, "test-model")
        self.assertIsNotNone(self.store.latest_freeze())
        with self.assertRaises(ProfileValidationError):
            self.store.freeze({"planner": "planner", "executor": "planner", "verifier": "verifier"})

    def test_profile_snapshot_is_foundation_compatible(self):
        self.create("planner", roles=("planner",))
        self.create("executor", roles=("executor",))
        self.create("verifier", roles=("verifier",))
        from ops.ai_fleet_settings import profile_snapshot
        snapshot = profile_snapshot(self.store, self.store.freeze({"planner": "planner", "executor": "executor", "verifier": "verifier"}))
        self.assertEqual(str(snapshot.version), "4")
        self.assertNotIn("secret-value", snapshot.model_dump_json())


if __name__ == "__main__":
    unittest.main()
