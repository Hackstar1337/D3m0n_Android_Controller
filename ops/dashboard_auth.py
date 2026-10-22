"""Dashboard-wide password unlock for D3m0n in the droid.

Every request that reaches the backend through the public HTTPS domain passes the
Oracle Nginx proxy, which always overwrites ``Host``, ``X-Real-IP`` and
``X-Forwarded-For``.  Such requests must carry a valid signed session cookie for
everything except the static dashboard shell, ``/api/health`` and the
``/api/auth/*`` endpoints.  Direct loopback calls from local services (the
availability watchdog, MCP bridge, agent, ADB-reversed phone config downloads)
carry none of those headers and remain trusted.

Only a PBKDF2-SHA256 hash of the password is stored, in a 0600 state file.
Sessions are stateless HMAC-signed cookies; ``rotate-sessions`` locks every
browser at once.  If the state file is missing or invalid the gate fails closed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse
from ops.platform_config import atomic_write_json, get_allowed_origins, state_dir

AUTH_FILE = Path(os.environ.get("D3M0N_DASHBOARD_AUTH_FILE", "")).expanduser() if os.environ.get("D3M0N_DASHBOARD_AUTH_FILE", "").strip() else state_dir() / "dashboard-auth.json"
COOKIE_NAME = "d3m0n_session"
SESSION_SECONDS = 30 * 24 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_ITERATIONS = 100_000
MAX_FAILURES = 8
FAILURE_WINDOW = 15 * 60
MAX_PASSWORD_LENGTH = 256
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}
LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}
OPEN_API_PATHS = {"/api/health", "/api/auth/status", "/api/auth/unlock", "/api/auth/lock"}
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
LOCKED_DETAIL = "Dashboard locked"
UNCONFIGURED_DETAIL = "Dashboard password is not configured"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> dict[str, Any]:
    """Return a storable PBKDF2 record; the password itself is never kept."""
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("Password must be 1-256 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"kdf": "pbkdf2_sha256", "iterations": iterations, "salt": _b64e(salt), "hash": _b64e(digest)}


class AuthConfig:
    """Validated, in-memory view of the private state file."""

    def __init__(self, data: dict[str, Any], *, min_iterations: int = MIN_ITERATIONS) -> None:
        if not isinstance(data, dict) or data.get("kdf") != "pbkdf2_sha256":
            raise ValueError("Unsupported dashboard auth record")
        iterations = data.get("iterations")
        if not isinstance(iterations, int) or iterations < min_iterations:
            raise ValueError("Iteration count too low")
        self.iterations = iterations
        self.salt = _b64d(str(data["salt"]))
        self.hash = _b64d(str(data["hash"]))
        self.secret = _b64d(str(data["session_secret"]))
        if len(self.salt) < 16 or len(self.hash) != 32 or len(self.secret) < 32:
            raise ValueError("Dashboard auth record is malformed")

    def verify_password(self, password: Any) -> bool:
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, self.iterations)
        return hmac.compare_digest(digest, self.hash)

    def _sign(self, body: str) -> str:
        return _b64e(hmac.new(self.secret, body.encode("ascii"), hashlib.sha256).digest())

    def issue_token(self, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_SECONDS)
        body = f"v1.{expires}.{secrets.token_urlsafe(12)}"
        return body + "." + self._sign(body)

    def token_valid(self, token: str, now: float | None = None) -> bool:
        if not token or len(token) > 256:
            return False
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
            return False
        body = ".".join(parts[:3])
        if not hmac.compare_digest(self._sign(body), parts[3]):
            return False
        return int(parts[1]) > (time.time() if now is None else now)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    # The helper applies restrictive POSIX permissions and an explicit Windows
    # ACL.  Failure is deliberate: an auth secret must never be written with
    # unknown privacy semantics.
    atomic_write_json(Path(path), data)


def install_hash(path: Path, record: dict[str, Any], *, min_iterations: int = MIN_ITERATIONS) -> None:
    """Store a new password hash with a fresh session secret (locks all browsers)."""
    data = {
        "version": 1,
        "kdf": record.get("kdf"),
        "iterations": record.get("iterations"),
        "salt": record.get("salt"),
        "hash": record.get("hash"),
        "session_secret": _b64e(secrets.token_bytes(32)),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    AuthConfig(data, min_iterations=min_iterations)  # validate before writing
    _write_private(Path(path), data)


def rotate_sessions(path: Path) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["session_secret"] = _b64e(secrets.token_bytes(32))
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_private(Path(path), data)


class AuthState:
    """Caches the state file by mtime and tracks failed unlock attempts per client."""

    def __init__(self, path: Path | None = None, *, min_iterations: int = MIN_ITERATIONS) -> None:
        self.path = Path(path) if path is not None else AUTH_FILE
        self.min_iterations = min_iterations
        self._cache: tuple[Any, AuthConfig | None] = (None, None)
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def config(self) -> AuthConfig | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        # Writes are atomic replaces (new inode). Also never trust the cache while the file's
        # timestamp is "racy" (modified in the last 2 s): coarse filesystem clocks can give two
        # rapid writes the same mtime, and a password change or session rotation must always apply.
        key = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        racy = time.time_ns() - max(stat.st_mtime_ns, stat.st_ctime_ns) < 2_000_000_000
        if self._cache[0] == key and not racy:
            return self._cache[1]
        try:
            config: AuthConfig | None = AuthConfig(json.loads(self.path.read_text(encoding="utf-8")), min_iterations=self.min_iterations)
        except Exception:
            config = None
        self._cache = (key, config)
        return config

    def _recent(self, client: str, now: float) -> list[float]:
        items = [stamp for stamp in self._failures.get(client, []) if now - stamp < FAILURE_WINDOW]
        if items:
            self._failures[client] = items
        else:
            self._failures.pop(client, None)
        return items

    def retry_after(self, client: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            items = self._recent(client, now)
            if len(items) >= MAX_FAILURES:
                return max(1, int(items[0] + FAILURE_WINDOW - now) + 1)
        return 0

    def record_failure(self, client: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            items = self._recent(client, now)
            items.append(now)
            self._failures[client] = items
            if len(self._failures) > 4096:
                oldest = sorted(self._failures, key=lambda name: self._failures[name][-1])[:1024]
                for name in oldest:
                    self._failures.pop(name, None)
            return max(0, MAX_FAILURES - len(items))

    def clear_failures(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


state = AuthState()


def _headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    return {bytes(key).lower(): bytes(value) for key, value in scope.get("headers") or []}


def _hostname(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_trusted_local(scope: dict[str, Any]) -> bool:
    """Direct loopback call from a local service (never true for Nginx-forwarded traffic)."""
    client = scope.get("client") or ("", 0)
    if not client or client[0] not in LOOPBACK_CLIENTS:
        return False
    headers = _headers(scope)
    if b"x-real-ip" in headers or b"x-forwarded-for" in headers or b"forwarded" in headers:
        return False
    return _hostname(headers.get(b"host", b"").decode("latin-1")) in LOCAL_HOSTNAMES


def client_id(scope: dict[str, Any]) -> str:
    real_ip = _headers(scope).get(b"x-real-ip", b"").decode("latin-1").strip()
    if real_ip:
        return real_ip[:64]
    client = scope.get("client") or ("unknown", 0)
    return str(client[0])[:64]


def _is_https(scope: dict[str, Any]) -> bool:
    proto = _headers(scope).get(b"x-forwarded-proto", b"").decode("latin-1").strip().lower()
    return proto == "https" or scope.get("scheme") in ("https", "wss")


def session_cookie(scope: dict[str, Any]) -> str:
    raw = _headers(scope).get(b"cookie", b"").decode("latin-1")
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value.strip().strip('"')
    return ""


class DashboardAuthMiddleware:
    """Pure ASGI gate covering HTTP and WebSocket traffic."""

    def __init__(self, app: Any, auth_state: AuthState | None = None, dist: Path | str | None = None) -> None:
        self.app = app
        self.auth_state = auth_state or state
        self.dist = Path(dist).resolve() if dist is not None else None

    def _open_path(self, method: str, path: str) -> bool:
        """Unlocked-free paths: health/auth API and real files of the built dashboard shell."""
        if path in OPEN_API_PATHS:
            return True
        if method not in ("GET", "HEAD") or ".." in path or "\\" in path or "\x00" in path:
            return False
        if path in ("/", "/index.html") or path.startswith("/assets/"):
            return True
        if self.dist is None or path.startswith("/api/") or not path.startswith("/"):
            return False
        try:
            candidate = (self.dist / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            return False
        return candidate.is_file() and (candidate == self.dist or self.dist in candidate.parents)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        scope["d3m0n_auth"] = None
        if is_trusted_local(scope):
            scope["d3m0n_auth"] = "local"
            return await self.app(scope, receive, send)
        config = self.auth_state.config()
        token = session_cookie(scope)
        if config is not None and token and config.token_valid(token):
            if kind == "websocket":
                origin = _headers(scope).get(b"origin", b"").decode("latin-1")
                if origin and origin not in get_allowed_origins():
                    return await self._deny_websocket(receive, send)
            scope["d3m0n_auth"] = "session"
            return await self.app(scope, receive, send)
        if kind == "websocket":
            return await self._deny_websocket(receive, send)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if self._open_path(method, path):
            return await self.app(scope, receive, send)
        if config is None:
            response: Any = JSONResponse({"detail": UNCONFIGURED_DETAIL}, status_code=503, headers={**NO_STORE, "X-D3m0n-Auth": "locked"})
        elif method in ("GET", "HEAD") and not path.startswith("/api/") and "text/html" in _headers(scope).get(b"accept", b"").decode("latin-1"):
            response = RedirectResponse("/", status_code=303, headers=NO_STORE)
        else:
            response = JSONResponse({"detail": LOCKED_DETAIL}, status_code=401, headers={**NO_STORE, "X-D3m0n-Auth": "locked"})
        await response(scope, receive, send)

    @staticmethod
    async def _deny_websocket(receive: Any, send: Any) -> None:
        message = await receive()
        if message.get("type") == "websocket.connect":
            await send({"type": "websocket.close", "code": 1008})


router = APIRouter(prefix="/api/auth", tags=["dashboard-auth"])


def _status_payload(request: Request) -> dict[str, Any]:
    mode = request.scope.get("d3m0n_auth")
    return {
        "configured": state.config() is not None,
        "unlocked": mode in ("local", "session"),
        "local": mode == "local",
        "session_days": SESSION_SECONDS // 86400,
    }


@router.get("/status")
async def auth_status(request: Request) -> JSONResponse:
    return JSONResponse(_status_payload(request), headers=NO_STORE)


@router.post("/unlock")
async def auth_unlock(request: Request) -> JSONResponse:
    config = state.config()
    if config is None:
        return JSONResponse({"detail": UNCONFIGURED_DETAIL}, status_code=503, headers=NO_STORE)
    client = client_id(request.scope)
    wait = state.retry_after(client)
    if wait:
        return JSONResponse({"detail": "Too many attempts", "retry_after": wait}, status_code=429, headers={**NO_STORE, "Retry-After": str(wait)})
    try:
        payload = await request.json()
    except Exception:
        payload = None
    password = payload.get("password") if isinstance(payload, dict) else None
    valid = await asyncio.to_thread(config.verify_password, password) if isinstance(password, str) else False
    if not valid:
        left = state.record_failure(client)
        await asyncio.sleep(0.35)
        return JSONResponse({"detail": "Incorrect password", "attempts_left": left}, status_code=401, headers=NO_STORE)
    state.clear_failures(client)
    response = JSONResponse({"unlocked": True, "local": False, "session_days": SESSION_SECONDS // 86400}, headers=NO_STORE)
    response.set_cookie(COOKIE_NAME, config.issue_token(), max_age=SESSION_SECONDS, path="/", httponly=True, secure=_is_https(request.scope), samesite="lax")
    return response


@router.post("/lock")
async def auth_lock(request: Request) -> JSONResponse:
    response = JSONResponse({"unlocked": request.scope.get("d3m0n_auth") == "local"}, headers=NO_STORE)
    response.delete_cookie(COOKIE_NAME, path="/", secure=_is_https(request.scope), httponly=True, samesite="lax")
    return response


def install_dashboard_auth(app: Any, dist: Path | str | None = None) -> None:
    """Register the auth routes and make the gate the outermost middleware."""
    app.include_router(router)
    app.add_middleware(DashboardAuthMiddleware, auth_state=state, dist=dist)


def _main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "install-hash":
        install_hash(AUTH_FILE, json.loads(sys.stdin.read()))
        print("DASHBOARD_PASSWORD_INSTALLED " + str(AUTH_FILE))
        return 0
    if command == "set-password":
        import getpass
        first = getpass.getpass("New dashboard password: ")
        if first != getpass.getpass("Repeat password: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
        install_hash(AUTH_FILE, hash_password(first))
        print("DASHBOARD_PASSWORD_INSTALLED " + str(AUTH_FILE))
        return 0
    if command == "rotate-sessions":
        rotate_sessions(AUTH_FILE)
        print("DASHBOARD_SESSIONS_ROTATED")
        return 0
    if command == "status":
        config = AuthState(AUTH_FILE).config()
        mode = oct(AUTH_FILE.stat().st_mode & 0o777) if AUTH_FILE.exists() else "-"
        print(f"configured={'yes' if config else 'no'} file={AUTH_FILE} mode={mode} iterations={config.iterations if config else '-'}")
        return 0 if config else 1
    print("usage: python -m ops.dashboard_auth install-hash|set-password|rotate-sessions|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
