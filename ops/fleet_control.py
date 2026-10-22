"""Stage 1 fleet groups and best-effort synchronized Android controls.

The router is deliberately self-contained: the parent application only needs to
``include_router(router)``.  All ADB access is injectable through the module-level
``connected_devices``, ``run_adb`` and ``action_executor`` helpers for tests and
future transport changes.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shlex
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from ops.platform_config import ensure_private_dir, ensure_private_file, get_allowed_origins, state_dir

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

_ALLOWED_ORIGINS = get_allowed_origins()
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,150}$")
_SIZE_RE = re.compile(r"(?:Physical|Override)\s+size:\s*(\d+)x(\d+)", re.I)
_ANY_SIZE_RE = re.compile(r"(?:size:\s*)?(\d+)x(\d+)", re.I)
_MAX_DEVICES = 50
_MAX_CONCURRENCY = 24
_OPERATION_TIMEOUT = 12.0
_SIZE_CACHE_SECONDS = 30.0

# These are intentionally module-level so tests and stage 2 can replace them.
_device_locks: dict[str, asyncio.Lock] = {}
_display_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_display_cache_locks: dict[str, asyncio.Lock] = {}


class GroupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    devices: list[str]


class GroupPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    devices: list[str] | None = None


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: dict[str, Any]
    action: str
    params: dict[str, Any]
    dry_run: bool = False


class AdbCommandError(RuntimeError):
    """An ADB subprocess failed without exposing request text in its message."""


class AdbTimeoutError(AdbCommandError):
    pass


def _state_db_path() -> Path:
    configured = os.environ.get("D3M0N_FLEET_STATE")
    directory = Path(configured) if configured else state_dir() / "fleet"
    ensure_private_dir(directory)
    return directory / "fleet.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _open_db() -> sqlite3.Connection:
    db_path = _state_db_path()
    connection = sqlite3.connect(db_path, timeout=15.0)
    ensure_private_file(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            devices_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return connection


def _row_to_group(row: sqlite3.Row) -> dict[str, Any]:
    try:
        devices = json.loads(row["devices_json"])
    except (TypeError, ValueError):
        devices = []
    return {
        "id": row["id"],
        "name": row["name"],
        "devices": devices,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise _validation_error("Group name must be a string")
    normalized = name.strip()
    if not 1 <= len(normalized) <= 60:
        raise _validation_error("Group name must contain 1 to 60 characters")
    return normalized


def _validate_serial(serial: Any) -> str:
    if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial):
        raise _validation_error("Device serial must match [A-Za-z0-9_.:-] and be 1 to 150 characters")
    if serial.casefold() == "ios" or serial.casefold().startswith("ios:"):
        raise _validation_error("iOS devices are not supported")
    return serial


def _validate_devices(devices: Any) -> list[str]:
    if not isinstance(devices, list):
        raise _validation_error("devices must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for serial in devices:
        serial = _validate_serial(serial)
        if serial not in seen:
            seen.add(serial)
            result.append(serial)
    if len(result) > _MAX_DEVICES:
        raise _validation_error("A group or action may contain at most 50 devices")
    return result


def _validate_group_id(group_id: str) -> str:
    if not isinstance(group_id, str) or not 1 <= len(group_id) <= 100:
        raise _validation_error("Invalid group id")
    return group_id


def _read_group(group_id: str) -> dict[str, Any] | None:
    with closing(_open_db()) as connection:
        row = connection.execute("SELECT * FROM fleet_groups WHERE id = ?", (group_id,)).fetchone()
        return _row_to_group(row) if row else None


async def _same_origin(request: Request) -> None:
    """Protect mutating browser requests while permitting machine clients."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if (origin and origin not in get_allowed_origins()) or fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin device control denied")


@router.get("/groups")
async def list_groups() -> dict[str, Any]:
    with closing(_open_db()) as connection:
        rows = connection.execute("SELECT * FROM fleet_groups ORDER BY name_key, id").fetchall()
        return {"ok": True, "groups": [_row_to_group(row) for row in rows]}


@router.post("/groups", dependencies=[Depends(_same_origin)])
async def create_group(body: GroupBody) -> dict[str, Any]:
    name = _validate_name(body.name)
    devices = _validate_devices(body.devices)
    group_id = uuid.uuid4().hex
    now = _utc_now()
    try:
        with closing(_open_db()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO fleet_groups (id,name,name_key,devices_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (group_id, name, name.casefold(), json.dumps(devices, separators=(",", ":")), now, now),
            )
            connection.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="A group with that name already exists") from None
    group = _read_group(group_id)
    assert group is not None
    return {"ok": True, "group": group}


