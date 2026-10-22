"""Confirmed fleet phone reboot with USB-assisted recovery status.

A reboot on these Android 9 phones drops wireless ADB (adbd returns to USB-only).
The host's d3m0n-adb-reconnect timer restores phones that are attached to the
host by USB (see ops.usb_recovery); phones without USB stay offline until they
are plugged in once.  This router therefore:

* reports, before rebooting, which phones are attached by USB (preflight);
* requires an explicit ``confirm: true``;
* skips phones that are offline, already rebooting or running an Agent task;
* tracks each reboot until the phone is genuinely back (fresh uptime and
  ``sys.boot_completed``) or needs USB.

Reboot is never mirrored through Sync; it is only reachable from this router.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ops import fleet_control, usb_recovery

router = APIRouter(prefix="/api/phone/reboot", tags=["fleet"])

MAX_DEVICES = 100
CONCURRENCY = 8
POLL_SECONDS = 3.0
BOOT_GRACE_SECONDS = 15.0       # the old transport can linger briefly after `adb reboot`
NO_RESTART_AFTER_SECONDS = 90.0  # still on the old uptime -> the reboot did not happen
NEEDS_USB_AFTER_SECONDS = 150.0
KEEP_FINISHED_SECONDS = 600.0
KEEP_WAITING_SECONDS = 6 * 3600.0
ACTIVE_STATES = {"rebooting", "recovering", "needs_usb"}

_records: dict[str, dict[str, Any]] = {}
_monitor_task: asyncio.Task | None = None
_start_lock = asyncio.Lock()


class DevicesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devices: list[str]


class RebootBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    devices: list[str]
    confirm: bool = False


def _validate(devices: Any) -> list[str]:
    if not isinstance(devices, list) or not devices:
        raise HTTPException(status_code=422, detail="Choose at least one phone")
    result: list[str] = []
    for serial in devices:
        serial = fleet_control._validate_serial(serial)
        if serial not in result:
            result.append(serial)
    if len(result) > MAX_DEVICES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_DEVICES} phones per reboot")
    return result


def agent_busy_devices() -> set[str]:
    try:
        from gitd.services.agent_chat import snapshot_sessions
    except Exception:
        return set()
    return {row.get("device") for row in snapshot_sessions() if row.get("running") or row.get("stopping")}


async def inventory() -> list[dict]:
    output = await fleet_control.run_adb("adb", "devices", "-l", timeout=6)
    return usb_recovery.parse_devices(output)


async def adb_shell(serial: str, command: str, timeout: float = 5) -> str:
    return await fleet_control.run_adb("adb", "-s", serial, "shell", command, timeout=timeout)


async def send_reboot(serial: str) -> None:
    try:
        await fleet_control.run_adb("adb", "-s", serial, "reboot", timeout=8)
    except fleet_control.AdbTimeoutError:
        pass  # the wireless transport usually drops before adb returns


async def _ensure_alias(serial: str) -> None:
    """Remember the phone's hardware serial so USB recovery can find it after the reboot."""
    if not usb_recovery.valid_peer(serial) or serial in usb_recovery.load_aliases().values():
        return
    try:
        hw = (await adb_shell(serial, "getprop ro.serialno", timeout=4)).strip()
    except fleet_control.AdbCommandError:
        return
    await asyncio.to_thread(usb_recovery.remember, serial, hw)


def _timer_usb_messages() -> dict[str, str]:
    """Latest USB recovery result per peer from the host timer state, if available."""
    try:
        state = json.loads((usb_recovery.state_dir() / "adb-reconnect.json").read_text())
    except (OSError, ValueError):
        return {}
    usb = state.get("usb") if isinstance(state, dict) else None
    items = usb.get("usb") if isinstance(usb, dict) else None
    return {item["peer"]: item.get("state", "") for item in items or [] if isinstance(item, dict) and item.get("peer")}


_USB_MESSAGES = {
    "usb_unauthorized": "On USB — tap Allow USB debugging on the phone",
    "usb_waiting_wifi": "On USB — waiting for the phone's Wi-Fi",
    "usb_connect_failed": "On USB — wireless reconnect will retry",
    "usb_retry_later": "On USB — restoring wireless ADB",
    "usb_recovered": "Wireless ADB restored — reconnecting",
}


