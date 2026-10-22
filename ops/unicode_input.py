"""Unicode input over the pinned scrcpy v5.0.1 control protocol.

This is deliberately a short-lived, control-only session.  It does not use an
IME, helper APK, root, or the existing streaming session.

Protocol sources verified against the official v5.0.1 sources:
- https://github.com/Genymobile/scrcpy/tree/v5.0.1/server/src/main/java/com/genymobile/scrcpy/control
- https://raw.githubusercontent.com/Genymobile/scrcpy/v5.0.1/server/src/main/java/com/genymobile/scrcpy/control/ControlMessageReader.java
- https://raw.githubusercontent.com/Genymobile/scrcpy/v5.0.1/server/src/main/java/com/genymobile/scrcpy/control/DeviceMessageWriter.java
- https://raw.githubusercontent.com/Genymobile/scrcpy/v5.0.1/server/src/main/java/com/genymobile/scrcpy/device/DesktopConnection.java
- https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/develop.md

In v5.0.1 a forward, control-only connection receives the one-byte forward
probe followed by 64 bytes of device metadata.  Control messages are big
endian: GET_CLIPBOARD is type 8 plus copy-key byte; SET_CLIPBOARD is type 9,
a signed 64-bit sequence, paste byte, four-byte UTF-8 length, and UTF-8 bytes.
The server replies with device-message type 0 plus a four-byte UTF-8 length and
payload, or type 1 plus the SET sequence as an acknowledgement.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import struct
from pathlib import Path
from typing import Any

VERSION = "5.0.1"
ROOT = Path(__file__).resolve().parents[1]
# Keep the same local asset and remote name as the supplied H.264 source.
SERVER = ROOT / "ops" / "scrcpy-server-v5.0.1.jar"
REMOTE = "/data/local/tmp/ghost-scrcpy-v5.0.1.jar"

MAX_TEXT_CHARS = 4096
MAX_TEXT_BYTES = 16 * 1024
MAX_DEVICE_MESSAGE_BYTES = (1 << 18) - 5
MAX_RESTORE_BYTES = (1 << 18) - 14
ADB_TIMEOUT = 15.0
START_TIMEOUT = 12.0
MESSAGE_TIMEOUT = 5.0
PASTE_SETTLE_SECONDS = 0.15

# A lock remains associated with a serial so concurrent callers cannot
# interleave GET/SET/restore operations on one Android clipboard.
_DEVICE_LOCKS: dict[str, asyncio.Lock] = {}


class UnicodeInputError(RuntimeError):
    """A generic transport/protocol error which never contains clipboard text."""


def _validate_serial(serial: str) -> str:
    if not isinstance(serial, str) or not serial or len(serial) > 150:
        raise ValueError("Invalid Android device serial")
    if any(ch in serial for ch in "\x00\r\n"):
        raise ValueError("Invalid Android device serial")
    return serial


def _encode_text(text: str) -> bytes:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("text exceeds the Unicode input limit")
    if "\x00" in text:
        raise ValueError("text contains a NUL character")
    try:
        raw = text.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        # Do not expose the offending value or its position in an exception.
        raise ValueError("text contains an invalid Unicode character") from None
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("text exceeds the UTF-8 input limit")
    return raw


def _get_lock(serial: str) -> asyncio.Lock:
    lock = _DEVICE_LOCKS.get(serial)
    if lock is None:
        lock = asyncio.Lock()
        _DEVICE_LOCKS[serial] = lock
    return lock


async def adb(serial: str, *args: str, timeout: float = ADB_TIMEOUT) -> str:
    """Run one direct adb command; this function is intentionally injectable in tests."""
    serial = _validate_serial(serial)
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    if proc.returncode:
        # adb output is intentionally not returned: it can contain device data.
        raise UnicodeInputError("ADB operation failed")
    return out.decode("utf-8", errors="replace").strip()


def _server_command(serial: str, scid: str) -> list[str]:
    # This mirrors the supplied H.264 source's app_process convention.  The
    # byte cap is checked on the Android-side command, not the host adb prefix.
    args = [
        "adb", "-s", serial, "shell", "CLASSPATH=" + REMOTE,
        "app_process", "/", "com.genymobile.scrcpy.Server", VERSION,
        "scid=" + scid,
        "tunnel_forward=true",
        "video=false",
        "audio=false",
        "control=true",
        "clipboard_autosync=false",
        "cleanup=false",
    ]
    if len(" ".join(args[5:]).encode("utf-8")) >= 255:
        raise UnicodeInputError("scrcpy control command exceeds the Android command limit")
    return args


async def _android9_clipboard_presence(serial: str) -> bool | None:
    """Use AOSP API28 hasPrimaryClip, never clipboard data or a timeout guess.

    Pinned AIDL: aosp-mirror/platform_frameworks_base/android-9.0.0_r61/
    core/java/android/content/IClipboard.aidl. Other SDKs fail closed here.
    """
    try:
        sdk = await adb(serial, "shell", "getprop", "ro.build.version.sdk", timeout=3)
        if sdk.strip() != "28":
            return None
        response = await adb(serial, "shell", "service", "call", "clipboard", "5", "s16", "com.android.shell", timeout=3)
        words = re.findall(r"\b[0-9a-fA-F]{8}\b", response)
        if len(words) != 2 or int(words[0], 16) != 0 or int(words[1], 16) not in (0, 1):
            return None
        return bool(int(words[1], 16))
    except Exception:
        return None


async def _restore_absent_android9_clipboard(serial: str) -> None:
    response = await adb(serial, "shell", "service", "call", "clipboard", "2", "s16", "com.android.shell", timeout=3)
    words = re.findall(r"\b[0-9a-fA-F]{8}\b", response)
    if len(words) != 1 or int(words[0], 16) != 0 or await _android9_clipboard_presence(serial) is not False:
        raise UnicodeInputError("Unable to restore the absent Android clipboard")


class _ControlSession:
    """One isolated adb-forwarded scrcpy control channel."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.scid = f"{secrets.randbits(31):08x}"
        self.port: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._closed = False
        self._original_clipboard_absent: bool | None = None

    async def open(self) -> None:
        try:
            forwarded = await adb(
                self.serial, "forward", "tcp:0", "localabstract:scrcpy_" + self.scid,
                timeout=8,
            )
            try:
                self.port = int(forwarded.rsplit(":", 1)[-1])
            except (TypeError, ValueError):
                raise UnicodeInputError("ADB returned an invalid control port") from None
            if not self.port or not (1 <= self.port <= 65535):
                raise UnicodeInputError("ADB returned an invalid control port")

            self.proc = await asyncio.create_subprocess_exec(
                *_server_command(self.serial, self.scid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            deadline = asyncio.get_running_loop().time() + START_TIMEOUT
            while True:
                if self.proc.returncode is not None:
                    raise UnicodeInputError("scrcpy control session exited during startup")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise UnicodeInputError("scrcpy control session did not connect")
                try:
                    self.reader, self.writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", self.port),
                        min(1.0, remaining),
                    )
                    # ADB accepts the local forward before Android creates the
                    # abstract socket. An early connection may reset; retry
                    # the handshake only, before any clipboard command exists.
                    probe = await asyncio.wait_for(self.reader.readexactly(1), min(MESSAGE_TIMEOUT, remaining))
                    if probe != b"\x00":
                        raise UnicodeInputError("invalid scrcpy control handshake")
                    await asyncio.wait_for(self.reader.readexactly(64), min(MESSAGE_TIMEOUT, remaining))
                    break
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                    if self.writer is not None:
                        self.writer.close()
                        with contextlib.suppress(Exception):
                            await self.writer.wait_closed()
                    self.reader = None
                    self.writer = None
                    await asyncio.sleep(0.05)
        except BaseException:
            await self.close()
            raise

    async def _read_message(self, timeout: float = MESSAGE_TIMEOUT) -> tuple[str, Any]:
        if self.reader is None:
            raise UnicodeInputError("control session is not connected")
        try:
            kind = (await asyncio.wait_for(self.reader.readexactly(1), timeout))[0]
            if kind == 0:  # DeviceMessage.TYPE_CLIPBOARD
                length = struct.unpack(">I", await asyncio.wait_for(self.reader.readexactly(4), timeout))[0]
                if length > MAX_DEVICE_MESSAGE_BYTES:
                    raise UnicodeInputError("clipboard response is too large")
                raw = await asyncio.wait_for(self.reader.readexactly(length), timeout)
                try:
                    return "clipboard", raw.decode("utf-8", "strict")
                except UnicodeDecodeError:
                    raise UnicodeInputError("device returned invalid clipboard text") from None
            if kind == 1:  # DeviceMessage.TYPE_ACK_CLIPBOARD
                sequence = struct.unpack(">q", await asyncio.wait_for(self.reader.readexactly(8), timeout))[0]
                return "ack", sequence
            raise UnicodeInputError("unexpected scrcpy device message")
        except asyncio.TimeoutError:
            raise UnicodeInputError("scrcpy clipboard response unavailable") from None
        except asyncio.IncompleteReadError:
            raise UnicodeInputError("scrcpy control session closed") from None

    async def get_clipboard(self) -> str:
        if self.writer is None:
            raise UnicodeInputError("control session is not connected")
        present = await _android9_clipboard_presence(self.serial)
        if present is False:
            if self._original_clipboard_absent is None:
                self._original_clipboard_absent = True
            return ""
        # TYPE_GET_CLIPBOARD=8, COPY_KEY_NONE=0.  With autosync disabled,
        # Android sends a clipboard response only when it can read the clip;
        # timeout therefore means unavailable, not an empty clipboard.
        self.writer.write(bytes((8, 0)))
        await self.writer.drain()
        kind, value = await self._read_message()
        if kind != "clipboard":
            raise UnicodeInputError("scrcpy did not return the clipboard")
        if self._original_clipboard_absent is None:
            self._original_clipboard_absent = False
        return value

    async def set_clipboard(self, raw: bytes, *, paste: bool) -> None:
        if self.writer is None:
            raise UnicodeInputError("control session is not connected")
        sequence = secrets.randbits(63) or 1
        # TYPE_SET_CLIPBOARD=9; sequence is long, then paste boolean, then
        # four-byte big-endian UTF-8 byte length and payload.
        self.writer.write(
            bytes((9,)) + struct.pack(">q", sequence) + bytes((1 if paste else 0,))
            + struct.pack(">I", len(raw)) + raw
        )
        await self.writer.drain()
        kind, received = await self._read_message()
        if kind != "ack" or received != sequence:
            raise UnicodeInputError("scrcpy clipboard update was not acknowledged")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
            self.writer = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                with contextlib.suppress(Exception):
                    await self.proc.wait()
            except Exception:
                with contextlib.suppress(Exception):
                    await self.proc.wait()
        if self.port is not None:
            with contextlib.suppress(Exception):
                await adb(self.serial, "forward", "--remove", "tcp:" + str(self.port), timeout=3)
            self.port = None

    async def __aenter__(self) -> "_ControlSession":
        await self.open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()


def _restorable_bytes(original: str) -> bytes:
    raw = original.encode("utf-8", "strict")
    if len(raw) > MAX_RESTORE_BYTES:
        raise UnicodeInputError("Original clipboard exceeds the protocol restoration limit")
    return raw


async def _restore(session: _ControlSession, original: str) -> None:
    # The restoration limit is checked before any caller overwrites the clip.
    if session._original_clipboard_absent:
        await _restore_absent_android9_clipboard(session.serial)
    else:
        await session.set_clipboard(_restorable_bytes(original), paste=False)


async def paste_text(serial: str, text: str) -> None:
    """Paste Unicode text through a private scrcpy control-only transaction.

    The original clipboard is read first, restored after the short paste
    consumption window, and is also restored on ordinary failures whenever the
    control channel remains usable.  No clipboard value is returned or logged.
    """
    serial = _validate_serial(serial)
    raw = _encode_text(text)
    async with _get_lock(serial):
        session = _ControlSession(serial)
        original: str | None = None
        try:
            await session.open()
            original = await session.get_clipboard()
            _restorable_bytes(original)
            await session.set_clipboard(raw, paste=False)
            if await session.get_clipboard() != text:
                raise UnicodeInputError("Unicode clipboard write could not be verified")
            await adb(serial, "shell", "input", "keyevent", "KEYCODE_PASTE", timeout=5)
            await asyncio.sleep(PASTE_SETTLE_SECONDS)
        except BaseException as exc:
            if original is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(_restore(session, original))
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, (ValueError, TypeError)):
                raise
            raise UnicodeInputError("Unicode paste failed; original clipboard restoration was attempted") from None
        else:
            try:
                if original is None:
                    raise UnicodeInputError("original clipboard was not captured")
                await _restore(session, original)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise UnicodeInputError("Unicode paste completed but clipboard restoration failed") from None
        finally:
            await session.close()


async def clipboard_roundtrip_test(serial: str, text: str) -> bool:
    """Validate SET/ACK and restoration without pasting or returning clip data."""
    serial = _validate_serial(serial)
    raw = _encode_text(text)
    async with _get_lock(serial):
        session = _ControlSession(serial)
        original: str | None = None
        try:
            await session.open()
            original = await session.get_clipboard()
            _restorable_bytes(original)
            await session.set_clipboard(raw, paste=False)
            if original is None:
                return False
            written = await session.get_clipboard()
            await _restore(session, original)
            restored = await session.get_clipboard()
            return written == text and restored == original
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            if original is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(_restore(session, original))
            raise
        except Exception:
            if original is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(_restore(session, original))
            return False
        finally:
            await session.close()


__all__ = ["paste_text", "clipboard_roundtrip_test"]
