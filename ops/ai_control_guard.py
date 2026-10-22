"""Ownership and fencing guard for AI fleet native mutations.

The guard is deliberately independent of Mobilerun and providers.  It binds a
JobStore authorization (child, serial, capability, fence) to an opaque control
arbiter owner token.  Manual HTTP, websocket, Sync and legacy native paths use
an owner-less token and are rejected while an AI lease exists.  An AI worker
must present the same child/fence/capability immediately before each native
operation; expiry, stop, stale fences and takeover fail closed.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ai_contracts.models import Authorization, Capability
from ops import control_arbiter


class AIControlError(RuntimeError):
    """Base class for safe, non-secret ownership failures."""


class AIControlDenied(AIControlError):
    pass


class AILeaseExpired(AIControlDenied):
    pass


class AILeaseRevoked(AIControlDenied):
    pass


class AILeaseConflict(AIControlDenied):
    pass


class AIAmbiguousWrite(AIControlDenied):
    """The underlying native path reported an accepted-but-unconfirmed write."""


@dataclass(frozen=True)
class AIOwnership:
    serial: str
    child_id: str
    fence: int
    capabilities: frozenset[Capability]
    owner_token: object
    lease_id: str
    expires_at: float


# A small process-local registry is intentional: the durable JobStore remains
# the authority for approval/fences; this registry only blocks same-process
# manual/native writers while a job is actively using a device.
_ACTIVE: dict[str, AIOwnership] = {}
_TAKEN_OVER: set[str] = set()


def reset_for_tests() -> None:
    for serial, lease in list(_ACTIVE.items()):
        control_arbiter.release_ai(serial, lease.owner_token)
    _ACTIVE.clear()
    _TAKEN_OVER.clear()


def active(serial: str) -> AIOwnership | None:
    lease = _ACTIVE.get(serial)
    if lease is None:
        return None
    if lease.expires_at <= time.monotonic():
        _expire(serial, lease)
        return None
    return lease


def _expire(serial: str, lease: AIOwnership) -> None:
    if _ACTIVE.get(serial) is lease:
        _ACTIVE.pop(serial, None)
        control_arbiter.release_ai(serial, lease.owner_token)


def _capability(value: Capability | str) -> Capability:
    try:
        return value if isinstance(value, Capability) else Capability(value)
    except (TypeError, ValueError) as exc:
        raise AIControlDenied("Unsupported AI capability") from exc


def _same_lease(a: AIOwnership | None, b: AIOwnership | None) -> bool:
    """Same grant across heartbeat renewals: id, fence, child and private owner token must all match."""
    return (a is not None and b is not None and a.serial == b.serial and a.lease_id == b.lease_id
            and a.fence == b.fence and a.child_id == b.child_id and a.owner_token is b.owner_token)


class AIControlGuard:
    """Bind trusted JobStore authorization to per-device native ownership."""

    def __init__(self, job_store: Any | None = None, *, lease_seconds: float = 30.0,
                 clock: Callable[[], float] | None = None) -> None:
        self.job_store = job_store
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.clock = clock or time.monotonic
        self._leases: dict[str, AIOwnership] = {}
        self._stopped: set[str] = set()

    def acquire(self, child_id: str, serial: str, capability: Capability | str, fence: int,
                *, owner_token: object | None = None, expires_at: float | None = None) -> AIOwnership:
        """Acquire exclusive AI ownership after trusted JobStore authorization.

        ``owner_token`` is never accepted from model/provider data; the runtime
        driver creates it and keeps it private.  The guard does not accept a
        free-form approval flag as a substitute for JobStore authorization.
        """
        cap = _capability(capability)
        if not isinstance(child_id, str) or not child_id or not isinstance(serial, str) or not serial:
            raise AIControlDenied("AI ownership identity is invalid")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise AIControlDenied("AI fencing token is invalid")
        if child_id in self._stopped:
            raise AILeaseRevoked("AI job is stopped")
        if self.job_store is not None:
            try:
                authorization: Authorization = self.job_store.authorize(child_id, serial, cap, fence)
            except Exception as exc:
                # Preserve only the safe typed failure class/message boundary.
                raise AIControlDenied("AI authorization was rejected") from exc
            expiry = authorization.expires_at.timestamp()
            if expiry <= time.time():
                raise AILeaseExpired("AI authorization expired")
        else:
            expiry = time.time() + self.lease_seconds
        existing = active(serial)
        if existing is not None and existing.child_id != child_id:
            raise AILeaseConflict("Device is owned by another AI worker")
        token = owner_token if owner_token is not None else object()
        if existing is not None:
            if existing.child_id != child_id or existing.fence != fence:
                raise AILeaseConflict("Device AI fence is already owned")
            capabilities = existing.capabilities | {cap}
            renewed = AIOwnership(existing.serial, existing.child_id, existing.fence, capabilities,
                                  existing.owner_token, existing.lease_id,
                                  self.clock() + self.lease_seconds)
            _ACTIVE[serial] = renewed
            self._leases[serial] = renewed
            control_arbiter.renew_ai(serial, existing.owner_token, renewed.expires_at)
            return renewed
        ttl = self.lease_seconds if expires_at is None else max(0.1, expires_at - time.time())
        lease = AIOwnership(serial, child_id, fence, frozenset({cap}), token, uuid.uuid4().hex,
                            self.clock() + ttl)
        try:
            control_arbiter.claim_ai(serial, token, lease.lease_id, child_id, fence,
                                     expires_at=lease.expires_at)
        except Exception as exc:
            raise AILeaseConflict("Device is currently controlled") from exc
        _ACTIVE[serial] = lease
        self._leases[serial] = lease
        return lease

    def renew(self, lease: AIOwnership, *, expires_at: float | None = None) -> AIOwnership:
        current = self._leases.get(lease.serial)
        if not (_same_lease(current, lease) and _same_lease(active(lease.serial), lease)):
            raise AILeaseRevoked("AI lease is no longer active")
        lease = current
        duration = self.lease_seconds if expires_at is None else max(0.1, expires_at - time.time())
        renewed = AIOwnership(lease.serial, lease.child_id, lease.fence, lease.capabilities,
                              lease.owner_token, lease.lease_id, self.clock() + duration)
        _ACTIVE[lease.serial] = renewed
        self._leases[lease.serial] = renewed
        control_arbiter.renew_ai(lease.serial, lease.owner_token, renewed.expires_at)
        return renewed

    def require(self, lease: AIOwnership | None = None, *, child_id: str | None = None,
                serial: str | None = None, capability: Capability | str | None = None,
                fence: int | None = None) -> AIOwnership:
        """Validate identity/fence/capability immediately before a native call."""
        item = lease or (self._leases.get(serial or "") if serial else None)
        if item is None:
            raise AIControlDenied("AI ownership is required")
        current = self._leases.get(item.serial)
        if not (_same_lease(current, item) and _same_lease(active(item.serial), item)) or item.serial in self._stopped:
            raise AILeaseRevoked("AI lease is no longer active")
        item = current  # A heartbeat renewal replaces the object; keep its fresh expiry/capabilities.
        if child_id is not None and child_id != item.child_id:
            raise AIControlDenied("AI child does not own this device")
        if serial is not None and serial != item.serial:
            raise AIControlDenied("AI target drift detected")
        if fence is not None and fence != item.fence:
            raise AIControlDenied("Stale AI fence")
        if capability is not None and _capability(capability) not in item.capabilities:
            raise AIControlDenied("AI capability is outside approved scope")
        try:
            control_arbiter.assert_write_allowed(item.serial, item.owner_token)
        except Exception as exc:
            raise AIControlDenied("AI control ownership was taken over") from exc
        return item

    def release(self, lease: AIOwnership) -> None:
        current = self._leases.get(lease.serial)
        if _same_lease(current, lease):
            self._leases.pop(lease.serial, None)
            if _same_lease(_ACTIVE.get(lease.serial), lease):
                _ACTIVE.pop(lease.serial, None)
            control_arbiter.release_ai(lease.serial, lease.owner_token)

    def stop(self, child_id: str) -> int:
        """Fence and release every device owned by a stopped child."""
        self._stopped.add(child_id)
        released = 0
        for serial, lease in list(self._leases.items()):
            if lease.child_id == child_id:
                self.release(lease)
                control_arbiter.takeover_ai(serial)
                released += 1
        return released

    def takeover(self, serial: str, *, owner: str = "owner") -> None:
        """Explicit owner/manual takeover; stale AI tokens cannot write afterward."""
        lease = self._leases.pop(serial, None)
        if lease is not None:
            _ACTIVE.pop(serial, None)
            control_arbiter.release_ai(serial, lease.owner_token)
        control_arbiter.takeover_ai(serial)
        _TAKEN_OVER.add(serial)

    def clear_takeover(self, serial: str) -> None:
        _TAKEN_OVER.discard(serial)


# Functional seams used by HTTP/native paths and tests.
def assert_manual_allowed(serial: str) -> None:
    control_arbiter.assert_write_allowed(serial, None)


def owner_for(lease: AIOwnership) -> object:
    return lease.owner_token


__all__ = [
    "AIControlError", "AIControlDenied", "AILeaseExpired", "AILeaseRevoked", "AILeaseConflict",
    "AIAmbiguousWrite", "AIOwnership", "AIControlGuard", "assert_manual_allowed", "owner_for",
    "active", "reset_for_tests",
]