@router.patch("/groups/{group_id}", dependencies=[Depends(_same_origin)])
async def update_group(group_id: str, body: GroupPatchBody) -> dict[str, Any]:
    group_id = _validate_group_id(group_id)
    if body.name is None and body.devices is None:
        raise _validation_error("At least one group field must be supplied")
    name = _validate_name(body.name) if body.name is not None else None
    devices = _validate_devices(body.devices) if body.devices is not None else None
    now = _utc_now()
    with closing(_open_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM fleet_groups WHERE id = ?", (group_id,)).fetchone()
        if current is None:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Group not found")
        next_name = name if name is not None else current["name"]
        next_devices = devices if devices is not None else json.loads(current["devices_json"])
        try:
            connection.execute(
                "UPDATE fleet_groups SET name=?, name_key=?, devices_json=?, updated_at=? WHERE id=?",
                (next_name, next_name.casefold(), json.dumps(next_devices, separators=(",", ":")), now, group_id),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise HTTPException(status_code=409, detail="A group with that name already exists") from None
        row = connection.execute("SELECT * FROM fleet_groups WHERE id = ?", (group_id,)).fetchone()
        return {"ok": True, "group": _row_to_group(row)}


@router.delete("/groups/{group_id}", dependencies=[Depends(_same_origin)])
async def delete_group(group_id: str) -> dict[str, Any]:
    group_id = _validate_group_id(group_id)
    with closing(_open_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("DELETE FROM fleet_groups WHERE id = ?", (group_id,))
        if cursor.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=404, detail="Group not found")
        connection.commit()
    return {"ok": True}


async def run_adb(*args: str, timeout: float = _OPERATION_TIMEOUT) -> str:
    """Run ADB without a shell and clean up the process on timeout/cancellation."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise AdbTimeoutError(f"ADB timed out after {timeout:g} seconds") from exc
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        # Never include action text in an error, and cap arbitrary ADB output.
        detail = re.sub(r"[\r\n\t]+", " ", detail)[:240]
        raise AdbCommandError(detail or f"ADB exited with status {process.returncode}")
    return (stdout or b"").decode("utf-8", "replace")


async def connected_devices() -> list[str]:
    output = await run_adb("adb", "devices", timeout=_OPERATION_TIMEOUT)
    result: list[str] = []
    try:
        from ops.usb_recovery import is_fleet_alias
    except Exception:  # pragma: no cover - recovery module unavailable
        def is_fleet_alias(_serial: str) -> bool:
            return False
    for line in output.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[1] == "device":
            serial = fields[0]
            if is_fleet_alias(serial):
                continue  # USB transport of a phone already listed by its Wi-Fi address
            if _SERIAL_RE.fullmatch(serial) and serial.casefold() != "ios" and not serial.casefold().startswith("ios:") and serial not in result:
                result.append(serial)
    return result


def _parse_display_size(output: str) -> tuple[int, int]:
    matches = _SIZE_RE.findall(output)
    if not matches:
        matches = _ANY_SIZE_RE.findall(output)
    if not matches:
        raise AdbCommandError("Unable to determine Android display size")
    # Android reports the override after physical size; the last valid line is active.
    width, height = (int(value) for value in matches[-1])
    if not (1 <= width <= 100_000 and 1 <= height <= 100_000):
        raise AdbCommandError("Android display size is out of range")
    return width, height


async def _display_size(serial: str) -> tuple[int, int]:
    now = time.monotonic()
    cached = _display_cache.get(serial)
    if cached and now - cached[0] < _SIZE_CACHE_SECONDS:
        return cached[1]
    lock = _display_cache_locks.setdefault(serial, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _display_cache.get(serial)
        if cached and now - cached[0] < _SIZE_CACHE_SECONDS:
            return cached[1]
        output = await run_adb("adb", "-s", serial, "shell", "wm", "size", timeout=_OPERATION_TIMEOUT)
        size = _parse_display_size(output)
        _display_cache[serial] = (time.monotonic(), size)
        return size


def _number(value: Any, label: str, *, normalized: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise _validation_error(f"{label} must be a finite number")
    number = float(value)
    if normalized and not 0.0 <= number <= 1.0:
        raise _validation_error(f"{label} must be normalized between 0 and 1")
    return number


def _pixel(value: float, extent: int) -> int:
    return max(0, min(extent - 1, int(round(value * (extent - 1)))))


def _validate_action(action: Any, params: Any) -> dict[str, Any]:
    if action not in {"tap", "swipe", "key", "text"}:
        raise _validation_error("Unsupported action")
    if not isinstance(params, dict):
        raise _validation_error("params must be an object")
    if action == "tap":
        if set(params) != {"x", "y"}:
            raise _validation_error("tap requires only x and y")
        return {"x": _number(params["x"], "x", normalized=True), "y": _number(params["y"], "y", normalized=True)}
    if action == "swipe":
        if set(params) - {"x1", "y1", "x2", "y2", "duration"} or not {"x1", "y1", "x2", "y2"}.issubset(params):
            raise _validation_error("swipe requires x1, y1, x2, y2 and optional duration")
        duration = params.get("duration", 300)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)):
            raise _validation_error("duration must be a finite number")
        if not 0 <= float(duration) <= 60_000:
            raise _validation_error("duration must be between 0 and 60000 milliseconds")
        return {
            "x1": _number(params["x1"], "x1", normalized=True),
            "y1": _number(params["y1"], "y1", normalized=True),
            "x2": _number(params["x2"], "x2", normalized=True),
            "y2": _number(params["y2"], "y2", normalized=True),
            "duration": int(round(float(duration))),
        }
    if action == "key":
        if set(params) != {"key"} or params["key"] not in {"BACK", "HOME", "APP_SWITCH", "WAKEUP", "VOLUME_UP", "VOLUME_DOWN", "ENTER", "DEL", "SPACE"}:
            raise _validation_error("Unsupported key")
        return {"key": params["key"]}
    if set(params) != {"text"} or not isinstance(params["text"], str):
        raise _validation_error("text requires only a string text field")
    text = params["text"]
    if len(text) > 4096:
        raise _validation_error("text is limited to 4096 characters")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise _validation_error("Text contains an unsupported NUL or surrogate character")
    return {"text": text}


def build_action_command(serial: str, action: str, params: dict[str, Any], size: tuple[int, int]) -> tuple[str, ...]:
    """Build a direct-exec ADB command; text is one safely quoted remote command."""
    width, height = size
    if action == "tap":
        return ("adb", "-s", serial, "shell", "input", "tap", str(_pixel(params["x"], width)), str(_pixel(params["y"], height)))
    if action == "swipe":
        return (
            "adb", "-s", serial, "shell", "input", "swipe",
            str(_pixel(params["x1"], width)), str(_pixel(params["y1"], height)),
            str(_pixel(params["x2"], width)), str(_pixel(params["y2"], height)), str(params["duration"]),
        )
    if action == "key":
        return ("adb", "-s", serial, "shell", "input", "keyevent", params["key"])
    # Passing this as one final argument makes it one remote-shell string while
    # shlex.quote prevents shell metacharacters from becoming commands.
    remote_command = "input text " + shlex.quote(params["text"])
    return ("adb", "-s", serial, "shell", remote_command)


async def action_executor(serial: str, action: str, params: dict[str, Any]) -> None:
    if action == "text" and (any(ord(c) >= 128 for c in params["text"]) or "%s" in params["text"] or any(c in params["text"] for c in "\n\r\t")):
        from ops.unicode_input import paste_text
        await paste_text(serial, params["text"])
        return
    size = await _display_size(serial) if action in {"tap", "swipe"} else (1, 1)
    await run_adb(*build_action_command(serial, action, params, size), timeout=_OPERATION_TIMEOUT)


def _target_serials(targets: Any, groups: list[dict[str, Any]]) -> list[str]:
    if not isinstance(targets, dict):
        raise _validation_error("targets must be an object")
    if set(targets) - {"devices", "group_ids", "all"}:
        raise _validation_error("targets contains unsupported fields")
    direct = targets.get("devices", [])
    group_ids = targets.get("group_ids", [])
    all_devices = targets.get("all", False)
    if not isinstance(direct, list) or not isinstance(group_ids, list) or not isinstance(all_devices, bool):
        raise _validation_error("targets.devices, targets.group_ids and targets.all have invalid types")
    selected: list[str] = []
    seen: set[str] = set()
    for serial in _validate_devices(direct):
        if serial not in seen:
            selected.append(serial)
            seen.add(serial)
    by_id = {group["id"]: group for group in groups}
    for raw_id in group_ids:
        group_id = _validate_group_id(raw_id)
        group = by_id.get(group_id)
        if group is None:
            raise _validation_error(f"Group not found: {group_id}")
        for serial in group["devices"]:
            if serial not in seen:
                selected.append(serial)
                seen.add(serial)
    if len(selected) > _MAX_DEVICES:
        raise _validation_error("An action may contain at most 50 devices")
    if all_devices:
        # Resolution is performed by the endpoint after group expansion.
        return selected
    if not selected:
        raise _validation_error("Explicitly select at least one device or group")
    return selected


async def _execute_one(serial: str, action: str, params: dict[str, Any], dry_run: bool, started: list[float]) -> dict[str, Any]:
    started_at = time.time()
    started.append(started_at)
    operation_start = time.monotonic()
    result: dict[str, Any] = {"device": serial, "ok": False, "started_at": started_at, "duration_ms": 0}
    if dry_run:
        result["ok"] = True
        result["dry_run"] = True
        result["duration_ms"] = int(round((time.monotonic() - operation_start) * 1000))
        return result
    lock = _device_locks.setdefault(serial, asyncio.Lock())
    try:
        # HTTP fleet actions are owner-less.  An active AI lease must fence
        # this path before it can fall through to ADB or an injected executor.
        from ops import control_arbiter
        control_arbiter.assert_write_allowed(serial, None)
        async with lock:
            await asyncio.wait_for(action_executor(serial, action, params), timeout=_OPERATION_TIMEOUT)
        result["ok"] = True
    except asyncio.TimeoutError:
        result["error"] = "Operation timed out after 12 seconds"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # isolate one device from every other result
        if action == "text":
            result["error"] = "Text input failed; verify that the device is online and ADB is authorized"
        else:
            message = str(exc).replace("\r", " ").replace("\n", " ").strip()
            result["error"] = message[:300] or "Device operation failed"
    result["duration_ms"] = int(round((time.monotonic() - operation_start) * 1000))
    return result


@router.post("/actions", dependencies=[Depends(_same_origin)])
async def actions(body: ActionBody) -> dict[str, Any]:
    action = body.action
    params = _validate_action(action, body.params)
    with closing(_open_db()) as connection:
        rows = connection.execute("SELECT * FROM fleet_groups").fetchall()
        groups = [_row_to_group(row) for row in rows]
    targets = body.targets
    selected = _target_serials(targets, groups)
    if bool(targets.get("all", False)):
        try:
            actual = await connected_devices()
        except Exception as exc:
            raise _validation_error(f"Unable to resolve connected devices: {str(exc)[:180]}") from None
        selected_seen = set(selected)
        for serial in actual:
            if serial not in selected_seen:
                selected.append(serial)
                selected_seen.add(serial)
        if len(selected) > _MAX_DEVICES:
            raise _validation_error("An action may contain at most 50 devices")
    if not selected:
        raise _validation_error("Explicitly select at least one device or group")

    started: list[float] = []
    batch_started = time.monotonic()
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def bounded(serial: str) -> dict[str, Any]:
        async with semaphore:
            return await _execute_one(serial, action, params, body.dry_run, started)

    tasks = [asyncio.create_task(bounded(serial)) for serial in selected]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    total_ms = int(round((time.monotonic() - batch_started) * 1000))
    spread_ms = int(round((max(started) - min(started)) * 1000)) if len(started) > 1 else 0
    return {
        "ok": all(result["ok"] for result in results),
        "action": action,
        "results": results,
        "duration_ms": max(0, total_ms),
        "dispatch_spread_ms": max(0, spread_ms),
        "dry_run": body.dry_run,
    }


@router.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    return {"ok": True, "unicode": True, "files": True, "batch_install": True, "sync": "best-effort", "max_file_bytes": 268435456, "standalone_apk_only": True}


def reset_state_for_tests() -> None:
    """Clear in-memory caches between isolated tests; SQLite remains durable."""
    _device_locks.clear()
    _display_cache.clear()
    _display_cache_locks.clear()
