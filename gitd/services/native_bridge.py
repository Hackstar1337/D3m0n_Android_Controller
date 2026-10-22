"""D3m0n native bridge used by the Mobilerun-only runtime driver.

Every mutating method requires an :class:`AIOwnership` and validates it right
before entering the authoritative native operation.  This facade never accepts
an arbitrary tool name, shell command, URL, SQL fragment, provider response or
untyped native payload.  Unavailable capabilities fail closed.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ai_contracts.models import Capability
from ops import ai_control_guard as guard_mod
from ops import control_arbiter
from ops import fleet_control as fleet
from ops import fleet_reboot as reboot
from ops import fast_control as fast
from ops import sync_links
from ops import android_h264 as h264
from .native_bridge_tools import (
    CapabilityNotSupported,
    NativeBridgeError,
    NativeCall,
    NativeConfirmationRequired,
    NativeResult,
    NativeTargetDrift,
    invoke_typed,
)
from .native_observation import (
    NativeCheckpointRequired,
    NativeSDKDriver,
    OwnerBuiltNativeObservation,
    build_sdk_state_provider,
)


class NativeInputRejected(NativeBridgeError):
    """The existing strict validator rejected input before any device action."""


_CAPABILITY_BY_OPERATION = {
    "inventory": Capability.INVENTORY,
    "observe": Capability.OBSERVE,
    "navigate": Capability.NAVIGATE,
    "input": Capability.INPUT,
    "app_state": Capability.APP_STATE,
    "group_read": Capability.GROUP_READ,
    "group_write": Capability.GROUP_WRITE,
    "proxy_read": Capability.PROXY_READ,
    "proxy_test": Capability.PROXY_TEST,
    "proxy_apply": Capability.PROXY_APPLY,
    "proxy_prepare": Capability.PROXY_PREPARE,
    "proxy_disable": Capability.PROXY_DISABLE,
    "file_read": Capability.FILE_READ,
    "file_write": Capability.FILE_WRITE,
    "apk_install": Capability.APK_INSTALL,
    "reboot_preflight": Capability.REBOOT_PREFLIGHT,
    "reboot": Capability.REBOOT,
    "recovery_status": Capability.RECOVERY_STATUS,
    "sync_status": Capability.SYNC_STATUS,
    "stream_status": Capability.STREAM_STATUS,
}


@dataclass(frozen=True)
class BridgeContext:
    guard: guard_mod.AIControlGuard
    lease: guard_mod.AIOwnership


class _BridgeStateProvider:
    """SDK state provider backed only by an explicitly registered observer."""

    def __init__(self, bridge: "NativeBridge", serial: str, fence: int, cancelled: Callable[[], bool]) -> None:
        self.bridge, self.serial, self.fence, self.cancelled = bridge, serial, fence, cancelled

    async def get_state(self, *args: Any, **kwargs: Any) -> Any:
        if self.cancelled():
            raise NativeBridgeError("Native observation was cancelled")
        lease = self.bridge._runtime_lease(self.serial, self.fence, Capability.OBSERVE)
        adapter = self.bridge._typed_adapters.get(Capability.OBSERVE)
        if adapter is None:
            raise CapabilityNotSupported("Typed observer adapter is not registered")
        return await invoke_typed(adapter, self.serial, *args, **kwargs)


class NativeBridge:
    """Typed native operation facade; modules are injectable for fake tests."""

    def __init__(self, guard: guard_mod.AIControlGuard | None = None, *,
                 fleet_module: Any = fleet, files_module: Any | None = None,
                 reboot_module: Any = reboot, fast_module: Any = fast,
                 proxy_module: Any | None = None, h264_module: Any = h264,
                 sync_module: Any = sync_links, observation: Any | None = None,
                 device_factory: Callable[[str], Any] | None = None,
                 package_allowlist: frozenset[str] | None = None) -> None:
        self.guard = guard or guard_mod.AIControlGuard()
        self.fleet, self.files, self.reboot, self.fast = fleet_module, files_module, reboot_module, fast_module
        self.proxy_module, self.h264, self.sync = proxy_module, h264_module, sync_module
        self._typed_adapters: dict[Capability, Callable[..., Any]] = {}
        from .native_observation import ADBOnlyNativeObservation
        self.observation = observation or ADBOnlyNativeObservation(
            device_factory, package_allowlist=package_allowlist
        )
        self.package_allowlist = package_allowlist

    def _runtime_lease(self, serial: str, fence: int, capability: Capability | None = None) -> guard_mod.AIOwnership:
        """Resolve the private active ownership before any SDK-facing call."""
        try:
            return self.guard.require(serial=serial, fence=fence, capability=capability)
        except Exception as exc:
            raise NativeBridgeError("AI ownership or fence was rejected") from exc

    def driver_for(self, *, serial: str, fence: int, cancelled: Callable[[], bool]) -> Any:
        if cancelled():
            raise NativeBridgeError("Native driver creation was cancelled")
        self._runtime_lease(serial, fence)
        # GuardedDriver in mobilerun_runtime routes calls back through this bridge;
        # returning the bridge itself prevents an unguarded raw-driver fallback.
        return self

    def state_provider_for(self, *, serial: str, fence: int, cancelled: Callable[[], bool]) -> Any:
        if cancelled():
            raise NativeBridgeError("Native observer creation was cancelled")
        self._runtime_lease(serial, fence, Capability.OBSERVE)
        async def fetch():
            if cancelled():
                raise NativeBridgeError("Native observation was cancelled")
            lease = self._runtime_lease(serial, fence, Capability.OBSERVE)
            state = await self.observe_state(lease, serial)
            return state
        try:
            driver = NativeSDKDriver(
                lambda operation, **kwargs: self.authorized_driver_call(
                    serial=serial, capability=Capability.INPUT if operation != "get_ui_tree" else Capability.OBSERVE,
                    fence=fence, operation=operation, args=(), kwargs=kwargs
                ), serial
            )
            return build_sdk_state_provider(driver, fetch)
        except ImportError:
            # The dashboard/backend environment intentionally does not require SDK installation.
            return _BridgeStateProvider(self, serial, fence, cancelled)

    async def authorized_driver_call(self, *, serial: str, capability: Capability, fence: int,
                                     operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        lease = self._runtime_lease(serial, fence, capability)
        if operation in {"tap", "click", "click_at", "swipe", "scroll", "type_text", "input_text", "press_back"}:
            action = "tap" if operation in {"tap", "click", "click_at"} else "swipe" if operation in {"swipe", "scroll"} else "text"
            params = dict(kwargs)
            if args:
                raise NativeBridgeError("Positional native input arguments are not accepted")
            if operation == "press_back":
                action, params = "key", {"key": "BACK"}
            return await self.input(lease, serial, action, params)
        if operation == "observe":
            return await self.observe(lease, serial)
        if operation == "get_ui_tree":
            return (await self.observe_state(lease, serial)).public()
        if operation == "get_current_app" or operation == "current_app":
            return await self.current_app(lease, serial)
        if operation in {"press_key_code", "start_app"}:
            if operation == "press_key_code":
                key = {3: "HOME", 4: "BACK"}.get(kwargs.get("key_code"))
                if key is None:
                    raise CapabilityNotSupported("Only Home and Back are approved navigation keys")
                return await self.navigate(lease, serial, "home" if key == "HOME" else "back")
            return await self.navigate(lease, serial, "open_app", package=kwargs.get("package"), activity=kwargs.get("activity"))
        if operation == "stream_status":
            return await self.stream_status(lease, serial)
        if operation == "sync_status":
            return await self.sync_status(lease, serial)
        if operation == "group_read":
            return await self.group_read(lease)
        if operation == "proxy_read":
            return await self.proxy_read(lease, serial)
        adapter = self._typed_adapters.get(capability)
        if adapter is None:
            raise CapabilityNotSupported(f"Typed adapter is not registered for {capability.value}")
        return await invoke_typed(adapter, serial, *args, operation=operation, **kwargs)

    def register_mobilerun_tools(self, *, serial: str, capability_authorizer: Callable[[Capability], Any],
                                 cancelled: Callable[[], bool], allowed_serials: Any | None = None,
                                 confirmation_grant: Callable[[Capability, str], Any] | None = None) -> Mapping[str, Mapping[str, Any]]:
        """Expose only closed tools whose calls re-check the current fence."""
        self._runtime_lease(serial, self.guard.require(serial=serial).fence)
        target_scope = None if allowed_serials is None else frozenset(
            item for item in allowed_serials if isinstance(item, str) and item
        )

        async def observe(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            if kwargs or cancelled():
                raise NativeBridgeError("Invalid or cancelled observation")
            capability_authorizer(Capability.OBSERVE)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.OBSERVE)
            return (await self.observe(lease, serial)).public()

        async def input_tool(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            if cancelled():
                raise NativeBridgeError("Native input was cancelled")
            action = kwargs.pop("action", None)
            params = kwargs.pop("params", {})
            if kwargs or not isinstance(action, str) or not isinstance(params, Mapping):
                raise NativeBridgeError("Typed input requires action and params")
            capability_authorizer(Capability.INPUT)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.INPUT)
            return (await self.input(lease, serial, action, params)).public()

        async def navigate_tool(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            if cancelled() or "confirmed" in kwargs:
                raise NativeBridgeError("Navigation was cancelled or contained an untrusted confirmation")
            action, package, activity = kwargs.get("action"), kwargs.get("package"), kwargs.get("activity")
            if set(kwargs) - {"action", "package", "activity"} or not isinstance(action, str):
                raise NativeBridgeError("Typed navigation requires action, package, and activity fields")
            capability_authorizer(Capability.NAVIGATE)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.NAVIGATE)
            return (await self.navigate(lease, serial, action, package=package, activity=activity)).public()

        async def current_app_tool(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            if kwargs or cancelled():
                raise NativeBridgeError("Invalid or cancelled app-state observation")
            capability_authorizer(Capability.APP_STATE)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.APP_STATE)
            return (await self.current_app(lease, serial)).public()

        async def app_state_tool(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            package = kwargs.get("package")
            if set(kwargs) != {"package"} or not isinstance(package, str) or cancelled():
                raise NativeBridgeError("Typed app-state observation requires package")
            capability_authorizer(Capability.APP_STATE)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.APP_STATE)
            return (await self.app_state(lease, serial, package)).public()

        async def group_write_tool(**kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            if cancelled() or "confirmed" in kwargs:
                raise NativeBridgeError("Group write was cancelled or contained an untrusted confirmation")
            operation = kwargs.get("operation")
            if operation not in {"create", "update", "delete"}:
                raise NativeBridgeError("Unsupported group write operation")
            devices = kwargs.get("devices")
            if devices is not None and (not isinstance(devices, list) or not all(isinstance(item, str) for item in devices)):
                raise NativeBridgeError("Group devices must be a typed serial list")
            if target_scope is None or (devices is not None and not set(devices) <= target_scope):
                raise NativeCheckpointRequired("Group write requires an owner-issued target scope")
            if confirmation_grant is None or confirmation_grant(Capability.GROUP_WRITE, operation) is not True:
                raise NativeConfirmationRequired("Group write requires an owner-issued confirmation grant")
            capability_authorizer(Capability.GROUP_WRITE)
            lease = self._runtime_lease(serial, self.guard.require(serial=serial).fence, Capability.GROUP_WRITE)
            fields = {key: value for key, value in kwargs.items() if key != "operation"}
            return (await self.group_write(lease, operation, **fields)).public()

        specs = {
            "observe": {"description": "Read safe device status metadata", "parameters": {"type": "object", "properties": {}}, "function": observe},
            "input": {"description": "Perform one validated input action within the leased device", "parameters": {"type": "object", "required": ["action", "params"], "properties": {"action": {"type": "string", "enum": ["tap", "swipe", "key", "text"]}, "params": {"type": "object"}}}, "function": input_tool},
            "navigate": {"description": "Navigate only Home, Back, or an approved allowlisted app", "parameters": {"type": "object", "required": ["action"], "properties": {"action": {"type": "string", "enum": ["home", "back", "open_app"]}, "package": {"type": "string"}, "activity": {"type": "string"}}}, "function": navigate_tool},
            "current_app": {"description": "Read the current foreground app metadata", "parameters": {"type": "object", "properties": {}}, "function": current_app_tool},
            "app_state": {"description": "Read the state of one validated package", "parameters": {"type": "object", "required": ["package"], "properties": {"package": {"type": "string"}}}, "function": app_state_tool},
            "group_write": {"description": "Apply an owner-approved typed fleet group write", "parameters": {"type": "object", "required": ["operation"], "properties": {"operation": {"type": "string", "enum": ["create", "update", "delete"]}, "group_id": {"type": "string"}, "name": {"type": "string"}, "devices": {"type": "array", "items": {"type": "string"}}}}, "function": group_write_tool},
        }
        async def tap_tool(x, y, ctx=None):
            return await input_tool(action="tap", params={"x":x,"y":y})
        async def home_tool(ctx=None):
            return await navigate_tool(action="home")
        async def back_tool(ctx=None):
            return await navigate_tool(action="back")
        async def open_app_tool(package, ctx=None):
            return await navigate_tool(action="open_app",package=package)
        specs.update({
            "tap": {"description":"Tap one approved native coordinate","parameters":{"type":"object","required":["x","y"],"properties":{"x":{"type":"integer"},"y":{"type":"integer"}}},"function":tap_tool},
            "home": {"description":"Go to Android Home","parameters":{"type":"object","properties":{}},"function":home_tool},
            "back": {"description":"Go Back once","parameters":{"type":"object","properties":{}},"function":back_tool},
            "open_app": {"description":"Open an allowlisted app by package name","parameters":{"type":"object","required":["package"],"properties":{"package":{"type":"string"}}},"function":open_app_tool},
        })
        from .native_tool_registry import extend_native_tool_specs
        return extend_native_tool_specs(
            bridge=self,
            specs=specs,
            serial=serial,
            capability_authorizer=capability_authorizer,
            cancelled=cancelled,
            confirmation_grant=confirmation_grant,
        )

    def _files(self) -> Any:
        if getattr(self, "files", None) is None:
            from ops import fleet_files
            self.files = fleet_files
        return self.files

    def _proxy(self) -> Any:
        if getattr(self, "proxy_module", None) is None:
            from ops import proxy_manager
            self.proxy_module = proxy_manager
        return self.proxy_module

    def register_typed_adapter(self, capability: Capability, adapter: Callable[..., Any]) -> None:
        """Register only a pre-selected typed adapter (used for app/navigation seams)."""
        if not isinstance(capability, Capability) or not callable(adapter):
            raise TypeError("A capability and callable adapter are required")
        self._typed_adapters[capability] = adapter

    def _ctx(self, lease: guard_mod.AIOwnership, capability: Capability, serial: str) -> BridgeContext:
        if not isinstance(lease, guard_mod.AIOwnership):
            raise NativeBridgeError("AI ownership is required")
        if lease.serial != serial:
            raise NativeTargetDrift("AI target does not match the lease")
        current = self.guard.require(lease, serial=serial, capability=capability,
                                     child_id=lease.child_id, fence=lease.fence)
        return BridgeContext(self.guard, current)

    def _read_ctx(self, lease: guard_mod.AIOwnership, capability: Capability, serial: str) -> BridgeContext:
        return self._ctx(lease, capability, serial)

    async def inventory(self, lease: guard_mod.AIOwnership) -> NativeResult:
        self.guard.require(lease, capability=Capability.INVENTORY)
        value = await invoke_typed(self.fleet.connected_devices)
        serials = [str(item) for item in (value or []) if isinstance(item, str)]
        return NativeResult(True, "inventory", summary="Inventory read", metadata={"count": len(serials), "online": len(serials)})

    async def observe(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.OBSERVE, serial)
        session = self.h264.SESSIONS.get(serial)
        status = session.status() if session is not None and hasattr(session, "status") else {"online": False}
        return NativeResult(True, "observe", serial, metadata={"online": bool(status.get("running", status.get("control", False))), "transport": status.get("transport", "unknown")})

    async def observe_state(self, lease: guard_mod.AIOwnership, serial: str) -> Any:
        """Return the redaction/checkpointed canonical state, never raw UI data."""
        for attempt in range(3):
            self._read_ctx(lease, Capability.OBSERVE, serial)  # Stop/takeover re-checked before every read.
            try:
                return await self.observation.state(serial)
            except NativeCheckpointRequired:
                raise
            except Exception as exc:
                if attempt == 2:
                    raise NativeBridgeError("Native UI observation failed") from exc
                await asyncio.sleep(0.7)  # Transient: app transition / momentary null focus.

    async def current_app(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.APP_STATE, serial)
        try:
            return await self._current_app_observation(serial)
        except Exception as exc:
            if isinstance(exc, NativeBridgeError):
                raise
            raise NativeBridgeError("Current app observation failed") from exc

    async def _current_app_observation(self, serial: str) -> NativeResult:
        """Read current app for a navigation verifier after nav scope was fenced."""
        device_context = __import__("gitd.services.device_context", fromlist=["get_phone_state"])
        value = await asyncio.to_thread(device_context.get_phone_state, serial)
        if not isinstance(value, Mapping):
            raise NativeBridgeError("Current app observation is invalid")
        package = str(value.get("packageName", value.get("package", value.get("current_package", ""))) or "")
        activity = str(value.get("activityName", value.get("activity", "")) or "")
        if not package:
            return NativeResult(False, "current_app", serial, status="uncertain", summary="Current app could not be independently verified", metadata={"checkpoint": True})
        checkpoint = self.package_allowlist is not None and bool(package) and package not in self.package_allowlist
        return NativeResult(True, "current_app", serial, status="waiting_for_owner" if checkpoint else "completed", metadata={"package": package[:250], "activity": activity[:250], "checkpoint": checkpoint})

    async def app_state(self, lease: guard_mod.AIOwnership, serial: str, package: str) -> NativeResult:
        self._read_ctx(lease, Capability.APP_STATE, serial)
        if not isinstance(package, str) or not package or len(package) > 250 or not re.fullmatch(r"[A-Za-z0-9_.]+", package):
            raise NativeBridgeError("Package name is invalid")
        device_context = __import__("gitd.services.device_context", fromlist=["app_state"])
        value = await asyncio.to_thread(device_context.app_state, serial, package)
        return NativeResult(True, "app_state", serial, metadata={"package": package, "state": str(value.get("state", "unknown")), "foreground": bool(value.get("foreground", False)), "running": bool(value.get("running", False))})

    async def navigate(self, lease: guard_mod.AIOwnership, serial: str, action: str, *, package: str | None = None, activity: str | None = None) -> NativeResult:
        ctx = self._ctx(lease, Capability.NAVIGATE, serial)
        if action not in {"home", "back", "open_app"}:
            raise CapabilityNotSupported("Safe navigation is limited to Home, Back, and approved app launch")
        if action == "open_app":
            if not isinstance(package, str) or self.package_allowlist is not None and package not in self.package_allowlist:
                raise NativeCheckpointRequired("App launch requires an allowlisted package")
            if activity is not None and (not isinstance(activity, str) or len(activity) > 250 or not re.fullmatch(r"[A-Za-z0-9_.$]+", activity)):
                raise NativeBridgeError("Activity is invalid")
        try:
            device = self.observation._device(serial)
            async with control_arbiter.acquired(serial, ctx.lease.owner_token):
                self.guard.require(lease, serial=serial, capability=Capability.NAVIGATE)
                if action == "home":
                    await asyncio.to_thread(device.adb, "shell", "input", "keyevent", "KEYCODE_HOME")
                elif action == "back":
                    await asyncio.to_thread(device.back)
                elif activity:
                    await asyncio.to_thread(device.adb, "shell", "am", "start", "-n", f"{package}/{activity}")
                else:
                    await asyncio.to_thread(device.adb, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        except NativeCheckpointRequired:
            raise
        except Exception as exc:
            raise NativeBridgeError("Native navigation failed") from exc
        self.guard.require(lease, serial=serial, capability=Capability.NAVIGATE)
        try:
            verified_ok = bool((await self._current_app_observation(serial)).ok)
        except Exception:
            verified_ok = False  # The action ran; the next observation re-establishes state.
        return NativeResult(True, "navigate", serial, metadata={"action": action, "verified": verified_ok, "package": package or ""})

    async def group_write(self, lease: guard_mod.AIOwnership, operation: str, **fields: Any) -> NativeResult:
        self.guard.require(lease, capability=Capability.GROUP_WRITE)
        if operation not in {"create", "update", "delete"}:
            raise CapabilityNotSupported("Unsupported group write operation")
        try:
            if operation == "create":
                name, devices = fields.get("name"), fields.get("devices")
                body = self.fleet.GroupBody(name=name, devices=devices)
                result = await invoke_typed(self.fleet.create_group, body)
            elif operation == "update":
                body = self.fleet.GroupPatchBody(name=fields.get("name"), devices=fields.get("devices"))
                result = await invoke_typed(self.fleet.update_group, fields.get("group_id"), body)
            else:
                result = await invoke_typed(self.fleet.delete_group, fields.get("group_id"))
        except Exception as exc:
            raise NativeBridgeError("Native group write failed") from exc
        return NativeResult(True, "group_write", "__fleet__", metadata={"operation": operation, "ok": bool(isinstance(result, dict) and result.get("ok"))})

    async def stream_status(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.STREAM_STATUS, serial)
        session = self.h264.SESSIONS.get(serial)
        status = session.status() if session is not None and hasattr(session, "status") else {"running": False}
        return NativeResult(True, "stream_status", serial, metadata={"running": bool(status.get("running", False)), "control": bool(status.get("control", False))})

    async def sync_status(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.SYNC_STATUS, serial)
        row = self.sync.links_summary().get("links", {}).get(serial, {})
        return NativeResult(True, "sync_status", serial, metadata={"ready": bool(row.get("ready", False)), "state": str(row.get("state", "unleased"))})

    async def input(self, lease: guard_mod.AIOwnership, serial: str, action: str, params: Mapping[str, Any]) -> NativeResult:
        ctx = self._ctx(lease, Capability.INPUT, serial)
        if not isinstance(action, str) or not isinstance(params, Mapping):
            raise NativeBridgeError("Typed input fields are invalid")
        action = {"click": "tap", "click_at": "tap", "press": "tap", "type": "text", "type_text": "text",
                  "input_text": "text", "press_key": "key", "scroll": "swipe"}.get(action, action)
        params = dict(params)
        try:
            clean = self.fleet._validate_action(action, params)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 422:
                raise
            adapted = await self._pixel_to_normalized(serial, action, params)
            if adapted is None:
                raise NativeInputRejected(str(getattr(exc, "detail", "Input rejected"))[:160]) from None
            try:
                clean = self.fleet._validate_action(action, adapted)
            except Exception as again:
                if getattr(again, "status_code", None) != 422:
                    raise
                raise NativeInputRejected(str(getattr(again, "detail", "Input rejected"))[:160]) from None
        if action == "tap":
            gesture = {"kind": "tap", "x": clean["x"], "y": clean["y"]}
        elif action == "swipe":
            gesture = {"kind": "swipe", **clean}
        elif action == "key":
            code = self.fast.keycode_for(clean["key"])
            if code is None:
                raise NativeBridgeError("Unsupported key")
            gesture = {"kind": "key", "code": code}
        else:
            gesture = {"kind": "text", "text": clean["text"]}
        result = await invoke_typed(self.fast.run_on_device, serial, gesture, owner_token=ctx.lease.owner_token)
        if not isinstance(result, dict) or not result.get("ok"):
            raise NativeBridgeError("Native input failed")
        return NativeResult(True, "input", serial, metadata={"via": result.get("via", "native")})

    async def _pixel_to_normalized(self, serial: str, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Convert pixel coordinates using the phone's real screen bounds; never guesses a size."""
        keys = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2")}.get(action)
        if keys is None or not all(k in params for k in keys):
            return None
        values = [params[k] for k in keys]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in values) or all(v <= 1 for v in values):
            return None
        reader = getattr(self.observation, "raw_state", None)
        if reader is None:
            return None
        try:
            bounds = (await reader(serial))["device_context"]["screen_bounds"]
            width, height = int(bounds["width"]), int(bounds["height"])
        except Exception:
            return None
        if not (1 < width <= 100000 and 1 < height <= 100000):
            return None
        adapted = dict(params)
        for key in keys:
            extent = width if key.startswith("x") else height
            value = float(params[key])
            if value > extent:
                return None
            adapted[key] = min(1.0, value / (extent - 1))
        return adapted

    async def group_read(self, lease: guard_mod.AIOwnership, serial: str = "__fleet__") -> NativeResult:
        self.guard.require(lease, capability=Capability.GROUP_READ)
        value = await invoke_typed(self.fleet.list_groups)
        groups = value.get("groups", []) if isinstance(value, dict) else []
        return NativeResult(True, "group_read", serial, metadata={"count": len(groups)})

    async def proxy_read(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.PROXY_READ, serial)
        proxy_module = self._proxy()
        profile = proxy_module.store.get_public(serial)
        return NativeResult(True, "proxy_read", serial, metadata={"configured": profile is not None, "applied_mode": (profile or {}).get("applied_mode", "normal")})

    async def proxy(self, lease: guard_mod.AIOwnership, serial: str, operation: str) -> NativeResult:
        cap = {"test": Capability.PROXY_TEST, "apply": Capability.PROXY_APPLY,
               "prepare": Capability.PROXY_PREPARE, "disable": Capability.PROXY_DISABLE}.get(operation)
        if cap is None:
            raise CapabilityNotSupported("Unsupported proxy operation")
        ctx = self._ctx(lease, cap, serial)
        proxy_module = self._proxy()
        driver = await invoke_typed(proxy_module.make_driver)
        # _run_one retains ProxyManager's profile lookup, per-device lock,
        # preflight, result sanitization, runtime state updates and bootstrap
        # revocation.  It is the authoritative native operation boundary.
        result = await invoke_typed(proxy_module._run_one, {"action": operation}, serial, driver)
        if not isinstance(result, dict) or not result.get("ok"):
            raise NativeBridgeError("Proxy operation failed")
        return NativeResult(True, "proxy_" + operation, serial, metadata={"status": result.get("status", "completed"), "verified_device": bool(result.get("verified_device", False))})

    async def file(self, lease: guard_mod.AIOwnership, serial: str, operation: str, *, file_id: str | None = None,
                   remote_path: str | None = None, replace: bool = False, confirm_install: bool = False) -> NativeResult:
        cap = Capability.APK_INSTALL if operation == "install" else Capability.FILE_WRITE
        if operation not in {"push", "pull", "install"}:
            raise CapabilityNotSupported("Unsupported file operation")
        ctx = self._ctx(lease, cap, serial)
        files_module = self._files()
        files_module._ensure_runtime()
        file_meta = files_module._load_file(file_id) if file_id else None
        if operation in {"push", "install"} and file_meta is None:
            raise NativeBridgeError("Staged file not found")
        if operation == "install" and (not file_meta.get("is_apk") or not confirm_install):
            raise NativeConfirmationRequired("APK installation requires explicit confirmation")
        if operation in {"push", "pull"}:
            if not isinstance(remote_path, str):
                raise NativeBridgeError("A validated shared-storage path is required")
            path = files_module._validate_remote_path(remote_path, destination=operation == "push")
        else:
            path = None
        async with self.fleet._device_locks.setdefault(serial, asyncio.Lock()):
            if operation == "push":
                await invoke_typed(files_module._push_one, serial, file_meta, path, replace)
            elif operation == "pull":
                await invoke_typed(files_module._pull_one, serial, {"device": serial}, path)
            else:
                await invoke_typed(files_module._install_one, serial, file_meta, replace)
        return NativeResult(True, "file_" + operation, serial, metadata={"file_id": file_id or "", "path_validated": bool(path) if path else False})

    async def reboot_preflight(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.REBOOT_PREFLIGHT, serial)
        checks = await invoke_typed(self.reboot._preflight, [serial])
        return NativeResult(True, "reboot_preflight", serial, metadata={"online": bool(checks.get(serial, {}).get("online", False)), "agent_busy": bool(checks.get(serial, {}).get("agent_busy", False)), "rebooting": bool(checks.get(serial, {}).get("rebooting", False))})

    async def reboot_device(self, lease: guard_mod.AIOwnership, serial: str, *, confirmed: bool = False) -> NativeResult:
        ctx = self._ctx(lease, Capability.REBOOT, serial)
        if confirmed is not True:
            raise NativeConfirmationRequired("Reboot requires owner confirmation")
        checks = await invoke_typed(self.reboot._preflight, [serial])
        check = checks.get(serial, {})
        if not check.get("online") or check.get("agent_busy") or check.get("rebooting"):
            raise NativeBridgeError("Reboot preflight did not pass")
        async with self.fleet._device_locks.setdefault(serial, asyncio.Lock()):
            await invoke_typed(self.reboot.send_reboot, serial)
        return NativeResult(True, "reboot", serial, metadata={"preflight": "passed"})

    async def recovery_status(self, lease: guard_mod.AIOwnership, serial: str) -> NativeResult:
        self._read_ctx(lease, Capability.RECOVERY_STATUS, serial)
        provider = getattr(self.reboot.usb_recovery, "usb_states_by_peer", None)
        if provider is None:
            raise CapabilityNotSupported("USB recovery status is unavailable")
        devices = await invoke_typed(self.reboot.inventory)
        states = await invoke_typed(provider, devices)
        return NativeResult(True, "recovery_status", serial, metadata={"usb_state": str(states.get(serial, "none"))})

    async def typed(self, lease: guard_mod.AIOwnership, serial: str, capability: Capability, *args: Any, **kwargs: Any) -> Any:
        """Call one explicitly registered non-core adapter, never arbitrary tools."""
        self._ctx(lease, capability, serial)
        adapter = self._typed_adapters.get(capability)
        if adapter is None:
            raise CapabilityNotSupported(f"{capability.value} is not implemented by current native modules")
        return await invoke_typed(adapter, serial, *args, **kwargs)


__all__ = ["NativeBridge", "BridgeContext", "NativeResult"]
