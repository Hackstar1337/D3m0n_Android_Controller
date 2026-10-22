"""Device proxy adapter for the installed Clash Meta Android client.

This module deliberately keeps credentials in memory.  It performs a bounded,
verified server-side probe before changing a device, then imports a transient
configuration through Clash Meta's documented URI and activity controls.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import shlex
import hashlib
import xml.etree.ElementTree as ET
import time
import os
import secrets
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote

try:  # PyYAML is part of the dashboard's supported runtime.
    import yaml
except Exception:  # pragma: no cover - the fallback is only for importability.
    yaml = None


PACKAGE = "com.github.metacubex.clash.meta"
CONTROL_ACTIVITY = "com.github.kr328.clash.ExternalControlActivity"
START_ACTION = "com.github.metacubex.clash.meta.action.START_CLASH"
STOP_ACTION = "com.github.metacubex.clash.meta.action.STOP_CLASH"
TOGGLE_ACTION = "com.github.metacubex.clash.meta.action.TOGGLE_CLASH"
BOOTSTRAP_SCHEMES = ("clash", "clashmeta")

# One-click client setup. The managed client is pinned to the release whose
# native screens the profile automation was verified against. Every APK, whether
# copied from a fleet phone or downloaded, must match the official release
# SHA-256 digest below before it is cached or installed.
CLIENT_VERSION_NAME = "2.11.31.Meta"
CLIENT_RELEASE_BASE = "https://github.com/MetaCubeX/ClashMetaForAndroid/releases/download/v2.11.31/"
CLIENT_APKS: dict[str, tuple[str, int, str]] = {
    "arm64-v8a": ("cmfa-2.11.31-meta-arm64-v8a-release.apk", 43980963, "1c02068183b9fbdf8926e8180d3e313d56eb19f8e5dd944aab909e288aceef5c"),
    "armeabi-v7a": ("cmfa-2.11.31-meta-armeabi-v7a-release.apk", 44178911, "19765f5b263dac1cfff4188d7416972ca63d62316dc2e49fd287320f1e523872"),
    "x86_64": ("cmfa-2.11.31-meta-x86_64-release.apk", 45739996, "a0f2387486a9f4e5b4bc90e613a42b844d32de4543dc7459a21bcad45400a6bc"),
    "x86": ("cmfa-2.11.31-meta-x86-release.apk", 46213856, "439c3c71148b618dcbeb1d635a65c28fe270aee53487734cdf8fe803b5e0705c"),
    "universal": ("cmfa-2.11.31-meta-universal-release.apk", 104908616, "106e59fde91148376ac9c5f0a0858ef4f7044fbd674bc89b3362186e4ed62837"),
}
CLIENT_CACHE_DIR = Path(os.environ.get("D3M0N_PROXY_CLIENT_CACHE", "~/.local/share/android-agent/proxy-client")).expanduser()
_INSTALL_TIMEOUT = 240.0
_apk_lock: asyncio.Lock | None = None
_digest_cache: dict[tuple[str, int, int], str] = {}


def _apk_guard() -> asyncio.Lock:
    global _apk_lock
    if _apk_lock is None:
        _apk_lock = asyncio.Lock()
    return _apk_lock


def _sha256_file(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _digest_cache:
        return _digest_cache[key]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _digest_cache[key] = value
    return value


def _verified(path: Path, size: int, digest: str) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size and _sha256_file(path) == digest
    except OSError:
        return False


def _download_sync(url: str, dest: Path, size: int, digest: str, timeout: float = 30.0) -> None:
    if not url.startswith(CLIENT_RELEASE_BASE):
        raise SetupRequired("Client download source is not the pinned release")
    request = urllib.request.Request(url, headers={"User-Agent": "d3m0n-proxy-client/1"})
    hasher = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise SetupRequired("Downloaded client is larger than the pinned release")
            hasher.update(chunk)
            out.write(chunk)
    if total != size or hasher.hexdigest() != digest:
        raise SetupRequired("Downloaded client failed checksum verification")


def _install_failure(output: str) -> str:
    match = re.search(r"\b(INSTALL_[A-Z_]+)\b", output or "")
    return f"Android rejected the Clash Meta install ({match.group(1)})" if match else "Android rejected the Clash Meta install"
TARGET_HOST = "api64.ipify.org"
TARGET_PORT = 443
TARGET_PATH = "/?format=json"
MAX_RESPONSE = 16 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{16,256}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")

PublishConfig = Callable[[str, bytes], tuple[str, str] | Awaitable[tuple[str, str]]]
RevokeConfig = Callable[[str], None | Awaitable[None]]


class AdapterError(Exception):
    """An internal error whose text is safe to turn into a generic result."""


class SetupRequired(AdapterError):
    """The user must complete an Android/client/permission step."""


@dataclass(frozen=True)
class _Proxy:
    kind: str
    host: str
    port: int
    username: str | None
    password: str | None


def _generic_message(exc: BaseException) -> str:
    """Never reflect exception text because it can contain a proxy credential."""
    if isinstance(exc, SetupRequired):
        return str(exc)
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Proxy connection timed out"
    if isinstance(exc, ssl.SSLError):
        return "TLS verification failed"
    if isinstance(exc, (ConnectionError, OSError)):
        return "Proxy connection failed"
    return "Proxy operation failed"


def _normalize_proxy_host(host: str) -> str:
    """Return a bare host, accepting exactly one valid IPv6 bracket pair."""
    if "[" in host or "]" in host:
        if not (host.startswith("[") and host.endswith("]")):
            raise AdapterError("Invalid proxy host")
        host = host[1:-1]
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise AdapterError("Invalid proxy host") from exc
        if address.version != 6:
            raise AdapterError("Invalid proxy host")
    elif ":" in host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise AdapterError("Invalid proxy host") from exc
        if address.version != 6:
            raise AdapterError("Invalid proxy host")
    return host


def _validated_global_exit_ip(value: object) -> str:
    exit_ip = str(value)
    address = ipaddress.ip_address(exit_ip)
    if not address.is_global:
        raise ValueError("not a public exit")
    return exit_ip


def _validate_config(config: Mapping[str, Any]) -> _Proxy:
    try:
        kind = str(config.get("proxy_type", "")).lower()
        host = config.get("host")
        port = config.get("port")
        username = config.get("username")
        password = config.get("password")
    except Exception as exc:
        raise AdapterError("Invalid proxy profile") from exc
    if kind not in {"http", "https", "socks5"}:
        raise AdapterError("Unsupported proxy type")
    if not isinstance(host, str) or not host or len(host) > 253:
        raise AdapterError("Invalid proxy host")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in host) or any(ch in host for ch in "/@?#"):
        raise AdapterError("Invalid proxy host")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise AdapterError("Invalid proxy port")
    if username is not None and (not isinstance(username, str) or len(username) > 512):
        raise AdapterError("Invalid proxy credentials")
    if password is not None and (not isinstance(password, str) or len(password) > 4096):
        raise AdapterError("Invalid proxy credentials")
    return _Proxy(kind, _normalize_proxy_host(host), port, username, password)


def _reject_local_addresses(host: str) -> None:
    """Reject literal or DNS-resolved local/metadata upstream endpoints."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConnectionError("proxy host could not be resolved") from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
    if not addresses:
        raise ConnectionError("proxy host could not be resolved")
    for address in addresses:
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            raise SetupRequired("Proxy host is not an allowed external endpoint")


