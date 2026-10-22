import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops import ai_settings


class AISettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"D3M0N_AI_SETTINGS_STATE_DIR": self.temp.name}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_state_dir_override_and_owner_only_encrypted_files(self):
        self.assertEqual(ai_settings.load_settings()["default_provider"], "ollama")
        data = ai_settings.load_settings()
        data["providers"]["anthropic"]["api_key"] = "sk-secret-test"
        ai_settings.save_settings(data)
        key = Path(self.temp.name) / "settings.key"
        encrypted = Path(self.temp.name) / "settings.enc"
        self.assertTrue(key.exists())
        self.assertTrue(encrypted.exists())
        self.assertNotIn(b"sk-secret-test", encrypted.read_bytes())
        if os.name == "posix":
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(encrypted.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(self.temp.name).stat().st_mode & 0o777, 0o700)
        public = ai_settings.public_settings()
        provider = next(item for item in public["providers"] if item["id"] == "anthropic")
        self.assertTrue(provider["api_key_configured"])
        self.assertEqual(provider["key_source"], "saved")
        self.assertNotIn("api_key", provider)
        self.assertNotIn("sk-secret-test", json.dumps(public))

    def test_missing_key_with_existing_ciphertext_fails_closed(self):
        ai_settings.save_settings(ai_settings.load_settings())
        (Path(self.temp.name) / "settings.key").unlink()
        with self.assertRaises(ai_settings.SettingsError):
            ai_settings.load_settings()

    def test_endpoint_validation_rejects_userinfo_query_public_local_and_nonofficial(self):
        for value in (
            "http://user:pass@127.0.0.1:11434",
            "http://127.0.0.1:11434?x=1",
            "http://example.com:11434",
        ):
            with self.assertRaises(ValueError):
                ai_settings.validate_endpoint("ollama", value)
        with self.assertRaises(ValueError):
            ai_settings.validate_endpoint("openrouter", "https://evil.example/api/v1")
        self.assertEqual(
            ai_settings.validate_endpoint("ollama", "http://127.0.0.1:11434/"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            ai_settings.validate_endpoint("openrouter", "https://openrouter.ai/api/v1/"),
            "https://openrouter.ai/api/v1",
        )

    def test_blank_key_retains_saved_key_and_clear_uses_environment_fallback(self):
        data = ai_settings.load_settings()
        data["providers"]["anthropic"]["api_key"] = "saved-secret"
        ai_settings.save_settings(data)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "environment-secret"}, clear=False):
            ai_settings.update_provider("anthropic", {"api_key": ""})
            runtime = ai_settings.provider_runtime("anthropic")
            self.assertEqual(runtime["api_key"], "saved-secret")
            ai_settings.update_provider("anthropic", {"clear_api_key": True})
            self.assertEqual(ai_settings.provider_runtime("anthropic")["api_key"], "environment-secret")
            provider = ai_settings.public_provider("anthropic")
            self.assertEqual(provider["key_source"], "environment")
            self.assertNotIn("environment-secret", json.dumps(provider))

    def test_current_runtime_reads_saved_ollama_endpoint_after_save(self):
        data = ai_settings.load_settings()
        data["providers"]["ollama"]["base_url"] = "http://127.0.0.1:11435"
        ai_settings.save_settings(data)
        self.assertEqual(ai_settings.provider_runtime("ollama")["base_url"], "http://127.0.0.1:11435")


if __name__ == "__main__":
    unittest.main()
