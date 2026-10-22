"""Closed Mobilerun tool registry for the concrete native bridge.

This module only maps already-typed :class:`NativeBridge` methods.  It never
accepts a model-selected callable, shell fragment, URL, credential, or raw
native payload.  High-impact mutation wrappers require a private owner-issued
confirmation grant; the grant is deliberately not a tool parameter, so a model
cannot manufacture ``confirmed=True``.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from ai_contracts.models import Capability
from .native_bridge_tools import NativeBridgeError, NativeConfirmationRequired


def _closed(schema: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(schema)
    if result.get("type") == "object":
        result["additionalProperties"] = False
    return result


def _annotate(specs: dict[str, dict[str, Any]], name: str, capability: Capability, mutation: bool) -> None:
    spec = specs.get(name)
    if spec is None:
        return
    spec["capability"] = capability.value
    spec["mutation"] = mutation
    parameters = spec.get("parameters")
    if isinstance(parameters, Mapping):
        spec["parameters"] = _closed(parameters)


def _grant(grant: Callable[[Capability, str], Any] | None, capability: Capability, operation: str) -> bool:
    if grant is None:
        return False
    try:
        return grant(capability, operation) is True
    except Exception:
        return False


def extend_native_tool_specs(*, bridge: Any, specs: Mapping[str, Mapping[str, Any]], serial: str,
                             capability_authorizer: Callable[[Capability], Any], cancelled: Callable[[], bool],
                             confirmation_grant: Callable[[Capability, str], Any] | None = None
                             ) -> dict[str, dict[str, Any]]:
    """Add concrete native mappings and annotate the pre-existing safe aliases.

    The returned mapping is intentionally plain data plus callables because the
    Mobilerun parent converts it into its own closed tool protocol.
    """
    result = {name: dict(spec) for name, spec in specs.items()}
    existing = {
        "observe": (Capability.OBSERVE, False),
        "input": (Capability.INPUT, True),
        "navigate": (Capability.NAVIGATE, True),
        "current_app": (Capability.APP_STATE, False),
        "app_state": (Capability.APP_STATE, False),
        "group_write": (Capability.GROUP_WRITE, True),
        "tap": (Capability.INPUT, True),
        "home": (Capability.NAVIGATE, True),
        "back": (Capability.NAVIGATE, True),
        "open_app": (Capability.NAVIGATE, True),
    }
    for name, (capability, mutation) in existing.items():
        _annotate(result, name, capability, mutation)

    def _lease(capability: Capability) -> Any:
        capability_authorizer(capability)
        current = bridge.guard.require(serial=serial)
        return bridge._runtime_lease(serial, current.fence, capability)

    def _empty(kwargs: dict[str, Any], label: str) -> None:
        kwargs.pop("ctx", None)
        if kwargs or cancelled():
            raise NativeBridgeError(f"Invalid or cancelled {label}")

    async def inventory_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "inventory")
        return (await bridge.inventory(_lease(Capability.INVENTORY))).public()

    async def group_read_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "group read")
        return (await bridge.group_read(_lease(Capability.GROUP_READ), "__fleet__")).public()

    async def proxy_read_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "proxy status")
        return (await bridge.proxy_read(_lease(Capability.PROXY_READ), serial)).public()

    async def proxy_test_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "proxy test")
        return (await bridge.proxy(_lease(Capability.PROXY_TEST), serial, "test")).public()

    async def reboot_preflight_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "reboot preflight")
        return (await bridge.reboot_preflight(_lease(Capability.REBOOT_PREFLIGHT), serial)).public()

    async def recovery_status_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "recovery status")
        return (await bridge.recovery_status(_lease(Capability.RECOVERY_STATUS), serial)).public()

    async def sync_status_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "sync status")
        return (await bridge.sync_status(_lease(Capability.SYNC_STATUS), serial)).public()

    async def stream_status_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "stream status")
        return (await bridge.stream_status(_lease(Capability.STREAM_STATUS), serial)).public()

    async def file_operation_tool(**kwargs: Any) -> Any:
        kwargs.pop("ctx", None)
        if cancelled() or "confirmed" in kwargs or "confirm_install" in kwargs:
            raise NativeBridgeError("File operation was cancelled or contained an untrusted confirmation")
        allowed = {"operation", "file_id", "remote_path", "replace"}
        if set(kwargs) - allowed or kwargs.get("operation") not in {"push", "pull", "install"}:
            raise NativeBridgeError("Typed file operation fields are invalid")
        operation = kwargs["operation"]
        capability = Capability.APK_INSTALL if operation == "install" else Capability.FILE_WRITE
        if not _grant(confirmation_grant, capability, operation):
            raise NativeConfirmationRequired("File transfer/install requires an owner-issued confirmation grant")
        lease = _lease(capability)
        return (await bridge.file(
            lease, serial, operation,
            file_id=kwargs.get("file_id"), remote_path=kwargs.get("remote_path"),
            replace=kwargs.get("replace", False), confirm_install=operation == "install",
        )).public()

    async def file_transfer_tool(**kwargs: Any) -> Any:
        operation = kwargs.pop("operation", None)
        if operation not in {"push", "pull"}:
            raise NativeBridgeError("File transfer operation must be push or pull")
        return await file_operation_tool(operation=operation, **kwargs)

    async def apk_install_tool(**kwargs: Any) -> Any:
        return await file_operation_tool(operation="install", **kwargs)

    async def reboot_tool(**kwargs: Any) -> Any:
        _empty(kwargs, "reboot")
        if not _grant(confirmation_grant, Capability.REBOOT, "reboot"):
            raise NativeConfirmationRequired("Reboot requires an owner-issued confirmation grant")
        return (await bridge.reboot_device(_lease(Capability.REBOOT), serial, confirmed=True)).public()

    def add(name: str, description: str, capability: Capability, mutation: bool,
            parameters: Mapping[str, Any], function: Callable[..., Any]) -> None:
        if not callable(getattr(bridge, name if name not in {"proxy_test", "file_transfer", "apk_install", "reboot"} else {
            "proxy_test": "proxy", "file_transfer": "file", "apk_install": "file", "reboot": "reboot_device",
        }[name], None)):
            return
        result[name] = {
            "description": description,
            "parameters": _closed(parameters),
            "function": function,
            "capability": capability.value,
            "mutation": mutation,
        }

    add("inventory", "Read connected device inventory metadata", Capability.INVENTORY, False,
        {"type": "object", "properties": {}}, inventory_tool)
    add("group_read", "Read typed fleet group metadata", Capability.GROUP_READ, False,
        {"type": "object", "properties": {}}, group_read_tool)
    add("proxy_read", "Read the saved redacted proxy status for this device", Capability.PROXY_READ, False,
        {"type": "object", "properties": {}}, proxy_read_tool)
    add("proxy_test", "Run the saved proxy test through the native proxy manager", Capability.PROXY_TEST, False,
        {"type": "object", "properties": {}}, proxy_test_tool)
    add("reboot_preflight", "Read reboot preflight state without rebooting", Capability.REBOOT_PREFLIGHT, False,
        {"type": "object", "properties": {}}, reboot_preflight_tool)
    add("recovery_status", "Read USB recovery state for this device", Capability.RECOVERY_STATUS, False,
        {"type": "object", "properties": {}}, recovery_status_tool)
    add("sync_status", "Read sync-link readiness for this device", Capability.SYNC_STATUS, False,
        {"type": "object", "properties": {}}, sync_status_tool)
    add("stream_status", "Read native H264 stream/control status", Capability.STREAM_STATUS, False,
        {"type": "object", "properties": {}}, stream_status_tool)
    add("file_transfer", "Push or pull a staged file. Call it when the owner task needs a file transfer; D3m0n pauses for owner approval automatically before anything changes", Capability.FILE_WRITE, True,
        {"type": "object", "required": ["operation"], "properties": {
            "operation": {"type": "string", "enum": ["push", "pull"]},
            "file_id": {"type": "string"}, "remote_path": {"type": "string"}, "replace": {"type": "boolean"},
        }}, file_transfer_tool)
    add("apk_install", "Install a staged APK. Call it when the owner task asks for an install; D3m0n pauses for owner approval automatically before installing", Capability.APK_INSTALL, True,
        {"type": "object", "required": ["file_id"], "properties": {
            "file_id": {"type": "string"}, "replace": {"type": "boolean"},
        }}, apk_install_tool)
    add("reboot", "Reboot this phone. Call it when the owner task asks for a reboot or restart; D3m0n pauses for owner approval automatically, then reboots", Capability.REBOOT, True,
        {"type": "object", "properties": {}}, reboot_tool)
    return result


__all__ = ["extend_native_tool_specs"]