def _read_response(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) <= MAX_RESPONSE:
        chunk = sock.recv(2048)
        if not chunk:
            break
        data.extend(chunk)
    marker = data.find(b"\r\n\r\n")
    if marker < 0 or marker > MAX_RESPONSE:
        raise ConnectionError("Invalid proxy response")
    head = bytes(data[:marker]).decode("iso-8859-1", "replace")
    body = bytes(data[marker + 4 :])
    first, *header_lines = head.split("\r\n")
    parts = first.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ConnectionError("Invalid proxy response")
    headers: dict[str, str] = {}
    for line in header_lines:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length = headers.get("content-length")
    if length and length.isdigit():
        remaining = min(MAX_RESPONSE - len(body), int(length))
        while len(body) < remaining:
            chunk = sock.recv(min(2048, remaining - len(body)))
            if not chunk:
                break
            body += chunk
    elif len(body) < MAX_RESPONSE and headers.get("transfer-encoding", "").lower() == "chunked":
        # A bounded chunked reader is enough for the small ipify response.
        while b"\r\n0\r\n" not in body and len(body) < MAX_RESPONSE:
            chunk = sock.recv(min(2048, MAX_RESPONSE - len(body)))
            if not chunk:
                break
            body += chunk
    return int(parts[1]), headers, body[:MAX_RESPONSE]


def _basic_auth(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _connect_upstream(proxy: _Proxy, timeout: float) -> socket.socket:
    host = _normalize_proxy_host(proxy.host)
    _reject_local_addresses(host)
    sock = socket.create_connection((host, proxy.port), timeout=timeout)
    sock.settimeout(timeout)
    if proxy.kind == "https":
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=host)
    auth = ""
    if proxy.username is not None:
        auth = f"Proxy-Authorization: Basic {_basic_auth(proxy.username, proxy.password or '')}\r\n"
    request = (
        f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
        f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n"
        "Proxy-Connection: Keep-Alive\r\n"
        f"{auth}\r\n"
    ).encode("ascii")
    sock.sendall(request)
    status, _, _ = _read_response(sock)
    if status != 200:
        sock.close()
        raise ConnectionError("Proxy CONNECT was rejected")
    return sock