def public_record(serial: str, record: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    item = {
        "state": record["state"],
        "message": record.get("message", ""),
        "started_at": record["started_at"],
        "elapsed_s": round(max(0.0, (record.get("finished_at") or now) - record["started_at"]), 1),
        "usb_connected": bool(record.get("usb_connected")),
    }
    if record.get("back_after_s") is not None:
        item["back_after_s"] = record["back_after_s"]
    return item


async def _check_back(serial: str, elapsed: float) -> str:
    """'back' (fresh boot finished), 'booting', or 'old' (never restarted)."""
    try:
        raw = (await adb_shell(serial, "echo $(getprop sys.boot_completed) $(cut -d' ' -f1 /proc/uptime)", timeout=4)).split()
    except fleet_control.AdbCommandError:
        return "booting"
    try:
        booted, uptime = raw[0] == "1", float(raw[1])
    except (IndexError, ValueError):
        return "booting"
    if uptime > elapsed + 30:
        return "old"
    return "back" if booted else "booting"


async def monitor_once(now: float | None = None) -> None:
    now = now or time.time()
    try:
        devices = await inventory()
    except fleet_control.AdbCommandError:
        return
    # A wireless serial is online by Wi-Fi; a USB-only phone's own serial counts too.
    online = {d["serial"] for d in devices if d["state"] == "device"}
    usb_states = await asyncio.to_thread(usb_recovery.usb_states_by_peer, devices)
    timer_states = _timer_usb_messages()
    for serial, record in list(_records.items()):
        if record["state"] not in ACTIVE_STATES:
            if now - (record.get("finished_at") or now) > KEEP_FINISHED_SECONDS:
                _records.pop(serial, None)
            continue
        elapsed = now - record["started_at"]
        if serial in usb_states:
            record["usb_connected"] = True
        if serial in online and elapsed >= BOOT_GRACE_SECONDS:
            status = await _check_back(serial, elapsed)
            if status == "back":
                record.update(state="online", finished_at=now, back_after_s=round(elapsed), message=f"Back online after {round(elapsed)} s")
                continue
            if status == "old" and elapsed >= NO_RESTART_AFTER_SECONDS:
                record.update(state="failed", finished_at=now, message="The phone did not restart — try again")
                continue
            record.update(state="rebooting", message="Starting Android…")
        elif serial in usb_states:
            usb_state = usb_states[serial]
            if usb_state == "unauthorized":
                message = _USB_MESSAGES["usb_unauthorized"]
            else:
                message = _USB_MESSAGES.get(timer_states.get(serial, ""), "On USB — restoring wireless ADB")
            record.update(state="recovering", message=message)
        elif elapsed >= NEEDS_USB_AFTER_SECONDS:
            if elapsed >= KEEP_WAITING_SECONDS:
                record.update(state="failed", finished_at=now, message="Still offline — plug it into the Ubuntu PC by USB")
            else:
                record.update(state="needs_usb", message="Offline — plug it into the Ubuntu PC by USB to bring it back")
        else:
            record.update(state="rebooting", message="Restarting…")


async def _monitor_loop() -> None:
    while any(r["state"] in ACTIVE_STATES for r in _records.values()):
        await asyncio.sleep(POLL_SECONDS)
        try:
            await monitor_once()
        except Exception:  # never let one bad poll end monitoring
            continue


async def _ensure_monitor() -> None:
    global _monitor_task
    async with _start_lock:
        if _monitor_task is None or _monitor_task.done():
            _monitor_task = asyncio.create_task(_monitor_loop(), name="d3m0n-reboot-monitor")


async def _preflight(serials: list[str]) -> dict[str, dict[str, Any]]:
    devices = await inventory()
    states = {d["serial"]: d["state"] for d in devices}
    await asyncio.gather(*(_ensure_alias(s) for s in serials if states.get(s) == "device"))
    usb_states = await asyncio.to_thread(usb_recovery.usb_states_by_peer, devices)
    busy = agent_busy_devices()
    result: dict[str, dict[str, Any]] = {}
    for serial in serials:
        record = _records.get(serial)
        on_usb_directly = usb_recovery.is_usb_serial(serial) and states.get(serial) == "device"
        result[serial] = {
            "online": states.get(serial) == "device",
            "usb_connected": serial in usb_states or on_usb_directly,
            "agent_busy": serial in busy,
            "rebooting": bool(record and record["state"] in ACTIVE_STATES),
        }
    return result


@router.post("/preflight", dependencies=[Depends(fleet_control._same_origin)])
async def reboot_preflight(body: DevicesBody) -> dict[str, Any]:
    serials = _validate(body.devices)
    try:
        return {"devices": await _preflight(serials)}
    except fleet_control.AdbCommandError as exc:
        raise HTTPException(status_code=503, detail="ADB inventory unavailable") from exc


@router.post("", dependencies=[Depends(fleet_control._same_origin)])
async def reboot_devices(body: RebootBody) -> dict[str, Any]:
    serials = _validate(body.devices)
    if body.confirm is not True:
        raise HTTPException(status_code=422, detail="Reboot requires confirm: true")
    try:
        checks = await _preflight(serials)
    except fleet_control.AdbCommandError as exc:
        raise HTTPException(status_code=503, detail="ADB inventory unavailable") from exc
    skipped: list[dict[str, str]] = []
    accepted: list[str] = []
    for serial in serials:
        check = checks[serial]
        reason = "already_rebooting" if check["rebooting"] else "offline" if not check["online"] else "agent_running" if check["agent_busy"] else ""
        if reason:
            skipped.append({"device": serial, "reason": reason})
        else:
            accepted.append(serial)
    gate = asyncio.Semaphore(CONCURRENCY)
    failed: list[dict[str, str]] = []
    started: list[str] = []

    async def one(serial: str) -> None:
        async with gate:
            now = time.time()
            _records[serial] = {"state": "rebooting", "started_at": now, "usb_connected": checks[serial]["usb_connected"], "message": "Restarting…"}
            try:
                await send_reboot(serial)
                started.append(serial)
            except fleet_control.AdbCommandError as exc:
                _records[serial].update(state="failed", finished_at=time.time(), message="Reboot command failed")
                failed.append({"device": serial, "error": str(exc)[:160]})

    await asyncio.gather(*(one(s) for s in accepted))
    if started:
        await _ensure_monitor()
    return {
        "ok": not failed,
        "started": started,
        "skipped": skipped,
        "failed": failed,
        "without_usb": [s for s in started if not checks[s]["usb_connected"]],
    }


@router.get("/status")
async def reboot_status() -> dict[str, Any]:
    now = time.time()
    return {
        "devices": {serial: public_record(serial, record, now) for serial, record in _records.items()},
        "active": sum(1 for r in _records.values() if r["state"] in ACTIVE_STATES),
    }
