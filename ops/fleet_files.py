"""Stage 2 private fleet file staging and transfer jobs.

This module intentionally owns all file-transfer state.  It uses the stage-one
``fleet_control`` helpers for origin validation, target expansion and injectable
ADB execution; no command is run at import time.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import uuid
import zipfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, field_validator

try:  # deployed package layout
    from ops import fleet_control as fleet
except ImportError:  # direct unit-test layout
    import fleet_control as fleet  # type: ignore[no-redef]

router = APIRouter(prefix="/api/fleet", tags=["fleet-files"])

MAX_FILE_SIZE = 256 * 1024 * 1024
MAX_STAGED_BYTES = 1024 * 1024 * 1024
MAX_ACTIVE_JOBS = 4
TRANSFER_CONCURRENCY = 3
INSTALL_CONCURRENCY = 2
TRANSFER_TIMEOUT = 300.0
INSTALL_TIMEOUT = 180.0
DOWNLOAD_TTL = 24 * 60 * 60
MAX_REMOTE_PATH = 4096
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NAME_RE = re.compile(r"^[^\x00\r\n]{1,255}$")

_job_tasks: dict[str, asyncio.Task[Any]] = {}
_job_cancel: set[str] = set()
_active_jobs = 0
_runtime_lock: asyncio.Lock | None = None
_transfer_sem: asyncio.Semaphore | None = None
_install_sem: asyncio.Semaphore | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _state_root() -> Path:
    root = fleet._state_db_path().parent / "files"
    from ops.platform_config import ensure_private_dir
    ensure_private_dir(root)
    for child in ("blobs", "meta", "jobs", "downloads"):
        path = root / child
        ensure_private_dir(path)
    return root


def _private(path: Path, mode: int = 0o600) -> None:
    from ops.platform_config import ensure_private_file
    ensure_private_file(path)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        _private(temporary)
        os.replace(temporary, path)
        _private(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise HTTPException(status_code=404, detail=f"Unknown {label}")
    return value


def _meta_path(file_id: str) -> Path:
    return _state_root() / "meta" / f"{_safe_id(file_id, 'file')}.json"


def _job_path(job_id: str) -> Path:
    return _state_root() / "jobs" / f"{_safe_id(job_id, 'job')}.json"


def _download_meta_path(download_id: str) -> Path:
    return _state_root() / "downloads" / f"{_safe_id(download_id, 'download')}.json"


def _load_file(file_id: str) -> dict[str, Any] | None:
    try:
        value = _read_json(_meta_path(file_id))
    except HTTPException:
        return None
    if not value or value.get("id") != file_id or value.get("kind") != "file":
        return None
    try:
        _trusted_blob_path(file_id)
    except OSError:
        return None
    return value


def _blob_path(file_id: str) -> Path:
    return _state_root() / "blobs" / f"{_safe_id(file_id, 'file')}.bin"


def _trusted_blob_path(file_id: str) -> Path:
    blob = _blob_path(file_id)
    if blob.is_symlink() or blob.resolve().parent != (_state_root() / "blobs").resolve() or not blob.is_file():
        raise OSError("staged file is unavailable")
    return blob


def _sanitize_upload_name(raw: str | None) -> str:
    if not isinstance(raw, str) or not raw:
        raise HTTPException(status_code=422, detail="A filename is required")
    # Browsers can submit either slash convention.  A transfer filename is a
    # basename only; silently retaining a path would make it a server path.
    if "\x00" in raw or "\r" in raw or "\n" in raw:
        raise HTTPException(status_code=422, detail="Filename contains forbidden characters")
    if "/" in raw or "\\" in raw or raw in {".", ".."} or any(part == ".." for part in re.split(r"[/\\]", raw)):
        raise HTTPException(status_code=422, detail="Filename path traversal is not allowed")
    name = raw.strip()
    if not name or name in {".", ".."} or not _NAME_RE.fullmatch(name):
        raise HTTPException(status_code=422, detail="Invalid filename")
    # Avoid Windows device names and names which become hidden control files.
    stem = name.split(".", 1)[0].rstrip(" .").casefold()
    if stem in {"con", "prn", "aux", "nul"} or stem.startswith(("com", "lpt")) and stem[3:].isdigit():
        raise HTTPException(status_code=422, detail="Invalid filename")
    return name


def _is_standalone_apk(name: str, path: Path) -> bool:
    if not name.casefold().endswith(".apk"):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return "AndroidManifest.xml" in archive.namelist()
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def _cleanup_stale() -> None:
    """Expire temporary server artifacts, never active refs or phone-side files."""
    root = _state_root()
    cutoff = time.time() - DOWNLOAD_TTL
    active_files = set()
    for job_path in (root / "jobs").glob("*.json"):
        job = _read_json(job_path)
        if job and job.get("status") in {"queued", "running"}:
            active_files.add(job.get("file_id"))
    for path in (root / "meta").glob("*.json"):
        item = _read_json(path)
        if not item or item.get("id") in active_files:
            continue
        try:
            created = datetime.fromisoformat(item.get("created_at", "")).timestamp()
        except (TypeError, ValueError):
            continue
        if created < cutoff:
            with suppress(OSError, HTTPException):
                _blob_path(item["id"]).unlink()
            with suppress(OSError):
                path.unlink()
    for path in (root / "downloads").glob("*.json"):
        item = _read_json(path)
        if not item:
            continue
        created = item.get("created_at_epoch", 0)
        if isinstance(created, (int, float)) and created < cutoff:
            download_id = item.get("id")
            stored = item.get("path")
            if isinstance(stored, str):
                candidate = Path(stored)
                with suppress(OSError):
                    if candidate.is_file() and candidate.resolve().parent == (root / "downloads").resolve():
                        candidate.unlink()
            with suppress(OSError):
                path.unlink()


def _load_job(job_id: str) -> dict[str, Any] | None:
    return _read_json(_job_path(job_id))


def _save_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _utc_now()
    _atomic_json(_job_path(job["id"]), job)


def _restore_jobs() -> None:
    root = _state_root() / "jobs"
    for path in root.glob("*.json"):
        if path.stem in _job_tasks:
            continue
        job = _read_json(path)
        if not job or job.get("status") not in {"running", "queued"}:
            continue
        job["status"] = "interrupted"
        now = _utc_now()
        job["updated_at"] = now
        for result in job.get("results", []):
            if result.get("status") in {"queued", "running"}:
                result["status"] = "failed"
                result["ok"] = False
                result["error"] = "Interrupted when the file service restarted"
        progress = job.get("progress")
        if isinstance(progress, dict):
            progress["done"] = sum(r.get("status") in {"completed", "failed", "cancelled"} for r in job.get("results", []))
        _atomic_json(path, job)


def _ensure_runtime() -> None:
    global _runtime_lock, _transfer_sem, _install_sem
    _cleanup_stale()
    _restore_jobs()
    if _runtime_lock is None:
        _runtime_lock = asyncio.Lock()
    if _transfer_sem is None:
        _transfer_sem = asyncio.Semaphore(TRANSFER_CONCURRENCY)
    if _install_sem is None:
        _install_sem = asyncio.Semaphore(INSTALL_CONCURRENCY)


def _validation(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


class FileJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: dict[str, Any]
    operation: str
    file_id: str | None = None
    remote_path: str | None = None
    confirm_install: bool = False
    replace: bool = False

    @field_validator("operation")
    @classmethod
    def valid_operation(cls, value: str) -> str:
        if value not in {"push", "pull", "install"}:
            raise ValueError("operation must be push, pull, or install")
        return value


def _validate_remote_path(raw: str, *, destination: bool) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_REMOTE_PATH:
        raise _validation("remote_path is required and too long")
    if "\x00" in raw or "\r" in raw or "\n" in raw or "\\" in raw:
        raise _validation("remote_path contains forbidden characters")
    if not (raw == "/sdcard" or raw.startswith("/sdcard/") or raw == "/storage/emulated/0" or raw.startswith("/storage/emulated/0/")):
        raise _validation("remote_path must be in shared storage")
    if any(part == ".." for part in raw.split("/")):
        raise _validation("remote_path traversal is not allowed")
    if destination and raw.rstrip("/") in {"/sdcard", "/storage/emulated/0"}:
        return raw.rstrip("/") or raw
    return raw.rstrip("/") or raw


def _safe_remote_output(output: str) -> str:
    value = output.strip().splitlines()
    return value[-1].strip() if value else ""


def _canonical_shared(path: str) -> bool:
    return path == "/sdcard" or path.startswith("/sdcard/") or path == "/storage/emulated/0" or path.startswith("/storage/emulated/0/")


def _shell(command: str) -> str:
    # This is passed as one adb shell argument. Every user path must already be
    # protected with shlex.quote before it reaches this helper.
    return command


async def _resolve_remote(serial: str, remote_path: str) -> str:
    output = await fleet.run_adb("adb", "-s", serial, "shell", _shell("realpath " + shlex.quote(remote_path)), timeout=TRANSFER_TIMEOUT)
    canonical = _safe_remote_output(output)
    if not canonical or not _canonical_shared(canonical) or any(part == ".." for part in canonical.split("/")):
        raise RuntimeError("Remote path resolves outside shared storage")
    return canonical


async def _resolve_existing_parent(serial: str, remote_path: str) -> str:
    """Resolve the nearest existing parent, validating every returned path."""
    candidate = remote_path.rstrip("/") or remote_path
    while candidate not in {"/sdcard", "/storage/emulated/0", "/"}:
        try:
            return await _resolve_remote(serial, candidate)
        except fleet.AdbCommandError:
            parent = candidate.rsplit("/", 1)[0] or "/"
            if parent == candidate:
                break
            candidate = parent
    if candidate in {"/sdcard", "/storage/emulated/0"}:
        return candidate
    raise RuntimeError("Remote destination parent is unavailable")


async def _remote_size(serial: str, remote_path: str) -> int:
    output = await fleet.run_adb("adb", "-s", serial, "shell", _shell("stat -c %s -- " + shlex.quote(remote_path)), timeout=TRANSFER_TIMEOUT)
    try:
        size = int(_safe_remote_output(output))
    except ValueError as exc:
        raise RuntimeError("Unable to determine remote file size") from exc
    if size < 0 or size > MAX_FILE_SIZE:
        raise RuntimeError("Remote file exceeds the 256 MiB limit")
    return size


async def _push_one(serial: str, file: dict[str, Any], remote_path: str, replace: bool) -> tuple[int | None, str | None]:
    # Resolve the existing parent first; mkdir is only attempted after that
    # parent is known to remain in Android shared storage.
    await _resolve_existing_parent(serial, remote_path)
    await fleet.run_adb("adb", "-s", serial, "shell", _shell("mkdir -p -- " + shlex.quote(remote_path)), timeout=TRANSFER_TIMEOUT)
    destination = await _resolve_remote(serial, remote_path)
    if not _canonical_shared(destination):
        raise RuntimeError("Remote path resolves outside shared storage")
    target = destination.rstrip("/") + "/" + file["name"]
    await fleet.run_adb("adb", "-s", serial, "push", str(_trusted_blob_path(file["id"])), target, timeout=TRANSFER_TIMEOUT)
    return file["size"], None


async def _pull_one(serial: str, file_job: dict[str, Any], remote_path: str) -> tuple[int | None, dict[str, str] | None]:
    canonical = await _resolve_remote(serial, remote_path)
    await fleet.run_adb("adb", "-s", serial, "shell", _shell("test -f " + shlex.quote(canonical)), timeout=TRANSFER_TIMEOUT)
    size = await _remote_size(serial, canonical)
    download_id = uuid.uuid4().hex
    name = _sanitize_upload_name(Path(canonical).name or "download.bin")
    destination = _state_root() / "downloads" / f"{download_id}.bin"
    temporary = destination.with_suffix(".part")
    try:
        await fleet.run_adb("adb", "-s", serial, "pull", canonical, str(temporary), timeout=TRANSFER_TIMEOUT)
        if not temporary.is_file() or temporary.stat().st_size > MAX_FILE_SIZE:
            raise RuntimeError("Downloaded file exceeds the 256 MiB limit")
        if temporary.stat().st_size != size:
            raise RuntimeError("Downloaded file size changed during transfer")
        os.replace(temporary, destination)
        _private(destination)
        metadata = {"id": download_id, "path": str(destination), "filename": name, "created_at_epoch": time.time()}
        _atomic_json(_download_meta_path(download_id), metadata)
    finally:
        with suppress(OSError):
            temporary.unlink()
    return size, {"download_id": download_id, "filename": name}


async def _install_one(serial: str, file: dict[str, Any], replace: bool) -> tuple[int | None, str | None]:
    source = _trusted_blob_path(file["id"])
    install_path = _state_root() / "blobs" / (uuid.uuid4().hex + ".apk")
    os.link(source, install_path)
    _private(install_path)
    try:
        argv = ["adb", "-s", serial, "install"]
        if replace:
            argv.append("-r")
        argv.append(str(install_path))
        output = await fleet.run_adb(*argv, timeout=INSTALL_TIMEOUT)
        if re.search(r"\bfailure\b", output or "", re.IGNORECASE) or not re.search(r"\bsuccess\b", output or "", re.IGNORECASE):
            raise RuntimeError("ADB did not report successful installation")
    finally:
        with suppress(OSError):
            install_path.unlink()
    return file["size"], None


async def _run_device(job: dict[str, Any], result: dict[str, Any], file: dict[str, Any] | None, replace: bool = False) -> None:
    serial = result["device"]
    if job["id"] in _job_cancel:
        result["status"] = "cancelled"
        result["ok"] = False
        result["error"] = "Job cancelled"
        return
    result["status"] = "running"
    _save_job(job)
    started = time.monotonic()
    try:
        operation = job["operation"]
        if operation == "push":
            if file is None:
                raise RuntimeError("Staged file not found")
            async with _transfer_sem:  # type: ignore[misc]
                bytes_done, _ = await _push_one(serial, file, job["remote_path"], replace)
            result["bytes"] = bytes_done
        elif operation == "pull":
            async with _transfer_sem:  # type: ignore[misc]
                bytes_done, download = await _pull_one(serial, job, job["remote_path"])
            result["bytes"] = bytes_done
            if download:
                result.update(download)
        else:
            if file is None:
                raise RuntimeError("Staged file not found")
            async with _install_sem:  # type: ignore[misc]
                bytes_done, _ = await _install_one(serial, file, replace)
            result["bytes"] = bytes_done
        result["status"] = "completed"
        result["ok"] = True
    except asyncio.CancelledError:
        result["status"] = "cancelled"
        result["ok"] = False
        result["error"] = "Job cancelled"
        raise
    except Exception as exc:
        result["status"] = "failed"
        result["ok"] = False
        message = str(exc).replace("\r", " ").replace("\n", " ").strip()
        result["error"] = message[:300] or "Device file operation failed"
    finally:
        result["duration_ms"] = int(round((time.monotonic() - started) * 1000))
        job["progress"]["done"] = sum(r.get("status") in {"completed", "failed", "cancelled"} for r in job["results"])
        _save_job(job)


async def _run_job(job: dict[str, Any], file: dict[str, Any] | None, replace: bool = False) -> None:
    global _active_jobs
    try:
        job["status"] = "running"
        _save_job(job)
        tasks = [asyncio.create_task(_run_device(job, result, file, replace)) for result in job["results"]]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for result in job["results"]:
                if result["status"] in {"queued", "running"}:
                    result["status"] = "cancelled"
                    result["ok"] = False
                    result["error"] = "Job cancelled"
            job["progress"]["done"] = sum(r.get("status") in {"completed", "failed", "cancelled"} for r in job["results"])
            job["status"] = "cancelled"
            _save_job(job)
            return
        job["status"] = "failed" if any(r.get("status") == "failed" for r in job["results"]) else "completed"
        _save_job(job)
    finally:
        _active_jobs -= 1
        _job_tasks.pop(job["id"], None)
        _job_cancel.discard(job["id"])


async def _schedule_job(job: dict[str, Any], file: dict[str, Any] | None, replace: bool = False) -> None:
    global _active_jobs
    # A small FIFO dispatcher is sufficient: the task itself waits for a slot,
    # while the API never creates more than MAX_ACTIVE_JOBS executing jobs.
    while _active_jobs >= MAX_ACTIVE_JOBS and job["id"] not in _job_cancel:
        await asyncio.sleep(0.05)
    if job["id"] in _job_cancel:
        job["status"] = "cancelled"
        for result in job["results"]:
            result["status"] = "cancelled"
            result["ok"] = False
            result["error"] = "Job cancelled"
        job["progress"]["done"] = job["progress"]["total"]
        _save_job(job)
        _job_tasks.pop(job["id"], None)
        return
    _active_jobs += 1
    await _run_job(job, file, replace)


def _job_response(job: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "job": job}


async def _resolve_targets(targets: Any) -> list[str]:
    groups_response = await fleet.list_groups()
    groups = groups_response.get("groups", []) if isinstance(groups_response, dict) else []
    selected = fleet._target_serials(targets, groups)
    if isinstance(targets, dict) and targets.get("all"):
        try:
            connected = await fleet.connected_devices()
        except Exception as exc:
            raise _validation(f"Unable to resolve connected devices: {str(exc)[:180]}") from None
        selected_set = set(selected)
        for serial in connected:
            if serial not in selected_set:
                selected.append(serial)
                selected_set.add(serial)
    if not selected:
        raise _validation("Explicitly select at least one device or group")
    if len(selected) > 50:
        raise _validation("A file job may contain at most 50 devices")
    return selected


@router.post("/files", dependencies=[Depends(fleet._same_origin)])
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    _ensure_runtime()
    name = _sanitize_upload_name(file.filename)
    file_id = uuid.uuid4().hex
    blob = _blob_path(file_id)
    temporary = blob.with_suffix(".part")
    total_staged = 0
    for metadata_path in (_state_root() / "meta").glob("*.json"):
        metadata = _read_json(metadata_path)
        if metadata and metadata.get("kind") == "file":
            size = metadata.get("size", 0)
            if isinstance(size, int):
                total_staged += size
    total = 0
    try:
        with temporary.open("wb") as output:
            _private(temporary)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE or total_staged + total > MAX_STAGED_BYTES:
                    raise HTTPException(status_code=413, detail="Staged file storage limit exceeded")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, blob)
        _private(blob)
        item = {"kind": "file", "id": file_id, "name": name, "size": total, "is_apk": _is_standalone_apk(name, blob), "created_at": _utc_now()}
        _atomic_json(_meta_path(file_id), item)
    except HTTPException:
        with suppress(OSError):
            temporary.unlink()
        raise
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        with suppress(OSError):
            blob.unlink()
        raise HTTPException(status_code=500, detail="Unable to stage file") from None
    finally:
        with suppress(Exception):
            await file.close()
    return {"ok": True, "file": {k: item[k] for k in ("id", "name", "size", "is_apk", "created_at")}}


@router.get("/files")
async def list_files() -> dict[str, Any]:
    _ensure_runtime()
    files: list[dict[str, Any]] = []
    for path in (_state_root() / "meta").glob("*.json"):
        item = _read_json(path)
        if item and item.get("kind") == "file" and _load_file(item.get("id", "")):
            files.append({k: item[k] for k in ("id", "name", "size", "is_apk", "created_at")})
    files.sort(key=lambda value: value["created_at"], reverse=True)
    return {"ok": True, "files": files}


@router.delete("/files/{file_id}", dependencies=[Depends(fleet._same_origin)])
async def delete_file(file_id: str) -> dict[str, Any]:
    _ensure_runtime()
    item = _load_file(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="File not found")
    for job_path in (_state_root() / "jobs").glob("*.json"):
        job = _read_json(job_path)
        if job and job.get("file_id") == file_id and job.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="File is referenced by an active job")
    with suppress(OSError):
        _blob_path(file_id).unlink()
    with suppress(OSError):
        _meta_path(file_id).unlink()
    return {"ok": True}


@router.post("/file-jobs", dependencies=[Depends(fleet._same_origin)])
async def create_file_job(body: FileJobBody) -> dict[str, Any]:
    _ensure_runtime()
    targets = await _resolve_targets(body.targets)
    file: dict[str, Any] | None = None
    if body.operation in {"push", "install"}:
        if not body.file_id:
            raise _validation("file_id is required for push and install")
        file = _load_file(body.file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="File not found")
    elif body.file_id is not None:
        raise _validation("file_id is only supported for push and install")
    if body.operation == "install":
        if file is None or not file.get("is_apk"):
            raise _validation("Only a standalone APK can be installed")
        if body.confirm_install is not True:
            raise _validation("Explicit install trust confirmation is required")
        if body.remote_path is not None:
            raise _validation("remote_path is not used for install")
    elif body.operation in {"push", "pull"}:
        if body.operation == "push":
            body.remote_path = body.remote_path or "/sdcard/Download/D3m0n"
        if not body.remote_path:
            raise _validation("remote_path is required")
        remote_path = _validate_remote_path(body.remote_path, destination=body.operation == "push")
    else:
        remote_path = None
    if len(_job_tasks) >= MAX_ACTIVE_JOBS:
        raise HTTPException(status_code=429, detail="Four file jobs are already active; wait or cancel one")
    job_id = uuid.uuid4().hex
    created = _utc_now()
    job: dict[str, Any] = {
        "id": job_id,
        "operation": body.operation,
        "status": "queued",
        "devices": targets,
        "created_at": created,
        "updated_at": created,
        "progress": {"done": 0, "total": len(targets)},
        "results": [{"device": serial, "status": "queued"} for serial in targets],
    }
    if file is not None:
        job["file_id"] = file["id"]
        job["filename"] = file["name"]
    if body.operation in {"push", "pull"}:
        job["remote_path"] = remote_path
    _save_job(job)
    task = asyncio.create_task(_schedule_job(job, file, bool(body.replace)), name=f"fleet-file-job-{job_id}")
    _job_tasks[job_id] = task
    return _job_response(job)


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    _ensure_runtime()
    jobs = [_read_json(path) for path in (_state_root() / "jobs").glob("*.json")]
    values = [job for job in jobs if job and isinstance(job.get("created_at"), str)]
    values.sort(key=lambda job: job["created_at"], reverse=True)
    return {"ok": True, "jobs": values[:20]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    _ensure_runtime()
    job = _load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(fleet._same_origin)])
async def cancel_job(job_id: str) -> dict[str, Any]:
    _ensure_runtime()
    job = _load_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
        return _job_response(job)
    _job_cancel.add(job_id)
    task = _job_tasks.get(job_id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    latest_after_task = _load_job(job_id)
    if latest_after_task is not None and latest_after_task.get("status") in {"queued", "running"}:
        job = latest_after_task
        for result in job["results"]:
            if result["status"] in {"queued", "running"}:
                result["status"] = "cancelled"
                result["ok"] = False
                result["error"] = "Job cancelled"
        job["status"] = "cancelled"
        job["progress"]["done"] = job["progress"]["total"]
        _save_job(job)
    latest = _load_job(job_id) or job
    return _job_response(latest)


@router.get("/downloads/{download_id}")
async def download_file(download_id: str) -> FileResponse:
    _ensure_runtime()
    metadata = _read_json(_download_meta_path(download_id))
    if not metadata or metadata.get("id") != download_id:
        raise HTTPException(status_code=404, detail="Download not found")
    path_value = metadata.get("path")
    if not isinstance(path_value, str):
        raise HTTPException(status_code=404, detail="Download not found")
    path = Path(path_value)
    root = (_state_root() / "downloads").resolve()
    try:
        if path.is_symlink() or path.resolve().parent != root or not path.is_file():
            raise ValueError
    except OSError:
        raise HTTPException(status_code=404, detail="Download not found") from None
    name = _sanitize_upload_name(metadata.get("filename"))
    return FileResponse(path, media_type="application/octet-stream", filename=name)


def reset_state_for_tests() -> None:
    """Clear task/semaphore state; persistent JSON deliberately remains durable."""
    global _runtime_lock, _transfer_sem, _install_sem, _active_jobs
    for task in list(_job_tasks.values()):
        task.cancel()
    _job_tasks.clear()
    _job_cancel.clear()
    _runtime_lock = None
    _transfer_sem = None
    _install_sem = None
    _active_jobs = 0
