"""Fail-closed configuration for the Mobilerun worker boundary.

The parent may choose a logical backend, never a command, executable, path, or
argument vector.  This module is intentionally stdlib-only so it is usable by
install/doctor tooling before the application environment is complete.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

PINNED_MOBILERUN_COMMIT = "301856138c6edeb2ae692bfab85b279ace520e59"
PINNED_MOBILERUN_VERSION = "0.7.0"
LINUX_HOST_ISOLATION = "linux-host-isolation"
WSL2_HOST_ISOLATION = "wsl2-host-isolation"
DISABLED = "disabled"
TEST_FALLBACK = "test-only-subprocess"
_ALLOWED_MODES = frozenset({DISABLED, LINUX_HOST_ISOLATION, WSL2_HOST_ISOLATION, TEST_FALLBACK})
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,62}$")
_ABS_POSIX = re.compile(r"^/[A-Za-z0-9._/@+:%=-]+$")
FIXED_PREFIX = "/opt/d3m0n-ai-worker"
FIXED_SOCKET_PATH = "/run/d3m0n-ai-worker.sock"
FIXED_WRAPPER_PATH = "/usr/local/libexec/d3m0n-ai-worker"
FIXED_CLIENT_PATH = "/usr/local/libexec/d3m0n-ai-worker-client"
FIXED_SOURCE_ROOT = "/opt/d3m0n-ai-worker/source"
FIXED_SDK_ROOT = "/opt/d3m0n-ai-worker/sdk"
FIXED_VENV_ROOT = "/opt/d3m0n-ai-worker/venv"
FIXED_CONFIG_PATH = "/etc/d3m0n-ai-worker/runtime.json"


class IsolationConfigError(ValueError):
    """A generated isolation configuration is missing or unsafe."""


def _absolute(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or not value.startswith("/"):
        raise IsolationConfigError(f"{field} must be an absolute POSIX path")
    path = Path(value)
    if "\x00" in value or any(part in {".", ".."} for part in path.parts):
        raise IsolationConfigError(f"{field} contains traversal")
    return value


def _name(value: str, field: str, *, nonroot: bool = False) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value) or (nonroot and value == "root"):
        raise IsolationConfigError(f"{field} is not a valid {'non-root ' if nonroot else ''}account name")
    return value


@dataclass(frozen=True)
class WorkerIsolationConfig:
    """Root-owned/generated settings shared by parent and isolated Linux host."""

    mode: str = DISABLED
    prefix: str = "/opt/d3m0n-ai-worker"
    socket_path: str = "/run/d3m0n-ai-worker.sock"
    wrapper_path: str = "/usr/local/libexec/d3m0n-ai-worker"
    client_path: str = "/usr/local/libexec/d3m0n-ai-worker-client"
    source_root: str = "/opt/d3m0n-ai-worker/source"
    sdk_root: str = "/opt/d3m0n-ai-worker/sdk"
    venv_root: str = "/opt/d3m0n-ai-worker/venv"
    wsl_distro: str = "D3m0nFleet"
    worker_user: str = "ash"
    worker_group: str = "ash"
    sdk_commit: str = PINNED_MOBILERUN_COMMIT
    sdk_version: str = PINNED_MOBILERUN_VERSION
    config_path: str = "/etc/d3m0n-ai-worker/runtime.json"

    def validate(self) -> "WorkerIsolationConfig":
        if self.mode not in _ALLOWED_MODES:
            raise IsolationConfigError("mode must be disabled, linux-host-isolation, wsl2-host-isolation, or test-only-subprocess")
        fixed_paths = {
            "prefix": FIXED_PREFIX,
            "socket_path": FIXED_SOCKET_PATH,
            "wrapper_path": FIXED_WRAPPER_PATH,
            "client_path": FIXED_CLIENT_PATH,
            "source_root": FIXED_SOURCE_ROOT,
            "sdk_root": FIXED_SDK_ROOT,
            "venv_root": FIXED_VENV_ROOT,
            "config_path": FIXED_CONFIG_PATH,
        }
        for field in fixed_paths:
            _absolute(getattr(self, field), field)
            if getattr(self, field) != fixed_paths[field]:
                raise IsolationConfigError(f"{field} must use the reviewed fixed path")
        for field in ("wsl_distro", "worker_user", "worker_group"):
            _name(getattr(self, field), field, nonroot=field == "worker_user")
        if self.worker_group == "root":
            raise IsolationConfigError("worker_group must be a dedicated non-root group")
        if not re.fullmatch(r"[0-9a-f]{40}", self.sdk_commit):
            raise IsolationConfigError("sdk_commit must be a full lowercase commit hash")
        if self.sdk_commit != PINNED_MOBILERUN_COMMIT or self.sdk_version != PINNED_MOBILERUN_VERSION:
            raise IsolationConfigError("Mobilerun SDK identity is not the reviewed pin")
        if self.mode == WSL2_HOST_ISOLATION and self.worker_user == "root":
            raise IsolationConfigError("WSL worker may not run as root")
        return self

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "WorkerIsolationConfig":
        allowed = {field for field in cls.__dataclass_fields__}
        unknown = set(values) - allowed
        if unknown:
            raise IsolationConfigError("unknown isolation configuration field")
        return cls(**{key: values[key] for key in values}).validate()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "WorkerIsolationConfig":
        candidate = Path(path)
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IsolationConfigError("isolation configuration is unavailable or invalid") from exc
        if not isinstance(data, dict):
            raise IsolationConfigError("isolation configuration must be an object")
        config = cls.from_mapping(data)
        if config.config_path != str(candidate):
            raise IsolationConfigError("config_path does not match the loaded root-owned config")
        return config

    def to_json(self) -> str:
        self.validate()
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def command(self, *, probe: bool = False) -> list[str]:
        """Return one fixed command vector; callers cannot supply argv."""
        self.validate()
        suffix = ["--probe"] if probe else []
        if self.mode == LINUX_HOST_ISOLATION:
            # Existing Linux installs may keep the root wrapper; the direct
            # client is the preferred socket path for an unprivileged parent.
            return [self.client_path, *suffix]
        if self.mode == WSL2_HOST_ISOLATION:
            return ["wsl.exe", "-d", self.wsl_distro, "-u", self.worker_user, "--exec", self.client_path, *suffix]
        if self.mode == TEST_FALLBACK:
            raise IsolationConfigError("test-only mode is resolved only with an explicit test flag")
        raise IsolationConfigError("worker isolation is disabled")

    def static_isolation_assertions(self) -> dict[str, Any]:
        self.validate()
        return {
            "mode": self.mode,
            "socket": {"path": self.socket_path, "family": "AF_UNIX", "network": "private"},
            "identity": {"user": self.worker_user, "group": self.worker_group, "nonroot_required": True},
            "readonly": {"source": self.source_root, "sdk": self.sdk_root, "venv": self.venv_root},
            "sdk": {"commit": self.sdk_commit, "version": self.sdk_version},
        }


def config_from_environment(environ: Mapping[str, str] | None = None) -> WorkerIsolationConfig:
    """Load only the generated config path; raw launcher environment is ignored."""
    source = os.environ if environ is None else environ
    path = source.get("D3M0N_AI_WORKER_CONFIG", "").strip()
    if not path:
        return WorkerIsolationConfig()
    return WorkerIsolationConfig.load(path)


def allow_test_fallback(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get("D3M0N_ALLOW_TEST_WORKER_FALLBACK", "") == "1"


def build_manifest(source_root: str | os.PathLike[str], relative_paths: Iterable[str], output: str | os.PathLike[str]) -> Path:
    """Write deterministic ``path sha256`` entries for a frozen source subset."""
    root = Path(source_root).resolve()
    rows: list[str] = []
    seen: set[str] = set()
    for relative in relative_paths:
        rel = Path(relative).as_posix()
        if rel in seen or rel.startswith("../") or "/../" in rel:
            raise IsolationConfigError("manifest path escapes source root")
        seen.add(rel)
        candidate = (root / rel).resolve()
        if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
            raise IsolationConfigError(f"manifest source is missing or unsafe: {rel}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        rows.append(f"{rel} {digest}")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(sorted(rows)) + ("\n" if rows else ""), encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--json"]:
        print(WorkerIsolationConfig().to_json(), end="")
        return 0
    if args == ["--manifest", "--help"]:
        print("use build_manifest(source_root, relative_paths, output)")
        return 0
    print("worker isolation helper: no activation or installation is performed", file=sys.stderr)
    return 2


__all__ = [
    "DISABLED", "LINUX_HOST_ISOLATION", "WSL2_HOST_ISOLATION", "TEST_FALLBACK",
    "PINNED_MOBILERUN_COMMIT", "PINNED_MOBILERUN_VERSION", "IsolationConfigError",
    "WorkerIsolationConfig", "allow_test_fallback", "build_manifest", "config_from_environment",
]

if __name__ == "__main__":
    raise SystemExit(main())
