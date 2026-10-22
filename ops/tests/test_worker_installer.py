from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).parents[2]
SHELL = ROOT / "scripts" / "install-fleet-runtime.sh"
PREPARE = ROOT / "scripts" / "prepare-fleet-sdk.sh"
POWERSHELL = ROOT / "scripts" / "install-fleet-runtime.ps1"
HOST = ROOT / "runtime" / "host-isolation"


class InstallerContractTests(unittest.TestCase):
    def test_config_rejects_every_arbitrary_runtime_path(self):
        from ops.worker_isolation import IsolationConfigError, WorkerIsolationConfig

        fields = ("prefix", "socket_path", "wrapper_path", "client_path", "source_root", "sdk_root", "venv_root", "config_path")
        for field in fields:
            with self.subTest(field=field):
                values = {field: "/tmp/not-reviewed"}
                with self.assertRaises(IsolationConfigError):
                    WorkerIsolationConfig(**values).validate()

    def test_templates_use_operator_and_fixed_paths(self):
        socket = (HOST / "d3m0n-ai-worker.socket").read_text(encoding="utf-8")
        sudoers = (HOST / "d3m0n-ai-worker.sudoers").read_text(encoding="utf-8")
        self.assertIn("SocketGroup=__OPERATOR__", socket)
        self.assertIn("__OPERATOR__ ALL=(root) NOPASSWD: D3M0N_AI_WORKER_CMDS", sudoers)
        self.assertNotIn("ash ALL=", sudoers)
        self.assertIn("PrivateNetwork=yes", (HOST / "d3m0n-ai-worker@.service").read_text(encoding="utf-8"))
        wrapper = (HOST / "d3m0n-ai-worker-wrapper.sh").read_text(encoding="utf-8")
        for control in ("DynamicUser=yes", "ProtectHome=yes", "ProtectSystem=strict", "RestrictAddressFamilies=AF_UNIX", "NoNewPrivileges=yes"):
            self.assertIn(control, wrapper)

    def test_linux_installer_has_explicit_activation_and_backup_contract(self):
        text = SHELL.read_text(encoding="utf-8")
        self.assertIn("--operator", text)
        self.assertIn("--enable-socket", text)
        self.assertIn("--probe", text)
        self.assertIn("refusing upgrade while a worker service is active", text)
        self.assertIn("runtime/prefix", text)
        self.assertIn("installed/usr/local/libexec", text)
        self.assertIn("installed/etc/systemd/system", text)
        self.assertIn("installed/etc/d3m0n-ai-worker/runtime.json", text)
        self.assertIn("manifest file missing or symlinked", text)
        self.assertNotIn("rsync", text)

    def test_windows_installer_has_fixed_linux_invocation_and_no_lifecycle_mutation(self):
        text = POWERSHELL.read_text(encoding="utf-8")
        self.assertIn("$FixedLinuxInstaller", text)
        self.assertIn("'/usr/bin/sudo', '-n'", text)
        self.assertIn("/proc/1/comm", text)
        self.assertRegex(text.lower(), r"(?:-replace|\.replace)\b")  # UTF16/NUL cleanup
        for forbidden in ("--import", "--unregister", "wsl --install", "Enable-WindowsOptionalFeature"):
            self.assertNotIn(forbidden, text)
        self.assertIn("linux_config_path", text)
        self.assertIn("ParentConfigPath", text)

    def test_prepare_script_has_fixed_upstream_and_noneditable_install(self):
        text = PREPARE.read_text(encoding="utf-8")
        self.assertIn("https://github.com/droidrun/mobilerun.git", text)
        self.assertIn("301856138c6edeb2ae692bfab85b279ace520e59", text)
        self.assertIn("--copies", text)
        self.assertIn("pip wheel", text)
        self.assertIn("--no-index", text)
        self.assertIn("pip check", text)
        self.assertNotIn("pip install --no-deps", text)
        self.assertNotIn("--url", text)

    def test_shell_and_powershell_parse_when_available(self):
        bash = shutil.which("bash") if os.name != "nt" else None
        if bash:
            for script in (SHELL, PREPARE, HOST / "install-ipc.sh", HOST / "validate-host-isolation.sh"):
                result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            path = str(POWERSHELL).replace("'", "''")
            command = f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{path}')) | Out-Null"
            result = subprocess.run([powershell, "-NoProfile", "-Command", command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_parent_config_contract_is_nonsecret_and_separate(self):
        example = json.loads((HOST / "runtime.json.example").read_text(encoding="utf-8"))
        self.assertEqual(example["config_path"], "/etc/d3m0n-ai-worker/runtime.json")
        self.assertEqual(example["client_path"], "/usr/local/libexec/d3m0n-ai-worker-client")
        self.assertNotIn("password", json.dumps(example).lower())


if __name__ == "__main__":
    unittest.main()
