from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_contracts import Role
from gitd.services.fleet_sdk_broker import (
    ChildBroker,
    WorkerRequest,
    build_sdk_child_environment,
    execute_process,
)
from ops.ai_fleet_services import SubprocessSDKSidecar


class _FakeStream:
    def write(self, value):
        return len(value)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class SDKProcessBoundaryTests(unittest.TestCase):
    def test_child_environment_is_allowlisted_and_keeps_safe_names(self):
        fake = {
            "PATH": "/offline/bin",
            "HOME": "/offline/home",
            "USERPROFILE": r"C:\offline\profile",
            "SYSTEMROOT": r"C:\Windows",
            "WINDIR": r"C:\Windows",
            "TEMP": "/offline/tmp",
            "TMP": "/offline/tmp",
            "D3M0N_TEST_SDK_PYTHON": "/offline/test-python",
            "PYTHONPATH": "/attacker/imports",
            "OPENAI_API_KEY": "fake-openai-value",
            "OPENROUTER_API_KEY": "fake-openrouter-value",
            "GITHUB_TOKEN": "fake-github-value",
            "GOOGLE_APPLICATION_CREDENTIALS": "/fake/google.json",
            "SPACESHIP_API_KEY": "fake-spaceship-value",
            "D3M0N_DASHBOARD_SECRET": "fake-dashboard-value",
        }
        env = build_sdk_child_environment(Path("/fixed/source"), fake)

        self.assertEqual(env["PATH"], "/offline/bin")
        self.assertEqual(env["HOME"], "/offline/home")
        self.assertEqual(env["USERPROFILE"], r"C:\offline\profile")
        self.assertEqual(env["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual(env["WINDIR"], r"C:\Windows")
        self.assertEqual(env["TEMP"], "/offline/tmp")
        self.assertEqual(env["TMP"], "/offline/tmp")
        self.assertEqual(env["D3M0N_TEST_SDK_PYTHON"], "/offline/test-python")
        self.assertEqual(env["PYTHONPATH"], str(Path("/fixed/source")))
        for name in (
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "SPACESHIP_API_KEY",
            "D3M0N_DASHBOARD_SECRET",
        ):
            self.assertNotIn(name, env)
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["OTEL_SDK_DISABLED"], "true")
        self.assertEqual(env["POSTHOG_DISABLED"], "true")

    def test_probe_and_execute_use_the_same_allowlisted_environment(self):
        fake_env = {
            "PATH": "/offline/bin",
            "HOME": "/offline/home",
            "D3M0N_ALLOW_TEST_WORKER_FALLBACK": "1",
            "OPENAI_API_KEY": "fake-openai-value",
            "GITHUB_TOKEN": "fake-github-value",
            "GOOGLE_API_KEY": "fake-google-value",
        }
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "python"
            python.write_text("", encoding="utf-8")
            with patch.dict(os.environ, fake_env, clear=True):
                with patch("ops.ai_fleet_services.subprocess.run") as run:
                    run.return_value = SimpleNamespace(returncode=0, stdout='{"ready":true}')
                    sidecar = SubprocessSDKSidecar(python)
                    self.assertTrue(sidecar.status()["ready"])
                    probe_env = run.call_args.kwargs["env"]
                self.assertNotIn("OPENAI_API_KEY", probe_env)
                self.assertNotIn("GITHUB_TOKEN", probe_env)
                self.assertEqual(probe_env["HOME"], "/offline/home")

                captured = {}
                fake_process = _FakeProcess()

                def capture_popen(*args, **kwargs):
                    captured.update(kwargs)
                    return fake_process

                broker = SimpleNamespace(
                    bootstrap=lambda mode: {"mode": mode},
                    heartbeat=lambda: None,
                    close=lambda: None,
                )
                with patch("gitd.services.fleet_sdk_broker.subprocess.Popen", side_effect=capture_popen):
                    with self.assertRaises(Exception):
                        execute_process(python, Path(temp), broker, mode="direct", heartbeat=lambda: None, cancelled=lambda: False, timeout=1)
                self.assertNotIn("OPENAI_API_KEY", captured["env"])
                self.assertNotIn("GITHUB_TOKEN", captured["env"])
                self.assertEqual(captured["env"]["HOME"], "/offline/home")


class FinishVerificationBoundaryTests(unittest.TestCase):
    def _broker(self, intent: str, packages: list[str], *, metadata=None, model_content='{"verified":true}'):
        broker = object.__new__(ChildBroker)
        broker.nonce = "nonce"
        broker.detail = SimpleNamespace(
            job_id="job",
            intent=SimpleNamespace(text=intent, metadata=metadata or {}),
            profile_snapshot=SimpleNamespace(role_bindings={Role.VERIFIER: "verifier"}),
        )
        broker.child = SimpleNamespace(child_id="child")
        broker.lease = SimpleNamespace(serial="serial", fence=1)
        broker.cancelled = lambda: False
        broker.seen = set()
        broker.packages = packages
        broker.actions = 2
        broker.authorize = lambda capability: None
        broker.observe = lambda: {"package": packages[-1]}
        broker._model = lambda request: {"content": model_content}
        return broker

    @staticmethod
    def _finish_request():
        return WorkerRequest(
            "finish-1", "nonce", "finish", "job", "child", "serial", 1,
            "verifier", {"success": True, "summary": "worker says done"},
        )

    def test_exact_safe_navigation_intent_uses_deterministic_native_preset(self):
        broker = self._broker(
            "Go Home, open Settings, then return Home",
            ["com.sec.android.app.launcher", "com.android.settings", "com.sec.android.app.launcher"],
        )
        result = broker.handle(self._finish_request())
        self.assertTrue(result["success"])
        self.assertEqual(result["verification_method"], "deterministic_native_preset")
        self.assertIn("Deterministic native", result["summary"])

    def test_broad_account_intent_and_expected_metadata_require_analysis(self):
        broker = self._broker(
            "Go Home, open Settings, then return Home and verify the account setup",
            ["com.sec.android.app.launcher", "com.android.settings", "com.sec.android.app.launcher"],
            metadata={"expected_packages": "com.sec.android.app.launcher,com.android.settings,com.sec.android.app.launcher"},
        )
        result = broker.handle(self._finish_request())
        self.assertTrue(result["success"])
        self.assertEqual(result["verification_method"], "read_only_analysis")
        self.assertFalse(result["verification_evidence"]["definitive_native_proof"])
        self.assertIn("not definitive native proof", result["summary"])

    def test_malformed_analysis_remains_uncertain_without_raw_response(self):
        broker = self._broker("Verify the account screen", ["com.android.settings"], model_content="not-json-secret")
        result = broker.handle(self._finish_request())
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "uncertain")
        self.assertNotIn("not-json-secret", result["summary"])


if __name__ == "__main__":
    unittest.main()
