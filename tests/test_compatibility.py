from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from ops.compatibility import (
    adb_capabilities,
    diagnostic_profile,
    node_supported,
    platform_tools_spec,
    probe_adb,
    python_supported,
)
from scripts.install_platform_tools import validate_archive


def test_runtime_boundaries_match_manifest():
    assert not python_supported("3.10", guided=True)
    assert python_supported("3.11", guided=True)
    assert python_supported("3.12.9", guided=True)
    assert not python_supported("3.13", guided=False)
    assert python_supported("3.10", guided=False)
    assert not node_supported("v20.18.0")
    assert node_supported("v20.19.0")
    assert not node_supported("v22.11.0")
    assert node_supported("v22.12.0")
    assert node_supported("v24.0.0")


def test_adb_capabilities_separate_tcp_and_pairing():
    old = adb_capabilities(
        "Android Debug Bridge version 1.0.41\nVersion 29.0.6",
        "adb devices\nadb connect HOST\nadb tcpip PORT\nadb shell COMMAND",
    )
    assert old["usb_tcp_control"] is True
    assert old["wireless_pairing"] is False
    assert "wireless pairing (pair + mdns)" in old["missing"]

    modern = adb_capabilities(
        "Android Debug Bridge version 1.0.41\nVersion 37.0.1",
        "adb devices\nadb connect HOST\nadb tcpip PORT\nadb pair HOST:PORT CODE\nadb mdns services\nadb shell COMMAND",
    )
    assert modern["usb_tcp_control"] is True
    assert modern["wireless_pairing"] is True
    assert modern["required_for_install"] is True


def test_probe_adb_only_runs_version_and_help(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        calls.append(command)
        assert "devices" not in command
        assert "start-server" not in command
        return Result(
            "Android Debug Bridge version 1.0.41\nVersion 37.0.1"
            if command[-1] == "version"
            else "adb devices adb connect adb tcpip adb pair adb mdns adb shell"
        )

    monkeypatch.setattr("ops.compatibility.shutil.which", lambda _name: "/fake/adb")
    monkeypatch.setattr("ops.compatibility.subprocess.run", fake_run)
    result = probe_adb("adb")
    assert result["capabilities"]["wireless_pairing"] is True
    assert [call[-1] for call in calls] == ["version", "help"]


def test_platform_tools_rejects_unsupported_architecture():
    with pytest.raises(ValueError, match="unsupported Platform-Tools target"):
        platform_tools_spec("linux", "arm64")
    spec = platform_tools_spec("linux", "amd64")
    assert spec["sha256"] == "d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1"
    assert spec["verified"] is True
    windows = platform_tools_spec("windows", "x64")
    assert windows["automatic_download"] is True
    assert windows["url"] == "https://dl.google.com/android/repository/platform-tools_r37.0.1-win.zip"
    assert windows["sha256"] == "45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99"


def test_windows_profile_does_not_require_node_in_backend():
    assert "node" not in diagnostic_profile("docker-backend")["required"]
    assert "node" in diagnostic_profile("docker-frontend")["required"]


def test_corrupt_archive_is_rejected_before_extraction():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("platform-tools/source.properties", "Pkg.Revision=37.0.1\n")
        archive.writestr("platform-tools/adb", "not-an-executable")
    data = payload.getvalue()
    spec = {"sha256": hashlib.sha256(data[:-1]).hexdigest(), "version": "37.0.1"}
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        validate_archive(data, spec)
