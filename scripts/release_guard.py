"""Deterministic source-export privacy guard.

The guard intentionally emits only ``path:line:category``.  It never prints a
matched value, file contents, environment values, or runtime state.  Run it
against the source checkout or a clean export; generated trees are findings, not
silently skipped inputs.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REQUIRED_FILES = (
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
REQUIRED_DIRS = ("gitd", "ops", "ai_contracts", "frontend", "tests")
FORBIDDEN_DIRS = {
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "dist", "build", "coverage", "data", "uploads", "runs", "logs",
    "recordings", "screenshots", "backups", ".work",
}
FORBIDDEN_SUFFIXES = {
    ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
    ".wal", ".shm", ".apk", ".aab", ".ipa", ".mp4", ".mov", ".webm", ".xml",
}
FORBIDDEN_NAMES = {
    ".env", "dashboard-auth.json", "settings.key", "settings.enc", "fleet.sqlite3",
}
KNOWN_DEPLOYMENT_HOSTS = ("deployment" + "-host.invalid",)
PRIVATE_PATH_RE = re.compile(r"(?<![\w/])(?:[A-Za-z]:[\\/]+Users[\\/][A-Za-z0-9_.-]+|/" + r"home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+)")
PRIVATE_IP_RE = re.compile(r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?![\w./])")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
SECRET_LITERAL_RE = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b"
    r"\s*[:=]\s*[\"'](?!\s*(?:$|\{|<|your[_-]|change[_-]|replace[_-]|example|placeholder|none|empty))"
    r"[^\"']{4,}[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.category}"


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        rel = _relative(root, path)
        parts = set(Path(rel).parts)
        if path.is_dir():
            if path.name in FORBIDDEN_DIRS or any(part in FORBIDDEN_DIRS for part in Path(rel).parts):
                findings.append(Finding(rel, 0, "forbidden-runtime-directory"))
            continue
        if path.name in FORBIDDEN_NAMES or path.name.endswith(".egg-info"):
            findings.append(Finding(rel, 0, "forbidden-runtime-file"))
        if path.suffix.lower() in FORBIDDEN_SUFFIXES and not (path.suffix.lower() == ".xml" and rel.startswith("portal/app/src/main/")):
            findings.append(Finding(rel, 0, "forbidden-runtime-suffix"))
        if any(part in FORBIDDEN_DIRS for part in Path(rel).parts):
            findings.append(Finding(rel, 0, "file-under-forbidden-runtime-directory"))
    return findings


def _is_env_example(path: Path) -> bool:
    return path.name == ".env.example"


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in {
        ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".vue", ".md", ".txt", ".json", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".cmd", ".bat", ".yaml", ".yml", ".env"
    } or path.name in {"LICENSE", "SECURITY.md", "CONTRIBUTING.md"}


def _looks_reserved_documentation(line: str) -> bool:
    lowered = line.lower()
    # /home/droid is the fixed non-secret UID home inside the Docker image, not
    # a developer workstation path. Keep it separate from host-user path checks.
    return "/home/droid" in lowered or any(token in lowered for token in ("example.com", "example.org", "example.net", "192.0.2.", "198.51.100.", "203.0.113.", "<user>", "<your-", "placeholder"))


def _content_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if not path.is_file() or not _is_text(path) or _is_env_example(path):
            continue
        rel = _relative(root, path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            findings.append(Finding(rel, 0, "unreadable-source-file"))
            continue
        for number, line in enumerate(lines, 1):
            if PRIVATE_KEY_RE.search(line):
                findings.append(Finding(rel, number, "private-key-material"))
            if SECRET_LITERAL_RE.search(line):
                findings.append(Finding(rel, number, "secret-like-literal"))
            if PRIVATE_PATH_RE.search(line) and not _looks_reserved_documentation(line):
                findings.append(Finding(rel, number, "machine-specific-user-path"))
            if PRIVATE_IP_RE.search(line) and not _looks_reserved_documentation(line) and not (rel.startswith(("ops/tests/", "tests/")) or rel.startswith("frontend/test_")):
                findings.append(Finding(rel, number, "private-ip-literal"))
            if any(host in line.lower() for host in KNOWN_DEPLOYMENT_HOSTS):
                findings.append(Finding(rel, number, "known-deployment-host"))
    return findings


def _required_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            findings.append(Finding(name, 0, "required-source-missing"))
    for name in REQUIRED_DIRS:
        if not (root / name).is_dir():
            findings.append(Finding(name, 0, "required-source-directory-missing"))
    return findings


def scan_tree(root: Path | str) -> list[Finding]:
    """Scan a candidate deterministically and return redacted findings."""
    root = Path(root).resolve()
    if not root.is_dir():
        return [Finding(str(root), 0, "candidate-root-missing")]
    findings = _required_findings(root) + _path_findings(root) + _content_findings(root)
    unique = {(item.path, item.line, item.category): item for item in findings}
    return [unique[key] for key in sorted(unique, key=lambda value: (value[0].lower(), value[1], value[2]))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a source export for runtime data and private values")
    parser.add_argument("root", nargs="?", default=".", help="source checkout or clean export root")
    args = parser.parse_args(argv)
    findings = scan_tree(args.root)
    for finding in findings:
        print(finding.render())
    if findings:
        print(f"release-guard: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("release-guard: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
