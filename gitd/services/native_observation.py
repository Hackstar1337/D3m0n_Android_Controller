"""Closed native observation and Mobilerun SDK mapping primitives.

Only owner-built device operations are used.  This module deliberately never
accepts a model command, shell string, URL, screenshot, or provider payload.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


class NativeObservationError(RuntimeError):
    pass


class NativeCheckpointRequired(NativeObservationError):
    pass


_SENSITIVE = re.compile(
    r"(?i)(password|passcode|one[- ]?time|otp|verification|security code|captcha|"
    r"sign in|log in|login|account|credential|token|api key|terms|conditions|"
    r"accept|agree|permission|allow access|unlock)"
)
_SECRET_VALUE = re.compile(r"(?i)\b(?:\d[ -]?){4,8}\b|sk-[A-Za-z0-9_-]{8,}")


@dataclass(frozen=True)
class CanonicalNode:
    node_id: str
    text: str
    content_description: str
    class_name: str
    resource_id: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    visible: bool
    children: tuple["CanonicalNode", ...] = ()
    sensitive: bool = False


@dataclass(frozen=True)
class CanonicalUIState:
    serial: str
    package: str | None
    activity: str | None
    screen_width: int
    screen_height: int
    nodes: tuple[CanonicalNode, ...]
    sensitive: bool = False
    checkpoint_reason: str | None = None

    def public(self) -> dict[str, Any]:
        def view(node: CanonicalNode) -> dict[str, Any]:
            return {
                "node_id": node.node_id,
                "text": node.text,
                "content_description": node.content_description,
                "class_name": node.class_name,
                "resource_id": node.resource_id,
                "bounds": node.bounds,
                "clickable": node.clickable,
                "enabled": node.enabled,
                "visible": node.visible,
                "sensitive": node.sensitive,
                "children": [view(c) for c in node.children],
            }
        return {
            "serial": self.serial,
            "package": self.package,
            "activity": self.activity,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "nodes": [view(n) for n in self.nodes],
            "sensitive": self.sensitive,
            "checkpoint_reason": self.checkpoint_reason,
        }


def verify_navigation_history(states: tuple[CanonicalUIState, ...] | list[CanonicalUIState], expected_packages: tuple[str, ...] | list[str]) -> bool:
    """Verify observed package history; never uses a model-reported success flag."""
    observed = tuple(state.package for state in states)
    expected = tuple(expected_packages)
    return bool(expected) and len(observed) >= len(expected) and observed[-len(expected):] == expected and all(state.checkpoint_reason is None for state in states[-len(expected):])


def _bounds(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, Mapping):
        vals = [value.get(k) for k in ("left", "top", "right", "bottom")]
    elif isinstance(value, str):
        try:
            vals = [int(x.strip()) for x in value.replace("[", "").replace("]", ",").split(",") if x.strip()]
        except ValueError:
            vals = []
    elif isinstance(value, (list, tuple)):
        vals = list(value)
    else:
        vals = []
    if len(vals) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals):
        raise NativeObservationError("UI node bounds are invalid")
    left, top, right, bottom = (int(v) for v in vals)
    if left < 0 or top < 0 or right < left or bottom < top:
        raise NativeObservationError("UI node bounds are out of range")
    return left, top, right, bottom


def _safe_text(value: Any, *, sensitive: bool) -> tuple[str, bool]:
    text = str(value or "")
    flagged = sensitive or bool(_SENSITIVE.search(text)) or bool(_SECRET_VALUE.search(text))
    return ("[REDACTED]" if flagged and text else text[:500]), flagged


def _node(raw: Mapping[str, Any], path: str, checkpoints: list[str]) -> CanonicalNode:
    if not isinstance(raw, Mapping):
        raise NativeObservationError("UI tree node is invalid")
    text, sensitive_text = _safe_text(raw.get("text"), sensitive=False)
    desc, sensitive_desc = _safe_text(raw.get("contentDescription", raw.get("content_description")), sensitive=False)
    class_name = str(raw.get("className", raw.get("class_name", "")) or "")[:200]
    resource_id = str(raw.get("resourceId", raw.get("resource_id", "")) or "")[:200]
    # Menu labels such as Accounts, Permissions or Terms are redacted but are
    # not themselves challenge screens. Actual credential inputs/dialogs pause.
    source_text = str(raw.get("text", "")) + " " + str(raw.get("contentDescription", ""))
    interactive = "edittext" in class_name.lower() or raw.get("isPassword") is True
    challenge = bool(re.search(r"(?i)(captcha|enter.*(?:password|verification|code|otp)|verify your|sign in|log in|accept.*terms|agree.*conditions|allow.*access|one.time.*code)", source_text))
    sensitive = raw.get("isPassword") is True or challenge or (interactive and bool(_SENSITIVE.search(source_text + " " + resource_id)))
    if sensitive:
        checkpoints.append("sensitive UI text or credential surface")
    children_raw = raw.get("children") or []
    if not isinstance(children_raw, (list, tuple)):
        raise NativeObservationError("UI node children are invalid")
    children = tuple(_node(child, f"{path}.{i}", checkpoints) for i, child in enumerate(children_raw))
    stable = str(raw.get("nodeId", raw.get("node_id", "")) or "")
    stable = stable[:160] or (resource_id + "@" + path if resource_id else path)
    return CanonicalNode(
        node_id=stable,
        text=text,
        content_description=desc,
        class_name=class_name,
        resource_id=resource_id,
        bounds=_bounds(raw.get("boundsInScreen", raw.get("bounds"))),
        clickable=bool(raw.get("isClickable", raw.get("clickable", False))),
        enabled=bool(raw.get("isEnabled", raw.get("enabled", True))),
        visible=bool(raw.get("isVisibleToUser", raw.get("visible", True))),
        children=children,
        sensitive=sensitive,
    )


def canonicalize_ui_state(raw: Mapping[str, Any], serial: str, *, package_allowlist: frozenset[str] | set[str] | None = None) -> CanonicalUIState:
    """Map the existing Portal/Android state shape to a bounded canonical DTO."""
    if not isinstance(raw, Mapping) or not isinstance(serial, str) or not serial:
        raise NativeObservationError("Native UI state is invalid")
    tree = raw.get("a11y_tree")
    if not isinstance(tree, (Mapping, list, tuple)):
        raise NativeObservationError("Native UI tree is unavailable")
    context = raw.get("device_context") or {}
    bounds = context.get("screen_bounds") if isinstance(context, Mapping) else None
    if not isinstance(bounds, Mapping):
        raise NativeObservationError("Native screen dimensions are unavailable")
    width, height = bounds.get("width"), bounds.get("height")
    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int) or not (1 <= width <= 100000 and 1 <= height <= 100000):
        raise NativeObservationError("Native screen dimensions are invalid")
    phone = raw.get("phone_state") or {}
    if not isinstance(phone, Mapping):
        phone = {}
    package = phone.get("packageName", phone.get("package", phone.get("current_package")))
    activity = phone.get("activityName", phone.get("activity"))
    package = str(package)[:250] if package else None
    activity = str(activity)[:250] if activity else None
    checkpoints: list[str] = []
    if package_allowlist is not None and package and package not in package_allowlist:
        checkpoints.append("package is outside the approved allowlist")
    raw_nodes = list(tree) if isinstance(tree, (list, tuple)) else [tree]
    nodes = tuple(_node(item, str(i), checkpoints) for i, item in enumerate(raw_nodes))
    reason = "; ".join(dict.fromkeys(checkpoints)) or None
    return CanonicalUIState(serial, package, activity, width, height, nodes, bool(reason), reason)


def sdk_elements(state):
    flattened=[]
    def visit(node):
        if node.visible:
            l,t,r,b=node.bounds
            flattened.append({"index":len(flattened),"nodeId":node.node_id,"text":node.text,
                "contentDescription":node.content_description,"className":node.class_name,
                "resourceId":node.resource_id,"bounds":f"{l},{t},{r},{b}",
                "isClickable":node.clickable,"isEnabled":node.enabled,"isVisibleToUser":node.visible})
        for child in node.children: visit(child)
    for root in state.nodes: visit(root)
    if len(flattened)>400: raise NativeObservationError("Flattened tree exceeds bounds")
    return flattened


def to_sdk_ui_state(state: CanonicalUIState) -> Any:
    """Construct the pinned SDK UIState, with no dependency at dashboard import time."""
    from mobilerun.tools.ui.state import UIState
    elements=sdk_elements(state)
    formatted="\n".join(f"[{n['index']}] {n['text'] or n['contentDescription']} ({n['bounds']})" for n in elements if n["text"] or n["contentDescription"])[:8000]
    return UIState(
        elements=elements,
        formatted_text=formatted,
        focused_text="",
        phone_state={"packageName": state.package or "", "activityName": state.activity or ""},
        screen_width=state.screen_width,
        screen_height=state.screen_height,
        use_normalized=False,
        coordinate_contract_active=False,
    )


class NativeSDKDriver:
    """Minimal pinned-SDK driver: every method delegates to a typed bridge callback."""
    def __init__(self, call: Callable[..., Awaitable[Any]], serial: str):
        self._call, self.serial = call, serial

    async def get_ui_tree(self) -> Mapping[str, Any]:
        return await self._call("get_ui_tree")
    async def tap(self, x: int, y: int) -> Any:
        return await self._call("tap", x=x, y=y)
    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: float = 1000) -> Any:
        return await self._call("swipe", x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)
    async def input_text(self, text: str, clear: bool = False) -> Any:
        return await self._call("input_text", text=text, clear=clear)
    async def press_key_code(self, key_code: int) -> Any:
        return await self._call("press_key_code", key_code=key_code)
    async def start_app(self, package: str, activity: str | None = None) -> Any:
        return await self._call("start_app", package=package, activity=activity)
    async def get_current_app(self) -> Any:
        return await self._call("get_current_app")


def build_sdk_state_provider(driver: NativeSDKDriver, fetch: Callable[[], Awaitable[CanonicalUIState]]) -> Any:
    """Use the actual pinned SDK StateProvider base and UIState constructor."""
    from mobilerun.tools.ui.provider import StateProvider
    class NativeStateProvider(StateProvider):
        supported = {"element_index", "convert_point"}
        screenshot_matches_input_coords = False
        resize_model_screenshot = False
        def __init__(self) -> None:
            super().__init__(driver)
        async def get_state(self) -> Any:
            state = await fetch()
            if state.sensitive:
                raise NativeCheckpointRequired(state.checkpoint_reason or "Owner checkpoint required")
            return to_sdk_ui_state(state)
        async def get_final_state(self) -> Any:
            return await self.get_state()
    return NativeStateProvider()


class OwnerBuiltNativeObservation:
    """Adapter over the existing Device abstraction; no model-controlled commands."""
    def __init__(self, device_factory: Callable[[str], Any] | None = None, *, package_allowlist: frozenset[str] | None = None):
        self.package_allowlist = package_allowlist
        self._factory = device_factory
    def _device(self, serial: str) -> Any:
        if self._factory is None:
            from gitd.bots.common.adb import Device
            return Device(serial)
        return self._factory(serial)
    async def raw_state(self, serial: str) -> Mapping[str, Any]:
        device = self._device(serial)
        def read() -> Mapping[str, Any]:
            tree = device.dump_portal_json()
            if tree is None:
                raise NativeObservationError("Native accessibility tree unavailable")
            from gitd.services.device_context import get_phone_state
            from gitd.services import device_context
            phone = get_phone_state(serial)
            size = device.adb("shell", "wm", "size", timeout=5)
            match = re.findall(r"(?:size:\s*)?(\d+)x(\d+)", size, re.I)
            if not match:
                raise NativeObservationError("Native screen dimensions unavailable")
            width, height = (int(x) for x in match[-1])
            return {"a11y_tree": tree, "phone_state": phone, "device_context": {"screen_bounds": {"width": width, "height": height}}}
        return await asyncio.to_thread(read)
    async def state(self, serial: str) -> CanonicalUIState:
        return canonicalize_ui_state(await self.raw_state(serial), serial, package_allowlist=self.package_allowlist)

__all__ = ["CanonicalNode", "CanonicalUIState", "NativeObservationError", "NativeCheckpointRequired", "canonicalize_ui_state", "sdk_elements", "to_sdk_ui_state", "NativeSDKDriver", "build_sdk_state_provider", "OwnerBuiltNativeObservation"]


def canonical_from_public(value: Mapping[str, Any]) -> CanonicalUIState:
    """Deserialize only parent-produced redacted state, with explicit limits."""
    count = [0]
    def node(raw, depth=0):
        count[0] += 1
        if count[0] > 400 or depth > 24 or not isinstance(raw, Mapping):
            raise NativeObservationError("Canonical tree exceeds bounds")
        b = raw.get("bounds")
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            raise NativeObservationError("Canonical node bounds missing")
        return CanonicalNode(str(raw["node_id"])[:160], str(raw.get("text", ""))[:500],
            str(raw.get("content_description", ""))[:500], str(raw.get("class_name", ""))[:200],
            str(raw.get("resource_id", ""))[:200], tuple(int(x) for x in b),
            raw.get("clickable") is True, raw.get("enabled") is True, raw.get("visible") is True,
            tuple(node(c, depth+1) for c in raw.get("children", [])), raw.get("sensitive") is True)
    state = CanonicalUIState(str(value["serial"]), value.get("package"), value.get("activity"),
        int(value["screen_width"]), int(value["screen_height"]), tuple(node(n) for n in value["nodes"]),
        value.get("sensitive") is True, value.get("checkpoint_reason"))
    if state.sensitive or state.checkpoint_reason:
        raise NativeCheckpointRequired("Owner input required")
    return state


class ADBOnlyNativeObservation(OwnerBuiltNativeObservation):
    """No Portal install/setup/forward; fixed native XML read with private cleanup."""
    async def raw_state(self, serial: str) -> Mapping[str, Any]:
        import xml.etree.ElementTree as ET
        import uuid
        device = self._device(serial)
        def read():
            path = "/sdcard/Download/.d3m0n-ai-ui-" + uuid.uuid4().hex + ".xml"
            try:
                device.adb("shell", "uiautomator", "dump", path, timeout=12)
                xml = device.adb("exec-out", "cat", path, timeout=6)
            finally:
                try: device.adb("shell", "rm", "-f", path, timeout=3)
                except Exception: pass
            if len(xml.encode("utf-8")) > 512 * 1024 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
                raise NativeObservationError("Native XML exceeds policy")
            root = ET.fromstring(xml)
            count = [0]
            def convert(element, depth=0):
                count[0] += 1
                if count[0] > 400 or depth > 24:
                    raise NativeObservationError("Native tree exceeds bounds")
                a = element.attrib
                return {"text": a.get("text", ""), "contentDescription": a.get("content-desc", ""),
                    "className": a.get("class", ""), "resourceId": a.get("resource-id", ""),
                    "bounds": a.get("bounds", "[0,0][0,0]"), "isClickable": a.get("clickable") == "true",
                    "isEnabled": a.get("enabled", "true") == "true", "isVisibleToUser": True,
                    "isPassword": a.get("password") == "true", "children": [convert(c, depth+1) for c in element if c.tag == "node"]}
            size = device.adb("shell", "wm", "size", timeout=5)
            sizes = re.findall(r"(\d+)x(\d+)", size)
            if not sizes: raise NativeObservationError("Display size unavailable")
            width, height = (int(x) for x in sizes[-1])
            focus = device.adb("shell", "dumpsys", "window", "windows", timeout=5)
            match = re.search(r"mCurrentFocus[^\n]*?\s([A-Za-z0-9_.]+)/([^\s}]+)", focus)
            if not match:
                focus = device.adb("shell", "dumpsys", "activity", "activities", timeout=5)
                match = re.search(r"mResumedActivity[^\n]*?\s([A-Za-z0-9_.]+)/([^\s}]+)", focus)
            if not match: raise NativeObservationError("Foreground app unavailable")
            return {"a11y_tree": [convert(c) for c in root if c.tag == "node"],
                "phone_state": {"packageName": match[1], "activityName": match[2]},
                "device_context": {"screen_bounds": {"width": width, "height": height}}}
        return await asyncio.to_thread(read)
