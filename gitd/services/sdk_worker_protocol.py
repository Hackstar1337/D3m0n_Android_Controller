"""Restricted parent/SDK-worker newline JSON protocol.

This module is intentionally transport-agnostic.  The parent owns stdin/stdout,
JobStore, native bridge, provider gateway, and secrets; the SDK subprocess can
only exchange validated bounded envelopes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

MAX_LINE_BYTES = 256 * 1024
MAX_ID = 128
MAX_PAYLOAD_BYTES = 128 * 1024
KINDS = frozenset({"observe", "action", "llm", "authorize", "heartbeat", "finish"})
ROLES = frozenset({"planner", "executor", "verifier"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

class WorkerProtocolError(ValueError):
    pass

@dataclass(frozen=True)
class WorkerRequest:
    request_id: str
    nonce: str
    kind: str
    job_id: str
    child_id: str
    serial: str
    fence: int
    role: str
    payload: Mapping[str, Any]
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1 or self.kind not in KINDS or self.role not in ROLES:
            raise WorkerProtocolError("unsupported protocol envelope")
        for value in (self.request_id, self.nonce, self.job_id, self.child_id, self.serial):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise WorkerProtocolError("invalid protocol identity")
        if isinstance(self.fence, bool) or not isinstance(self.fence, int) or self.fence < 1:
            raise WorkerProtocolError("invalid protocol fence")
        if not isinstance(self.payload, Mapping) or len(json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            raise WorkerProtocolError("protocol payload exceeds limit")

    @classmethod
    def from_line(cls, line: str | bytes) -> "WorkerRequest":
        raw = line.encode() if isinstance(line, str) else line
        if len(raw) > MAX_LINE_BYTES:
            raise WorkerProtocolError("protocol line exceeds limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerProtocolError("invalid JSON envelope") from exc
        if not isinstance(value, Mapping):
            raise WorkerProtocolError("envelope must be an object")
        allowed = {"v", "kind", "request_id", "nonce", "job_id", "child_id", "serial", "fence", "role", "payload"}
        if set(value) != allowed:
            raise WorkerProtocolError("unknown or missing envelope field")
        return cls(version=value["v"], kind=value["kind"], request_id=value["request_id"], nonce=value["nonce"], job_id=value["job_id"], child_id=value["child_id"], serial=value["serial"], fence=value["fence"], role=value["role"], payload=value["payload"])

    def as_line(self) -> str:
        return json.dumps({"v": self.version, "kind": self.kind, "request_id": self.request_id, "nonce": self.nonce, "job_id": self.job_id, "child_id": self.child_id, "serial": self.serial, "fence": self.fence, "role": self.role, "payload": dict(self.payload)}, separators=(",", ":"), ensure_ascii=False)

@dataclass(frozen=True)
class WorkerResponse:
    request_id: str
    nonce: str
    kind: str
    child_id: str
    fence: int
    payload: Mapping[str, Any]
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1 or self.kind not in {"progress", "result", "error"} or not _ID.fullmatch(self.request_id) or not _ID.fullmatch(self.nonce) or not _ID.fullmatch(self.child_id):
            raise WorkerProtocolError("invalid response envelope")
        if isinstance(self.fence, bool) or not isinstance(self.fence, int) or self.fence < 1:
            raise WorkerProtocolError("invalid response fence")
        if not isinstance(self.payload, Mapping) or len(json.dumps(self.payload, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
            raise WorkerProtocolError("response payload exceeds limit")

    @classmethod
    def from_line(cls, line: str | bytes) -> "WorkerResponse":
        raw = line.encode() if isinstance(line, str) else line
        if len(raw) > MAX_LINE_BYTES:
            raise WorkerProtocolError("protocol line exceeds limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkerProtocolError("invalid JSON response") from exc
        if not isinstance(value, Mapping):
            raise WorkerProtocolError("response must be an object")
        allowed = {"v", "kind", "request_id", "nonce", "child_id", "fence", "payload"}
        if set(value) != allowed:
            raise WorkerProtocolError("unknown or missing response field")
        return cls(version=value["v"], kind=value["kind"], request_id=value["request_id"], nonce=value["nonce"], child_id=value["child_id"], fence=value["fence"], payload=value["payload"])

    def as_line(self) -> str:
        return json.dumps({"v": self.version, "kind": self.kind, "request_id": self.request_id, "nonce": self.nonce, "child_id": self.child_id, "fence": self.fence, "payload": dict(self.payload)}, separators=(",", ":"), ensure_ascii=False)

class WorkerBroker:
    """Parent-side validator; callback is reached only after identity checks."""
    def __init__(self, *, nonce: str, child_id: str, serial: str, fence: int, role: str, stopped: Any = None):
        self.nonce, self.child_id, self.serial, self.fence, self.role, self.stopped = nonce, child_id, serial, fence, role, stopped
        if role not in ROLES or not _ID.fullmatch(nonce) or not _ID.fullmatch(child_id) or not _ID.fullmatch(serial) or fence < 1:
            raise WorkerProtocolError("invalid broker binding")

    def validate(self, request: WorkerRequest) -> None:
        if request.nonce != self.nonce or request.child_id != self.child_id or request.serial != self.serial or request.fence != self.fence or request.role != self.role:
            raise WorkerProtocolError("worker request binding mismatch")
        if self.stopped is not None and bool(self.stopped() if callable(self.stopped) else self.stopped):
            raise WorkerProtocolError("worker request rejected after Stop")

__all__ = ["WorkerProtocolError", "WorkerRequest", "WorkerResponse", "WorkerBroker"]
