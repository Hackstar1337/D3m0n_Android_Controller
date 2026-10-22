from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "package_release.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, text=True, capture_output=True)


def test_check_only_passes_source_guard():
    result = _run("--check-only")
    assert result.returncode == 0, result.stderr
    assert "source-only tree passed release guard" in result.stdout


def test_release_mode_requires_successful_ci_proof(tmp_path: Path):
    result = _run("--release", "--output", str(tmp_path / "release.zip"))
    assert result.returncode != 0
    assert "requires --ci-proof" in result.stderr


def test_release_mode_rejects_failed_proof(tmp_path: Path):
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"status": "failure", "workflow": "CI", "commit": "abc", "docker_acceptance": True}), encoding="utf-8")
    result = _run("--release", "--ci-proof", str(proof), "--output", str(tmp_path / "release.zip"))
    assert result.returncode != 0
    assert "status=success" in result.stderr


def test_release_mode_requires_docker_acceptance_proof(tmp_path: Path):
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"status": "success", "workflow": "CI", "commit": "abc"}), encoding="utf-8")
    result = _run("--release", "--ci-proof", str(proof), "--output", str(tmp_path / "release.zip"))
    assert result.returncode != 0
    assert "docker_acceptance" in result.stderr


def test_release_zip_is_portable_and_excludes_runtime_state(tmp_path: Path):
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"status": "success", "workflow": "CI", "commit": "abc", "docker_acceptance": True, "native_acceptance": True}), encoding="utf-8")
    output = tmp_path / "release.zip"
    result = _run("--release", "--ci-proof", str(proof), "--output", str(output))
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    assert "D3m0n_Android_Controller/compose.dev.yaml" in names
    assert "D3m0n_Android_Controller/compose.prod.yaml" in names
    assert "D3m0n_Android_Controller/Dockerfile.dev" in names
    for lock_name in ("backend.lock", "dev.lock", "build.lock"):
        assert f"D3m0n_Android_Controller/requirements/{lock_name}" in names
    assert "D3m0n_Android_Controller/.github/workflows/ci.yml" in names
    assert "D3m0n_Android_Controller/RELEASE-MANIFEST.json" in names
    assert not any("node_modules" in name or ".venv" in name or name.endswith(".db") for name in names)
    assert not any("dashboard-auth" in name or name.endswith("/.env") for name in names)


def test_release_guard_rejects_private_fixture(tmp_path: Path):
    fixture = tmp_path / "source"
    fixture.mkdir()
    # The command should fail before packaging because the allowlist excludes this file.
    (fixture / ".env").write_text("D3M0N_PASSWORD='not-for-release'\n", encoding="utf-8")
    result = _run("--source", str(fixture), "--check-only")
    assert result.returncode != 0
    assert "No allowlisted source files" in result.stderr or "required-source-missing" in result.stderr
