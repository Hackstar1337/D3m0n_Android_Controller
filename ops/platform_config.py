"""Portable, security-sensitive application configuration.

This module is intentionally small and dependency-free.  It is the single source
for dashboard origins, the safe bind default, and per-user runtime state.  Origin
values are normalized once and compared as exact strings; a LAN subnet is never a
trust boundary.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5055
_DEFAULT_FRONTEND_PORTS = (5173, 6175, 4321)
_ORIGINS_ENV = "D3M0N_ALLOWED_ORIGINS"
_PUBLIC_ORIGIN_ENV = "D3M0N_PUBLIC_ORIGIN"
_STATE_ENV = "D3M0N_STATE_DIR"


def _is_loopback_or_private(host: str) -> bool:
    import ipaddress

    candidate = host.strip("[]").lower().rstrip(".")
    if candidate in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if address.version != 4:
        return False
    return any(address in ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def normalize_origin(value: str, *, require_https: bool = False) -> str:
    """Return a canonical origin or raise ``ValueError``.

    HTTP is limited to loopback/RFC1918 literal hosts (and localhost).  HTTPS
    may use a configured DNS name, but credentials, paths, queries, fragments,
    wildcards, and arbitrary origin suffixes are never accepted.
    """
    if not isinstance(value, str):
        raise ValueError("Origin must be a string")
    raw = value.strip()
    if not raw or "*" in raw or any(char.isspace() for char in raw):
        raise ValueError("Origin must be an exact URL without wildcards")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Origin is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Origin must use http or https")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Origin must not contain credentials or a path")
    scheme = parsed.scheme.lower()
    if require_https and scheme != "https":
        raise ValueError("Public origin must use HTTPS")
    host = host.lower().rstrip(".")
    if scheme == "http" and not _is_loopback_or_private(host):
        raise ValueError("HTTP origins are limited to local/private hosts")
    if port is None:
        port_text = ""
    elif not 1 <= port <= 65535:
        raise ValueError("Origin port is invalid")
    elif (scheme, port) in {("http", 80), ("https", 443)}:
        port_text = ""
    else:
        port_text = f":{port}"
    host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{host_text}{port_text}"


def get_public_origin() -> str | None:
    """Return the explicitly configured HTTPS advertised origin, if any."""
    raw = os.environ.get(_PUBLIC_ORIGIN_ENV, "").strip()
    if not raw:
        return None
    return normalize_origin(raw, require_https=True)


def _default_origins() -> set[str]:
    values = {"http://127.0.0.1:5055", "http://localhost:5055"}
    values.update(f"http://127.0.0.1:{port}" for port in _DEFAULT_FRONTEND_PORTS)
    values.update(f"http://localhost:{port}" for port in _DEFAULT_FRONTEND_PORTS)
    public = get_public_origin()
    if public:
        values.add(public)
    return values


def get_allowed_origins() -> frozenset[str]:
    """Return normalized exact origins for CORS, WebSocket, and upload checks."""
    raw = os.environ.get(_ORIGINS_ENV, "").strip()
    values: Iterable[str] = raw.split(",") if raw else _default_origins()
    normalized = {normalize_origin(item) for item in values if str(item).strip()}
    public = get_public_origin()
    if public:
        normalized.add(public)
    if not normalized:
        raise ValueError("At least one exact allowed origin is required")
    return frozenset(normalized)


def bind_host() -> str:
    """Return the configured bind host; loopback is the only implicit default."""
    configured = os.environ.get("D3M0N_BIND_HOST", "").strip()
    return configured or DEFAULT_HOST


def api_port() -> int:
    raw = os.environ.get("D3M0N_API_PORT", str(DEFAULT_PORT)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("D3M0N_API_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError("D3M0N_API_PORT must be between 1 and 65535")
    return value


def state_dir() -> Path:
    """Return a per-user state directory outside the source checkout."""
    override = os.environ.get(_STATE_ENV, "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        checkout = Path(__file__).resolve().parents[1]
        if path == checkout or checkout in path.parents:
            raise ValueError("Runtime state must be outside the source checkout")
        return path
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
        return Path(root) / "D3m0n" / "state"
    root = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
    return Path(root) / "d3m0n"


def _windows_private_acl(path: Path, *, directory: bool) -> None:
    """Remove inherited Windows ACLs and grant only the current user access."""
    if os.name != "nt":
        return
    from ops.windows_security import protect
    protect(path, directory=directory)


def ensure_private_dir(path: Path) -> Path:
    """Create a private directory, failing closed if platform privacy cannot apply."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        if os.name != "nt":
            raise PermissionError("Unable to protect private state directory")
    _windows_private_acl(path, directory=True)
    return path


def ensure_private_file(path: Path) -> Path:
    """Apply private file permissions/ACLs, failing closed for secret state."""
    path = Path(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise PermissionError("Unable to protect private state file")
    _windows_private_acl(path, directory=False)
    return path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write private bytes durably and atomically outside the source tree."""
    path = Path(path)
    ensure_private_dir(path.parent)
    tmp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=f".{secrets.token_hex(4)}.tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(tmp)
        os.replace(tmp, path)
        ensure_private_file(path)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
