"""Private, resumable native-install state and staging primitives.

This module deliberately uses only the standard library.  It records hashes and
output validation rather than trusting a completion flag, writes diagnostics
outside the checkout, and promotes built directories only after validation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import subprocess
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ops.platform_config import atomic_write_json, ensure_private_dir, ensure_private_file, state_dir

STATE_SCHEMA = 2
STAGES = (
    "prerequisites",
    "backend_dependencies",
    "frontend_dependencies",
    "frontend_build",
    "authentication",
    "readiness",
)
_MAX_LOG_LINE = 64 * 1024
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|credential|authorization)"
    r"([\s:=]+)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(://)[^/\s:@]+:[^@\s]+@")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_paths(paths: Iterable[Path], *, root: Path | None = None) -> str:
    """Hash named files and their contents in deterministic order."""
    root = Path(root).resolve() if root else None
    entries: list[tuple[str, Path]] = []
    for raw in paths:
        path = Path(raw)
        name = str(path.resolve().relative_to(root)) if root else str(path.resolve())
        entries.append((name.replace("\\", "/"), path))
    digest = hashlib.sha256()
    for name, path in sorted(entries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            digest.update(b"MISSING\0")
            continue
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def fingerprint_tree(root: Path, *, exclude: Iterable[str] = ()) -> str:
    """Hash a source tree without following symlinked files or directories."""
    root = Path(root).resolve()
    excluded = {str(item).replace("\\", "/").strip("/") for item in exclude}
    files: list[Path] = []
    if not root.exists():
        return fingerprint_paths([root])
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in excluded):
            continue
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return fingerprint_paths(files, root=root)


def redact_text(value: Any) -> str:
    """Redact values that look like credentials before they reach a private log."""
    text = str(value)
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[:_MAX_LOG_LINE]


class StageLogger:
    """Append-only private JSON-lines logger for one installation stage."""

    def __init__(self, directory: Path, stage: str):
        if stage not in STAGES:
            raise ValueError(f"Unknown installation stage: {stage}")
        self.directory = ensure_private_dir(Path(directory))
        self.path = self.directory / f"install-{stage}.log"
        self._handle = self.path.open("a", encoding="utf-8")
        ensure_private_file(self.path)

    def write(self, message: Any, *, level: str = "info", **fields: Any) -> None:
        record: dict[str, Any] = {"time": utc_now(), "level": level, "message": redact_text(message)}
        for key, value in fields.items():
            record[key] = redact_text(value) if isinstance(value, (str, bytes)) else value
        self._handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        self._handle.flush()
        try:
            os.fsync(self._handle.fileno())
        except OSError:
            pass

    def command_output(self, output: str, *, exit_code: int) -> None:
        self.write("command completed", exit_code=exit_code)
        for line in output.splitlines():
            self.write(line, level="command")

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "StageLogger":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def output_records(paths: Iterable[Path], *, root: Path | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        # POSIX virtualenv bin/python is normally a symlink outside the checkout.
        # Keep its lexical checkout location, but hash the actual interpreter bytes.
        named_path = Path(os.path.abspath(path))
        recorded_path = str(named_path.relative_to(root.resolve())) if root else str(named_path)
        if path.is_file():
            records.append({
                "path": recorded_path,
                "kind": "file",
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            })
        elif path.is_dir():
            records.append({
                "path": recorded_path,
                "kind": "directory",
                "fingerprint": fingerprint_tree(path),
            })
        else:
            records.append({"path": recorded_path, "kind": "missing"})
    return records


def outputs_valid(outputs: Iterable[Mapping[str, Any]], *, root: Path | None = None) -> bool:
    for record in outputs:
        try:
            path = Path(str(record["path"]))
            if root and not path.is_absolute():
                path = root / path
            if record.get("kind") == "file":
                if not path.is_file() or sha256_file(path) != record.get("sha256"):
                    return False
            elif record.get("kind") == "directory":
                if not path.is_dir() or fingerprint_tree(path) != record.get("fingerprint"):
                    return False
            else:
                return False
        except (KeyError, OSError, TypeError, ValueError):
            return False
    return True


def new_install_state(repo: Path, *, provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "repo": str(Path(repo).resolve()),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "stages": {},
        "provenance": dict(provenance or {}),
    }


def load_install_state(path: Path | None = None, *, expected_repo: Path | None = None) -> dict[str, Any] | None:
    path = Path(path) if path else state_dir() / "launcher" / "install-state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
            return None
        if not isinstance(value.get("repo"), str) or not isinstance(value.get("stages"), dict):
            return None
        if expected_repo and Path(value["repo"]).resolve() != Path(expected_repo).resolve():
            return None
        if any(stage not in STAGES for stage in value["stages"]):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def save_install_state(state: Mapping[str, Any], path: Path | None = None) -> Path:
    path = Path(path) if path else state_dir() / "launcher" / "install-state.json"
    value = dict(state)
    value["schema"] = STATE_SCHEMA
    value["updated_at"] = utc_now()
    atomic_write_json(path, value)
    return path


def stage_complete(state: Mapping[str, Any], stage: str, fingerprint: str, *, root: Path | None = None) -> bool:
    record = state.get("stages", {}).get(stage)
    return bool(
        isinstance(record, Mapping)
        and record.get("fingerprint") == fingerprint
        and record.get("completed_at")
        and outputs_valid(record.get("outputs", ()), root=root)
    )


def mark_stage(
    state: dict[str, Any],
    stage: str,
    fingerprint: str,
    *,
    outputs: Iterable[Path] = (),
    root: Path | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"Unknown installation stage: {stage}")
    state.setdefault("stages", {})[stage] = {
        "fingerprint": fingerprint,
        "completed_at": utc_now(),
        "outputs": output_records(outputs, root=root),
        "log": str(log_path) if log_path else None,
    }
    state["updated_at"] = utc_now()
    return state


def _safe_marker(path: Path, expected_root: Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        keys = {"destination", "backup", "staging"}
        if set(value) & keys != keys:
            return None
        result = {key: str(Path(value[key]).resolve()) for key in keys}
        if any(Path(item).parent != expected_root.resolve() for item in (result["destination"], result["backup"])):
            return None
        return result
    except (OSError, ValueError, TypeError):
        return None


def recover_atomic_promotion(state_dir_path: Path, *, destination: Path) -> bool:
    """Recover only a promotion transaction created by this module."""
    marker = Path(state_dir_path) / "frontend-promotion.json"
    if not marker.is_file():
        return False
    info = _safe_marker(marker, Path(destination).resolve().parent)
    if not info or Path(info["destination"]) != Path(destination).resolve():
        return False
    dest, backup = Path(info["destination"]), Path(info["backup"])
    try:
        if not dest.exists() and backup.exists():
            os.replace(backup, dest)
        elif dest.exists() and backup.exists():
            shutil.rmtree(backup)
        marker.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def atomic_promote_directory(staging: Path, destination: Path, state_dir_path: Path) -> None:
    """Promote a validated directory with a recoverable old-artifact fallback."""
    staging, destination, state_dir_path = Path(staging), Path(destination), Path(state_dir_path)
    if not staging.is_dir() or not (staging / "index.html").is_file():
        raise ValueError("Refusing to promote an invalid frontend artifact")
    ensure_private_dir(state_dir_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".d3m0n-dist-backup-{secrets.token_hex(8)}"
    marker = state_dir_path / "frontend-promotion.json"
    atomic_write_json(marker, {"destination": str(destination.resolve()), "backup": str(backup), "staging": str(staging.resolve()), "created_at": utc_now()})
    try:
        if destination.exists():
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup.exists():
            shutil.rmtree(backup)
        marker.unlink(missing_ok=True)
    except BaseException:
        try:
            if destination.exists() and backup.exists():
                shutil.rmtree(destination)
            if not destination.exists() and backup.exists():
                os.replace(backup, destination)
        finally:
            marker.unlink(missing_ok=True)
        raise


def source_revision(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        value = result.stdout.strip()
        return value if re.fullmatch(r"[0-9a-fA-F]{40}", value) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
