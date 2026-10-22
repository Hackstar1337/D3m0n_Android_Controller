"""Private proxy-management API for the D3m0n dashboard.

This module deliberately owns only proxy state and orchestration.  The Android
adapter is imported lazily by :func:`make_driver`, so importing the router never
starts ADB, a VPN client, or a network listener.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

try:  # Pydantic 1 and 2 both support the class Config form.
    class _ForbidModel(BaseModel):
        class Config:
            extra = "forbid"
except Exception:  # pragma: no cover - defensive import fallback
    _ForbidModel = BaseModel

from ops.platform_config import get_allowed_origins, state_dir

STATE_DIR = Path(os.environ.get("D3M0N_PROXY_STATE", str(state_dir() / "proxies"))).expanduser()
KEY_FILE = STATE_DIR / "key"
DB_FILE = STATE_DIR / "proxies.sqlite3"
SUPPORTED_TYPES = ("http", "https", "socks5")
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,150}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_ALLOWED_ORIGINS = get_allowed_origins()
_MAX_PROFILE_TEXT = 253
_MAX_CONFIG_BYTES = 256 * 1024
_JOB_TIMEOUT = 90.0
# Apply may install the managed client first (one click), so it gets more time.
_APPLY_TIMEOUT = 300.0
_PREPARE_TIMEOUT = 300.0

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


class ProfileBody(_ForbidModel):
    serial: str
    label: str
    proxy_type: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


class JobBody(_ForbidModel):
    action: str
    devices: list[str]
    confirmed: bool = False


class ProxyStore:
    """Encrypted-at-rest proxy profiles and the durable device registry."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir).expanduser() if state_dir is not None else STATE_DIR
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._secure_mode(self.state_dir, 0o700)
        self.key_file = self.state_dir / "key"
        self.db_file = self.state_dir / "proxies.sqlite3"
        self._fernet = Fernet(self._read_or_create_secret(self.key_file, Fernet.generate_key))
        self._ensure_database()

    @staticmethod
    def _secure_mode(path: Path, mode: int) -> None:
        from ops.platform_config import ensure_private_dir, ensure_private_file
        if mode == 0o700:
            ensure_private_dir(path)
        else:
            ensure_private_file(path)

    @staticmethod
    def _read_or_create_secret(path: Path, generator: Callable[[], bytes]) -> bytes:
        try:
            value = path.read_bytes().strip()
        except FileNotFoundError:
            value = generator()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(path, flags, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(value)
            except FileExistsError:
                value = path.read_bytes().strip()
        ProxyStore._secure_mode(path, 0o600)
        return value


    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._secure_mode(self.state_dir, 0o700)
        connection = sqlite3.connect(self.db_file, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        self._secure_mode(self.db_file, 0o600)
        return connection

    def _ensure_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS proxy_profiles (
                    serial TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    proxy_type TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username_cipher BLOB,
                    password_cipher BLOB,
                    desired_mode TEXT NOT NULL DEFAULT 'normal',
                    applied_mode TEXT NOT NULL DEFAULT 'normal',
                    last_result TEXT,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _enc(self, value: str | None) -> bytes | None:
        return self._fernet.encrypt(value.encode("utf-8")) if value is not None else None

    def _dec(self, value: bytes | None) -> str | None:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(bytes(value)).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Stored proxy credentials are unavailable") from exc

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            result = json.loads(row["last_result"]) if row["last_result"] else None
        except (TypeError, ValueError):
            result = None
        return {
            "serial": row["serial"],
            "label": row["label"],
            "proxy_type": row["proxy_type"],
            "host": row["host"],
            "port": int(row["port"]),
            "username_set": row["username_cipher"] is not None,
            "password_set": row["password_cipher"] is not None,
            "desired_mode": row["desired_mode"],
            "applied_mode": row["applied_mode"],
            "last_result": result,
            "updated_at": row["updated_at"],
        }

    def list_profiles(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM proxy_profiles ORDER BY serial").fetchall()
            return [self._public(row) for row in rows]

    def get_public(self, serial: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM proxy_profiles WHERE serial=?", (serial,)).fetchone()
            return self._public(row) if row else None

    def get_config(self, serial: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM proxy_profiles WHERE serial=?", (serial,)).fetchone()
        if not row:
            return None
        return {
            "serial": row["serial"],
            "label": row["label"],
            "proxy_type": row["proxy_type"],
            "host": row["host"],
            "port": int(row["port"]),
            "username": self._dec(row["username_cipher"]),
            "password": self._dec(row["password_cipher"]),
        }

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        serial = data["serial"]
        with closing(self._connect()) as connection:
            old = connection.execute("SELECT * FROM proxy_profiles WHERE serial=?", (serial,)).fetchone()
            username = data["username"] if data["username"] is not None else (self._dec(old["username_cipher"]) if old else None)
            password = data["password"] if data["password"] is not None else (self._dec(old["password_cipher"]) if old else None)
            # None preserves the saved value; an explicit empty string clears it (no empty Basic auth is stored).
            username = username or None
            password = password or None
            desired = old["desired_mode"] if old else "normal"
            applied = old["applied_mode"] if old else "normal"
            last_result = old["last_result"] if old else None
            connection.execute(
                """INSERT INTO proxy_profiles
                (serial,label,proxy_type,host,port,username_cipher,password_cipher,desired_mode,applied_mode,last_result,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(serial) DO UPDATE SET label=excluded.label, proxy_type=excluded.proxy_type,
                host=excluded.host, port=excluded.port, username_cipher=excluded.username_cipher,
                password_cipher=excluded.password_cipher, updated_at=excluded.updated_at""",
                (serial, data["label"], data["proxy_type"], data["host"], data["port"], self._enc(username), self._enc(password), desired, applied, last_result, self._now()),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM proxy_profiles WHERE serial=?", (serial,)).fetchone()
            return self._public(row)

    def delete(self, serial: str) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM proxy_profiles WHERE serial=?", (serial,))
            connection.commit()
            return cursor.rowcount == 1

    def update_runtime(self, serial: str, *, desired: str | None = None, applied: str | None = None, result: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT desired_mode,applied_mode,last_result FROM proxy_profiles WHERE serial=?", (serial,)).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE proxy_profiles SET desired_mode=?, applied_mode=?, last_result=?, updated_at=? WHERE serial=?",
                (desired if desired is not None else row["desired_mode"], applied if applied is not None else row["applied_mode"], json.dumps(result, separators=(",", ":")) if result is not None else row["last_result"], self._now(), serial),
            )
            connection.commit()


store = ProxyStore()
# A named seam makes parent integration and isolated tests straightforward.
proxy_store = store
inventory_provider: Callable[[], Any] | None = None
driver_factory: Callable[[], Any] | None = None


def set_store(value: ProxyStore) -> None:
    global store, proxy_store
    store = value
    proxy_store = value


def _safe_serial(value: Any) -> str:
    if not isinstance(value, str) or not _SERIAL_RE.fullmatch(value) or value.casefold() == "ios" or value.casefold().startswith("ios:"):
        raise HTTPException(status_code=422, detail="Invalid device serial")
    return value


def _safe_text(value: Any, field: str, *, max_length: int = _MAX_PROFILE_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HTTPException(status_code=422, detail=f"Invalid {field}")
    return value.strip()


def _safe_credential(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise HTTPException(status_code=422, detail=f"Invalid {field}")
    return value


def _safe_host(value: Any) -> str:
    host = _safe_text(value, "host", max_length=253)
    if any(char in host for char in "/\\@?#") or "://" in host or any(char.isspace() for char in host):
        raise HTTPException(status_code=422, detail="Host must not contain a scheme, path, userinfo, or whitespace")
    try:
        parsed = urlsplit("//" + host)
        if parsed.hostname != host and not (host.startswith("[") and host.endswith("]")):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid proxy host") from None
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise HTTPException(status_code=422, detail="Invalid proxy host")
    return host


def _validate_profile(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"serial", "label", "proxy_type", "host", "port", "username", "password"}:
        raise HTTPException(status_code=422, detail="Profile fields are invalid")
    serial = _safe_serial(data["serial"])
    label = _safe_text(data["label"], "label", max_length=120)
    proxy_type = data["proxy_type"]
    if proxy_type not in SUPPORTED_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported proxy type")
    port = data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Invalid proxy port")
    return {"serial": serial, "label": label, "proxy_type": proxy_type, "host": _safe_host(data["host"]), "port": port, "username": _safe_credential(data["username"], "username"), "password": _safe_credential(data["password"], "password")}


def _validation_payload(request: Request) -> dict[str, Any]:
    # Kept synchronous for direct unit tests; endpoint uses request.json().
    return {}


async def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    configured = set(get_allowed_origins())
    configured.update(value.strip() for value in os.environ.get("D3M0N_PROXY_ORIGINS", "").split(",") if value.strip())
    if (origin and origin not in configured) or fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-origin proxy administration denied")


async def _admin(request: Request) -> None:
    """Proxy routes require an unlocked dashboard (session cookie or trusted local call).

    The dashboard auth middleware marks the request scope; if it is ever missing
    the routes fail closed instead of becoming public.
    """
    await _same_origin(request)
    if request.scope.get("d3m0n_auth") not in ("local", "session"):
        raise HTTPException(status_code=401, detail="Dashboard unlock required")


async def connected_devices() -> list[str]:
    """Call the fleet signature with no arguments; kept injectable for tests."""
    try:
        from ops.fleet_control import connected_devices as fleet_connected_devices
        result = fleet_connected_devices()
        if asyncio.iscoroutine(result):
            result = await result
        return list(result or [])
    except Exception:
        return []


def _saved_devices() -> list[dict[str, Any]]:
    """Best-effort SQLAlchemy inventory using only columns present in ``phones``."""
    try:
        from gitd.models.base import SessionLocal
        from sqlalchemy import inspect, text
        session = SessionLocal()
        try:
            bind = session.get_bind() if hasattr(session, "get_bind") else getattr(session, "bind", None)
            columns = {column["name"] for column in inspect(bind).get_columns("phones")} if bind is not None else set()
            wanted = [name for name in ("wifi_ip", "wifi_port", "device_id", "serial", "nickname", "model") if name in columns]
            if not ({"device_id", "serial"}.intersection(wanted) or {"wifi_ip", "wifi_port"}.issubset(wanted)):
                return []
            rows = session.execute(text("SELECT " + ",".join(wanted) + " FROM phones")).mappings().all()
            return [dict(row) for row in rows]
        finally:
            session.close()
    except Exception:
        return []


def _normalize_inventory(items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            serial, label = item, item
            online = True
        elif isinstance(item, dict):
            serial = item.get("serial") or item.get("device_id")
            if item.get("wifi_ip") and item.get("wifi_port"):
                try:
                    ip = ipaddress.ip_address(str(item["wifi_ip"]))
                    port = int(item["wifi_port"])
                    if ip.version == 4 and 1 <= port <= 65535:
                        serial = f"{ip}:{port}"
                except (ValueError, TypeError):
                    pass
            label = item.get("label") or item.get("nickname") or item.get("model") or serial
            online = bool(item.get("online", True))
        else:
            continue
        if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial) or serial in seen:
            continue
        seen.add(serial)
        normalized.append({"serial": serial, "label": str(label or serial)[:120], "online": online})
    return normalized


async def inventory() -> list[dict[str, Any]]:
    if inventory_provider is not None:
        value = inventory_provider()
        if asyncio.iscoroutine(value):
            value = await value
        live = value
        saved: list[dict[str, Any]] = []
    else:
        live_task = asyncio.create_task(connected_devices())
        saved = await asyncio.to_thread(_saved_devices)
        live = await live_task
    result = _normalize_inventory(live)
    by_serial = {item["serial"]: item for item in result}
    for item in _normalize_inventory(saved):
        if item["serial"] in by_serial:
            by_serial[item["serial"]]["label"] = item["label"]
        else:
            item["online"] = False
            result.append(item)
            by_serial[item["serial"]] = item
    for profile in store.list_profiles():
        if profile["serial"] not in by_serial:
            result.append({"serial": profile["serial"], "label": profile["label"], "online": False})
    return result


_bootstrap: dict[str, tuple[float, str, bytes]] = {}
_bootstrap_lock = asyncio.Lock()


def publish_config(serial: str, yaml_bytes: bytes) -> tuple[str, str]:
    if not isinstance(yaml_bytes, (bytes, bytearray)) or not 0 < len(yaml_bytes) <= _MAX_CONFIG_BYTES:
        raise ValueError("Invalid temporary proxy configuration")
    token = secrets.token_urlsafe(48)
    _bootstrap[token] = (time.monotonic() + 90.0, serial, bytes(yaml_bytes))
    return token, f"/api/proxy/bootstrap/{token}"


def revoke_config(token: str) -> None:
    _bootstrap.pop(token, None)


def _expire_bootstrap() -> None:
    now = time.monotonic()
    for token, (expires, _serial, _payload) in list(_bootstrap.items()):
        if expires <= now:
            _bootstrap.pop(token, None)


async def bootstrap(token: str) -> Response:
    if not _TOKEN_RE.fullmatch(token):
        raise HTTPException(status_code=404, detail="Bootstrap link unavailable")
    _expire_bootstrap()
    record = _bootstrap.get(token)
    if record is None or record[0] <= time.monotonic():
        _bootstrap.pop(token, None)
        raise HTTPException(status_code=404, detail="Bootstrap link unavailable")
    return Response(content=record[2], media_type="application/yaml", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


router.add_api_route("/bootstrap/{token}", bootstrap, methods=["GET"])


def make_driver() -> Any:
    """Lazily construct the separate Android adapter with synchronous callbacks."""
    if driver_factory is not None:
        return driver_factory()
    from ops.proxy_device import ProxyDeviceDriver
    return ProxyDeviceDriver(publish_config, revoke_config)


def _safe_message(value: Any, secrets_to_hide: list[str] | None = None) -> str:
    message = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    for secret in secrets_to_hide or []:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300] or "Proxy operation failed"


def _result(raw: Any, *, operation: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    hidden = [config.get("username", ""), config.get("password", "")] if config else []
    if not isinstance(raw, dict):
        result = {"ok": False, "status": "failed", "message": "Proxy driver returned an invalid result"}
        if operation == "test":
            result["verified_device"] = False
        return result
    ok = bool(raw.get("ok", False))
    result: dict[str, Any] = {"ok": ok, "status": _safe_message(raw.get("status", "completed" if ok else "failed"), hidden), "message": _safe_message(raw.get("message", ""), hidden)}
    if isinstance(raw.get("exit_ip"), str) and len(raw["exit_ip"]) <= 128:
        result["exit_ip"] = _safe_message(raw["exit_ip"], hidden)
    if operation in {"apply", "disable"}:
        result["verified_device"] = bool(raw.get("verified_device", False)) and ok if operation == "apply" else bool(raw.get("verified_device", False))
    elif operation == "test":
        result["verified_device"] = False
    return result


def _failure(message: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "message": message[:300]}


_jobs: dict[str, dict[str, Any]] = {}
_job_tasks: set[asyncio.Task[Any]] = set()
_device_locks: dict[str, asyncio.Lock] = {}
_global_slots = asyncio.Semaphore(2)
_prepare_slots = asyncio.Semaphore(4)


def _prepare_result(raw: Any) -> dict[str, Any]:
    result = _result(raw, operation="prepare")
    if isinstance(raw, dict):
        if isinstance(raw.get("installed_now"), bool):
            result["installed_now"] = raw["installed_now"]
        if isinstance(raw.get("version"), str):
            result["version"] = _safe_message(raw["version"])[:40]
        if isinstance(raw.get("steps"), list):
            result["steps"] = [str(step)[:32] for step in raw["steps"][:8]]
    result["verified_device"] = False
    return result


async def _run_one(job: dict[str, Any], serial: str, driver: Any) -> dict[str, Any]:
    lock = _device_locks.setdefault(serial, asyncio.Lock())
    async with lock:
        if job["action"] == "prepare":
            # Client setup needs no proxy profile and never changes routing.
            prepare = getattr(driver, "prepare_client", None)
            if prepare is None:
                return {"serial": serial, **_failure("Client setup is not supported by this adapter"), "verified_device": False}
            try:
                raw = await asyncio.wait_for(prepare(serial), _PREPARE_TIMEOUT)
                return {"serial": serial, **_prepare_result(raw)}
            except asyncio.TimeoutError:
                return {"serial": serial, **_failure("Client setup timed out"), "verified_device": False}
            except asyncio.CancelledError:
                raise
            except Exception:
                return {"serial": serial, **_failure("Client setup failed"), "verified_device": False}
        config = store.get_config(serial)
        if config is None:
            return {"serial": serial, **_failure("No saved proxy profile")}
        action = job["action"]
        token: str | None = None
        try:
            if action == "test":
                raw = await asyncio.wait_for(driver.test(config), _JOB_TIMEOUT)
                return {"serial": serial, **_result(raw, operation=action, config=config)}
            if action == "apply":
                # Authentication/HTTPS preflight must pass before any phone changes.
                preflight = _result(await asyncio.wait_for(driver.test(config), _JOB_TIMEOUT), operation="test", config=config)
                if not preflight["ok"]:
                    return {"serial": serial, **preflight, "verified_device": False}
                raw = await asyncio.wait_for(driver.apply(serial, config), _APPLY_TIMEOUT)
                result = _result(raw, operation=action, config=config)
                if result["ok"] and result.get("verified_device") is True:
                    store.update_runtime(serial, desired="proxy", applied="proxy", result=result)
                else:
                    if result["ok"]:
                        result["ok"] = False
                        result["status"] = "setup-required"
                        result["message"] = "Device-side routing verification was unavailable"
                    result["verified_device"] = False
                    store.update_runtime(serial, desired="proxy", applied="unknown", result=result)
                return {"serial": serial, **result}
            raw = _result(await asyncio.wait_for(driver.disable(serial), _JOB_TIMEOUT), operation=action)
            if raw["ok"]:
                store.update_runtime(serial, desired="normal", applied="normal", result=raw)
            else:
                store.update_runtime(serial, desired="normal", applied="unknown", result=raw)
            return {"serial": serial, **raw}
        except asyncio.TimeoutError:
            result = _failure("Proxy operation timed out")
            if action == "apply":
                store.update_runtime(serial, desired="proxy", applied="unknown", result=result)
            return {"serial": serial, **result, "verified_device": False if action == "apply" else False}
        except asyncio.CancelledError:
            raise
        except Exception:
            result = _failure("Proxy driver operation failed")
            if action == "apply":
                store.update_runtime(serial, desired="proxy", applied="unknown", result=result)
            return {"serial": serial, **result, "verified_device": False if action == "apply" else False}
        finally:
            # Adapter normally revokes in its own finally; this also cleans up a
            # failed adapter without touching another serial's one-shot link.
            if action == "apply":
                _expire_bootstrap()
                for candidate, (_expires, owner, _payload) in list(_bootstrap.items()):
                    if owner == serial:
                        revoke_config(candidate)


async def _run_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    try:
        driver = make_driver()
        semaphore = _prepare_slots if job["action"] == "prepare" else _global_slots

        async def bounded(serial: str) -> dict[str, Any]:
            async with semaphore:
                return await _run_one(job, serial, driver)

        results = await asyncio.gather(*(bounded(serial) for serial in job["devices"]))
        job["results"] = results
        job["status"] = "completed" if all(item.get("ok") for item in results) else "failed"
    except asyncio.CancelledError:
        job["status"] = "failed"
        raise
    except Exception:
        job["results"] = [_failure("Proxy job could not be started")]
        job["status"] = "failed"
    finally:
        job["finished_at"] = _utc_now()
        if job.get("action") in {"prepare", "apply", "disable"}:
            # Client readiness may have changed; the next readiness read re-checks the phones.
            _client_cache.update(at=0.0)


def _track_task(task: asyncio.Task[Any]) -> None:
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@router.get("/capabilities", dependencies=[Depends(_admin)])
async def capabilities() -> dict[str, Any]:
    return {"device_wide": True, "authentication": "dashboard-session", "supported_types": list(SUPPORTED_TYPES), "detection_note": "Websites see the proxy exit IP, not a guaranteed mobile/carrier IP. VPN/proxy detection cannot be hidden or guaranteed away. Test is server-side; Apply requires the managed Android VPN and device-side verification. UDP/QUIC and private-DNS behavior depend on the upstream and client."}


@router.get("/devices", dependencies=[Depends(_admin)])
async def devices() -> dict[str, Any]:
    items = await inventory()
    profiles = {profile["serial"]: profile for profile in store.list_profiles()}
    output = []
    for item in items:
        output.append({"serial": item["serial"], "label": item["label"], "online": item["online"], "profile": profiles.get(item["serial"])})
    return {"devices": output, "total": len(output)}


@router.get("/routing", dependencies=[Depends(_admin)])
async def routing() -> dict[str, Any]:
    """Store-only routing summary for Live grid tiles: no ADB, no credentials, no proxy host."""
    devices: dict[str, Any] = {}
    for profile in store.list_profiles():
        result = profile.get("last_result") if isinstance(profile.get("last_result"), dict) else {}
        exit_ip = result.get("exit_ip")
        devices[profile["serial"]] = {
            "mode": profile["applied_mode"],
            "desired_mode": profile["desired_mode"],
            "label": profile["label"],
            "proxy_type": profile["proxy_type"],
            "exit_ip": exit_ip if isinstance(exit_ip, str) and exit_ip else None,
            "verified": bool(result.get("verified_device")),
            "updated_at": profile["updated_at"],
        }
    return {"devices": devices, "total": len(devices)}


@router.post("/profiles", dependencies=[Depends(_admin)])
async def save_profile(request: Request) -> dict[str, Any]:
    try:
        data = _validate_profile(await request.json())
        return {"profile": store.save(data)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid profile request") from None


@router.delete("/profiles/{serial}", dependencies=[Depends(_admin)])
async def delete_profile(serial: str) -> dict[str, Any]:
    serial = _safe_serial(serial)
    profile = store.get_public(serial)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile["applied_mode"] == "proxy" or profile["desired_mode"] == "proxy" or any(job.get("status") in {"queued", "running"} and serial in job.get("devices", []) for job in _jobs.values()):
        raise HTTPException(status_code=409, detail="Disable routing and wait for active jobs before deleting this profile")
    store.delete(serial)
    return {"ok": True}


@router.post("/jobs", dependencies=[Depends(_admin)])
async def create_job(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"action", "devices", "confirmed"}:
            raise HTTPException(status_code=422, detail="Job fields are invalid")
        action = body["action"]
        if action not in {"test", "apply", "disable", "prepare"}:
            raise HTTPException(status_code=422, detail="Unsupported job action")
        raw_devices = body["devices"]
        if not isinstance(raw_devices, list) or not raw_devices or len(raw_devices) > 1000:
            raise HTTPException(status_code=422, detail="Select at least one device")
        selected: list[str] = []
        for raw in raw_devices:
            serial = _safe_serial(raw)
            if serial not in selected:
                selected.append(serial)
        if action in {"apply", "disable", "prepare"} and body["confirmed"] is not True:
            raise HTTPException(status_code=422, detail="Explicit confirmation is required for device changes")
        known = {item["serial"] for item in await inventory()}
        unknown = [serial for serial in selected if serial not in known]
        if unknown:
            raise HTTPException(status_code=404, detail="One or more selected devices are not in the registry")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid job request") from None
    if any(job.get("status") in {"queued", "running"} and set(selected).intersection(job.get("devices", [])) for job in _jobs.values()):
        raise HTTPException(status_code=409, detail="A proxy job is already active for a selected device")
    if sum(job.get("status") in {"queued", "running"} for job in _jobs.values()) >= 8:
        raise HTTPException(status_code=409, detail="Wait for active proxy jobs to finish")
    for old_id in [jid for jid, item in _jobs.items() if item.get("status") in {"completed", "failed"}][:-200]:
        _jobs.pop(old_id, None)
    job = {"id": uuid.uuid4().hex, "action": action, "status": "queued", "devices": selected, "results": [], "created_at": _utc_now()}
    _jobs[job["id"]] = job
    task = asyncio.create_task(_run_job(job["id"]))
    _track_task(task)
    return {"job": job}


@router.get("/jobs/{job_id}", dependencies=[Depends(_admin)])
async def get_job(job_id: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@router.get("/jobs", dependencies=[Depends(_admin)])
async def list_jobs() -> dict[str, Any]:
    """Recent jobs (newest first) so an unlocked dashboard can re-attach progress after a reload."""
    items = sorted(_jobs.values(), key=lambda job: str(job.get("created_at", "")), reverse=True)[:50]
    return {"jobs": items}


_CLIENT_STATUSES = {"ready", "client_missing", "other_vpn", "unknown", "offline"}
_CLIENT_CACHE_SECONDS = 60.0
_CLIENT_MIN_REFRESH_SECONDS = 10.0
_client_cache: dict[str, Any] = {"at": 0.0, "items": None, "checked_at": None}
_client_lock: asyncio.Lock | None = None


def _client_guard() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _client_one(driver: Any, serial: str) -> dict[str, Any]:
    probe = getattr(driver, "client_status", None)
    unknown = {"serial": serial, "status": "unknown", "client_installed": None, "always_on_vpn": None, "message": "Client readiness could not be checked"}
    if probe is None:
        return unknown
    try:
        raw = await asyncio.wait_for(probe(serial), timeout=25.0)
    except Exception:
        return unknown
    if not isinstance(raw, dict):
        return unknown
    status = raw.get("status") if raw.get("status") in _CLIENT_STATUSES else "unknown"
    installed = raw.get("client_installed") if isinstance(raw.get("client_installed"), bool) else None
    always_on = raw.get("always_on_vpn") if isinstance(raw.get("always_on_vpn"), bool) else None
    consent = raw.get("vpn_consent") if isinstance(raw.get("vpn_consent"), bool) else None
    return {"serial": serial, "status": status, "client_installed": installed, "always_on_vpn": always_on, "vpn_consent": consent, "message": _safe_message(raw.get("message", ""))}


@router.get("/clients", dependencies=[Depends(_admin)])
async def clients(refresh: bool = False) -> dict[str, Any]:
    """Read-only managed-client readiness for online devices (cached; never changes a device)."""
    async with _client_guard():
        now = time.monotonic()
        cached = _client_cache.get("items")
        age = now - float(_client_cache.get("at") or 0.0)
        if cached is not None and (age < _CLIENT_MIN_REFRESH_SECONDS or (not refresh and age < _CLIENT_CACHE_SECONDS)):
            return {"clients": cached, "checked_at": _client_cache.get("checked_at"), "cached": True}
        items = await inventory()
        driver = make_driver()
        slots = asyncio.Semaphore(4)

        async def bounded(serial: str) -> dict[str, Any]:
            async with slots:
                return await _client_one(driver, serial)

        online = [item["serial"] for item in items if item.get("online")]
        results = list(await asyncio.gather(*(bounded(serial) for serial in online)))
        results.extend({"serial": item["serial"], "status": "offline", "client_installed": None, "always_on_vpn": None, "message": "Device is offline"} for item in items if not item.get("online"))
        _client_cache.update(at=time.monotonic(), items=results, checked_at=_utc_now())
        return {"clients": results, "checked_at": _client_cache["checked_at"], "cached": False}


# Alias names are useful to lightweight parent adapters without changing routes.
list_devices = devices
upsert_profile = save_profile


def reset_state_for_tests() -> None:
    global _global_slots, _prepare_slots, _client_lock
    _global_slots = asyncio.Semaphore(2)
    _prepare_slots = asyncio.Semaphore(4)
    _jobs.clear()
    _job_tasks.clear()
    _device_locks.clear()
    _bootstrap.clear()
    _client_cache.update(at=0.0, items=None, checked_at=None)
    _client_lock = None
