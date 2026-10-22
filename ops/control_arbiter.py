"""Authoritative per-device serialization for all control-channel writers.

A lease token may span a complete live gesture while lower-level writers remain
safe when called independently by HTTP or other control paths.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any



class AmbiguousWrite(RuntimeError):
    """The OS accepted write bytes but drain did not confirm delivery."""


@dataclass
class _Entry:
    lock: asyncio.Lock
    owner: object | None = None
    depth: int = 0


_ENTRIES: dict[str, _Entry] = {}
_AI_OWNERS: dict[str, tuple[object, str, str, int, float]] = {}


class AIOwnershipError(RuntimeError):
    """A manual/native writer attempted to bypass an active AI lease."""


def _expire_ai(serial: str) -> None:
    item = _AI_OWNERS.get(serial)
    if item is not None and item[4] <= time.monotonic():
        _AI_OWNERS.pop(serial, None)


def assert_write_allowed(serial: str, owner: object | None = None) -> None:
    """Reject owner-less/manual writes while an AI lease is active."""
    _expire_ai(serial)
    item = _AI_OWNERS.get(serial)
    if item is not None and item[0] is not owner:
        raise AIOwnershipError("Device is controlled by an AI worker")


def claim_ai(serial: str, owner: object, lease_id: str, child_id: str, fence: int,
             *, expires_at: float) -> None:
    _expire_ai(serial)
    manual = _ENTRIES.get(serial)
    if manual is not None and manual.lock.locked() and manual.owner is not owner:
        raise AIOwnershipError("A native gesture is already in progress")
    existing = _AI_OWNERS.get(serial)
    if existing is not None and existing[0] is not owner:
        raise AIOwnershipError("Device is controlled by another AI worker")
    _AI_OWNERS[serial] = (owner, lease_id, child_id, fence, float(expires_at))


def renew_ai(serial: str, owner: object, expires_at: float) -> None:
    item = _AI_OWNERS.get(serial)
    if item is None or item[0] is not owner:
        raise AIOwnershipError("AI lease is no longer active")
    _AI_OWNERS[serial] = (item[0], item[1], item[2], item[3], float(expires_at))


def release_ai(serial: str, owner: object) -> None:
    item = _AI_OWNERS.get(serial)
    if item is not None and item[0] is owner:
        _AI_OWNERS.pop(serial, None)


def takeover_ai(serial: str) -> None:
    """Invalidate the current AI owner; explicit owner takeover path."""
    _AI_OWNERS.pop(serial, None)


def reset_for_tests() -> None:
    _ENTRIES.clear()
    _AI_OWNERS.clear()


def token() -> object:
    return object()


def _entry(serial: str) -> _Entry:
    entry = _ENTRIES.get(serial)
    if entry is None:
        entry = _ENTRIES[serial] = _Entry(asyncio.Lock())
    return entry


class Lease:
    def __init__(self, serial: str, owner: object | None = None) -> None:
        self.serial = serial
        self.owner = owner if owner is not None else token()
        self._entry: _Entry | None = None
        self._acquired = False

    async def acquire(self) -> object:
        entry = _entry(self.serial)
        assert_write_allowed(self.serial, self.owner)
        if entry.owner is not self.owner:
            await entry.lock.acquire()
            try:
                assert_write_allowed(self.serial, self.owner)
            except BaseException:
                entry.lock.release()
                raise
            entry.owner = self.owner
            entry.depth = 0
        entry.depth += 1
        self._entry = entry
        self._acquired = True
        return self.owner

    async def release(self) -> None:
        if not self._acquired or self._entry is None:
            return
        entry = self._entry
        self._acquired = False
        entry.depth -= 1
        if entry.depth == 0:
            entry.owner = None
            entry.lock.release()
            waiters = getattr(entry.lock, "_waiters", None) or ()
            if not entry.lock.locked() and not waiters and _ENTRIES.get(self.serial) is entry:
                _ENTRIES.pop(self.serial, None)

    async def __aenter__(self) -> object:
        return await self.acquire()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


def hold(serial: str, owner: object | None = None) -> Lease:
    return Lease(serial, owner)


async def write(serial: str, writer: Any, payload: bytes, *, owner: object | None = None,
                drain_timeout: float = 2.0) -> None:
    """Write exactly once under the serial lease.

    A successful ``writer.write`` followed by a failed drain is explicitly
    ambiguous: callers must not replay the action through ADB.
    """
    async with hold(serial, owner):
        if writer is None or writer.is_closing():
            raise RuntimeError("No live control channel")
        try:
            writer.write(payload)
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            raise RuntimeError(str(error) or error.__class__.__name__) from error
        try:
            await asyncio.wait_for(writer.drain(), drain_timeout)
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            raise AmbiguousWrite(str(error) or error.__class__.__name__) from error


@contextlib.asynccontextmanager
async def acquired(serial: str, owner: object | None = None):
    lease = hold(serial, owner)
    actual = await lease.acquire()
    try:
        yield actual
    finally:
        await lease.release()
