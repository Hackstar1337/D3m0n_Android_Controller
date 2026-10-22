from __future__ import annotations

from pathlib import Path

from scripts.release_guard import scan_tree


_REQUIRED_FILES = (
    "LICENSE",
    "SECURITY.md",
    ".env.example",
    "pyproject.toml",
    "ops/android_h264.py",
    "ops/dashboard_auth.py",
    "ops/fleet_control.py",
    "ops/ai_settings.py",
    "ops/server.py",
    "gitd/app.py",
    "ai_contracts/models.py",
    "frontend/src/App.vue",
    "ops/scrcpy-server-v5.0.1.jar",
    "compose.prod.yaml",
    "requirements/backend.lock",
    "requirements/dev.lock",
    "requirements/build.lock",
)
_REQUIRED_DIRS = ("gitd", "ops", "ai_contracts", "frontend", "tests")


def _clean_candidate(root: Path) -> None:
    for relative in _REQUIRED_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in _REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic source\n")


def test_clean_synthetic_candidate_passes(tmp_path: Path) -> None:
    _clean_candidate(tmp_path)
    (tmp_path / ".env.example").write_text("TOKEN=\nPUBLIC_ORIGIN=https://example.test\n", encoding="utf-8")
    assert scan_tree(tmp_path) == []


def test_guard_reports_paths_and_categories_without_values(tmp_path: Path) -> None:
    _clean_candidate(tmp_path)
    secret = "sy" + "nthetic-secret-value"
    (tmp_path / "notes.txt").write_text(
        f'password = "{secret}"\naddress = "192.168.77.9"\npath = "{chr(47)}home{chr(47)}owner/private"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("placeholder\n", encoding="utf-8")
    findings = scan_tree(tmp_path)
    rendered = "\n".join(item.render() for item in findings)
    assert "secret-like-literal" in rendered
    assert "private-ip-literal" in rendered
    assert "machine-specific-user-path" in rendered
    assert "forbidden-runtime-file" in rendered
    assert "forbidden-runtime-directory" in rendered
    assert secret not in rendered
    assert "192.168.77.9" not in rendered
    assert ("/" + "/".join(("home", "owner", "private"))) not in rendered
