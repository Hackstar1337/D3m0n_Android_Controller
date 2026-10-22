"""One-time owner confirmation grants for high-impact native tools.

A grant is issued only by the authenticated owner through the reconcile API
for one exact child job, capability and operation.  The SDK worker never sees
or supplies it; the parent broker consumes it once when the native wrapper
asks.  Grants live in memory only, so a backend restart fails closed.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

GRANTABLE: Dict[str, frozenset] = {
    "reboot": frozenset({"reboot"}),
    "file_write": frozenset({"push", "pull"}),
    "apk_install": frozenset({"install"}),
    "group_write": frozenset({"create", "update", "delete", "add", "remove", "rename", "assign", "unassign"}),
}
DEFAULT_TTL_SECONDS = 600
MAX_GRANTS = 500


class OwnerGrantStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: Dict[Tuple[str, str, str], float] = {}

    def _prune(self, now: float) -> None:
        for key in [key for key, expiry in self._grants.items() if expiry <= now]:
            self._grants.pop(key, None)

    def issue(self, child_id: str, capability: str, operation: str, ttl: int = DEFAULT_TTL_SECONDS) -> float:
        capability = str(capability or "").strip().lower()
        operation = str(operation or "").strip().lower()
        if capability not in GRANTABLE or operation not in GRANTABLE[capability]:
            raise ValueError("grant capability/operation is not an owner-confirmable action")
        if not child_id or len(child_id) > 120:
            raise ValueError("grant child id is invalid")
        ttl = max(30, min(int(ttl), 3600))
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._grants) >= MAX_GRANTS:
                raise ValueError("too many pending owner grants")
            expiry = now + ttl
            self._grants[(child_id, capability, operation)] = expiry
            return expiry

    def consume(self, child_id: str, capability: str, operation: str) -> bool:
        key = (str(child_id), str(capability).lower(), str(operation).lower())
        now = time.monotonic()
        with self._lock:
            expiry = self._grants.pop(key, None)
            return expiry is not None and expiry > now

    def pending(self, child_id: str) -> list[dict[str, str]]:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return [{"capability": cap, "operation": op} for (cid, cap, op) in self._grants if cid == child_id]

    def revoke_child(self, child_id: str) -> None:
        with self._lock:
            for key in [key for key in self._grants if key[0] == child_id]:
                self._grants.pop(key, None)


_STORE = OwnerGrantStore()


def get_owner_grants() -> OwnerGrantStore:
    return _STORE


__all__ = ["GRANTABLE", "OwnerGrantStore", "get_owner_grants"]