def _socks5_connect(proxy: _Proxy, timeout: float) -> socket.socket:
    host = _normalize_proxy_host(proxy.host)
    _reject_local_addresses(host)
    sock = socket.create_connection((host, proxy.port), timeout=timeout)
    sock.settimeout(timeout)
    has_auth = proxy.username is not None
    sock.sendall(b"\x05\x01\x02" if has_auth else b"\x05\x01\x00")
    greeting = sock.recv(2)
    if len(greeting) != 2 or greeting[0] != 5 or greeting[1] == 0xFF:
        raise ConnectionError("SOCKS5 authentication was rejected")
    if greeting[1] == 2:
        if proxy.username is None:
            raise ConnectionError("SOCKS5 authentication was rejected")
        user = proxy.username.encode("utf-8")
        password = (proxy.password or "").encode("utf-8")
        if len(user) > 255 or len(password) > 255:
            raise AdapterError("Invalid proxy credentials")
        sock.sendall(bytes((1, len(user))) + user + bytes((len(password),)) + password)
        auth_result = sock.recv(2)
        if len(auth_result) != 2 or auth_result[1] != 0:
            raise ConnectionError("SOCKS5 authentication was rejected")
    elif greeting[1] != 0:
        raise ConnectionError("SOCKS5 authentication was rejected")
    target = TARGET_HOST.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes((len(target),)) + target + TARGET_PORT.to_bytes(2, "big"))
    response = sock.recv(4)
    if len(response) != 4 or response[0] != 5 or response[1] != 0:
        raise ConnectionError("SOCKS5 connection was rejected")
    atyp = response[3]
    if atyp == 1:
        tail = sock.recv(4 + 2)
    elif atyp == 4:
        tail = sock.recv(16 + 2)
    elif atyp == 3:
        size = sock.recv(1)
        tail = sock.recv((size[0] if size else 0) + 2)
    else:
        raise ConnectionError("Invalid SOCKS5 response")
    if len(tail) < 2:
        raise ConnectionError("Invalid SOCKS5 response")
    return sock


