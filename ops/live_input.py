"""Live input channel for D3m0n in the droid.

One WebSocket per dashboard page (``/api/phone/input/ws``) carries real-time
touch (down / move / up) and keys. Every event is written to the leader and to
each Sync follower that has an instant control path (live H.264 stream control
socket or leased sync link) in the same event-loop turn, so the phones follow
the operator's finger together instead of replaying a gesture after release.

Followers without an instant path receive the completed gesture through the
existing ADB executor when the finger lifts (tap, long-press or swipe).

A finger is never left down: a disconnect, a new gesture, an idle gap or the
maximum hold time releases it on every phone.

Authentication is enforced by the dashboard ASGI gate (session cookie and
public Origin, or trusted loopback); this route also checks the Origin like
the H.264 stream route.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket

from ops import android_h264 as h264
from ops import ai_control_guard
from ops import control_arbiter
from ops import fast_control as fc
from ops import fleet_control as fleet
from ops import sync_links
from ops.platform_config import get_allowed_origins

router = APIRouter(tags=["phone-live-input"])

PROTOCOL = 1
ALLOWED_ORIGINS = get_allowed_origins()
MAX_MESSAGE_BYTES = 8192
MAX_MESSAGES_PER_SECOND = 400
IDLE_RELEASE = 4.0          # seconds without any message while a finger is down
MAX_HOLD = 30.0             # longest single touch before the server releases it
WATCHDOG_EVERY = 0.25
FLUSH_LIMIT = 4096          # bytes buffered on a control socket before we wait for it
FLUSH_TIMEOUT = 0.5
TAP_SLOP = 0.012            # normalized distance still treated as a tap for ADB replay
LONG_PRESS = 0.5            # seconds
OUTBOX_LIMIT = 64
COORD_MARGIN = 0.05

STATS: dict[str, Any] = {
    "connections": 0, "open": 0, "gestures": 0, "moves": 0, "keys": 0, "adb_replays": 0,
    "released_by_server": 0, "rejected": 0, "write_failures": 0,
    "dispatch_us_last": None, "dispatch_us_max": 0,
}


def reset_state_for_tests() -> None:
    for key in STATS:
        STATS[key] = None if key == "dispatch_us_last" else 0


def status() -> dict[str, Any]:
    return {"ok": True, "protocol": PROTOCOL, "stats": dict(STATS), "idle_release_ms": int(IDLE_RELEASE * 1000),
            "max_sync_devices": fc.MAX_SYNC_DEVICES}


# ---------------------------------------------------------------- helpers
class LiveError(ValueError):
    """A client message was rejected (reported back, never fatal)."""


def _unit(value: Any) -> float:
    number = fc._finite(value)
    if number is None or number < -COORD_MARGIN or number > 1 + COORD_MARGIN:
        raise LiveError("Touch position must be a number between 0 and 1")
    return max(0.0, min(1.0, number))


def _gesture_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2 ** 31:
        raise LiveError("Missing or invalid id")
    return value


def _serial(value: Any) -> str:
    try:
        serial = fleet._validate_serial(value)
    except HTTPException as error:
        raise LiveError(str(error.detail)[:200]) from error
    if not fc._is_android(serial):
        raise LiveError("Live input supports Android phones only")
    return serial


def _followers(value: Any, leader: str) -> list[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise LiveError("s must be a list of follower serials")
    try:
        followers = fc.sync_targets({"sync": {"devices": value}}, leader)
    except HTTPException as error:
        raise LiveError(str(error.detail)[:200]) from error
    for serial in followers:
        if not fc._is_android(serial):
            raise LiveError("Live input supports Android phones only")
    return followers


def _control_writer(session):
    # ControlLink keeps its socket in ``writer``; an H.264 session keeps the
    # control socket in ``control_writer`` (its ``writer`` is the video socket).
    if isinstance(session, sync_links.ControlLink):
        return session.writer
    return getattr(session, "control_writer", None)


def _note_written(session) -> None:
    if isinstance(session, sync_links.ControlLink):
        session.events += 1
    else:
        with contextlib.suppress(Exception):
            session.control_events += 1


def _note_error(session, text: str) -> None:
    with contextlib.suppress(Exception):
        if isinstance(session, sync_links.ControlLink):
            session.error = text
        else:
            session.control_error = text


def _buffered(writer) -> int:
    try:
        return int(writer.transport.get_write_buffer_size())
    except Exception:
        return 0


class Target:
    __slots__ = ("serial", "session", "via", "alive", "error", "sent", "ambiguous", "down_delivered",
                 "release_attempted", "release_confirmed")

    def __init__(self, serial: str, session) -> None:
        self.serial = serial
        self.session = session
        self.via = fc.via_of(session)
        self.alive = True
        self.error = ""
        self.sent = False
        self.ambiguous = False
        self.down_delivered = False
        self.release_attempted = False
        self.release_confirmed = False


def resolve(serials: list[str]) -> tuple[list[Target], list[str]]:
    instant: list[Target] = []
    adb: list[str] = []
    for serial in serials:
        session = fc.control_session(serial)
        if session is None:
            adb.append(serial)
        else:
            instant.append(Target(serial, session))
    return instant, adb


async def broadcast(targets: list[Target], build, *, owner=None, phase: str = "", include_failed: bool = False) -> float:
    """Write one control message to every live target in the same loop turn.

    ``build(session)`` returns the bytes for that phone. Returns the dispatch
    time in microseconds. Failed targets are excluded from ordinary events, but
    a target that delivered DOWN is included in the final UP cleanup attempt."""
    started = time.perf_counter()
    async def send_one(target: Target):
        if not target.alive and not (include_failed and target.down_delivered):
            return None
        session = target.session
        try:
            if not session.control_ready():
                raise h264.ControlUnavailable("No live control channel")
            payload = build(session)
            writer = _control_writer(session)
            if writer is None or writer.is_closing():
                raise h264.ControlUnavailable("No live control channel")
            writer_now = getattr(session, "write_control_now", None)
            needs_drain = writer_now is not None
            if writer_now is not None:
                writer = await writer_now(payload, arbiter_token=owner)
            else:
                writer_write = getattr(session, "write_control", None)
                if writer_write is not None:
                    try:
                        await writer_write(payload, arbiter_token=owner)
                    except TypeError:
                        await writer_write(payload)
                else:
                    writer.write(payload)
        except Exception as error:  # noqa: BLE001 - every failure is per phone
            if phase == "down" and isinstance(error, h264.ControlWriteAmbiguous):
                target.down_delivered = True
            _fail(target, error)
            if phase == "up" and target.down_delivered:
                invalidate = getattr(session, "invalidate_control", None)
                if invalidate is not None:
                    with contextlib.suppress(Exception):
                        invalidate("Control channel release unconfirmed")
            return None
        target.sent = True
        if phase == "down":
            target.down_delivered = True
        if phase == "up":
            target.release_attempted = True
            target.release_confirmed = True
            if target.ambiguous:
                invalidate = getattr(session, "invalidate_control", None)
                if invalidate is not None:
                    with contextlib.suppress(Exception):
                        invalidate("Control channel delivery was ambiguous; release confirmed")
        _note_written(session)
        return target, writer, needs_drain

    # Start every device write before waiting for any slow drain.  The gesture
    # lease is already held by the caller, so these tasks cannot interleave
    # another gesture on the same serial while distinct serials fan out.
    outcomes = await asyncio.gather(*(send_one(target) for target in targets))
    written = [outcome for outcome in outcomes if outcome is not None]
    dispatch_us = (time.perf_counter() - started) * 1e6
    backlog = [(t, w, needs_drain) for t, w, needs_drain in written
               if needs_drain or _buffered(w) > FLUSH_LIMIT]
    if backlog:
        outcomes = await asyncio.gather(*(asyncio.wait_for(w.drain(), FLUSH_TIMEOUT) for _, w, _ in backlog),
                                        return_exceptions=True)
        for (target, _, needs_drain), outcome in zip(backlog, outcomes):
            if isinstance(outcome, BaseException):
                error = control_arbiter.AmbiguousWrite(str(outcome)) if needs_drain else outcome
                if phase == "up":
                    target.release_attempted = True
                    target.release_confirmed = False
                _fail(target, error)
                if phase == "up" and target.down_delivered:
                    invalidate = getattr(target.session, "invalidate_control", None)
                    if invalidate is not None:
                        with contextlib.suppress(Exception):
                            invalidate("Control channel release unconfirmed")
    STATS["dispatch_us_last"] = round(dispatch_us, 1)
    STATS["dispatch_us_max"] = max(STATS["dispatch_us_max"] or 0, round(dispatch_us, 1))
    return dispatch_us


def _fail(target: Target, error: BaseException) -> None:
    target.alive = False
    if isinstance(error, h264.ControlWriteAmbiguous):
        target.ambiguous = True
    target.error = ("Control channel lost during the gesture" if target.sent else fc._safe_error(error))
    _note_error(target.session, fc._safe_error(error))
    STATS["write_failures"] += 1


def _touch(action: int, nx: float, ny: float, pressure: float):
    def build(session) -> bytes:
        x, y, w, h = session.point(nx, ny)
        return h264.touch_message(action, x, y, w, h, pressure)
    return build


def _key(code: int):
    def build(_session) -> bytes:
        return h264.key_message(h264.ACTION_DOWN, code) + h264.key_message(h264.ACTION_UP, code)
    return build


def _record(serial: str, ok: bool, via: str, error: str, ms: float, action: str) -> None:
    fc.LAST_RESULTS.pop(serial, None)
    fc.LAST_RESULTS[serial] = {"ok": ok, "via": via, "error": error, "ms": round(ms, 1), "action": action, "at": time.time()}
    if ok:
        fc.LAST_ERRORS.pop(serial, None)
        fc.STATS[via] = fc.STATS.get(via, 0) + 1
    else:
        fc.LAST_ERRORS[serial] = {"error": error, "at": time.time()}
    if len(fc.LAST_RESULTS) > fc.RESULT_LIMIT:
        for key in list(fc.LAST_RESULTS)[:50]:
            fc.LAST_RESULTS.pop(key, None)


def _spawn_adb(serial: str, gesture: dict[str, Any]) -> bool:
    if fc._adb_backlog.get(serial, 0) >= fc.MAX_FOLLOWER_BACKLOG:
        return False
    task = asyncio.create_task(fc.run_on_device(serial, gesture), name="d3m0n-live-adb-" + serial)
    fc._background.add(task)
    task.add_done_callback(fc._background.discard)
    fc._adb_backlog[serial] = fc._adb_backlog.get(serial, 0) + 1
    task.add_done_callback(lambda _t, s=serial: fc._release_backlog(s))
    STATS["adb_replays"] += 1
    return True


class Gesture:
    def __init__(self, gid: int, leader: str, followers: list[str], instant: list[Target], adb: list[str],
                 nx: float, ny: float, now: float) -> None:
        self.id = gid
        self.leader = leader
        self.followers = followers
        self.instant = instant
        self.adb = adb
        self.start = (nx, ny)
        self.last = (nx, ny)
        self.started = now
        self.touched = now
        self.arbiter_token = control_arbiter.token()
        self.leases = {}

    async def acquire(self) -> None:
        unique = {target.serial for target in self.instant}
        leases = {serial: control_arbiter.hold(serial, self.arbiter_token) for serial in unique}
        acquired = []
        try:
            for serial in sorted(leases):
                await leases[serial].acquire()
                acquired.append(serial)
        except BaseException:
            for serial in reversed(acquired):
                await leases[serial].release()
            raise
        self.leases = leases

    async def release(self) -> None:
        leases, self.leases = self.leases, {}
        for serial in sorted(leases, reverse=True):
            await leases[serial].release()


# ---------------------------------------------------------------- connection
class LiveConnection:
    """Protocol state for one dashboard WebSocket (transport-independent)."""

    def __init__(self, send) -> None:
        self._send = send            # callable(dict) -> None, must not block
        self.gesture: Gesture | None = None
        self.lock = asyncio.Lock()
        self._window = 0.0
        self._count = 0

    def send(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._send(message)

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def handle_text(self, raw: str) -> None:
        if len(raw) > MAX_MESSAGE_BYTES:
            STATS["rejected"] += 1
            self.send({"t": "error", "error": "Message too large"})
            return
        try:
            message = json.loads(raw)
        except ValueError:
            STATS["rejected"] += 1
            self.send({"t": "error", "error": "Invalid JSON"})
            return
        if not isinstance(message, dict):
            STATS["rejected"] += 1
            self.send({"t": "error", "error": "Message must be an object"})
            return
        kind = message.get("t")
        now = self._now()
        if now - self._window >= 1.0:
            self._window, self._count = now, 0
        self._count += 1
        if self._count > MAX_MESSAGES_PER_SECOND:
            if kind not in ("move", "hold"):
                STATS["rejected"] += 1
                self.send({"t": "error", "id": message.get("id"), "error": "Too many input messages"})
            return
        async with self.lock:
            try:
                if kind == "down":
                    await self._down(message)
                elif kind == "move":
                    await self._move(message)
                elif kind == "hold":
                    self._hold(message)
                elif kind == "up":
                    await self._up(message, cancelled=False)
                elif kind == "cancel":
                    await self._up(message, cancelled=True)
                elif kind == "key":
                    await self._keypress(message)
                elif kind == "ping":
                    self.send({"t": "pong", "id": message.get("id")})
                else:
                    raise LiveError("Unknown message type")
            except (LiveError, control_arbiter.AIOwnershipError, ai_control_guard.AIControlError) as error:
                STATS["rejected"] += 1
                detail = "Device is controlled by an AI worker" if isinstance(error, (control_arbiter.AIOwnershipError, ai_control_guard.AIControlError)) else str(error)
                self.send({"t": "error", "id": message.get("id"), "error": detail[:200]})

    # ---- touch -----------------------------------------------------------
    async def _down(self, message: dict[str, Any]) -> None:
        gid = _gesture_id(message.get("id"))
        leader = _serial(message.get("d"))
        followers = _followers(message.get("s"), leader)
        nx, ny = _unit(message.get("x")), _unit(message.get("y"))
        if self.gesture is not None:
            await self._finish(self.gesture, self.gesture.last, cancelled=True, reason="superseded")
        instant, adb = resolve([leader] + followers)
        gesture = Gesture(gid, leader, followers, instant, adb, nx, ny, self._now())
        self.gesture = gesture
        try:
            await gesture.acquire()
            await broadcast(instant, _touch(h264.ACTION_DOWN, nx, ny, 1.0), owner=gesture.arbiter_token, phase="down")
        except BaseException:
            await gesture.release()
            if self.gesture is gesture:
                self.gesture = None
            raise
        STATS["gestures"] += 1
        lead = next((t for t in instant if t.serial == leader), None)
        if lead is not None:
            ok, via, error = lead.alive, lead.via, lead.error
        else:
            ok, via, error = True, "adb", ""
        ack: dict[str, Any] = {"t": "ack", "id": gid, "phase": "down", "device": leader, "ok": ok, "via": via,
                               "live": sum(1 for t in instant if t.alive), "adb": len(adb)}
        if error:
            ack["error"] = error
        self.send(ack)

    async def _move(self, message: dict[str, Any]) -> None:
        gesture = self.gesture
        if gesture is None or message.get("id") != gesture.id:
            return  # stale or already released
        nx, ny = _unit(message.get("x")), _unit(message.get("y"))
        gesture.last = (nx, ny)
        gesture.touched = self._now()
        STATS["moves"] += 1
        await broadcast(gesture.instant, _touch(h264.ACTION_MOVE, nx, ny, 1.0),
                        owner=gesture.arbiter_token, phase="move")

    def _hold(self, message: dict[str, Any]) -> None:
        gesture = self.gesture
        if gesture is not None and message.get("id") == gesture.id:
            gesture.touched = self._now()

    async def _up(self, message: dict[str, Any], cancelled: bool) -> None:
        gesture = self.gesture
        if gesture is None or message.get("id") != gesture.id:
            return
        point = gesture.last
        if not cancelled:
            point = (_unit(message.get("x")), _unit(message.get("y")))
        await self._finish(gesture, point, cancelled=cancelled, reason="cancel" if cancelled else "")

    async def _finish(self, gesture: Gesture, point: tuple[float, float], cancelled: bool, reason: str) -> None:
        if self.gesture is gesture:
            self.gesture = None
        try:
            dispatch_us = await broadcast(gesture.instant, _touch(h264.ACTION_UP, point[0], point[1], 0.0),
                                           owner=gesture.arbiter_token, phase="up", include_failed=True)
            duration = max(0.0, self._now() - gesture.started)
            results: dict[str, dict[str, Any]] = {}
            for target in gesture.instant:
                error = target.error
                if target.down_delivered and not target.release_confirmed:
                    error = (error + "; " if error else "") + "release unconfirmed"
                results[target.serial] = {"ok": target.alive and target.release_confirmed, "via": target.via,
                                          "error": error, "release_attempted": target.release_attempted,
                                          "release_confirmed": target.release_confirmed}
                _record(target.serial, target.alive, target.via, target.error, duration * 1000, "touch")
            pending: list[str] = []
            refused: list[str] = []
            if gesture.adb:
                replay = None if cancelled else self._replay(gesture, point, duration)
                for serial in gesture.adb:
                    if replay is None:
                        results[serial] = {"ok": False, "via": "adb", "error": "Touch was cancelled before release"}
                    elif _spawn_adb(serial, replay):
                        pending.append(serial)
                        results[serial] = {"ok": True, "via": "adb", "error": "", "pending": True}
                    else:
                        refused.append(serial)
                        results[serial] = {"ok": False, "via": "adb",
                                           "error": "Phone is still catching up on earlier mirrored actions (ADB)"}
            lead = results.get(gesture.leader, {"ok": False, "via": "", "error": "No result"})
            ack: dict[str, Any] = {"t": "ack", "id": gesture.id, "phase": "up", "device": gesture.leader,
                                   "ok": bool(lead["ok"]), "via": lead["via"], "cancelled": cancelled}
            if lead.get("error"):
                ack["error"] = lead["error"]
            if lead.get("pending"):
                ack["pending"] = True
            if gesture.followers:
                ack["sync"] = self._summary(gesture.followers, results, "touch", dispatch_us)
            if reason and reason != "cancel":
                ack["reason"] = reason
            self.send(ack)
        finally:
            await gesture.release()

    @staticmethod
    def _replay(gesture: Gesture, point: tuple[float, float], duration: float) -> dict[str, Any]:
        (x1, y1), (x2, y2) = gesture.start, point
        if math.hypot(x2 - x1, y2 - y1) < TAP_SLOP and duration < LONG_PRESS:
            return {"kind": "tap", "x": x2, "y": y2}
        ms = int(max(fc.SWIPE_MIN_MS, min(fc.SWIPE_MAX_MS, duration * 1000)))
        return {"kind": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": ms}

    @staticmethod
    def _summary(followers: list[str], results: dict[str, dict[str, Any]], action: str, dispatch_us: float) -> dict[str, Any]:
        failed = [{"device": s, "error": results[s]["error"] or "failed"} for s in followers
                  if s in results and not results[s]["ok"]]
        pending = sum(1 for s in followers if results.get(s, {}).get("pending"))
        via: dict[str, int] = {}
        for serial in followers:
            r = results.get(serial)
            if r and r["ok"] and not r.get("pending"):
                via[r["via"]] = via.get(r["via"], 0) + 1
        summary = {"action": action, "targets": len(followers), "ok": sum(via.values()), "failed": failed,
                   "pending": pending, "via": via, "ms": round(dispatch_us / 1000, 2), "live": True}
        fc.STATS["sync_requests"] += 1
        fc.STATS["sync_targets"] += len(followers)
        fc.STATS["sync_failures"] += len(failed)
        fc.STATS["last_sync"] = {**summary, "at": time.time()}
        return summary

    # ---- keys --------------------------------------------------------------
    async def _keypress(self, message: dict[str, Any]) -> None:
        gid = _gesture_id(message.get("id"))
        leader = _serial(message.get("d"))
        followers = _followers(message.get("s"), leader)
        code = fc.keycode_for(message.get("k"))
        if code is None:
            raise LiveError("Unsupported key")
        instant, adb = resolve([leader] + followers)
        token = control_arbiter.token()
        leases = {target.serial: control_arbiter.hold(target.serial, token) for target in instant}
        acquired = []
        try:
            for serial in sorted(leases):
                await leases[serial].acquire()
                acquired.append(serial)
        except BaseException:
            for serial in reversed(acquired):
                await leases[serial].release()
            raise
        try:
            dispatch_us = await broadcast(instant, _key(code), owner=token, phase="key")
        finally:
            for serial in sorted(leases, reverse=True):
                await leases[serial].release()
        STATS["keys"] += 1
        results: dict[str, dict[str, Any]] = {}
        for target in instant:
            results[target.serial] = {"ok": target.alive, "via": target.via, "error": target.error}
            _record(target.serial, target.alive, target.via, target.error, dispatch_us / 1000, "key")
        for serial in adb:
            if _spawn_adb(serial, {"kind": "key", "code": code}):
                results[serial] = {"ok": True, "via": "adb", "error": "", "pending": True}
            else:
                results[serial] = {"ok": False, "via": "adb",
                                   "error": "Phone is still catching up on earlier mirrored actions (ADB)"}
        lead = results[leader]
        ack: dict[str, Any] = {"t": "ack", "id": gid, "phase": "key", "device": leader,
                               "ok": bool(lead["ok"]), "via": lead["via"]}
        if lead.get("error"):
            ack["error"] = lead["error"]
        if lead.get("pending"):
            ack["pending"] = True
        if followers:
            ack["sync"] = self._summary(followers, results, "key", dispatch_us)
        self.send(ack)

    # ---- safety ------------------------------------------------------------
    async def watchdog_tick(self) -> None:
        gesture = self.gesture
        if gesture is None:
            return
        now = self._now()
        reason = ""
        if now - gesture.touched > IDLE_RELEASE:
            reason = "idle"
        elif now - gesture.started > MAX_HOLD:
            reason = "max-hold"
        if not reason:
            return
        async with self.lock:
            if self.gesture is not gesture:
                return
            STATS["released_by_server"] += 1
            await self._finish(gesture, gesture.last, cancelled=True, reason=reason)
            self.send({"t": "released", "id": gesture.id, "reason": reason})

    async def close(self) -> None:
        async with self.lock:
            gesture = self.gesture
            if gesture is not None:
                STATS["released_by_server"] += 1
                await self._finish(gesture, gesture.last, cancelled=True, reason="disconnect")


# ---------------------------------------------------------------- routes
@router.get("/api/phone/input/status", summary="Live input channel status")
async def live_status():
    return status()


@router.websocket("/api/phone/input/ws")
async def live_input(client: WebSocket):
    origin = client.headers.get("origin", "")
    if origin and origin not in get_allowed_origins():
        await client.close(code=1008, reason="Untrusted input origin")
        return
    await client.accept()
    outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=OUTBOX_LIMIT)

    def send(message: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            outbox.put_nowait(json.dumps(message, separators=(",", ":")))

    connection = LiveConnection(send)
    STATS["connections"] += 1
    STATS["open"] += 1

    async def sender() -> None:
        while True:
            text = await outbox.get()
            await client.send_text(text)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(WATCHDOG_EVERY)
            await connection.watchdog_tick()

    send({"t": "hello", "v": PROTOCOL, "max_sync": fc.MAX_SYNC_DEVICES, "idle_release_ms": int(IDLE_RELEASE * 1000)})
    tasks = [asyncio.create_task(sender(), name="d3m0n-live-sender"),
             asyncio.create_task(watchdog(), name="d3m0n-live-watchdog")]
    try:
        while True:
            message = await client.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                send({"t": "error", "error": "Text messages only"})
                continue
            await connection.handle_text(text)
    except Exception:  # noqa: BLE001 - a broken socket ends this connection only
        pass
    finally:
        with contextlib.suppress(Exception):
            await connection.close()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        STATS["open"] = max(0, STATS["open"] - 1)
