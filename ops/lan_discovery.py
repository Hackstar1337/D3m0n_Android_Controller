"""Read-only-first Android discovery primitives.

This module deliberately keeps discovery separate from phone control.  Interface
inspection, mDNS, and TCP probes never call ``adb connect`` or ``adb tcpip``.
Enrollment is an explicit, candidate-id-only operation and is the only place
where an ADB transport is created.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Iterable, Mapping

try:
    import psutil
except ImportError:  # The injected providers keep parser/API tests dependency-light.
    psutil = None  # type: ignore[assignment]

MAX_HOSTS = 1024
MAX_WORKERS = 16
MAX_PROBE_TIMEOUT = 0.3
MAX_SCAN_SECONDS = 15.0
DEFAULT_CANDIDATE_TTL = 300.0
_MDNS_SERVICES = {"_adb-tls-pairing._tcp", "_adb-tls-connect._tcp"}
_RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _utc_iso(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or time.time(), tz=timezone.utc).isoformat()


def _private_network(network: ipaddress.IPv4Network) -> bool:
    """RFC1918 only; excludes loopback, link-local, multicast and public space."""
    return any(network.subnet_of(allowed) for allowed in _RFC1918_NETWORKS)


def _clean_service(value: str) -> str:
    value = value.strip().rstrip(".")
    return value if value in _MDNS_SERVICES else ""


def list_ipv4_interfaces(
    *,
    addresses: Callable[[], Mapping[str, Iterable[Any]]] | None = None,
    stats: Callable[[], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return usable IPv4 interface metadata without invoking platform commands."""
    if addresses is None or stats is None:
        if psutil is None:
            raise RuntimeError("psutil is required for host interface discovery")
        addresses = addresses or psutil.net_if_addrs
        stats = stats or psutil.net_if_stats
    result: list[dict[str, Any]] = []
    for name, entries in addresses().items():
        up = bool(getattr(stats().get(name), "isup", False))
        for entry in entries:
            if getattr(entry, "family", None) != socket.AF_INET:
                continue
            address = str(getattr(entry, "address", ""))
            netmask = str(getattr(entry, "netmask", ""))
            try:
                ip = ipaddress.ip_address(address)
                network = ipaddress.ip_network(f"{address}/{netmask}", strict=False)
            except ValueError:
                continue
            if not isinstance(ip, ipaddress.IPv4Address) or not netmask:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                continue
            result.append(
                {
                    "name": str(name),
                    "address": address,
                    "netmask": netmask,
                    "cidr": str(network),
                    "up": up,
                    "private": _private_network(network),
                }
            )
    return sorted(result, key=lambda item: (not item["up"], item["name"], item["address"]))


def parse_mdns_services(output: str) -> list[dict[str, Any]]:
    """Parse platform-specific ``adb mdns services`` text.

    ADB has emitted both ``host:port service`` and ``service host:port`` forms.
    Only the two Wireless Debugging service names are accepted.  Ports are kept
    per service; a pairing port is never promoted to a connect port.
    """
    found: dict[tuple[str, int, str], dict[str, Any]] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(("list of", "error", "failed")):
            continue
        service_match = re.search(r"(_adb-tls-(?:pairing|connect)\._tcp)\.?", line, re.I)
        if not service_match:
            continue
        service = _clean_service(service_match.group(1).lower())
        if not service:
            continue
        before = line[: service_match.start()]
        after = line[service_match.end() :]
        endpoint_match = re.search(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})(?!\d)", before + " " + after)
        if not endpoint_match:
            continue
        host, port_text = endpoint_match.groups()
        try:
            address = ipaddress.ip_address(host)
            port = int(port_text)
        except ValueError:
            continue
        if not isinstance(address, ipaddress.IPv4Address) or not (1 <= port <= 65535):
            continue
        key = (host, port, service)
        found[key] = {"host": host, "port": port, "service": service}
    return sorted(found.values(), key=lambda row: (row["host"], row["service"], row["port"]))


def _run_adb(args: list[str], *, timeout: float = 2.0) -> tuple[int, str, str]:
    """Run a read-only ADB metadata command; output is intentionally not logged."""
    executable = os.environ.get("D3M0N_ADB", "adb")
    try:
        completed = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", exc.__class__.__name__


