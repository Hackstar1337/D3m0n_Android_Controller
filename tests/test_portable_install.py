from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_discovers_custom_packages_and_asset():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"ops*"' in text
    assert '"ai_contracts*"' in text
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    assets = tomllib.loads(text)["tool"]["setuptools"]["package-data"]["ops"]
    assert {"scrcpy-server-v5.0.1.jar", "SCRCPY-LICENSE.txt"}.issubset(assets)
    assert "websocket-client" in text and "websockets" in text and "tomli" in text


def test_scripts_exist_and_do_not_use_global_install_commands():
    for relative in ("scripts/bootstrap.ps1", "scripts/bootstrap.sh", "scripts/start.ps1", "scripts/start.sh"):
        assert (ROOT / relative).is_file()
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    assert "--prefix" in bootstrap
    assert "pip install" in bootstrap
    assert "apt" not in bootstrap and "sudo" not in bootstrap


def test_doctor_never_runs_adb_devices(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from ops import doctor

    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="tool version\n", stderr="")

    monkeypatch.setattr(doctor.shutil, "which", lambda name: name)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    doctor.collect()
    assert all(command[0:2] != ["adb", "devices"] for command in seen)
    assert all("devices" not in command for command in seen)


def test_launcher_defaults_to_loopback_and_derives_exact_origin(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from ops import launch

    monkeypatch.setattr(launch, "FRONTEND_DIST", tmp_path)
    (tmp_path / "index.html").write_text("fixture", encoding="utf-8")
    monkeypatch.delenv("D3M0N_HOST", raising=False)
    monkeypatch.delenv("D3M0N_PORT", raising=False)
    monkeypatch.delenv("D3M0N_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("D3M0N_PUBLIC_ORIGIN", raising=False)
    assert launch.main(["--dry-run"]) == 0
    assert os.environ["D3M0N_ALLOWED_ORIGINS"] == "http://127.0.0.1:5055"


def test_launcher_rejects_wildcard_and_unapproved_non_loopback(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from ops import launch

    monkeypatch.setattr(launch, "FRONTEND_DIST", tmp_path)
    (tmp_path / "index.html").write_text("fixture", encoding="utf-8")
    with pytest.raises(SystemExit):
        launch.main(["--host", "192.168.50.5", "--dry-run"])
    monkeypatch.setenv("D3M0N_ALLOWED_ORIGINS", "*")
    with pytest.raises(SystemExit):
        launch.main(["--dry-run"])
