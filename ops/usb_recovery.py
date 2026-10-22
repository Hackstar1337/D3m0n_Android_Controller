"""USB-assisted recovery of fleet phones that lost wireless ADB.

These Android 9 phones keep the TCP ADB port only until the next reboot (the
persistent port property is not writable without root).  After a reboot adbd
listens on USB only.  When a *known* fleet phone appears on the host's USB, this
module switches it back to TCP 5555 and reconnects its wireless address.

Safety rules:
* Only phones already mapped to a fleet wireless address are touched.  Unknown
  USB phones are reported, never modified.
* A phone whose wireless transport is already online is never switched again
  (``adb tcpip`` restarts adbd and would drop its live stream).
* No scans, no ``adb kill-server``; every subprocess has a timeout.
* The alias file holds hardware serials, so it is owner-only (0600) and the
  serials are never included in reported results.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

TCP_PORT = 5555
MAX_ALIAS_QUERIES = 8
MAX_RECOVERIES_PER_PASS = 4  # each can take ~30 s; the timer unit allows 120 s
_HW_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")
_INET_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")
_alias_cache: dict[str, tuple[float, frozenset[str]]] = {}

Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def state_dir() -> Path:
    from ops.platform_config import state_dir as application_state_dir
    return application_state_dir()


def alias_path() -> Path:
    return state_dir() / "adb-usb-aliases.json"


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)


def valid_peer(peer: object) -> bool:
    if not isinstance(peer, str) or peer.count(":") != 1:
        return False
    host, port = peer.split(":")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return str(address) == host and address.is_private and not address.is_loopback and port.isdigit() and 1 <= int(port) <= 65535


def is_usb_serial(serial: str) -> bool:
    return ":" not in serial and not serial.startswith("emulator-") and serial.casefold() != "ios"


def parse_devices(output: str) -> list[dict]:
    """Parse ``adb devices -l`` into [{serial, state, usb}] (header/blank lines skipped)."""
    devices: list[dict] = []
    seen: set[str] = set()
    for line in (output or "").splitlines():
        fields = line.strip().split()
        if len(fields) < 2 or fields[0] == "List" or fields[0] in seen:
            continue
        serial, state = fields[0], fields[1]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,150}", serial):
            continue
        seen.add(serial)
        devices.append({"serial": serial, "state": state, "usb": is_usb_serial(serial)})
    return devices


def load_aliases(path: Path | None = None) -> dict[str, str]:
    """Return {hardware_serial: wireless_peer}; a missing or corrupt file is empty."""
    path = path or alias_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    result: dict[str, str] = {}
    if isinstance(raw, dict):
        for hw, entry in raw.items():
            peer = entry.get("peer") if isinstance(entry, dict) else None
            if isinstance(hw, str) and _HW_RE.fullmatch(hw) and valid_peer(peer):
                result[hw] = peer
    return result


def save_aliases(aliases: dict[str, str], path: Path | None = None) -> None:
    path = path or alias_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.time()
    payload = {hw: {"peer": peer, "updated_at": now} for hw, peer in sorted(aliases.items())}
    tmp = path.with_suffix(".next")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _alias_cache.pop(str(path), None)


def remember(peer: str, hw: str, path: Path | None = None) -> bool:
    """Map a hardware serial to its wireless peer (one hardware serial per peer)."""
    if not valid_peer(peer) or not isinstance(hw, str) or not _HW_RE.fullmatch(hw):
        return False
    aliases = load_aliases(path)
    if aliases.get(hw) == peer and list(aliases.values()).count(peer) == 1:
        return False
    aliases = {k: v for k, v in aliases.items() if v != peer}
    aliases[hw] = peer
    save_aliases(aliases, path)
    return True


def fleet_alias_serials(path: Path | None = None) -> frozenset[str]:
    """Hardware serials of known fleet phones, cached by file modification time."""
    path = path or alias_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset()
    cached = _alias_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    serials = frozenset(load_aliases(path))
    _alias_cache[str(path)] = (mtime, serials)
    return serials


def is_fleet_alias(serial: str, path: Path | None = None) -> bool:
    """True for a USB transport of a phone that the fleet already knows by Wi-Fi."""
    return isinstance(serial, str) and is_usb_serial(serial) and serial in fleet_alias_serials(path)


def query_hw_serial(peer: str, run: Runner = _run) -> str | None:
    try:
        result = run(["-s", peer, "shell", "getprop", "ro.serialno"], 4)
    except (OSError, subprocess.TimeoutExpired):
        return None
    hw = (result.stdout or "").strip()
    return hw if result.returncode == 0 and _HW_RE.fullmatch(hw) else None


def refresh_aliases(devices: list[dict], run: Runner = _run, path: Path | None = None, limit: int = MAX_ALIAS_QUERIES) -> int:
    """Learn hardware serials of online wireless phones that are not mapped yet."""
    known = set(load_aliases(path).values())
    learned = 0
    for device in devices:
        if learned >= limit:
            break
        peer = device["serial"]
        if device["usb"] or device["state"] != "device" or not valid_peer(peer) or peer in known:
            continue
        hw = query_hw_serial(peer, run)
        if hw and remember(peer, hw, path):
            known.add(peer)
            learned += 1
    return learned


def usb_states_by_peer(devices: list[dict], path: Path | None = None) -> dict[str, str]:
    """{wireless_peer: usb_state} for fleet phones currently attached by USB."""
    usb = {d["serial"]: d["state"] for d in devices if d["usb"]}
    return {peer: usb[hw] for hw, peer in load_aliases(path).items() if hw in usb}


def _wifi_ip(hw: str, run: Runner) -> str | None:
    try:
        result = run(["-s", hw, "shell", "ip", "-4", "addr", "show", "wlan0"], 4)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _INET_RE.search(result.stdout or "")
    if not match:
        return None
    try:
        address = ipaddress.IPv4Address(match.group(1))
    except ValueError:
        return None
    return str(address) if address.is_private and not address.is_loopback else None


def recover_usb(devices: list[dict], run: Runner = _run, path: Path | None = None, sleep: Callable[[float], None] = time.sleep, limit: int = MAX_RECOVERIES_PER_PASS) -> list[dict]:
    """Restore wireless ADB for known fleet phones that are on USB but offline by Wi-Fi."""
    aliases = load_aliases(path)
    online = {d["serial"] for d in devices if not d["usb"] and d["state"] == "device"}
    results: list[dict] = []
    attempts = 0
    for device in devices:
        if not device["usb"]:
            continue
        hw, state = device["serial"], device["state"]
        peer = aliases.get(hw)
        if not peer:
            results.append({"peer": None, "state": "usb_unknown"})
            continue
        if state != "device":
            # unauthorized: the phone shows an "Allow USB debugging" prompt.
            results.append({"peer": peer, "state": "usb_" + re.sub(r"[^a-z]", "", state.lower())[:20]})
            continue
        if peer in online:
            results.append({"peer": peer, "state": "usb_alias_online"})
            continue
        if attempts >= limit:
            results.append({"peer": peer, "state": "usb_retry_later"})
            continue
        attempts += 1
        try:
            port = run(["-s", hw, "shell", "getprop", "service.adb.tcp.port"], 4)
            switched = False
            if (port.stdout or "").strip() != str(TCP_PORT):
                run(["-s", hw, "tcpip", str(TCP_PORT)], 8)
                switched = True
                sleep(3)  # adbd restarts on the phone and re-enumerates on USB
                run(["-s", hw, "wait-for-device"], 10)
            ip = _wifi_ip(hw, run)
            if not ip:
                results.append({"peer": peer, "state": "usb_waiting_wifi", "switched": switched})
                continue
            target = f"{ip}:{TCP_PORT}"
            run(["disconnect", target], 3)
            run(["connect", target], 6)
            current = run(["-s", target, "get-state"], 3)
            ok = current.returncode == 0 and (current.stdout or "").strip() == "device"
            item = {"peer": peer, "state": "usb_recovered" if ok else "usb_connect_failed", "switched": switched}
            if target != peer:
                item["new_peer"] = target
                if ok:
                    remember(target, hw, path)
            results.append(item)
        except (OSError, subprocess.TimeoutExpired):
            results.append({"peer": peer, "state": "usb_retry_later"})
    return results


def cycle(run: Runner = _run, path: Path | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """One timer pass: learn missing aliases, then recover USB-attached fleet phones."""
    inventory = run(["devices", "-l"], 5)
    if inventory.returncode != 0:
        return {"error": "adb_inventory_unavailable"}
    devices = parse_devices(inventory.stdout)
    learned = refresh_aliases(devices, run, path)
    results = recover_usb(devices, run, path, sleep)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {"aliases_learned": learned, "aliases_known": len(load_aliases(path)), "usb_counts": counts, "usb": results}