def discover_mdns(
    *,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Passively ask ADB's mDNS browser for Wireless Debugging services."""
    runner = runner or (lambda args, limit: _run_adb(args, timeout=limit))
    try:
        code, stdout, _stderr = runner(["mdns", "services"], min(max(timeout, 0.1), 3.0))
    except Exception as exc:  # injected runner diagnostics must stay safe
        return {"available": False, "services": [], "diagnostic": exc.__class__.__name__}
    services = parse_mdns_services(stdout)
    if code != 0:
        return {"available": False, "services": services, "diagnostic": "mDNS unavailable or firewalled"}
    return {
        "available": True,
        "services": services,
        "diagnostic": "ok" if services else "mDNS returned no Wireless Debugging services",
    }


def _interface_networks(interfaces: Iterable[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], ipaddress.IPv4Network]]:
    result = []
    for interface in interfaces:
        try:
            network = ipaddress.ip_network(str(interface["cidr"]), strict=False)
        except (KeyError, ValueError, TypeError):
            continue
        if isinstance(network, ipaddress.IPv4Network):
            result.append((interface, network))
    return result


def validate_private_cidr(
    cidr: str, *, interfaces: Iterable[Mapping[str, Any]], max_hosts: int = MAX_HOSTS
) -> tuple[ipaddress.IPv4Network, Mapping[str, Any]]:
    """Validate a user-selected subnet is private and contained by a local interface."""
    try:
        network = ipaddress.ip_network(str(cidr), strict=False)
    except ValueError as exc:
        raise ValueError("a valid IPv4 CIDR is required") from exc
    if not isinstance(network, ipaddress.IPv4Network) or not _private_network(network):
        raise ValueError("only RFC1918 IPv4 networks may be scanned")
    if network.num_addresses > max_hosts + 2:
        raise ValueError(f"selected network exceeds the {max_hosts}-host cap")
    matches = [item for item, parent in _interface_networks(interfaces) if network.subnet_of(parent)]
    if not matches:
        raise ValueError("selected network is not contained by a local interface")
    if not any(bool(item.get("up", True)) for item in matches):
        raise ValueError("selected interface is not up")
    return network, next(item for item in matches if bool(item.get("up", True)))


def _hosts(network: ipaddress.IPv4Network, max_hosts: int) -> list[str]:
    return [str(host) for host in network.hosts()][:max_hosts]


