"""Instant dashboard input and SYNC ALL fan-out for D3m0n in the droid.

This module overrides ``POST /api/phone/tap``, ``/swipe``, ``/key`` and
``/input`` by inserting its routes ahead of the upstream gitd routes.

* Fast path: when a phone has a live H.264 session whose scrcpy control socket
  is open, gestures and keys are injected directly over that socket
  (milliseconds) instead of spawning ``adb shell input`` (hundreds of
  milliseconds). Coordinates arrive in the viewer's stream-frame space and the
  scrcpy session uses the same frame, so mapping is exact.
* Fallback: everything else (iOS, no live stream, raw device coordinates,
  text, legacy actions) is handed to the original upstream handler unchanged.
* SYNC ALL: an optional ``sync: {"devices": [serial, ...]}`` field mirrors the
  same action to other phones at the same time. Positions are normalized to the
  leader's frame, so phones with other resolutions receive proportional
  positions. Followers without a stream use a leased control-only scrcpy link
  (``ops.sync_links``); only phones with neither use the ADB fleet executor.
  The leader and every instant follower start on one shared loop deadline and
  step swipes on absolute deadlines, so phones move in lockstep. The leader's
  response waits only for instant followers (plus a short grace for ADB-only
  ones); late results are kept per phone and reported by
  ``GET /api/phone/sync/status``.
* Live input: ``ops.live_input`` adds a WebSocket that streams real-time
  touch (down/move/up) and keys to the leader and followers together.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import re
import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

from ops import android_h264 as h264
from ops import control_arbiter
from ops import fleet_control as fleet
from ops import sync_links

MAX_SYNC_DEVICES = 100
FOLLOWER_WAIT = 0.35         # extra seconds the leader's response waits for followers
TEXT_FOLLOWER_WAIT = 1.0
ADB_FOLLOWER_WAIT = 0.06     # grace for ADB-only followers; they keep running after
START_ALIGN = 0.004          # shared start offset so leader and followers touch down together
MAX_FOLLOWER_BACKLOG = 6     # queued ADB-only mirrored actions per phone before refusing more
TAP_HOLD = 0.035             # seconds between finger down and up for a tap
SWIPE_STEP = 0.016           # seconds between MOVE events (~60 Hz)
MAX_SWIPE_STEPS = 90
SWIPE_MIN_MS, SWIPE_MAX_MS, SWIPE_DEFAULT_MS = 100, 1500, 500   # upstream clamp
TEXT_TIMEOUT = 20.0
_KEY_NAME_RE = re.compile(r"^[A-Z0-9_]{1,40}$")

# Android KeyEvent codes for every key the dashboard sends.
KEYCODES: dict[str, int] = {
    "HOME": 3, "BACK": 4, "CALL": 5, "ENDCALL": 6,
    "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22, "DPAD_CENTER": 23,
    "VOLUME_UP": 24, "VOLUME_DOWN": 25, "POWER": 26, "CAMERA": 27,
    "TAB": 61, "SPACE": 62, "ENTER": 66, "DEL": 67, "MENU": 82, "SEARCH": 84,
    "MEDIA_PLAY_PAUSE": 85, "PAGE_UP": 92, "PAGE_DOWN": 93, "ESCAPE": 111,
    "FORWARD_DEL": 112, "MOVE_HOME": 122, "MOVE_END": 123, "VOLUME_MUTE": 164,
    "APP_SWITCH": 187, "SLEEP": 223, "WAKEUP": 224,
}

router = APIRouter(tags=["phone-fast-input"])
_locks: dict[str, asyncio.Lock] = {}
_background: set[asyncio.Task] = set()
_adb_backlog: dict[str, int] = {}
_adb_slots: asyncio.Semaphore | None = None
STATS: dict[str, Any] = {
    "control": 0, "link": 0, "adb": 0, "upstream": 0, "sync_requests": 0,
    "sync_targets": 0, "sync_failures": 0, "last_sync": None,
}
LAST_ERRORS: dict[str, dict[str, Any]] = {}
LAST_RESULTS: dict[str, dict[str, Any]] = {}
RESULT_LIMIT = 300


def reset_state_for_tests() -> None:
    global _adb_slots
    _locks.clear(); _background.clear(); _adb_backlog.clear(); LAST_ERRORS.clear(); LAST_RESULTS.clear(); _adb_slots = None
    for key in STATS:
        STATS[key] = None if key == "last_sync" else 0


def _upstream():
    from gitd.routers import phone
    return phone


def _slots() -> asyncio.Semaphore:
    global _adb_slots
    if _adb_slots is None:
        _adb_slots = asyncio.Semaphore(fleet._MAX_CONCURRENCY)
    return _adb_slots


# ---------------------------------------------------------------- parsing
def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize(value: Any, extent: Any) -> float | None:
    v, e = _finite(value), _finite(extent)
    if v is None or e is None or e < 1:
        return None
    return 0.0 if e <= 1 else max(0.0, min(1.0, v / (e - 1)))


def keycode_for(value: Any) -> int | None:
    """Map a key name (BACK, KEYCODE_BACK) or numeric keycode to an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 400 else None
    if isinstance(value, str):
        text = value.strip().upper()
        if text.isdigit():
            number = int(text)
            return number if 0 < number < 400 else None
        if text.startswith("KEYCODE_"):
            text = text[8:]
        return KEYCODES.get(text)
    return None


