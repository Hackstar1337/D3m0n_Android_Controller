from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops import docker_setup


def test_docker_bootstrap_files_and_targets_exist():
    assert (ROOT / "compose.dev.yaml").is_file()
    assert (ROOT / "compose.prod.yaml").is_file()
    assert (ROOT / "Dockerfile.dev").is_file()
    assert (ROOT / "scripts" / "frontend_entrypoint.sh").is_file()
    dockerfile = (ROOT / "Dockerfile.dev").read_text(encoding="utf-8")
    assert "frontend-dev" in dockerfile and "frontend-prod" in dockerfile
    assert "backend-dev" in dockerfile and "backend-prod" in dockerfile
    assert "--no-deps -e ." in dockerfile
    assert "--require-hashes" in dockerfile
    assert "sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205" in dockerfile
    assert "sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70" in dockerfile


def test_compose_preserves_loopback_non_root_and_named_state_volumes():
    compose = (ROOT / "compose.dev.yaml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${D3M0N_HOST_API_PORT:-5055}:5055"' in compose
    assert '"127.0.0.1:${D3M0N_HOST_UI_PORT:-5173}:5173"' in compose
    assert "${D3M0N_COMPOSE_PROJECT:-d3m0n-droid}_private_state" in compose
    assert "${D3M0N_COMPOSE_PROJECT:-d3m0n-droid}_adb_identity" in compose
    assert "cap_drop: [ALL]" in compose
    assert "no-new-privileges:true" in compose
    assert "condition: service_healthy" in compose
    assert "api/auth/status" not in compose.split("healthcheck:", 1)[1].split("frontend:", 1)[0]


def test_choose_port_never_returns_reserved_or_occupied(monkeypatch):
    monkeypatch.setattr(docker_setup, "_port_free", lambda port: port == 5055)
    monkeypatch.setattr(docker_setup.socket, "socket", lambda *args, **kwargs: _FakeSocket(6001))
    assert docker_setup.choose_port(5055, reserved=(5055,)) == 6001


class _FakeSocket:
    def __init__(self, port):
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, _address):
        return None

    def getsockname(self):
        return ("127.0.0.1", self.port)

    def setsockopt(self, *_args):
        return None


def test_state_is_private_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_setup, "state_path", lambda: tmp_path / "state.json")
    docker_setup._write_state({"repo": str(docker_setup.ROOT.resolve()), "api_port": 5055, "ui_port": 5173})
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["api_port"] == 5055
    assert docker_setup.load_state()["ui_port"] == 5173


def test_readiness_requires_liveness_auth_and_protected_api(monkeypatch):
    responses = {
        "/api/health": (200, {"status": "ok"}),
        "/api/auth/status": (200, {"configured": True}),
        "/api/phone/devices": (401, None),
        "/": (200, None),
    }

    def fake_http(url, timeout=2.0):
        del timeout
        from urllib.parse import urlsplit
        return responses[urlsplit(url).path]

    monkeypatch.setattr(docker_setup, "_http_status", fake_http)
    result = docker_setup.readiness(5055, 5173)
    assert result["ready"] is True
    assert result["auth_configured"] is True
    assert result["protected_api"] is True

    responses["/api/auth/status"] = (503, {"configured": False})
    result = docker_setup.readiness(5055, 5173)
    assert result["liveness"] is True
    assert result["ready"] is False


def test_dry_run_never_calls_engine_or_claims_ready(monkeypatch, capsys):
    monkeypatch.setattr(docker_setup, "check_engine", lambda: (_ for _ in ()).throw(AssertionError("engine called")))
    assert docker_setup.bootstrap(dry_run=True) == 0
    output = capsys.readouterr().out
    assert "Dry run" in output
    assert "READY" not in output


def test_frontend_dependency_refresh_is_before_vite():
    script = (ROOT / "scripts" / "frontend_entrypoint.sh").read_text(encoding="utf-8")
    assert "sha256sum" in script
    assert "npm ci" in script
    assert "mv \"$TMP\" \"$FINGERPRINT\"" in script
    assert "exec npm run dev" in script
    assert script.index("npm ci") < script.index("exec npm run dev")
    assert "frontend_modules" not in script or "D3M0N_FRONTEND_MODULE_DIR" in script


def test_bootstrap_cli_exposes_explicit_modes():
    assert docker_setup.main(["install", "--mode", "native", "--dry-run"]) == 0
