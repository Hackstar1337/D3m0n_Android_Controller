"""Safe, private backup and restore for native runtime state.

Backups are explicit secret-bearing artifacts.  Restore never writes into an
existing directory or the live checkout; it verifies a manifest and every file
hash in a temporary sibling before atomically creating a new target.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tarfile
from datetime import datetime, timezone
from typing import Any

from ops.platform_config import ensure_private_dir, ensure_private_file, state_dir

BACKUP_SCHEMA = 1
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 1_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_checkout(path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    path = path.resolve()
    if path == checkout or checkout in path.parents:
        raise ValueError("Runtime backup paths must be outside the source checkout")


def _files(source: Path) -> list[Path]:
    result: list[Path] = []
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Backup source contains unsupported symlink: {item}")
        if item.is_file():
            result.append(item)
    return sorted(result, key=lambda item: item.relative_to(source).as_posix())


def _manifest(source: Path, files: list[Path]) -> dict[str, Any]:
    total = 0
    entries: list[dict[str, Any]] = []
    for item in files:
        size = item.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"Backup file exceeds safety limit: {item.name}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Backup exceeds total size safety limit")
        entries.append({
            "path": item.relative_to(source).as_posix(),
            "size": size,
            "sha256": _sha256(item),
            "mode": item.stat().st_mode & 0o777,
        })
    return {
        "schema": BACKUP_SCHEMA,
        "created_at": _now(),
        "source_label": "private runtime state",
        "files": entries,
        "total_bytes": total,
    }


def _safe_relative(value: Any) -> Path:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8", "ignore")) > 4096
            or "\\" in value or "\x00" in value or ":" in value):
        raise ValueError("Backup manifest contains an invalid path")
    path = Path(value)
    if path.is_absolute() or "/" not in value and path.parts == ("",) or "." in path.parts or ".." in path.parts:
        raise ValueError("Backup manifest contains an unsafe path")
    if path.as_posix() != value or not path.parts:
        raise ValueError("Backup manifest contains a non-normalized path")
    return path


def validate_backup(archive: Path) -> dict[str, Any]:
    """Validate archive members and hashes without extracting into live state."""
    archive = Path(archive)
    if not archive.is_file():
        raise ValueError("Backup archive does not exist")
    try:
        with tarfile.open(archive, "r:gz") as handle:
            members = handle.getmembers()
            if len(members) > MAX_ENTRIES + 1:
                raise ValueError("Backup archive contains too many members")
            normalized_members: set[str] = set()
            for member in members:
                if member.name == "manifest.json":
                    normalized = member.name
                else:
                    relative = _safe_relative(member.name)
                    normalized = relative.as_posix()
                    if normalized != member.name:
                        raise ValueError("Backup archive contains a non-normalized path")
                normalized_key = normalized.casefold()
                if normalized_key in normalized_members:
                    raise ValueError("Backup archive contains duplicate normalized members")
                normalized_members.add(normalized_key)
                if not member.isfile() or member.issym() or member.islnk() or member.isdir():
                    raise ValueError(f"Backup member is not a regular file: {member.name}")
                limit = MAX_MANIFEST_BYTES if member.name == "manifest.json" else MAX_FILE_BYTES
                if member.size < 0 or member.size > limit:
                    raise ValueError(f"Backup member exceeds safety limit: {member.name}")
            manifest_member = next((member for member in members if member.name == "manifest.json"), None)
            if manifest_member is None or not manifest_member.isfile():
                raise ValueError("Backup manifest is missing")
            if manifest_member.size > MAX_MANIFEST_BYTES:
                raise ValueError("Backup manifest exceeds safety limit")
            raw = handle.extractfile(manifest_member)
            manifest = json.loads(raw.read().decode("utf-8")) if raw else None
            if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
                raise ValueError("Unsupported backup schema")
            files = manifest.get("files")
            if not isinstance(files, list) or len(files) > MAX_ENTRIES:
                raise ValueError("Backup manifest file list is invalid")
            expected = {"manifest.json"}
            manifest_paths: set[str] = set()
            total = 0
            for entry in files:
                if not isinstance(entry, dict):
                    raise ValueError("Backup manifest entry is invalid")
                relative = _safe_relative(entry.get("path"))
                name = relative.as_posix()
                if name.casefold() in manifest_paths or name.casefold() == "manifest.json":
                    raise ValueError("Backup manifest contains duplicate normalized entries")
                manifest_paths.add(name.casefold())
                expected.add(name)
                member = next((candidate for candidate in members if candidate.name == name), None)
                if member is None or not member.isfile():
                    raise ValueError(f"Backup member is missing: {name}")
                size_value = entry.get("size", -1)
                if isinstance(size_value, bool):
                    raise ValueError(f"Backup member size is invalid: {name}")
                size = int(size_value)
                if size < 0 or size > MAX_FILE_BYTES or member.size != size:
                    raise ValueError(f"Backup member size is invalid: {name}")
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("Backup exceeds total size safety limit")
                digest = entry.get("sha256")
                if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
                    raise ValueError(f"Backup member hash is invalid: {name}")
                stream = handle.extractfile(member)
                if stream is None:
                    raise ValueError(f"Backup member could not be read: {name}")
                actual_digest = hashlib.sha256()
                remaining = size
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError(f"Backup member is truncated: {name}")
                    actual_digest.update(block)
                    remaining -= len(block)
                if actual_digest.hexdigest() != digest.lower():
                    raise ValueError(f"Backup member hash verification failed: {name}")
            actual = {member.name for member in members}
            if actual != expected:
                raise ValueError("Backup contains unlisted or unsafe members")
            total_value = manifest.get("total_bytes")
            if isinstance(total_value, bool) or not isinstance(total_value, int) or total_value != total:
                raise ValueError("Backup total size does not match manifest")
            return manifest
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Backup archive is unreadable") from exc


def create_backup(source: Path | None = None, destination: Path | None = None) -> Path:
    """Create a gzip tar backup with a hash manifest and private permissions."""
    source = Path(source) if source else state_dir()
    source = source.resolve()
    if not source.is_dir():
        raise ValueError("Backup source state directory does not exist")
    _reject_checkout(source)
    files = _files(source)
    manifest = _manifest(source, files)
    destination = Path(destination) if destination else source.parent / f"d3m0n-state-{secrets.token_hex(8)}.tar.gz"
    destination = destination.resolve()
    _reject_checkout(destination.parent)
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
    try:
        with tarfile.open(temporary, "w:gz") as handle:
            manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_bytes)
            info.mode = 0o600
            info.mtime = 0
            import io
            handle.addfile(info, io.BytesIO(manifest_bytes))
            for item in files:
                relative = item.relative_to(source).as_posix()
                info = tarfile.TarInfo(relative)
                info.size = item.stat().st_size
                info.mode = item.stat().st_mode & 0o600
                info.mtime = 0
                with item.open("rb") as file_handle:
                    handle.addfile(info, file_handle)
        os.replace(temporary, destination)
        ensure_private_file(destination)
        validate_backup(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup(archive: Path, target: Path) -> Path:
    """Restore only into a new, isolated target directory after full validation."""
    archive = Path(archive).resolve()
    target = Path(target).resolve()
    _reject_checkout(target)
    if target.exists():
        raise FileExistsError("Restore target must be a new directory; refusing to overwrite existing state")
    manifest = validate_backup(archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.restore-{secrets.token_hex(8)}"
    ensure_private_dir(temporary)
    try:
        with tarfile.open(archive, "r:gz") as handle:
            for entry in manifest["files"]:
                relative = _safe_relative(entry["path"])
                destination = temporary / relative
                ensure_private_dir(destination.parent)
                member = handle.getmember(relative.as_posix())
                stream = handle.extractfile(member)
                if stream is None:
                    raise ValueError(f"Backup member could not be read: {relative}")
                with destination.open("xb") as output:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                ensure_private_file(destination)
                if destination.stat().st_size != entry["size"] or _sha256(destination) != entry["sha256"]:
                    raise ValueError(f"Backup hash verification failed: {relative}")
            if target.exists():
                raise FileExistsError("Restore target appeared during restore; no existing state was changed")
            os.replace(temporary, target)
            ensure_private_dir(target)
            return target
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def backup_and_restore(source: Path, archive: Path, target: Path) -> Path:
    """Convenience API used by tests and migration tooling."""
    create_backup(source, archive)
    return restore_backup(archive, target)