def tap_gesture(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("keyevent"):
        code = keycode_for(data.get("keyevent"))
        return {"kind": "key", "code": code} if code else None
    x = _normalize(data.get("x"), data.get("stream_w"))
    y = _normalize(data.get("y"), data.get("stream_h"))
    if x is None or y is None:
        return None
    return {"kind": "tap", "x": x, "y": y}


def swipe_gesture(data: dict[str, Any]) -> dict[str, Any] | None:
    w, h = data.get("stream_w"), data.get("stream_h")
    points = [_normalize(data.get("x1"), w), _normalize(data.get("y1"), h),
              _normalize(data.get("x2"), w), _normalize(data.get("y2"), h)]
    if any(p is None for p in points):
        return None
    duration = _finite(data.get("duration", SWIPE_DEFAULT_MS))
    duration = SWIPE_DEFAULT_MS if duration is None else duration
    duration = int(max(SWIPE_MIN_MS, min(SWIPE_MAX_MS, duration)))
    x1, y1, x2, y2 = points
    return {"kind": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}


def key_gesture(data: dict[str, Any]) -> dict[str, Any] | None:
    code = keycode_for(data.get("key"))
    return {"kind": "key", "code": code} if code else None


def input_gesture(data: dict[str, Any]) -> dict[str, Any] | None:
    action = data.get("action")
    if action == "keyevent":
        code = keycode_for(data.get("keycode", data.get("key")))
        return {"kind": "key", "code": code} if code else None
    if action == "text" and isinstance(data.get("text"), str):
        return {"kind": "text", "text": data["text"]}
    return None


def sync_targets(data: dict[str, Any], leader: Any) -> list[str]:
    """Validate the optional sync field; returns follower serials (leader excluded)."""
    spec = data.get("sync")
    if spec in (None, False, {}):
        return []
    if not isinstance(spec, dict) or not isinstance(spec.get("devices"), list):
        raise HTTPException(status_code=422, detail="sync must be an object with a devices list")
    devices = spec["devices"]
    if len(devices) > MAX_SYNC_DEVICES:
        raise HTTPException(status_code=422, detail=f"sync is limited to {MAX_SYNC_DEVICES} devices")
    result: list[str] = []
    seen = {leader} if isinstance(leader, str) else set()
    for serial in devices:
        serial = fleet._validate_serial(serial)
        if serial not in seen:
            seen.add(serial)
            result.append(serial)
    return result


def _is_android(serial: Any) -> bool:
    return isinstance(serial, str) and bool(fleet._SERIAL_RE.fullmatch(serial)) and not serial.casefold().startswith("ios")


def control_session(serial: str):
    """Instant input path for a phone: its stream's control socket, else its sync link."""
    session = h264.SESSIONS.get(serial)
    if session is not None:
        try:
            if session.control_ready():
                return session
        except Exception:
            pass
    return sync_links.link_for(serial)


def via_of(session) -> str:
    return "link" if isinstance(session, sync_links.ControlLink) else "control"


def follower_wait(gesture: dict[str, Any]) -> float:
    """How long the leader's response waits for followers (they keep running after)."""
    kind = gesture.get("kind")
    if kind == "swipe":
        return FOLLOWER_WAIT + gesture.get("duration", SWIPE_DEFAULT_MS) / 1000.0
    if kind == "text":
        return TEXT_FOLLOWER_WAIT
    return FOLLOWER_WAIT


def _safe_error(error: BaseException) -> str:
    if isinstance(error, HTTPException):
        return str(error.detail)[:200]
    if isinstance(error, asyncio.TimeoutError):
        return "Timed out"
    text = str(error) or error.__class__.__name__
    return text.splitlines()[0][:200]


# ---------------------------------------------------------------- execution
class ControlLost(RuntimeError):
    """The control socket failed after part of a gesture was delivered."""


def _invalidate_session(session, error: str) -> None:
    invalidate = getattr(session, "invalidate_control", None)
    if invalidate is not None:
        with contextlib.suppress(Exception):
            invalidate(error)


async def _sleep_until(deadline: float | None) -> None:
    """Sleep until an absolute event-loop time (no-op when already past)."""
    if deadline is None:
        return
    loop = asyncio.get_running_loop()
    if deadline <= loop.time():
        return
    ready = loop.create_future()
    def wake() -> None:
        if not ready.done():
            ready.set_result(None)
    # sleep(deadline-now) schedules call_later from a second clock reading. A
    # Windows coarse-clock tick between those reads shifts one follower by a
    # full tick. call_at keeps every phone on the exact shared absolute timer.
    timer = loop.call_at(deadline, wake)
    try:
        await ready
    finally:
        timer.cancel()


async def perform_control(session, gesture: dict[str, Any], arbiter_token=None) -> None:
    """Inject a gesture. Raises ControlUnavailable only when nothing was sent,
    so callers may safely fall back to ADB without repeating a gesture.

    An optional ``t0`` (event-loop time) aligns the first event across phones;
    later events use absolute deadlines so timing does not drift."""
    sent = False
    loop = asyncio.get_running_loop()

    async def release_touch(x: float, y: float) -> bool:
        try:
            try:
                await session.inject_touch(h264.ACTION_UP, x, y, 0.0, arbiter_token=arbiter_token)
            except TypeError:
                await session.inject_touch(h264.ACTION_UP, x, y, 0.0)
            return True
        except Exception:
            return False

    async def touch(action: int, x: float, y: float, pressure: float = 1.0) -> None:
        nonlocal sent
        try:
            try:
                await session.inject_touch(action, x, y, pressure, arbiter_token=arbiter_token)
            except TypeError:
                await session.inject_touch(action, x, y, pressure)
        except h264.ControlWriteAmbiguous as error:
            sent = True
            released = await release_touch(x, y) if action == h264.ACTION_DOWN else False
            detail = "release confirmed" if released else "release unconfirmed"
            _invalidate_session(session, "Control channel delivery is ambiguous; " + detail)
            raise ControlLost("Control channel delivery is ambiguous; action was not replayed; " + detail) from error
        except h264.ControlUnavailable as error:
            if action == h264.ACTION_UP and sent:
                _invalidate_session(session, "Control channel release unconfirmed")
                raise ControlLost("Control channel lost during the gesture; release unconfirmed") from error
            if sent:
                raise ControlLost("Control channel lost during the gesture") from error
            raise
        sent = True

    kind = gesture["kind"]
    await _sleep_until(gesture.get("t0"))
    if kind == "tap":
        await touch(h264.ACTION_DOWN, gesture["x"], gesture["y"])
        down_at = loop.time()
        try:
            await _sleep_until(down_at + TAP_HOLD)
        finally:
            await touch(h264.ACTION_UP, gesture["x"], gesture["y"], 0.0)
        return
    if kind == "swipe":
        x1, y1, x2, y2 = gesture["x1"], gesture["y1"], gesture["x2"], gesture["y2"]
        seconds = gesture["duration"] / 1000.0
        steps = max(1, min(MAX_SWIPE_STEPS, int(round(seconds / SWIPE_STEP))))
        gesture_error: BaseException | None = None
        try:
            await touch(h264.ACTION_DOWN, x1, y1)
            base = loop.time()
            for index in range(1, steps + 1):
                await _sleep_until(base + seconds * index / steps)
                t = index / steps
                await touch(h264.ACTION_MOVE, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        except BaseException as error:
            gesture_error = error
        if isinstance(gesture_error, h264.ControlUnavailable) and not sent:
            raise gesture_error
        release_already_attempted = isinstance(gesture_error, ControlLost) and "release " in str(gesture_error)
        released = ("release confirmed" in str(gesture_error)) if release_already_attempted else await release_touch(x2, y2)
        if not released:
            _invalidate_session(session, "Control channel release unconfirmed")
            raise ControlLost("Control channel lost during the gesture; release unconfirmed") from gesture_error
        if gesture_error is not None:
            if isinstance(gesture_error, h264.ControlWriteAmbiguous):
                _invalidate_session(session, "Control channel delivery is ambiguous; release confirmed")
                raise ControlLost("Control channel delivery is ambiguous; action was not replayed; release confirmed") from gesture_error
            raise gesture_error
        return
    if kind == "key":
        try:
            await session.inject_key(gesture["code"], arbiter_token=arbiter_token)
        except TypeError:
            await session.inject_key(gesture["code"])
        except h264.ControlWriteAmbiguous as error:
            detail = str(error) or "Control channel delivery is ambiguous"
            _invalidate_session(session, detail)
            raise ControlLost(detail) from error
        return
    raise h264.ControlUnavailable("Text uses the Unicode-safe ADB path")


async def perform_adb(serial: str, gesture: dict[str, Any], owner_token=None) -> None:
    # ADB fallback is still a native mutation and must not evade an active AI
    # lease after the scrcpy path becomes unavailable.
    control_arbiter.assert_write_allowed(serial, owner_token)
    kind = gesture["kind"]
    if kind == "text":
        params = fleet._validate_action("text", {"text": gesture["text"]})
        async with fleet._device_locks.setdefault(serial, asyncio.Lock()):
            await asyncio.wait_for(fleet.action_executor(serial, "text", params), timeout=TEXT_TIMEOUT)
        return
    async with _slots():
        if kind == "tap":
            await asyncio.wait_for(fleet.action_executor(serial, "tap", {"x": gesture["x"], "y": gesture["y"]}), timeout=fleet._OPERATION_TIMEOUT)
        elif kind == "swipe":
            params = {k: gesture[k] for k in ("x1", "y1", "x2", "y2", "duration")}
            await asyncio.wait_for(fleet.action_executor(serial, "swipe", params), timeout=fleet._OPERATION_TIMEOUT)
        elif kind == "key":
            await fleet.run_adb("adb", "-s", serial, "shell", "input", "keyevent", str(int(gesture["code"])), timeout=fleet._OPERATION_TIMEOUT)
        else:
            raise ValueError("Unsupported action")


async def run_on_device(serial: str, gesture: dict[str, Any], owner_token=None) -> dict[str, Any]:
    """Run one gesture on one phone: control channel first, ADB fallback."""
    started = time.perf_counter()
    result: dict[str, Any] = {"device": serial, "ok": False, "via": ""}
    try:
        control_arbiter.assert_write_allowed(serial, owner_token)
        if gesture["kind"] == "text":
            if owner_token is None:
                await perform_adb(serial, gesture)
            else:
                # Delegated AI text still uses the existing validated ADB path;
                # perform_adb's owner-less check is intentionally bypassed only
                # after this immediate owner check and device lock.
                async with control_arbiter.acquired(serial, owner_token):
                    await perform_adb(serial, gesture, owner_token)
            result["via"] = "adb"
        else:
            async with control_arbiter.acquired(serial, owner_token) as arbiter_token:
                session = control_session(serial)
                via = ""
                if session is not None:
                    ready = True
                    with contextlib.suppress(Exception):
                        ready = bool(session.control_ready())
                    if ready:
                        try:
                            await perform_control(session, gesture, arbiter_token)
                            via = via_of(session)
                        except h264.ControlUnavailable:
                            via = ""
                if not via:
                    if owner_token is None:
                        await perform_adb(serial, gesture)
                    else:
                        # Native ADB fallback is covered by the same delegated
                        # lease; do not replay an ambiguous control gesture.
                        await perform_adb(serial, gesture, owner_token)
                    via = "adb"
            result["via"] = via
        STATS[result["via"]] = STATS.get(result["via"], 0) + 1
        result["ok"] = True
        LAST_ERRORS.pop(serial, None)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result["error"] = _safe_error(error)
        LAST_ERRORS[serial] = {"error": result["error"], "at": time.time()}
        if len(LAST_ERRORS) > 200:
            for key in list(LAST_ERRORS)[:50]:
                LAST_ERRORS.pop(key, None)
    result["ms"] = round((time.perf_counter() - started) * 1000, 1)
    LAST_RESULTS.pop(serial, None)
    LAST_RESULTS[serial] = {"ok": result["ok"], "via": result["via"], "error": result.get("error", ""),
                            "ms": result["ms"], "action": gesture.get("kind", ""), "at": time.time()}
    if len(LAST_RESULTS) > RESULT_LIMIT:
        for key in list(LAST_RESULTS)[:50]:
            LAST_RESULTS.pop(key, None)
    return result


async def fan_out(targets: list[str], gesture: dict[str, Any], wait: float | None = None) -> dict[str, Any]:
    """Start the gesture on every follower at once.

    The response waits for instant followers (stream/link) up to the gesture
    time; ADB-only followers get a short grace and are reported as pending.
    An explicit ``wait`` applies one limit to every follower."""
    started = time.perf_counter()
    text = gesture.get("kind") == "text"
    instant = {serial: (not text and control_session(serial) is not None) for serial in targets}
    refused: list[dict[str, Any]] = []
    runnable: list[str] = []
    for serial in targets:
        if not instant[serial] and _adb_backlog.get(serial, 0) >= MAX_FOLLOWER_BACKLOG:
            refused.append({"device": serial, "error": "Phone is still catching up on earlier mirrored actions (ADB)"})
        else:
            runnable.append(serial)
    tasks: list[asyncio.Task] = []
    for serial in runnable:
        task = asyncio.create_task(run_on_device(serial, gesture), name="d3m0n-sync-" + serial)
        tasks.append(task)
        _background.add(task)
        task.add_done_callback(_background.discard)
        if not instant[serial]:
            _adb_backlog[serial] = _adb_backlog.get(serial, 0) + 1
            task.add_done_callback(lambda _t, s=serial: _release_backlog(s))
    limit = follower_wait(gesture) if wait is None else wait
    if tasks:
        fast = [t for s, t in zip(runnable, tasks) if instant[s]]
        slow = [t for s, t in zip(runnable, tasks) if not instant[s]]
        slow_limit = limit if (text or wait is not None) else min(limit, ADB_FOLLOWER_WAIT)
        waits = []
        if fast:
            waits.append(asyncio.wait(fast, timeout=limit))
        if slow:
            waits.append(asyncio.wait(slow, timeout=slow_limit))
        await asyncio.gather(*waits)
    done = {t for t in tasks if t.done()}
    pending = {t for t in tasks if not t.done()}
    results = [task.result() for task in tasks if task in done and not task.cancelled()]
    failed = [{"device": r["device"], "error": r.get("error", "failed")} for r in results if not r["ok"]] + refused
    via: dict[str, int] = {}
    for r in results:
        if r["ok"]:
            via[r["via"]] = via.get(r["via"], 0) + 1
    summary = {
        "action": gesture["kind"], "targets": len(targets), "ok": sum(1 for r in results if r["ok"]),
        "failed": failed, "pending": len(pending), "via": via,
        "ms": round((time.perf_counter() - started) * 1000, 1),
        "slowest_ms": max((r["ms"] for r in results), default=0),
    }
    STATS["sync_requests"] += 1
    STATS["sync_targets"] += len(targets)
    STATS["sync_failures"] += len(failed)
    STATS["last_sync"] = {**summary, "at": time.time()}
    return summary


def _release_backlog(serial: str) -> None:
    count = _adb_backlog.get(serial, 0) - 1
    if count > 0:
        _adb_backlog[serial] = count
    else:
        _adb_backlog.pop(serial, None)


async def handle(request: Request, data: dict[str, Any], gesture: dict[str, Any] | None, upstream_call) -> Any:
    leader = data.get("device", "")
    targets = sync_targets(data, leader)
    if targets:
        await fleet._same_origin(request)
    clean = {k: v for k, v in data.items() if k != "sync"}
    manual_lease = control_arbiter.hold(leader)
    try:
        manual_token = await manual_lease.acquire()
    except control_arbiter.AIOwnershipError:
        return {"ok": False, "device": leader, "error": "Device is controlled by an AI worker",
                "sync": {"targets": len(targets), "ok": 0, "pending": 0, "via": {},
                         "failed": [{"device": serial, "error": "Leader is controlled by an AI worker"} for serial in targets]}}
    try:
        follower_task = None
        if targets and gesture is not None:
            if gesture["kind"] != "text":
                # One shared start so the leader and instant followers touch down together.
                # Python 3.10/3.11 Windows monotonic clocks can tick every 15.6ms.
                # A 4ms lead would already be due while followers are prepared.
                lead = max(START_ALIGN, 2 * time.get_clock_info("monotonic").resolution)
                gesture = {**gesture, "t0": asyncio.get_running_loop().time() + lead}
            follower_task = asyncio.create_task(fan_out(targets, gesture))
        response: Any = None
        leader_error: BaseException | None = None
        leader_blocked = False
        try:
            try:
                control_arbiter.assert_write_allowed(leader, None)
            except control_arbiter.AIOwnershipError as error:
                leader_blocked = True
                response = {"ok": False, "device": leader, "error": "Device is controlled by an AI worker"}
            if gesture is not None and gesture["kind"] != "text" and _is_android(leader):
                session = control_session(leader)
                if session is not None:
                    try:
                        async with control_arbiter.acquired(leader, manual_token) as arbiter_token:
                            await perform_control(session, gesture, arbiter_token)
                        via = via_of(session)
                        STATS[via] = STATS.get(via, 0) + 1
                        response = {"ok": True, "device": leader, "platform": "android", "via": via}
                    except h264.ControlUnavailable:
                        response = None
                    except ControlLost as error:
                        LAST_ERRORS[leader] = {"error": _safe_error(error), "at": time.time()}
                        response = {"ok": False, "device": leader, "platform": "android", "via": "control",
                                    "error": _safe_error(error)}
            if response is None and not leader_blocked:
                STATS["upstream"] += 1
                response = await upstream_call(clean)
                if isinstance(response, dict) and "via" not in response:
                    response = {**response, "via": "adb"}
        except BaseException as error:  # re-raised after followers are reported
            leader_error = error
        summary = None
        if follower_task is not None:
            summary = await follower_task
        elif targets:
            summary = {"action": "unsupported", "targets": len(targets), "ok": 0, "pending": 0, "via": {},
                       "failed": [{"device": d, "error": "This action cannot be mirrored"} for d in targets]}
        if leader_error is not None:
            raise leader_error
        if summary is not None and isinstance(response, dict):
            response = {**response, "sync": summary}
        return response
    finally:
        await manual_lease.release()


# ---------------------------------------------------------------- routes
@router.post("/api/phone/tap", summary="Tap On Phone Screen (instant control, optional sync)")
async def fast_tap(request: Request, data: dict = Body({})):
    return await handle(request, data, tap_gesture(data), lambda d: asyncio.to_thread(_upstream().api_phone_tap, d))


@router.post("/api/phone/swipe", summary="Swipe On Phone Screen (instant control, optional sync)")
async def fast_swipe(request: Request, data: dict = Body({})):
    return await handle(request, data, swipe_gesture(data), lambda d: asyncio.to_thread(_upstream().api_phone_swipe, d))


@router.post("/api/phone/key", summary="Send Keyevent To Phone (instant control, optional sync)")
async def fast_key(request: Request, data: dict = Body({})):
    return await handle(request, data, key_gesture(data), lambda d: asyncio.to_thread(_upstream().api_phone_key, d))


@router.post("/api/phone/input", summary="Send Input To Phone (instant control, optional sync)")
async def fast_input(request: Request, data: dict = Body({})):
    return await handle(request, data, input_gesture(data), lambda d: _upstream().phone_input(request, d))


@router.get("/api/phone/control/status", summary="Instant control and sync status")
async def control_status():
    devices = {}
    for serial, session in list(h264.SESSIONS.items()):
        with contextlib.suppress(Exception):
            devices[serial] = {
                "control": session.control_ready(), "control_enabled": session.control_enabled,
                "profile": session.profile, "width": session.width, "height": session.height,
                "control_events": session.control_events, "control_error": session.control_error,
                "viewers": len(session.clients),
            }
    now = time.time()
    disabled = {serial: round(until - now) for serial, until in h264.CONTROL_DISABLED.items() if until > now}
    return {"ok": True, "control_enabled": h264.CONTROL_ENABLED, "devices": devices, "disabled": disabled,
            "ready": sum(1 for d in devices.values() if d["control"]), "stats": STATS,
            "errors": dict(list(LAST_ERRORS.items())[-50:]), "max_sync_devices": MAX_SYNC_DEVICES}


def _stream_control_ready(serial: str) -> bool:
    session = h264.SESSIONS.get(serial)
    try:
        return bool(session is not None and session.control_ready())
    except Exception:
        return False


@router.get("/api/phone/sync/status", summary="SYNC ALL readiness and per-phone results")
async def sync_status():
    """Per phone: which instant path is ready, and the result of its last mirrored action."""
    links = sync_links.links_summary()
    serials = set(links["links"]) | set(h264.SESSIONS) | set(LAST_RESULTS)
    now = time.time()
    devices: dict[str, Any] = {}
    for serial in sorted(serials):
        stream = _stream_control_ready(serial)
        link = links["links"].get(serial)
        link_ready = bool(link and link["ready"])
        last = LAST_RESULTS.get(serial)
        devices[serial] = {
            "instant": stream or link_ready,
            "path": "stream" if stream else "link" if link_ready else "adb",
            "link_state": link["state"] if link else None,
            "link_error": link["error"] if link else "",
            "last": {**last, "age": round(now - last["at"], 1)} if last else None,
        }
    return {"ok": True, "devices": devices, "links_ready": links["ready"], "links_total": links["total"],
            "lease_seconds": links["lease_seconds"], "stats": STATS, "max_sync_devices": MAX_SYNC_DEVICES}


def install_fast_control(app) -> None:
    """Register these routes ahead of the upstream phone routes."""
    from ops import live_input  # imported here: live_input depends on this module
    before = len(app.router.routes)
    app.include_router(router)
    app.include_router(sync_links.router)
    app.include_router(live_input.router)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = added