def probe_private_lan(
    cidr: str,
    ports: Iterable[int] = (5555,),
    *,
    interfaces: Iterable[Mapping[str, Any]] | None = None,
    max_hosts: int = MAX_HOSTS,
    max_workers: int = MAX_WORKERS,
    timeout: float = MAX_PROBE_TIMEOUT,
    total_timeout: float = MAX_SCAN_SECONDS,
    socket_factory: Callable[[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Bounded TCP openness probe; this never invokes ADB and never creates a transport."""
    max_hosts = min(max(int(max_hosts), 1), MAX_HOSTS)
    max_workers = min(max(int(max_workers), 1), MAX_WORKERS)
    timeout = min(max(float(timeout), 0.01), MAX_PROBE_TIMEOUT)
    total_timeout = min(max(float(total_timeout), 0.1), MAX_SCAN_SECONDS)
    interface_rows = list(interfaces if interfaces is not None else list_ipv4_interfaces())
    network, interface = validate_private_cidr(cidr, interfaces=interface_rows, max_hosts=max_hosts)
    port_list = sorted({int(port) for port in ports if 1 <= int(port) <= 65535})
    if not port_list:
        raise ValueError("at least one valid TCP port is required")
    targets = [(host, port) for host in _hosts(network, max_hosts) for port in port_list]
    socket_factory = socket_factory or socket.create_connection
    deadline = time.monotonic() + total_timeout

    def check(target: tuple[str, int]) -> tuple[str, int] | None:
        if time.monotonic() >= deadline:
            return None
        host, port = target
        remaining = min(timeout, max(deadline - time.monotonic(), 0.01))
        try:
            connection = socket_factory((host, port), timeout=remaining)
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            return target
        except (OSError, TimeoutError):
            return None

    open_targets: list[tuple[str, int]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(check, target) for target in targets]
        for future in as_completed(futures):
            if time.monotonic() > deadline:
                break
            try:
                result = future.result(timeout=max(0.01, deadline - time.monotonic()))
            except Exception:
                result = None
            if result:
                open_targets.append(result)
    open_targets.sort()
    expires = time.time() + DEFAULT_CANDIDATE_TTL
    return {
        "interface": dict(interface),
        "cidr": str(network),
        "hosts_considered": min(len(_hosts(network, max_hosts)), max_hosts),
        "ports": port_list,
        "capped": network.num_addresses - 2 > max_hosts,
        "elapsed_seconds": round(min(total_timeout, max(time.monotonic() - (deadline - total_timeout), 0)), 3),
        "candidates": [
            {
                "source": "tcp_probe",
                "host": host,
                "port": port,
                "service": "adb-tcp",
                "transport": "adb",
                "pairing_required": False,
                "interface": dict(interface),
                "expires_at": _utc_iso(expires),
            }
            for host, port in open_targets
        ],
    }


def _candidate_from_service(service: Mapping[str, Any], interfaces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    host = str(service["host"])
    interface = next(
        (dict(item) for item in interfaces if _host_in_interface(host, item)),
        {},
    )
    pairing = service["service"] == "_adb-tls-pairing._tcp"
    return {
        "source": "mdns",
        "host": host,
        "port": int(service["port"]),
        "service": str(service["service"]),
        "transport": "adb-tls",
        "pairing_required": pairing,
        "interface": interface,
        "expires_at": _utc_iso(time.time() + DEFAULT_CANDIDATE_TTL),
    }


def _host_in_interface(host: str, interface: Mapping[str, Any]) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(str(interface["cidr"]), strict=False)
    except (ValueError, KeyError, TypeError):
        return False


class CandidateStore:
    """Ephemeral candidate IDs with TTL and per-candidate enrollment leases."""

    def __init__(self, ttl: float = DEFAULT_CANDIDATE_TTL, clock: Callable[[], float] = time.time) -> None:
        self.ttl = float(ttl)
        self.clock = clock
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._leased: set[str] = set()
        self._lock = threading.RLock()

    def put(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        now = self.clock()
        candidate_id = secrets.token_urlsafe(18)
        expires = min(float(candidate.get("expires_at_epoch", now + self.ttl)), now + self.ttl)
        stored = dict(candidate)
        stored.pop("expires_at_epoch", None)
        stored["candidate_id"] = candidate_id
        stored["expires_at"] = _utc_iso(expires)
        with self._lock:
            self._purge(now)
            self._items[candidate_id] = (expires, stored)
        return dict(stored)

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge(self.clock())
            item = self._items.get(str(candidate_id))
            return dict(item[1]) if item else None

    def lease(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            self._purge(self.clock())
            if candidate_id in self._leased:
                raise RuntimeError("candidate is already being enrolled")
            item = self._items.get(str(candidate_id))
            if not item:
                raise KeyError("candidate expired or unknown")
            self._leased.add(candidate_id)
            return dict(item[1])

    def release(self, candidate_id: str, *, consume: bool = False) -> None:
        with self._lock:
            self._leased.discard(candidate_id)
            if consume:
                self._items.pop(candidate_id, None)

    def _purge(self, now: float) -> None:
        for candidate_id, (expires, _candidate) in list(self._items.items()):
            if expires <= now:
                self._items.pop(candidate_id, None)
                self._leased.discard(candidate_id)


candidate_store = CandidateStore()


def scan_candidates(
    *,
    methods: Iterable[str] = ("mdns",),
    interfaces: Iterable[Mapping[str, Any]] | None = None,
    cidr: str | None = None,
    ports: Iterable[int] = (5555,),
    max_hosts: int = MAX_HOSTS,
    runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    socket_factory: Callable[[str, int], Any] | None = None,
) -> dict[str, Any]:
    interfaces = list(interfaces if interfaces is not None else list_ipv4_interfaces())
    normalized = {str(method).lower() for method in methods}
    if not normalized or not normalized <= {"mdns", "probe", "lan", "tcp"}:
        raise ValueError("methods must contain only mdns and probe")
    rows: list[dict[str, Any]] = []
    mdns: dict[str, Any] | None = None
    if "mdns" in normalized:
        mdns = discover_mdns(runner=runner)
        for item in mdns["services"]:
            try:
                host_ip = ipaddress.ip_address(item["host"])
            except ValueError:
                continue
            if not isinstance(host_ip, ipaddress.IPv4Address) or not any(
                _private_network(ipaddress.ip_network(str(row["cidr"]), strict=False)) and _host_in_interface(item["host"], row)
                for row in interfaces
            ):
                continue
            rows.append(_candidate_from_service(item, interfaces))
    probe_result: dict[str, Any] | None = None
    if normalized & {"probe", "lan", "tcp"}:
        if not cidr:
            raise ValueError("a selected private CIDR is required for LAN probing")
        probe_result = probe_private_lan(
            cidr,
            ports,
            interfaces=interfaces,
            max_hosts=max_hosts,
            socket_factory=socket_factory,
        )
        rows.extend(probe_result["candidates"])
    # De-duplicate service advertisements without merging pairing/connect ports.
    unique: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        unique[(row["host"], int(row["port"]), row["service"])] = row
    issued = [candidate_store.put(row) for row in unique.values()]
    public = [{key: value for key, value in row.items() if key != "candidate_id"} | {"candidate_id": row["candidate_id"]} for row in issued]
    return {"ok": True, "methods": sorted(normalized), "mdns": mdns, "probe": probe_result, "candidates": public}


def _adb_success(output: str) -> bool:
    text = (output or "").lower()
    return "connected" in text or "already connected" in text or "successfully paired" in text


def _run_enrollment_adb(
    args: list[str], *, timeout: float = 10.0, stdin: str | None = None,
    runner: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[int, str, str]:
    if runner is not None:
        try:
            return runner(args, timeout, stdin)
        except TypeError:
            return runner(args, timeout)
    executable = os.environ.get("D3M0N_ADB", "adb")
    try:
        completed = subprocess.run(
            [executable, *args], input=stdin, capture_output=True, text=True, timeout=timeout, check=False
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", exc.__class__.__name__


def enroll_candidate(
    candidate_id: str,
    *,
    action: str,
    pairing_code: str | None = None,
    mdns_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    adb_runner: Callable[..., tuple[int, str, str]] | None = None,
    interfaces: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explicitly connect or pair one issued candidate; pairing code only enters stdin."""
    action = str(action).lower().strip()
    if action not in {"connect", "pair"}:
        raise ValueError("action must be connect or pair")
    if action == "pair" and not pairing_code:
        raise ValueError("pairing code is required for explicit pairing")
    candidate = candidate_store.lease(str(candidate_id))
    consume = False
    try:
        if action == "pair" and not candidate.get("pairing_required"):
            raise ValueError("candidate does not require pairing")
        if action == "connect" and candidate.get("pairing_required"):
            raise ValueError("pairing is required for this candidate")
        host = str(candidate["host"])
        port = int(candidate["port"])
        if action == "pair":
            code, stdout, _stderr = _run_enrollment_adb(
                ["pair", f"{host}:{port}"], stdin=str(pairing_code).strip() + "\n", runner=adb_runner
            )
            # The code is not retained, included in errors, or passed as an argv token.
            if code != 0 or not _adb_success(stdout):
                return {"ok": False, "candidate_id": candidate_id, "error": "pairing failed"}
            refreshed = discover_mdns(runner=mdns_runner)
            connect_services = [
                item for item in refreshed["services"]
                if item["host"] == host and item["service"] == "_adb-tls-connect._tcp"
            ]
            if not connect_services:
                return {"ok": False, "candidate_id": candidate_id, "error": "paired, but no connect service was discovered"}
            connect = min(connect_services, key=lambda item: int(item["port"]))
            port = int(connect["port"])
        serial = f"{host}:{port}"
        code, stdout, _stderr = _run_enrollment_adb(
            ["connect", serial], runner=adb_runner, timeout=5.0
        )
        if code != 0 or not _adb_success(stdout):
            return {"ok": False, "candidate_id": candidate_id, "error": "ADB connect failed"}
        state_code, state_out, _ = _run_enrollment_adb(["-s", serial, "get-state"], runner=adb_runner, timeout=3.0)
        identity_code, identity_out, _ = _run_enrollment_adb(["-s", serial, "get-serialno"], runner=adb_runner, timeout=3.0)
        if state_code != 0 or state_out.strip().lower() != "device" or identity_code != 0 or not identity_out.strip():
            return {"ok": False, "candidate_id": candidate_id, "error": "connected device did not pass read-only verification"}
        consume = True
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "enrolled": {"device_serial": serial, "host": host, "port": port, "transport": "adb"},
            "event": "enrolled",
        }
    finally:
        candidate_store.release(str(candidate_id), consume=consume)


def usb_transport_status(
    *, runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> list[dict[str, str]]:
    """Parse all ADB transport states while redacting serials from the response."""
    runner = runner or (lambda args, limit: _run_adb(args, timeout=limit))
    try:
        code, stdout, _stderr = runner(["devices", "-l"], 3.0)
    except Exception:
        return []
    if code != 0:
        return []
    result = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        fields = line.split()
        if len(fields) < 2 or fields[1] not in {"device", "unauthorized", "offline"}:
            continue
        state = fields[1]
        result.append({"state": state, "transport": "usb" if ":" not in fields[0] else "adb", "identity": "redacted"})
    return result


def discovery_status(
    *, runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    mdns = discover_mdns(runner=runner)
    return {"ok": True, "usb": usb_transport_status(runner=runner), "mdns": mdns}


__all__ = [
    "CandidateStore", "MAX_HOSTS", "MAX_WORKERS", "MAX_PROBE_TIMEOUT", "MAX_SCAN_SECONDS",
    "candidate_store", "discover_mdns", "discovery_status", "enroll_candidate", "list_ipv4_interfaces",
    "parse_mdns_services", "probe_private_lan", "scan_candidates", "usb_transport_status", "validate_private_cidr",
]
