"""Control-only scrcpy links that make SYNC ALL instant on every phone.

A phone with a live H.264 stream already has a scrcpy control socket (see
``ops.android_h264``). Phones that are not streaming - stopped, hidden or
off-screen in a large fleet - would otherwise mirror input through
``adb shell input``, which costs 0.5-1 s per action on Android 9 and makes the
fleet drift out of step.

While SYNC ALL is on, the dashboard leases a lightweight control-only scrcpy
server (``video=false audio=false``) for each phone in the sync pool. No video
is encoded. Leases expire on their own, so closing the dashboard tab releases
every link within a minute.

scrcpy v5.0.1 has no position mapper when video is disabled and injects touch
events at raw display coordinates, so each link tracks the phone's current
logical display size (rotation applied) from ``dumpsys display``. Coordinates
that are older than ``DISPLAY_STALE`` seconds are not trusted and the link
reports itself as not ready, which makes callers fall back to ADB.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from ops import android_h264 as h264
from ops import control_arbiter
from ops import fleet_control as fleet

LOG = logging.getLogger("gitd.sync_links")
router = APIRouter(tags=["phone-sync-links"])

LEASE_SECONDS = 60.0        # a lease lives this long without a dashboard heartbeat
MAX_LINKS = 100
DISPLAY_REFRESH = 4.0       # seconds between display size / rotation checks
DISPLAY_STALE = 15.0        # older display data is not used for coordinates
CONNECT_TIMEOUT = 12.0
RETRY_FIRST, RETRY_MAX = 3.0, 60.0
SUPERVISE_TICK = 1.0
LINKS: dict[str, "ControlLink"] = {}
_tasks: set[asyncio.Task] = set()
_DISPLAY_CMD = "dumpsys display | grep -m1 mOverrideDisplayInfo"
_REAL_RE = re.compile(r"\breal (\d{2,5}) x (\d{2,5})\b")
_ROTATION_RE = re.compile(r"\brotation ([0-3])\b")


def parse_display(text: str) -> tuple[int, int, int] | None:
    """Return (width, height, rotation) of the logical display, or None."""
    text = text or ""
    match = _REAL_RE.search(text)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (100 <= width <= 10000 and 100 <= height <= 10000):
        return None
    rotation = _ROTATION_RE.search(text, match.end()) or _ROTATION_RE.search(text)
    return width, height, int(rotation.group(1)) if rotation else 0


def _short(error: BaseException) -> str:
    text = str(error) or error.__class__.__name__
    return text.splitlines()[0][:200]


class ControlLink:
    """One control-only scrcpy server for one phone."""

    def __init__(self, device: str):
        self.device = device
        self.scid = f"{secrets.randbits(31):08x}"
        self.proc: Any = None
        self.port: int | None = None
        self.writer: Any = None
        self.reader_task: asyncio.Task | None = None
        self.log_task: asyncio.Task | None = None
        self.log_tail = b""
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.lease_until = 0.0
        self.stopping = False
        self.state = "starting"
        self.error = ""
        self.width = self.height = self.rotation = 0
        self.display_at = 0.0
        self.events = 0
        self.failures = 0
        self.restarts = 0
        self.connected_at = 0.0
        self.created = time.time()

    # ---- lifecycle ------------------------------------------------------
    def leased(self) -> bool:
        return not self.stopping and self.lease_until > time.monotonic()

    def command(self) -> list[str]:
        return ["adb", "-s", self.device, "shell", "CLASSPATH=" + h264.REMOTE, "app_process", "/",
                "com.genymobile.scrcpy.Server", h264.VERSION, "scid=" + self.scid, "tunnel_forward=true",
                "video=false", "audio=false", "clipboard_autosync=false", "cleanup=false"]

    async def run(self) -> None:
        delay = RETRY_FIRST
        try:
            while self.leased():
                try:
                    await self._connect_and_supervise()
                    delay = RETRY_FIRST
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.failures += 1
                    self.error = _short(error)
                    LOG.info("Sync link %s: %s", self.device, self.error)
                    if self.connected_at and time.time() - self.connected_at > 30:
                        delay = RETRY_FIRST     # a long healthy link dropped: retry promptly
                finally:
                    await self._teardown()
                if not self.leased():
                    break
                self.state = "retrying"
                self.restarts += 1
                await self._sleep_while_leased(delay)
                delay = min(RETRY_MAX, delay * 2)
        finally:
            self.stopping = True
            self.state = "closed"
            await self._teardown()

    async def _sleep_while_leased(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.leased() and time.monotonic() < end:
            await asyncio.sleep(max(0.01, min(SUPERVISE_TICK, end - time.monotonic())))

    async def _connect_and_supervise(self) -> None:
        self.state = "starting"
        self.scid = f"{secrets.randbits(31):08x}"
        try:
            adb_state = await h264.adb(self.device, "get-state", timeout=3)
        except Exception:
            adb_state = "offline"
        if adb_state != "device":
            raise RuntimeError("Wireless ADB is offline")
        if self.device not in h264.PREPARED:
            await h264.adb(self.device, "push", str(h264.SERVER), h264.REMOTE, timeout=30)
            h264.PREPARED.add(self.device)
        await self.refresh_display()
        self.port = int(await h264.adb(self.device, "forward", "tcp:0", "localabstract:scrcpy_" + self.scid, timeout=8))
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.log_task = asyncio.create_task(self._collect_log(self.proc.stdout), name="sync-link-log-" + self.device)
        reader = writer = None
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while True:
            if self.proc.returncode is not None:
                raise RuntimeError("scrcpy control link exited: " + self._tail())
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                if await asyncio.wait_for(reader.readexactly(1), 1) != b"\x00":
                    raise RuntimeError("Invalid scrcpy handshake")
                break
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                if writer is not None:
                    writer.close()
                    writer = None
                if time.monotonic() > deadline:
                    raise RuntimeError("Timed out connecting the control link")
                await asyncio.sleep(0.15)
        try:
            # With video and audio disabled the control socket is the first
            # socket, so it also carries the default 64-byte device name.
            await asyncio.wait_for(reader.readexactly(64), 5)
        except BaseException:
            writer.close()
            raise
        self.writer = writer
        self.reader_task = asyncio.create_task(self._drain(reader), name="sync-link-read-" + self.device)
        self.state = "ready"
        self.error = ""
        self.connected_at = time.time()
        LOG.info("Sync link ready for %s (%sx%s)", self.device, self.width, self.height)
        while self.leased():
            if self.reader_task.done():
                raise RuntimeError("Control link closed by the phone")
            if self.proc.returncode is not None:
                raise RuntimeError("scrcpy control link exited: " + self._tail())
            if self.writer is None or self.writer.is_closing():
                raise RuntimeError(self.error or "Control link write failed")
            if time.monotonic() - self.display_at >= DISPLAY_REFRESH:
                try:
                    await self.refresh_display()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOG.debug("Sync link display refresh failed for %s: %s", self.device, error)
            await asyncio.sleep(SUPERVISE_TICK)

    async def _drain(self, reader) -> None:
        # Device -> client messages (acks, clipboard) are not used.
        with contextlib.suppress(Exception):
            while await reader.read(65536):
                pass

    async def _collect_log(self, stream) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self.log_tail = (self.log_tail + chunk)[-2000:]

    def _tail(self) -> str:
        return self.log_tail.decode(errors="replace").strip().replace("\n", " ")[-300:] or "no output"

    async def _teardown(self) -> None:
        if self.state == "ready":
            self.state = "down"
        writer, self.writer = self.writer, None
        reader_task, self.reader_task = self.reader_task, None
        if reader_task is not None:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        log_task, self.log_task = self.log_task, None
        if log_task is not None:
            log_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await log_task
        port, self.port = self.port, None
        if port:
            with contextlib.suppress(Exception):
                await h264.adb(self.device, "forward", "--remove", "tcp:" + str(port), timeout=3)

    async def refresh_display(self) -> None:
        out = await h264.adb(self.device, "shell", _DISPLAY_CMD, timeout=5)
        parsed = parse_display(out)
        if parsed is None:
            raise RuntimeError("Display size unavailable")
        self.width, self.height, self.rotation = parsed
        self.display_at = time.monotonic()

    # ---- control --------------------------------------------------------
    def control_ready(self) -> bool:
        writer = self.writer
        return bool(self.state == "ready" and writer is not None and not writer.is_closing()
                    and self.width and self.height and self.display_at
                    and time.monotonic() - self.display_at < DISPLAY_STALE and self.leased())

    def invalidate_control(self, error: str = "Control channel invalidated") -> None:
        """Retire a channel after accepted bytes had unknown delivery."""
        writer, self.writer = self.writer, None
        self.state = "down"
        self.error = _short(RuntimeError(error))
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()

    def point(self, nx: float, ny: float) -> tuple[int, int, int, int]:
        """Map normalized 0..1 coordinates to raw display pixels."""
        w, h = int(self.width), int(self.height)
        if w <= 0 or h <= 0:
            raise h264.ControlUnavailable("Display size unknown")
        nx = max(0.0, min(1.0, float(nx)))
        ny = max(0.0, min(1.0, float(ny)))
        return int(round(nx * (w - 1))), int(round(ny * (h - 1))), w, h

    async def _write(self, payload: bytes, arbiter_token=None) -> None:
        if not self.control_ready():
            raise h264.ControlUnavailable("Sync link not ready")
        writer = self.writer
        try:
            await control_arbiter.write(self.device, writer, payload, owner=arbiter_token, drain_timeout=2)
        except control_arbiter.AmbiguousWrite as error:
            self.error = _short(error)
            raise h264.ControlWriteAmbiguous(self.error) from error
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            self.error = _short(error)
            with contextlib.suppress(Exception):
                writer.close()
            raise h264.ControlUnavailable(self.error) from error
        self.events += 1

    async def write_control(self, payload: bytes, arbiter_token=None) -> None:
        await self._write(payload, arbiter_token)

    async def write_control_now(self, payload: bytes, arbiter_token=None):
        """Write without draining; live fan-out drains all targets together."""
        if not self.control_ready():
            raise h264.ControlUnavailable("Sync link not ready")
        writer = self.writer
        async with control_arbiter.hold(self.device, arbiter_token):
            if writer is None or writer.is_closing():
                raise h264.ControlUnavailable("Sync link not ready")
            try:
                writer.write(payload)
            except (OSError, asyncio.TimeoutError, RuntimeError) as error:
                self.error = _short(error)
                raise h264.ControlUnavailable(self.error) from error
        return writer

    async def inject_touch(self, action: int, nx: float, ny: float, pressure: float = 1.0, arbiter_token=None) -> None:
        x, y, w, h = self.point(nx, ny)
        await self._write(h264.touch_message(action, x, y, w, h, pressure), arbiter_token)

    async def inject_key(self, keycode: int, arbiter_token=None) -> None:
        try:
            await self._write(h264.key_message(h264.ACTION_DOWN, keycode), arbiter_token)
        except h264.ControlWriteAmbiguous as error:
            released = False
            try:
                await self._write(h264.key_message(h264.ACTION_UP, keycode), arbiter_token)
                released = True
            except Exception:
                pass
            self.invalidate_control("Control channel delivery is ambiguous; " +
                                    ("release confirmed" if released else "release unconfirmed"))
            raise h264.ControlWriteAmbiguous(self.error) from error
        await self._write(h264.key_message(h264.ACTION_UP, keycode), arbiter_token)

    def status(self) -> dict[str, Any]:
        return {
            "device": self.device, "state": self.state, "ready": self.control_ready(),
            "width": self.width, "height": self.height, "rotation": self.rotation,
            "display_age": round(time.monotonic() - self.display_at, 1) if self.display_at else None,
            "events": self.events, "failures": self.failures, "restarts": self.restarts, "error": self.error,
            "lease_seconds": max(0, round(self.lease_until - time.monotonic())),
            "connected_at": self.connected_at or None,
        }


# ---------------------------------------------------------------- registry
def _forget(serial: str, link: ControlLink) -> None:
    if LINKS.get(serial) is link:
        LINKS.pop(serial, None)


def active_links() -> dict[str, ControlLink]:
    return {serial: link for serial, link in LINKS.items() if not link.stopping}


def lease(devices: list[str], seconds: float = LEASE_SECONDS) -> None:
    """Start or extend a link for each phone."""
    now = time.monotonic()
    for serial in devices:
        link = LINKS.get(serial)
        if link is None or link.stopping or link.task is None or link.task.done():
            if len(active_links()) >= MAX_LINKS:
                LOG.warning("Sync link limit (%s) reached; %s uses ADB", MAX_LINKS, serial)
                break
            link = ControlLink(serial)
            LINKS[serial] = link
            link.lease_until = now + seconds
            link.task = asyncio.create_task(link.run(), name="sync-link-" + serial)
            _tasks.add(link.task)
            link.task.add_done_callback(_tasks.discard)
            link.task.add_done_callback(lambda _t, s=serial, l=link: _forget(s, l))
        else:
            link.lease_until = max(link.lease_until, now + seconds)


def release(keep: set[str] | None = None) -> int:
    """Let every link not in ``keep`` expire now; returns how many were released."""
    count = 0
    for serial, link in list(LINKS.items()):
        if keep is None or serial not in keep:
            if link.lease_until:
                count += 1
            link.lease_until = 0.0
    return count


def link_for(serial: str) -> ControlLink | None:
    link = LINKS.get(serial)
    if link is None:
        return None
    try:
        return link if link.control_ready() else None
    except Exception:
        return None


def links_summary() -> dict[str, Any]:
    links = {serial: link.status() for serial, link in active_links().items()}
    return {"links": links, "ready": sum(1 for item in links.values() if item["ready"]),
            "total": len(links), "lease_seconds": LEASE_SECONDS, "max_links": MAX_LINKS}


async def shutdown_for_tests() -> None:
    release()
    for link in list(LINKS.values()):
        link.stopping = True
    tasks = [t for t in list(_tasks) if not t.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    LINKS.clear()
    _tasks.clear()


def _validate_devices(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="devices must be a list")
    if len(raw) > MAX_LINKS:
        raise HTTPException(status_code=422, detail=f"Sync links are limited to {MAX_LINKS} phones")
    result: list[str] = []
    seen: set[str] = set()
    for serial in raw:
        serial = fleet._validate_serial(serial)
        if serial not in seen:
            seen.add(serial)
            result.append(serial)
    return result


# ---------------------------------------------------------------- routes
@router.post("/api/phone/sync/links", summary="Lease instant control links for the SYNC ALL pool")
async def sync_links(request: Request, data: dict = Body({})):
    """The list is the complete pool: phones not listed are released."""
    await fleet._same_origin(request)
    devices = _validate_devices(data.get("devices", []))
    release(set(devices))
    if devices:
        lease(devices, LEASE_SECONDS)
    return {"ok": True, **links_summary()}