def _probe_sync(proxy: _Proxy, timeout: float = 8.0, *, allow_local: bool = False) -> str:
    """Probe the proxy and return only the externally observed IP."""
    if not allow_local and proxy.kind in {"http", "https"}:
        # HTTPX handles HTTPS-proxy TLS-in-TLS and bounded HTTP framing correctly.
        # Authentication is separate from the URL so it cannot leak in log URLs.
        import httpx
        host = _normalize_proxy_host(proxy.host)
        _reject_local_addresses(host)
        host = f"[{host}]" if ":" in host else host
        authentication = (proxy.username, proxy.password or "") if proxy.username is not None else None
        upstream = httpx.Proxy(f"{proxy.kind}://{host}:{proxy.port}", auth=authentication)
        with httpx.Client(proxy=upstream, verify=True, trust_env=False, follow_redirects=False, timeout=timeout) as client:
            with client.stream("GET", f"https://{TARGET_HOST}{TARGET_PATH}") as response:
                if response.status_code != 200:
                    raise ConnectionError("Proxy HTTPS probe was rejected")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise ConnectionError("Proxy HTTPS probe was too large")
                try:
                    exit_ip = _validated_global_exit_ip(json.loads(body)["ip"])
                except (ValueError, KeyError, TypeError):
                    raise ConnectionError("Proxy HTTPS probe returned an invalid address") from None
                return exit_ip
    if allow_local:
        # Used solely for the phone's loopback mixed-port verification.
        host = _normalize_proxy_host(proxy.host)
        sock = socket.create_connection((host, proxy.port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            request = (
                f"CONNECT {TARGET_HOST}:{TARGET_PORT} HTTP/1.1\r\n"
                f"Host: {TARGET_HOST}:{TARGET_PORT}\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            status, _, _ = _read_response(sock)
            if status != 200:
                raise ConnectionError("Device proxy is not ready")
        except Exception:
            sock.close()
            raise
    elif proxy.kind == "socks5":
        sock = _socks5_connect(proxy, timeout)
    else:
        sock = _connect_upstream(proxy, timeout)
    try:
        context = ssl.create_default_context()
        with context.wrap_socket(sock, server_hostname=TARGET_HOST) as tls_sock:
            request = (
                f"GET {TARGET_PATH} HTTP/1.1\r\nHost: {TARGET_HOST}\r\n"
                "Accept: application/json\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            tls_sock.sendall(request)
            status, headers, body = _read_response(tls_sock)
        if 300 <= status < 400 or status != 200:
            raise ConnectionError("Proxy HTTPS probe returned an unexpected status")
        content_type = headers.get("content-type", "")
        if content_type and "json" not in content_type.lower():
            raise ConnectionError("Proxy HTTPS probe returned an unexpected response")
        try:
            value = json.loads(body.decode("utf-8"))
            exit_ip = _validated_global_exit_ip(value["ip"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ConnectionError("Proxy HTTPS probe returned an invalid address") from exc
        return exit_ip
    finally:
        try:
            sock.close()
        except OSError:
            pass


def build_profile_yaml(config: Mapping[str, Any], *, mixed_port: int = 7890) -> bytes:
    """Build the one-upstream, loopback-only Clash Meta config in memory."""
    proxy = _validate_config(config)
    if yaml is None:
        raise AdapterError("YAML support is unavailable")
    proxy_entry: dict[str, Any] = {
        "name": "d3m0n-upstream",
        "type": "http" if proxy.kind in {"http", "https"} else "socks5",
        "server": proxy.host,
        "port": proxy.port,
    }
    if proxy.kind == "https":
        proxy_entry["tls"] = True
        proxy_entry["skip-cert-verify"] = False
    if proxy.username is not None:
        proxy_entry["username"] = proxy.username
        proxy_entry["password"] = proxy.password or ""
    document: dict[str, Any] = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        # Rule mode makes the explicit LAN-direct rules and final MATCH rule
        # authoritative without relying on a GUI-selected GLOBAL group.
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "proxies": [proxy_entry],
        "proxy-groups": [{"name": "D3MON", "type": "select", "proxies": ["d3m0n-upstream"]}],
        "rules": [
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
            "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
            "IP-CIDR6,::1/128,DIRECT,no-resolve",
            "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
            "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
            "MATCH,D3MON",
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False).encode("utf-8")


class ProxyDeviceDriver:
    """Async adapter; all socket and subprocess work is bounded and off-loop."""

    def __init__(
        self,
        publish_config: PublishConfig,
        revoke_config: RevokeConfig,
        *,
        adb_binary: str = "adb",
        backend_port: int = 5055,
        mixed_port: int = 7890,
        command_timeout: float = 15.0,
        probe_timeout: float = 8.0,
    ) -> None:
        self.publish_config = publish_config
        self.revoke_config = revoke_config
        self.adb_binary = adb_binary
        self.backend_port = backend_port
        self.mixed_port = mixed_port
        self.command_timeout = command_timeout
        self.probe_timeout = probe_timeout
        self._reverse_devices: set[str] = set()
        self._changed_devices: set[str] = set()

    async def _run_adb(self, serial: str, *args: str, timeout: float | None = None) -> tuple[int, str]:
        if not isinstance(serial, str) or not SERIAL_RE.fullmatch(serial):
            raise SetupRequired("Device serial is not valid")
        try:
            process = await asyncio.create_subprocess_exec(
                self.adb_binary,
                "-s",
                serial,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(
                process.communicate(), timeout=timeout or self.command_timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                process.kill()  # type: ignore[union-attr]
                await process.wait()
            except Exception:
                pass
            raise SetupRequired("Android command timed out") from exc
        except asyncio.CancelledError:
            try:
                process.kill()  # type: ignore[union-attr]
                await process.wait()
            except Exception:
                pass
            raise
        except OSError as exc:
            raise SetupRequired("ADB is unavailable") from exc
        return process.returncode or 0, out.decode("utf-8", "replace")[:131072]

    async def _probe(self, config: Mapping[str, Any]) -> str:
        proxy = _validate_config(config)
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_sync, proxy, self.probe_timeout),
            timeout=self.probe_timeout + 1.0,
        )

    async def test(self, config: Mapping[str, Any]) -> dict[str, Any]:
        try:
            exit_ip = await self._probe(config)
            return {
                "ok": True,
                "status": "ok",
                "message": "Server-side authenticated HTTPS preflight passed; device routing was not changed",
                "exit_ip": exit_ip,
                "verified_device": False,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "message": _generic_message(exc),
                "verified_device": False,
            }

    async def _vpn_state(self, serial: str) -> tuple[str, bool, bool]:
        code, provider = await self._run_adb(serial, "shell", "settings", "get", "secure", "always_on_vpn_app")
        if code != 0:
            raise SetupRequired("Android VPN settings could not be inspected")
        code, state = await self._run_adb(serial, "shell", "dumpsys", "connectivity")
        if code != 0:
            state = ""
        owners = re.findall(r"mPackage=([A-Za-z0-9_.]+)", state)
        other = provider.strip() not in ("", "null", PACKAGE) or any(x != PACKAGE for x in owners)
        code, interfaces = await self._run_adb(serial, "shell", "ip", "-brief", "address")
        tunnel = code == 0 and any(line.split() and line.split()[0].startswith("tun") and "DOWN" not in line for line in interfaces.splitlines())
        managed = PACKAGE in owners
        if tunnel and not owners:
            # Android 9/Samsung dumps omit mPackage/ownerUid completely. Do not
            # infer VPN ownership just from tun0 or a listening proxy socket.
            # Require CONNECTED VPN plus the system-bound managed VpnService.
            connected = bool(re.search(r"(?:type:\s*VPN|NetworkAgentInfo[^\n]*\bVPN\b)[^\n]*\bCONNECTED\b", state))
            code, services = await self._run_adb(serial, "shell", "dumpsys", "activity", "services", PACKAGE)
            component = PACKAGE + "/com.github.kr328.clash.service.TunService"
            managed = code == 0 and connected and component in services and "android.net.VpnService" in services and "ServiceRecord{" in services
            other = other or not managed
        return provider.strip(), other, tunnel and managed and not other

    async def _existing_vpn_is_other_app(self, serial: str) -> bool:
        return (await self._vpn_state(serial))[1]

    async def client_status(self, serial: str) -> dict[str, Any]:
        """Read-only readiness: is the managed client installed, and does another app own always-on VPN?

        Never changes settings, starts apps or touches routing.
        """
        try:
            code, output = await self._run_adb(serial, "shell", "pm", "path", PACKAGE, timeout=10)
            installed = code == 0 and "package:" in output
            code, provider = await self._run_adb(serial, "shell", "settings", "get", "secure", "always_on_vpn_app", timeout=10)
            provider = provider.strip() if code == 0 else ""
        except Exception as exc:
            return {"status": "unknown", "client_installed": None, "always_on_vpn": None, "message": _generic_message(exc)}
        always_on = provider not in ("", "null")
        if not installed:
            if always_on and provider != PACKAGE:
                return {"status": "other_vpn", "client_installed": False, "always_on_vpn": True, "vpn_consent": None, "message": "Another app owns the always-on VPN; setup will not take it over"}
            return {"status": "client_missing", "client_installed": False, "always_on_vpn": always_on, "vpn_consent": None, "message": "Clash Meta is not installed yet; Prepare or Apply installs it automatically"}
        if always_on and provider != PACKAGE:
            return {"status": "other_vpn", "client_installed": True, "always_on_vpn": True, "vpn_consent": None, "message": "Another app owns the always-on VPN; Apply will not take it over"}
        consent: bool | None = None
        try:
            code, ops = await self._run_adb(serial, "shell", "appops", "get", PACKAGE, "ACTIVATE_VPN", timeout=10)
            if code == 0:
                consent = "allow" in ops.casefold()
        except Exception:
            consent = None
        if consent is False:
            return {"status": "ready", "client_installed": True, "always_on_vpn": always_on, "vpn_consent": False, "message": "Client installed; VPN consent is pre-approved automatically on Prepare or Apply"}
        return {"status": "ready", "client_installed": True, "always_on_vpn": always_on, "vpn_consent": consent, "message": "Managed client installed and ready"}

    # ---- one-click client preparation -------------------------------------------------

    async def _run_host_adb(self, *args: str, timeout: float | None = None) -> tuple[int, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.adb_binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout or self.command_timeout)
        except asyncio.TimeoutError as exc:
            try:
                process.kill()  # type: ignore[union-attr]
                await process.wait()
            except Exception:
                pass
            raise SetupRequired("Android command timed out") from exc
        except OSError as exc:
            raise SetupRequired("ADB is unavailable") from exc
        return process.returncode or 0, out.decode("utf-8", "replace")[:131072]

    async def _installed_version(self, serial: str) -> tuple[bool, str | None]:
        code, output = await self._run_adb(serial, "shell", "pm", "path", PACKAGE, timeout=10)
        if code != 0 or "package:" not in output:
            return False, None
        code, info = await self._run_adb(serial, "shell", "dumpsys", "package", PACKAGE, timeout=15)
        match = re.search(r"versionName=(\S+)", info or "") if code == 0 else None
        return True, (match.group(1)[:40] if match else None)

    async def _device_abi(self, serial: str) -> str:
        code, output = await self._run_adb(serial, "shell", "getprop", "ro.product.cpu.abilist", timeout=10)
        for abi in (output.strip().split(",") if code == 0 else []):
            if abi.strip() in CLIENT_APKS:
                return abi.strip()
        return "universal"

    async def _online_serials(self) -> list[str]:
        code, output = await self._run_host_adb("devices", timeout=15)
        if code != 0:
            return []
        serials = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device" and SERIAL_RE.fullmatch(parts[0]):
                serials.append(parts[0])
        return serials

    async def _copy_from_fleet(self, abi: str, dest: Path, exclude: str) -> bool:
        """Copy the pinned APK from a phone that already has exactly that build."""
        wanted = {CLIENT_APKS[abi][2]: CLIENT_APKS[abi], CLIENT_APKS["universal"][2]: CLIENT_APKS["universal"]}
        for donor in await self._online_serials():
            if donor == exclude:
                continue
            try:
                code, output = await self._run_adb(donor, "shell", "pm", "path", PACKAGE, timeout=10)
                paths = [line[len("package:"):].strip() for line in output.splitlines() if line.startswith("package:")]
                if code != 0 or len(paths) != 1 or not re.fullmatch(r"/data/app/[A-Za-z0-9._=+-]+/base\.apk", paths[0]):
                    continue
                code, remote = await self._run_adb(donor, "shell", "sha256sum", paths[0], timeout=90)
                remote_digest = remote.split()[0].strip().lower() if code == 0 and remote.split() else ""
                if remote_digest not in wanted:
                    continue
                name, size, digest = wanted[remote_digest]
                temp = dest.parent / f".partial-{secrets.token_hex(6)}.apk"
                try:
                    code, _ = await self._run_adb(donor, "pull", paths[0], str(temp), timeout=_INSTALL_TIMEOUT)
                    if code == 0 and await asyncio.to_thread(_verified, temp, size, digest):
                        os.replace(temp, dest.parent / name)
                        return True
                finally:
                    temp.unlink(missing_ok=True)
            except SetupRequired:
                continue
        return False

    async def _ensure_client_apk(self, abi: str, exclude: str = "") -> Path:
        """Return a cached APK that matches the pinned official digest, fetching it once if needed."""
        if abi not in CLIENT_APKS:
            abi = "universal"
        async with _apk_guard():
            for candidate in (abi, "universal"):
                name, size, digest = CLIENT_APKS[candidate]
                path = CLIENT_CACHE_DIR / name
                if await asyncio.to_thread(_verified, path, size, digest):
                    return path
            CLIENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(CLIENT_CACHE_DIR, 0o700)
            except OSError:
                pass
            if await self._copy_from_fleet(abi, CLIENT_CACHE_DIR / CLIENT_APKS[abi][0], exclude):
                for candidate in (abi, "universal"):
                    name, size, digest = CLIENT_APKS[candidate]
                    if await asyncio.to_thread(_verified, CLIENT_CACHE_DIR / name, size, digest):
                        return CLIENT_CACHE_DIR / name
            name, size, digest = CLIENT_APKS[abi]
            temp = CLIENT_CACHE_DIR / f".partial-{secrets.token_hex(6)}.apk"
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_download_sync, CLIENT_RELEASE_BASE + name, temp, size, digest),
                    timeout=_INSTALL_TIMEOUT,
                )
                os.replace(temp, CLIENT_CACHE_DIR / name)
            except SetupRequired:
                raise
            except Exception as exc:
                raise SetupRequired("Clash Meta could not be fetched; no fleet phone has the pinned build and the download failed") from exc
            finally:
                temp.unlink(missing_ok=True)
            return CLIENT_CACHE_DIR / name

    async def prepare_client(self, serial: str) -> dict[str, Any]:
        """One-click client readiness: install the pinned client if missing and pre-approve its VPN consent.

        Never imports a profile, starts the VPN, or changes routing. Another app's VPN is never touched.
        """
        steps: list[str] = []
        try:
            if await self._existing_vpn_is_other_app(serial):
                return {"ok": False, "status": "other_vpn", "message": "Another app owns or runs the VPN; it was not changed", "steps": steps}
            installed, version = await self._installed_version(serial)
            installed_now = False
            if not installed:
                apk = await self._ensure_client_apk(await self._device_abi(serial), exclude=serial)
                steps.append("apk-verified")
                code, output = await self._run_adb(serial, "install", "-r", "-g", str(apk), timeout=_INSTALL_TIMEOUT)
                if code != 0 or "Success" not in output:
                    raise SetupRequired(_install_failure(output))
                installed, version = await self._installed_version(serial)
                if not installed:
                    raise SetupRequired("Clash Meta install could not be confirmed")
                installed_now = True
                steps.append("installed")
            else:
                steps.append("already-installed")
            # Pre-approve Android's VPN consent so the managed start needs no on-phone dialog.
            await self._run_adb(serial, "shell", "appops", "set", PACKAGE, "ACTIVATE_VPN", "allow", timeout=10)
            code, ops = await self._run_adb(serial, "shell", "appops", "get", PACKAGE, "ACTIVATE_VPN", timeout=10)
            if code != 0 or "allow" not in ops.casefold():
                raise SetupRequired("Android VPN consent could not be pre-approved; open Clash Meta once and allow its VPN request")
            steps.append("vpn-consent")
            # Keep the VPN service alive in Doze (best effort; not required for routing).
            try:
                code, _ = await self._run_adb(serial, "shell", "dumpsys", "deviceidle", "whitelist", "+" + PACKAGE, timeout=10)
                if code == 0:
                    steps.append("battery-exempt")
            except SetupRequired:
                pass
            message = ("Clash Meta " + (version or CLIENT_VERSION_NAME) + " installed and VPN consent pre-approved") if installed_now else "Client ready; VPN consent pre-approved"
            return {"ok": True, "status": "ready", "message": message, "installed_now": installed_now, "version": version, "steps": steps}
        except Exception as exc:
            return {"ok": False, "status": "setup-required" if isinstance(exc, SetupRequired) else "failed", "message": _generic_message(exc), "steps": steps}

    async def _ui_tree(self, serial: str):
        # Samsung/Android 9 uiautomator reports only the output filename on
        # stdout. /dev/tty does not return XML, even when the command succeeds.
        # Keep the transient hierarchy private to the shell UID and remove it.
        path = "/data/local/tmp/d3m0n-proxy-ui-" + secrets.token_hex(6) + ".xml"
        try:
            code, _ = await self._run_adb(serial, "shell", "uiautomator", "dump", path, timeout=10)
            if code != 0:
                raise SetupRequired("The native proxy setup screen could not be read")
            code, raw = await self._run_adb(serial, "shell", "cat", path, timeout=5)
        finally:
            try:
                await self._run_adb(serial, "shell", "rm", "-f", path, timeout=5)
            except Exception:
                pass
        start, end = raw.find("<hierarchy"), raw.rfind("</hierarchy>")
        if code != 0 or start < 0 or end < 0:
            raise SetupRequired("Open Clash Meta on this phone to finish the managed profile setup")
        try:
            return ET.fromstring(raw[start:end+len("</hierarchy>")])
        except ET.ParseError:
            raise SetupRequired("The native proxy setup screen could not be read") from None

    async def _tap_node(self, serial: str, root, node) -> None:
        parents = {child: parent for parent in root.iter() for child in parent}
        while node.get("clickable") != "true" and node in parents:
            node = parents[node]
        bounds = list(map(int, re.findall(r"\d+", node.get("bounds", ""))))
        if node.get("clickable") != "true" or node.get("enabled", "true") != "true" or len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise SetupRequired("The native profile setup control is not available")
        code, _ = await self._run_adb(serial, "shell", "input", "tap", str((bounds[0]+bounds[2])//2), str((bounds[1]+bounds[3])//2))
        if code != 0:
            raise SetupRequired("The native profile setup action could not be completed")

    async def _scroll_profiles(self, serial: str, root) -> bool:
        """Scroll only the observed native Profiles list, never unrelated UI."""
        nodes = list(root.iter("node"))
        if not any(n.get("package") == PACKAGE and n.get("text", "").casefold() == "profiles" for n in nodes):
            return False
        lists = [n for n in nodes if n.get("package") == PACKAGE and n.get("resource-id", "").split("/")[-1] == "main_list" and n.get("class", "").endswith("RecyclerView")]
        if len(lists) != 1:
            return False
        bounds = list(map(int, re.findall(r"\d+", lists[0].get("bounds", ""))))
        if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3]-bounds[1] < 200:
            return False
        x = (bounds[0]+bounds[2])//2
        height = bounds[3]-bounds[1]
        # The activity toolbar may overlay the top of the RecyclerView.
        start = bounds[1]+int(height*.82)
        end = bounds[1]+int(height*.35)
        code, _ = await self._run_adb(serial, "shell", "input", "swipe", str(x), str(start), str(x), str(end), "300")
        if code != 0:
            raise SetupRequired("The managed profile list could not be scrolled")
        await asyncio.sleep(.3)
        return True

    async def _save_and_select_profile(self, serial: str, name: str) -> None:
        root = await self._ui_tree(serial)
        for _ in range(5):
            if any(n.get("text") == name for n in root.iter("node")):
                break
            await asyncio.sleep(.4)
            root = await self._ui_tree(serial)
        if not any(n.get("text") == name for n in root.iter("node")):
            raise SetupRequired("Complete the managed profile import in Clash Meta, then retry Apply")
        saves = [n for n in root.iter("node") if n.get("package") == PACKAGE and (n.get("content-desc", "").casefold() == "save" or n.get("text", "").casefold() == "save") and n.get("clickable") == "true"]
        if len(saves) != 1:
            raise SetupRequired("Save the new managed profile in Clash Meta before routing can be verified")
        await self._tap_node(serial, root, saves[0])
        await asyncio.sleep(1)
        # Navigate using exported launcher + observed native labels, never a non-exported activity.
        await self._run_adb(serial, "shell", "monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")
        for _ in range(12):
            root = await self._ui_tree(serial)
            labels = [n for n in root.iter("node") if n.get("package") == PACKAGE and n.get("text") == name]
            if len(labels) == 1:
                # Only an imported (not unsaved) profile may be selected.
                parents = {child: parent for parent in root.iter() for child in parent}
                row = labels[0]
                while row.get("resource-id", "").split("/")[-1] != "root_view" and row in parents:
                    row = parents[row]
                if any("unsaved" in (n.get("text", "").casefold()) for n in row.iter("node")):
                    await asyncio.sleep(1)
                    continue
                self._changed_devices.add(serial)
                await self._tap_node(serial, root, labels[0])
                root = await self._ui_tree(serial)
                matches = [n for n in root.iter("node") if n.get("text") == name]
                if len(matches) != 1:
                    raise SetupRequired("The managed profile selection could not be confirmed")
                parents = {child: parent for parent in root.iter() for child in parent}
                row = matches[0]
                while row.get("resource-id", "").split("/")[-1] != "root_view" and row in parents:
                    row = parents[row]
                if not any(n.get("class", "").endswith("RadioButton") and n.get("checked") == "true" for n in row.iter("node")):
                    raise SetupRequired("Select the managed profile in Clash Meta before applying routing")
                return
            if await self._scroll_profiles(serial, root):
                continue
            profiles = [n for n in root.iter("node") if n.get("package") == PACKAGE and n.get("text", "").casefold() in {"profile", "profiles"} and n.get("resource-id", "").split("/")[-1] != "activity_bar_title_view"]
            if len(profiles) == 1:
                await self._tap_node(serial, root, profiles[0])
            else:
                await asyncio.sleep(1)
        raise SetupRequired("Open Clash Meta Profiles and select the new D3m0n profile")

    async def _import_and_start(self, serial: str, token: str, path: str) -> None:
        if not TOKEN_RE.fullmatch(token) or path != "/api/proxy/bootstrap/" + token:
            raise SetupRequired("Client bootstrap link was invalid")
        if await self._existing_vpn_is_other_app(serial):
            raise SetupRequired("Another VPN is configured; it was not changed")
        name = "D3m0n-" + hashlib.sha256(serial.encode()).hexdigest()[:8] + "-" + token[:6]
        url = f"http://127.0.0.1:{self.backend_port}{path}"
        uri = f"clash://install-config?url={quote(url, safe='')}&name={name}&update-interval=0"
        code, _ = await self._run_adb(serial, "reverse", "--no-rebind", f"tcp:{self.backend_port}", f"tcp:{self.backend_port}")
        if code != 0:
            raise SetupRequired("Temporary Android config link is already in use or unavailable")
        self._reverse_devices.add(serial)
        code, output = await self._run_adb(serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(uri), "-p", PACKAGE)
        if code != 0 or "Error:" in output or "SecurityException" in output:
            raise SetupRequired("Clash Meta import UI is unavailable")
        await self._save_and_select_profile(serial, name)
        code, output = await self._run_adb(serial, "shell", "am", "start", "-n", f"{PACKAGE}/{CONTROL_ACTIVITY}", "-a", START_ACTION)
        if code != 0 or "Error:" in output or "SecurityException" in output:
            raise SetupRequired("Clash Meta start control is unavailable; open the app and allow VPN setup")
        for _ in range(12):
            await asyncio.sleep(.5)
            if (await self._vpn_state(serial))[2]:
                return
        raise SetupRequired("Clash Meta VPN is not active; open it and complete Android VPN consent")

    async def _verify_device(self, serial: str) -> str:
        # Forward only the phone's loopback mixed port; never expose a LAN listener.
        local_port = await asyncio.to_thread(self._free_local_port)
        code, _ = await self._run_adb(
            serial,
            "forward",
            f"tcp:{local_port}",
            f"tcp:{self.mixed_port}",
        )
        if code != 0:
            raise SetupRequired("Device proxy verification forwarding is unavailable")
        try:
            proxy = _Proxy("http", "127.0.0.1", local_port, None, None)
            return await asyncio.wait_for(
                asyncio.to_thread(_probe_sync, proxy, self.probe_timeout, allow_local=True),
                timeout=self.probe_timeout + 1.0,
            )
        finally:
            await self._run_adb(serial, "forward", "--remove", f"tcp:{local_port}")

    @staticmethod
    def _free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _stop_clash(self, serial: str) -> None:
        code, _ = await self._run_adb(
            serial,
            "shell",
            "am",
            "start",
            "-n",
            f"{PACKAGE}/{CONTROL_ACTIVITY}",
            "-a",
            STOP_ACTION,
        )
        if code != 0:
            raise SetupRequired("Clash Meta stop control is unavailable")

    async def apply(self, serial: str, config: Mapping[str, Any]) -> dict[str, Any]:
        preflight = await self.test(config)
        if not preflight.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "message": "Server-side authenticated HTTPS preflight failed; device was not changed",
                "verified_device": False,
            }
        # One click: install the managed client and pre-approve VPN consent when needed.
        prepared = await self.prepare_client(serial)
        if not prepared.get("ok"):
            return {
                "ok": False,
                "status": "setup-required",
                "message": str(prepared.get("message") or "Client setup failed") + "; routing was not changed",
                "verified_device": False,
            }
        token: str | None = None
        reverse_set = False
        device_change_started = False
        try:
            payload = build_profile_yaml(config, mixed_port=self.mixed_port)
            published = self.publish_config(serial, payload)
            if hasattr(published, "__await__"):
                token, path = await published  # type: ignore[misc]
            else:
                token, path = published  # type: ignore[misc]
            await self._import_and_start(serial, token, path)
            reverse_set = True
            device_change_started = True
            exit_ip = await self._verify_device(serial)
            if not (await self._vpn_state(serial))[2]:
                raise SetupRequired("The managed VPN tunnel is not active")
            return {
                "ok": True,
                "status": "ok",
                "message": "Clash Meta routing applied and verified from the device",
                "exit_ip": exit_ip,
                "verified_device": True,
            }
        except Exception as exc:
            # Do not leave a possibly active managed profile on an unverifiable device.
            if serial in self._changed_devices:
                try:
                    await self._stop_clash(serial)
                except Exception:
                    pass
            return {
                "ok": False,
                "status": "setup-required" if isinstance(exc, SetupRequired) or device_change_started else "failed",
                "message": _generic_message(exc),
                "verified_device": False,
            }
        except asyncio.CancelledError:
            if serial in self._changed_devices:
                try:
                    await asyncio.shield(self._stop_clash(serial))
                except Exception:
                    pass
            raise
        finally:
            self._changed_devices.discard(serial)
            if serial in self._reverse_devices:
                try:
                    await self._run_adb(
                        serial,
                        "reverse",
                        "--remove",
                        f"tcp:{self.backend_port}",
                    )
                except Exception:
                    pass
            self._reverse_devices.discard(serial)
            if token is not None:
                try:
                    result = self.revoke_config(token)
                    if hasattr(result, "__await__"):
                        await result  # type: ignore[misc]
                except Exception:
                    pass

    async def disable(self, serial: str) -> dict[str, Any]:
        try:
            if await self._existing_vpn_is_other_app(serial):
                raise SetupRequired("Another VPN is active; it was not changed")
            await self._stop_clash(serial)
            await asyncio.sleep(.3)
            _, other, active = await self._vpn_state(serial)
            code, rules = await self._run_adb(serial, "shell", "ip", "-4", "rule")
            if active or other or code != 0 or "prohibit" in rules:
                raise SetupRequired("Turn off Always-on VPN and Block connections without VPN in Android Settings to restore normal internet")
            return {
                "ok": True,
                "status": "ok",
                "message": "Managed Clash Meta routing disabled; user profiles were preserved",
                "verified_device": True,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "setup-required" if isinstance(exc, SetupRequired) else "failed",
                "message": _generic_message(exc),
                "verified_device": False,
            }


__all__ = ["ProxyDeviceDriver", "build_profile_yaml"]
