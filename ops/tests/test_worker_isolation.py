from __future__ import annotations

import hashlib
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from ops.worker_isolation import (  # noqa: E402
    DISABLED,
    LINUX_HOST_ISOLATION,
    PINNED_MOBILERUN_COMMIT,
    PINNED_MOBILERUN_VERSION,
    WSL2_HOST_ISOLATION,
    IsolationConfigError,
    WorkerIsolationConfig,
    build_manifest,
)
from gitd.services.fleet_sdk_broker import build_sdk_worker_command  # noqa: E402


class WorkerIsolationTests(unittest.TestCase):
    def config(self, mode=WSL2_HOST_ISOLATION):
        return WorkerIsolationConfig(mode=mode)

    def test_wsl_vector_is_fixed_and_nonroot(self):
        cfg = self.config()
        self.assertEqual(
            cfg.command(probe=True),
            ["wsl.exe", "-d", "D3m0nFleet", "-u", "ash", "--exec", "/usr/local/libexec/d3m0n-ai-worker-client", "--probe"],
        )
        self.assertNotIn("sudo", cfg.command())
        self.assertNotEqual(cfg.worker_user, "root")
        self.assertEqual(build_sdk_worker_command(None, config=cfg), cfg.command())

    def test_linux_socket_backend_preserves_direct_relay(self):
        self.assertEqual(
            build_sdk_worker_command(None, config=self.config(LINUX_HOST_ISOLATION)),
            ["/usr/local/libexec/d3m0n-ai-worker-client"],
        )

    def test_invalid_distro_user_paths_and_backend_are_rejected(self):
        for values in (
            {"mode": WSL2_HOST_ISOLATION, "wsl_distro": "D3m0n Fleet"},
            {"mode": WSL2_HOST_ISOLATION, "worker_user": "root"},
            {"mode": WSL2_HOST_ISOLATION, "client_path": "C:/worker.exe"},
            {"mode": "native-unconfined"},
            {"mode": WSL2_HOST_ISOLATION, "sdk_version": "latest"},
        ):
            with self.subTest(values=values), self.assertRaises(IsolationConfigError):
                WorkerIsolationConfig(**values).validate()
        with self.assertRaises(ValueError):
            build_sdk_worker_command("python", launcher="wsl.exe -d D3m0nFleet")

    def test_disabled_and_plain_modes_cannot_be_activation_ready(self):
        disabled = WorkerIsolationConfig(mode=DISABLED)
        with self.assertRaises(ValueError):
            build_sdk_worker_command("python", config=disabled)
        plain = WorkerIsolationConfig(mode="test-only-subprocess")
        with self.assertRaises(IsolationConfigError):
            plain.command()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                build_sdk_worker_command("python")
        with patch.dict(os.environ, {"D3M0N_ALLOW_TEST_WORKER_FALLBACK": "1"}, clear=True):
            self.assertEqual(build_sdk_worker_command("python"), ["python", "-m", "ops.ai_fleet_sidecar"])

    def test_config_json_and_static_assertions_agree(self):
        cfg = self.config()
        loaded = WorkerIsolationConfig.from_mapping(json.loads(cfg.to_json()))
        self.assertEqual(loaded.command(), cfg.command())
        assertions = loaded.static_isolation_assertions()
        self.assertEqual(assertions["socket"]["family"], "AF_UNIX")
        self.assertTrue(assertions["identity"]["nonroot_required"])
        self.assertEqual(assertions["sdk"]["commit"], PINNED_MOBILERUN_COMMIT)
        self.assertEqual(assertions["sdk"]["version"], PINNED_MOBILERUN_VERSION)

    def test_manifest_is_deterministic_and_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "source"
            root.mkdir()
            (root / "a.py").write_text("safe\n", encoding="utf-8")
            out = Path(td) / "manifest.sha256"
            self.assertEqual(build_manifest(root, ["a.py"], out), out)
            line = out.read_text(encoding="utf-8").strip()
            digest = hashlib.sha256((root / "a.py").read_bytes()).hexdigest()
            self.assertEqual(line, f"a.py {digest}")
            with self.assertRaises(IsolationConfigError):
                build_manifest(root, ["missing.py"], out)

    def test_no_credentials_cross_boundary_environment(self):
        from gitd.services.fleet_sdk_broker import build_sdk_child_environment
        env = build_sdk_child_environment("/opt/reviewed/source", {
            "PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "redacted-fixture",
            "PROVIDER_API_KEY": "redacted-fixture", "D3M0N_AI_WORKER_LAUNCHER": "/tmp/evil",
        })
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("PROVIDER_API_KEY", env)
        self.assertNotIn("D3M0N_AI_WORKER_LAUNCHER", env)


if __name__ == "__main__":
    unittest.main()
