"""Small typed seams shared by the Mobilerun native bridge and tests.

This module contains no transport, shell, ADB, URL or provider implementation.
Only explicitly registered typed callables may be injected for capabilities not
present in the current D3m0n native modules.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ai_contracts.models import Capability, redact_metadata


class NativeBridgeError(RuntimeError):
    pass


class CapabilityNotSupported(NativeBridgeError):
    pass


class NativeTargetDrift(NativeBridgeError):
    pass


class NativeConfirmationRequired(NativeBridgeError):
    pass


@dataclass(frozen=True)
class NativeCall:
    serial: str
    capability: Capability
    operation: str
    fence: int
    child_id: str


@dataclass(frozen=True)
class NativeResult:
    """Metadata-only result; arbitrary native/provider payloads are excluded."""
    ok: bool
    operation: str
    serial: str | None = None
    status: str = "completed"
    summary: str = ""
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "serial": self.serial,
            "status": self.status,
            "summary": self.summary[:500],
            "metadata": redact_metadata(dict(self.metadata or {})),
        }


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def invoke_typed(callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke an already-selected adapter callable, never a user-supplied name."""
    if not callable(callable_):
        raise CapabilityNotSupported("Native capability adapter is unavailable")
    return await maybe_await(callable_(*args, **kwargs))


__all__ = [
    "NativeBridgeError", "CapabilityNotSupported", "NativeTargetDrift",
    "NativeConfirmationRequired", "NativeCall", "NativeResult", "maybe_await",
    "invoke_typed",
]
