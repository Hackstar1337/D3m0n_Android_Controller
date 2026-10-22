"""Build and validate a source-only D3m0n release ZIP.

The packager deliberately copies an allowlisted source tree rather than the whole
checkout.  Runtime state, credentials, build outputs, and machine-specific files
therefore cannot become release inputs by accident.  ``--release`` additionally
requires a successful CI proof JSON produced by the release workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep this list boring and explicit.  New release inputs require a reviewed edit.
ALLOWED_ROOT_FILES = {
    ".dockerignore",
    ".gitattributes",
    ".env.example",
    "alembic.ini",
    "CONTRIBUTING.md",
    "D3m0n-Droid.code-workspace",
    "DEVELOPMENT-DOCKER.md",
    "Dockerfile.dev",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "UPSTREAM-README.md",
    "compose.dev.yaml",
    "compose.prod.yaml",
    "dev.ps1",
    "dev.sh",
    "edit-dev.cmd",
    "install.cmd",
    "install.ps1",
    "install.sh",
    "pyproject.toml",
    "run.py",
    "start-dev.cmd",
    "start.cmd",
    "start.sh",
    "stop-dev.cmd",
}
ALLOWED_ROOT_DIRS = {
    ".devcontainer",
    ".github",
    "ai_contracts",
    "docs",
    "frontend",
    "gitd",
    "integrations",
    "ios",
    "ops",
    "patches",
    "portal",
    "registry",
    "runtime",
    "scripts",
    "site",
    "tests",
    "requirements",
}
FORBIDDEN_NAMES = {
    ".env",
    "dashboard-auth.json",
    "settings.key",
    "settings.enc",
    "fleet.sqlite3",
}
FORBIDDEN_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
    "uploads",
    "runs",
    "logs",
    "recordings",
    "screenshots",
    "results",
    "test_recordings",
    "backups",
    ".work",
}
FORBIDDEN_SUFFIXES = {
    ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
    ".wal", ".shm", ".apk", ".aab", ".ipa", ".mp4", ".mov", ".webm",
}


def _candidate_files(source: Path) -> list[tuple[Path, str]]:
    """Return allowlisted files as (absolute path, ZIP path), deterministically."""
    result: list[tuple[Path, str]] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(source)
        parts = relative.parts
        if not parts or parts[0] not in ALLOWED_ROOT_DIRS and len(parts) > 1:
            continue
        if len(parts) == 1 and parts[0] not in ALLOWED_ROOT_FILES and not path.is_dir():
            continue
        if any(part in FORBIDDEN_DIRS for part in parts):
            continue
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            continue
        if path.is_symlink():
            raise RuntimeError(f"Refusing symlink release input: {relative.as_posix()}")
        result.append((path, relative.as_posix()))
    if not result:
        raise RuntimeError("No allowlisted source files found")
    return result


def _copy_source(source: Path, staging: Path) -> None:
    for path, relative in _candidate_files(source):
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def _guard(staging: Path) -> list[str]:
    scripts = staging / "scripts"
    sys.path.insert(0, str(scripts))
    previous_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from release_guard import scan_tree
    finally:
        sys.dont_write_bytecode = previous_dont_write
        if sys.path and sys.path[0] == str(scripts):
            sys.path.pop(0)
    return [finding.render() for finding in scan_tree(staging)]


def _read_ci_proof(path: Path) -> dict[str, object]:
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read CI proof: {path}") from exc
    if not isinstance(proof, dict) or proof.get("status") != "success":
        raise RuntimeError("CI proof must be a JSON object with status=success")
    return proof


def _check_release_gate(*, release: bool, ci_proof: Path | None) -> dict[str, object] | None:
    if not release:
        return None
    if ci_proof is None:
        raise RuntimeError("--release requires --ci-proof from the successful CI workflow")
    proof = _read_ci_proof(ci_proof)
    if not proof.get("workflow") or not proof.get("commit"):
        raise RuntimeError("CI proof must identify workflow and commit")
    if proof.get("docker_acceptance") is not True:
        raise RuntimeError("CI proof must identify successful docker_acceptance")
    if proof.get("native_acceptance") is not True:
        raise RuntimeError("CI proof must identify successful native_acceptance")
    return proof


def _write_manifest(staging: Path, proof: dict[str, object] | None) -> None:
    payload = {
        "format": 1,
        "source_only": True,
        "files_sha256": {
            relative: hashlib.sha256(path.read_bytes()).hexdigest()
            for path, relative in _candidate_files(staging)
        },
    }
    if proof is not None:
        payload["ci"] = proof
    (staging / "RELEASE-MANIFEST.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _zip(staging: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix().lower()):
            if path.is_dir():
                continue
            relative = f"{staging.name}/{path.relative_to(staging).as_posix()}"
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def build(source: Path, output: Path | None, *, release: bool, ci_proof: Path | None, check_only: bool) -> Path | None:
    source = source.resolve()
    if not source.is_dir():
        raise RuntimeError(f"Source directory does not exist: {source}")
    proof = _check_release_gate(release=release, ci_proof=ci_proof)
    with tempfile.TemporaryDirectory(prefix="d3m0n-release-") as temporary:
        staging = Path(temporary) / "D3m0n_Android_Controller"
        staging.mkdir()
        _copy_source(source, staging)
        findings = _guard(staging)
        if findings:
            raise RuntimeError("release-guard findings:\n" + "\n".join(findings))
        _write_manifest(staging, proof)
        # The manifest itself is intentionally generated after the source guard; run
        # the guard once more so a future metadata change cannot bypass the check.
        findings = _guard(staging)
        if findings:
            raise RuntimeError("release-guard findings after manifest:\n" + "\n".join(findings))
        if check_only:
            return None
        if output is None:
            raise RuntimeError("--output is required unless --check-only is used")
        _zip(staging, output.resolve())
    return output.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a guarded source-only D3m0n ZIP")
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release", action="store_true", help="require a successful CI proof")
    parser.add_argument("--ci-proof", type=Path, help="successful CI proof JSON")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.output, release=args.release, ci_proof=args.ci_proof, check_only=args.check_only)
    except (OSError, RuntimeError) as exc:
        print(f"package-release: {exc}", file=sys.stderr)
        return 2
    print("package-release: source-only tree passed release guard")
    if result:
        print(f"package-release: wrote {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
