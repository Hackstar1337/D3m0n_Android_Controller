from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gitd.services.fleet_sdk_broker import (  # noqa: E402
    FIXED_RELAY_LAUNCHER,
    REVIEWED_ROOT_LAUNCHER,
    build_sdk_child_environment,
    build_sdk_worker_command,
)


class FixedWorkerLauncherTests(unittest.TestCase):
    def test_relay_client_is_direct_and_nnp_compatible(self):
        self.assertEqual(
            build_sdk_worker_command("ignored-test-python", FIXED_RELAY_LAUNCHER),
            [FIXED_RELAY_LAUNCHER],
        )
        self.assertEqual(
            build_sdk_worker_command("ignored-test-python", FIXED_RELAY_LAUNCHER, probe=True),
            [FIXED_RELAY_LAUNCHER, "--probe"],
        )
        self.assertNotIn("sudo", build_sdk_worker_command("ignored-test-python", FIXED_RELAY_LAUNCHER))

    def test_legacy_root_wrapper_remains_reviewed_process_path(self):
        self.assertEqual(
            build_sdk_worker_command("ignored-test-python", REVIEWED_ROOT_LAUNCHER),
            ["sudo", "-n", REVIEWED_ROOT_LAUNCHER],
        )
        self.assertEqual(
            build_sdk_worker_command("ignored-test-python", REVIEWED_ROOT_LAUNCHER, probe=True),
            ["sudo", "-n", REVIEWED_ROOT_LAUNCHER, "--probe"],
        )

    def test_plain_interpreter_remains_available_for_offline_tests(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"D3M0N_ALLOW_TEST_WORKER_FALLBACK": "1"}, clear=False):
            self.assertEqual(
                build_sdk_worker_command("C:/task/backend-venv/Scripts/python.exe"),
                ["C:/task/backend-venv/Scripts/python.exe", "-m", "ops.ai_fleet_sidecar"],
            )
            self.assertEqual(
                build_sdk_worker_command("C:/task/backend-venv/Scripts/python.exe", probe=True),
                ["C:/task/backend-venv/Scripts/python.exe", "-m", "ops.ai_fleet_sidecar", "--probe"],
            )

    def test_arbitrary_paths_and_arguments_fail_closed(self):
        for bad in (
            "/tmp/worker",
            "/usr/local/libexec/d3m0n-ai-worker-client --probe",
            [FIXED_RELAY_LAUNCHER],
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build_sdk_worker_command("python", bad)
        with self.assertRaises(ValueError):
            build_sdk_worker_command(None)

    def test_child_environment_is_allowlisted_and_has_no_credentials(self):
        env = build_sdk_child_environment(
            "/reviewed/source",
            {
                "PATH": "/usr/bin",
                "HOME": "/srv/fixture-owner",
                "D3M0N_AI_WORKER_LAUNCHER": FIXED_RELAY_LAUNCHER,
                "AWS_SECRET_ACCESS_KEY": "must-not-cross-boundary",
                "PROVIDER_API_KEY": "must-not-cross-boundary",
                "ARBITRARY_FLAG": "must-not-cross-boundary",
            },
        )
        self.assertEqual(env["PYTHONPATH"], "/reviewed/source")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("D3M0N_AI_WORKER_LAUNCHER", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("PROVIDER_API_KEY", env)
        self.assertNotIn("ARBITRARY_FLAG", env)
        self.assertEqual(env["OTEL_SDK_DISABLED"], "true")
        self.assertEqual(env["POSTHOG_DISABLED"], "true")

    def test_daemon_probe_has_explicit_empty_pipe_not_inherited_devnull(self):
        import os
        from unittest.mock import patch
        from types import SimpleNamespace
        from ops.ai_fleet_services import SubprocessSDKSidecar
        with patch.dict(os.environ, {"D3M0N_AI_WORKER_LAUNCHER": FIXED_RELAY_LAUNCHER}), patch("ops.ai_fleet_services.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"ready":true}')) as run:
            self.assertTrue(SubprocessSDKSidecar().status()["ready"])
            self.assertEqual(run.call_args.args[0], [FIXED_RELAY_LAUNCHER, "--probe"])
            self.assertEqual(run.call_args.kwargs["input"], "")

if __name__ == "__main__":
    unittest.main()
