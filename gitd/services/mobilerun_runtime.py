"""D3m0n's Mobilerun-only execution seam.

The control plane remains authoritative: this module never discovers devices, owns
credentials, or calls a provider directly.  A caller supplies an already leased
child, a native bridge, and a profile gateway.  The SDK is imported lazily so the
main dashboard environment does not need Mobilerun installed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import inspect
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Protocol
from xml.sax.saxutils import escape as _xml_escape

from ai_contracts import Capability, ChildSummary, JobDetail, JobState, Role, StepOutcome, StepReceipt
from ops.ai_fleet_jobs import JobStore

PINNED_COMMIT = "301856138c6edeb2ae692bfab85b279ace520e59"
PINNED_VERSION = "0.7.0"
FORBIDDEN_TOOL_NAMES = frozenset(
    {
        "shell", "run_shell", "run_command", "command", "python", "python_exec",
        "code", "run_code", "execute_code", "eval", "exec", "browser", "web",
        "fetch", "fetch_url", "http", "request", "url", "open_url", "mcp",
        "sql", "adb", "device_shell", "computer_use",
    }
)
_SAFE_DRIVER_OPERATIONS = frozenset({
    "tap", "click", "click_at", "swipe", "scroll", "type_text", "input_text",
    "press_back", "open_app", "navigate", "get_state", "get_ui", "screenshot",
    "get_current_app", "current_app",
})
_SAFE_STATE_PROPERTIES = frozenset({"requires_coordinate_tools", "platform", "serial"})
_MUTATING_CAPABILITIES = {
    "input": Capability.INPUT,
    "tap": Capability.INPUT,
    "click": Capability.INPUT,
    "click_at": Capability.INPUT,
    "swipe": Capability.INPUT,
    "scroll": Capability.INPUT,
    "type_text": Capability.INPUT,
    "input_text": Capability.INPUT,
    "press_back": Capability.NAVIGATE,
    "open_app": Capability.NAVIGATE,
    "navigate": Capability.NAVIGATE,
    "observe": Capability.OBSERVE,
    "app_state": Capability.APP_STATE,
    "complete": Capability.OBSERVE,
}
_READ_CAPABILITIES = {
    "screenshot": Capability.OBSERVE,
    "get_state": Capability.OBSERVE,
    "get_ui": Capability.OBSERVE,
    "get_current_app": Capability.APP_STATE,
    "current_app": Capability.APP_STATE,
}
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:api[_-]?key|token|secret|password|cookie|authorization)\s*[:=]\s*[^\s,;]+)"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|cookie|authorization|credential|"
    r"screenshot|raw[_-]?tree|provider[_-]?response|prompt)"
)

_FAST_AGENT_SYSTEM_PROMPT = """You are the Mobilerun device agent. Act only through the exact closed tools listed below.
Explain the next step briefly, then emit one canonical XML tool-call block. Never emit provider-native
tool calls, JSON tool-call objects, Markdown fences, Python, shell, URLs, or alternate XML tags.

The only valid call syntax is:
<function_calls>
<invoke name=\"EXACT_TOOL_NAME\">
<parameter name=\"PARAMETER_NAME\">value</parameter>
</invoke>
</function_calls>

Use only an exact tool name from this list and only its declared parameters:
{{ tool_descriptions }}

Emit exactly one <function_calls> block per response. Put multiple sequential invokes in that one
block only when the current state makes every call safe. Use `complete` with success=true or false
when the bounded task is finished or cannot be completed. Do not invent a tool name or parameter.
The current platform is {{ platform }}. Inspect the supplied device state before mutating it.
"""
_MAX_NORMALIZED_TOOL_CALLS = 8


def _mapping_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _native_tool_calls(response: Any) -> list[Any]:
    """Read only known LLM tool-call envelopes; never evaluate provider payloads."""
    message = getattr(response, "message", None)
    for owner in (message, response):
        if owner is None:
            continue
        for container in (
            owner,
            _mapping_value(owner, "additional_kwargs"),
            _mapping_value(owner, "raw"),
        ):
            if isinstance(container, Mapping):
                for key in ("tool_calls", "function_calls"):
                    found = container.get(key)
                    if isinstance(found, (list, tuple)):
                        return list(found)
                choices = container.get("choices")
                if isinstance(choices, (list, tuple)):
                    for choice in choices[:1]:
                        nested_message = _mapping_value(choice, "message")
                        for key in ("tool_calls", "function_calls"):
                            found = _mapping_value(nested_message, key)
                            if isinstance(found, (list, tuple)):
                                return list(found)
            else:
                for key in ("tool_calls", "function_calls"):
                    found = getattr(container, key, None)
                    if isinstance(found, (list, tuple)):
                        return list(found)
    return []


def _canonical_native_call(call: Any, allowed_names: frozenset[str]) -> tuple[str, dict[str, Any]] | None:
    function = _mapping_value(call, "function", call)
    name = _mapping_value(function, "name")
    arguments = _mapping_value(function, "arguments", _mapping_value(call, "parameters", _mapping_value(call, "args", {})))
    if not isinstance(name, str) or name not in allowed_names or len(name) > 80:
        return None
    if isinstance(arguments, str):
        if len(arguments) > 16000:
            return None
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, Mapping):
        return None
    return name, {str(key): value for key, value in arguments.items()}


def _xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return _xml_escape(json.dumps(value, separators=(",", ":"), ensure_ascii=True))
    if value is None:
        return ""
    return _xml_escape(str(value))


def _native_calls_as_xml(response: Any, allowed_names: frozenset[str]) -> str | None:
    raw_calls = _native_tool_calls(response)
    if not raw_calls or len(raw_calls) > _MAX_NORMALIZED_TOOL_CALLS:
        return None
    calls = [_canonical_native_call(call, allowed_names) for call in raw_calls]
    if not calls or any(call is None for call in calls):
        return None
    lines = ["<function_calls>"]
    for name, arguments in calls:
        lines.append(f'<invoke name="{name}">')
        for key, value in arguments.items():
            lines.append(f'<parameter name="{key}">{_xml_value(value)}</parameter>')
        lines.append("</invoke>")
    lines.append("</function_calls>")
    return "\n".join(lines)


def _replace_response_content(response: Any, content: str) -> Any:
    message = getattr(response, "message", None)
    try:
        # LlamaIndex ChatMessage derives ``content`` from typed text blocks;
        # updating only the legacy field leaves content empty on 0.14.x.
        from llama_index.core.base.llms.types import TextBlock
        message = message.model_copy(update={"blocks": [TextBlock(text=content)], "content": content})
        return response.model_copy(update={"message": message})
    except Exception:
        if message is None:
            message = SimpleNamespace(role="assistant", content=content, blocks=[])
        else:
            message = SimpleNamespace(
                role=getattr(message, "role", "assistant"), content=content,
                blocks=list(getattr(message, "blocks", ()) or ()),
            )
        return SimpleNamespace(
            message=message,
            raw=getattr(response, "raw", None),
            additional_kwargs=getattr(response, "additional_kwargs", {}),
        )


class _SdkFastAgentLLM:
    """Normalize legitimate provider function calls to the pinned SDK XML contract."""

    def __init__(self, delegate: Any, allowed_names: frozenset[str]) -> None:
        self._delegate = delegate
        self._allowed_names = allowed_names

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def class_name(self) -> str:
        value = getattr(self._delegate, "class_name", None)
        return str(value()) if callable(value) else type(self._delegate).__name__

    async def achat(self, messages: Any, **kwargs: Any) -> Any:
        response = await self._delegate.achat(messages=messages, **kwargs)
        content = getattr(getattr(response, "message", None), "content", None)
        normalized = _native_calls_as_xml(response, self._allowed_names)
        if normalized is not None:
            return _replace_response_content(response, normalized)
        if isinstance(content, str) and content.strip():
            return response
        raise ValueError("LLM response had no canonical XML or valid closed native tool calls")


class RuntimeErrorBase(RuntimeError):
    """Safe base exception for SDK readiness and lifecycle failures."""


class RuntimeUnavailable(RuntimeErrorBase):
    pass


class RuntimeCancelled(RuntimeErrorBase):
    pass


class BridgeUnavailable(RuntimeErrorBase):
    pass


class NativeBridge(Protocol):
    """Narrow seam implemented by the D3m0n native/control layer.

    ``authorized_driver_call`` must execute the operation through existing native
    locks and validation.  The runtime deliberately has no fallback to a raw
    driver method for mutations.
    """

    def authorized_driver_call(self, *, serial: str, capability: Capability, fence: int,
                               operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any: ...

    def register_mobilerun_tools(self, *, serial: str, capability_authorizer: Callable[[Capability], Any],
                                  cancelled: Callable[[], bool]) -> Mapping[str, Mapping[str, Any]]: ...

    def state_provider_for(self, *, serial: str, fence: int, cancelled: Callable[[], bool]) -> Any: ...

    def driver_for(self, *, serial: str, fence: int, cancelled: Callable[[], bool]) -> Any: ...


class ProfileGateway(Protocol):
    def build_llm(self, *, role: str, profile_id: str, redactor: Callable[[Any], Any]) -> Any: ...


@dataclass(frozen=True)
class RuntimePolicy:
    """Per-child execution limits; all values are local safety limits."""

    timeout_seconds: float = 180.0
    max_turns: int = 15
    max_cloud_calls: int = 30
    vision: bool = False
    reasoning: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("timeout_seconds must be between 1 and 1800")
        if not 1 <= self.max_turns <= 100:
            raise ValueError("max_turns must be between 1 and 100")
        if not 1 <= self.max_cloud_calls <= 300:
            raise ValueError("max_cloud_calls must be between 1 and 300")
        if self.vision:
            raise ValueError("vision is disabled for the default fleet runtime")


@dataclass(frozen=True)
class RuntimeReadiness:
    ready: bool
    sdk_identity: str
    sdk_version: str | None
    compatibility: str
    disabled_features: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass
class RuntimeCallbacks:
    on_progress: Callable[[Mapping[str, Any]], Any] | None = None
    on_receipt: Callable[[StepReceipt], Any] | None = None


def _safe_text(value: Any) -> str:
    text = str(value)
    return _SECRET.sub("[redacted]", text)[:2000]


def redact_for_cloud(value: Any, *, key: str = "") -> Any:
    """Defence-in-depth redaction applied immediately before every LLM call."""
    if key and _SENSITIVE_KEY.search(key):
        return "[redacted]"
    # llama-index ChatMessage/TextBlock instances must remain typed objects for
    # the SDK.  Copy only their textual fields; vision blocks are denied rather
    # than converted to an opaque string or exported unexpectedly.
    if hasattr(value, "model_copy") and hasattr(value, "blocks"):
        blocks = []
        for block in getattr(value, "blocks", ()) or ():
            if hasattr(block, "text"):
                try:
                    blocks.append(block.model_copy(update={"text": redact_for_cloud(block.text)}))
                except Exception:
                    blocks.append(block)
            elif block.__class__.__name__.lower().endswith("imageblock"):
                raise RuntimeErrorBase("vision payload blocked by fleet policy")
            else:
                blocks.append(block)
        try:
            return value.model_copy(update={"blocks": blocks, "content": redact_for_cloud(getattr(value, "content", ""))})
        except Exception:
            return value
    if isinstance(value, Mapping):
        return {str(k): redact_for_cloud(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_for_cloud(v) for v in value)
    if isinstance(value, str):
        return _SECRET.sub("[redacted]", value)[:12000]
    if isinstance(value, (bytes, bytearray)):
        return "[redacted-binary]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    probe = getattr(cancel, "is_set", None)
    return bool(probe()) if callable(probe) else bool(cancel)


def _check_cancel(cancel: Any) -> None:
    if _cancelled(cancel):
        raise RuntimeCancelled("Mobilerun child was cancelled")


class GuardedDriver:
    """SDK driver facade; no raw fallback exists for native calls."""

    def __init__(self, raw: Any, store: JobStore, child: ChildSummary, lease: Any, bridge: NativeBridge, cancel: Any) -> None:
        self._raw, self._store, self._child, self._lease, self._bridge, self._cancel = raw, store, child, lease, bridge, cancel

    def _capability(self, operation: str) -> Capability:
        return _READ_CAPABILITIES.get(operation, _MUTATING_CAPABILITIES.get(operation, Capability.INPUT))

    async def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if operation not in _SAFE_DRIVER_OPERATIONS:
            raise BridgeUnavailable("native operation is not on the reviewed allowlist")
        _check_cancel(self._cancel)
        capability = self._capability(operation)
        authorization = self._store.authorize(self._child.child_id, self._lease.serial, capability, self._lease.fence)
        if authorization.fence != self._lease.fence:
            raise BridgeUnavailable("native authorization fence mismatch")
        result = self._bridge.authorized_driver_call(
            serial=self._lease.serial, capability=capability, fence=self._lease.fence,
            operation=operation, args=args, kwargs=kwargs,
        )
        return await result if inspect.isawaitable(result) else result

    async def tap(self, x: float, y: float, **kwargs: Any) -> Any:
        return await self._call("tap", float(x), float(y), **kwargs)

    async def click(self, x: float, y: float, **kwargs: Any) -> Any:
        return await self._call("click", float(x), float(y), **kwargs)

    async def click_at(self, x: float, y: float, **kwargs: Any) -> Any:
        return await self._call("click_at", float(x), float(y), **kwargs)

    async def swipe(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("swipe", *args, **kwargs)

    async def scroll(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("scroll", *args, **kwargs)

    async def type_text(self, text: str, **kwargs: Any) -> Any:
        if not isinstance(text, str) or len(text) > 4096:
            raise BridgeUnavailable("bounded text input required")
        return await self._call("type_text", text, **kwargs)

    async def input_text(self, text: str, **kwargs: Any) -> Any:
        return await self.type_text(text, **kwargs)

    async def press_back(self, **kwargs: Any) -> Any:
        return await self._call("press_back", **kwargs)

    async def open_app(self, package: str, **kwargs: Any) -> Any:
        if not isinstance(package, str) or len(package) > 256 or "://" in package:
            raise BridgeUnavailable("invalid app identifier")
        return await self._call("open_app", package, **kwargs)


class GuardedStateProvider:
    def __init__(self, raw: Any, store: JobStore, child: ChildSummary, lease: Any, cancel: Any) -> None:
        self._raw, self._store, self._child, self._lease, self._cancel = raw, store, child, lease, cancel

    async def get_state(self, *args: Any, **kwargs: Any) -> Any:
        _check_cancel(self._cancel)
        self._store.authorize(self._child.child_id, self._lease.serial, Capability.OBSERVE, self._lease.fence)
        value = self._raw.get_state(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    @property
    def requires_coordinate_tools(self) -> bool:
        return bool(getattr(self._raw, "requires_coordinate_tools", False))


def _sdk_import(sdk_source: str | None = None) -> tuple[Any, dict[str, Any]]:
    source = sdk_source or os.environ.get("D3M0N_MOBILERUN_SDK_SOURCE")
    if source and source not in sys.path:
        sys.path.insert(0, source)
    try:
        modules = {
            "root": importlib.import_module("mobilerun"),
            "config": importlib.import_module("mobilerun.config_manager.config_manager"),
            "state": importlib.import_module("mobilerun.agent.droid.state"),
            "action": importlib.import_module("mobilerun.agent.action_context"),
            "registry": importlib.import_module("mobilerun.agent.tool_registry"),
            "manager": importlib.import_module("mobilerun.agent.manager.manager_agent"),
            "executor": importlib.import_module("mobilerun.agent.executor.executor_agent"),
            "fast": importlib.import_module("mobilerun.agent.fast_agent.fast_agent"),
            "prompt_resolver": importlib.import_module("mobilerun.agent.utils.prompt_resolver"),
        }
    except Exception as exc:
        raise RuntimeUnavailable(f"pinned Mobilerun SDK is unavailable: {type(exc).__name__}") from exc
    return modules["root"], modules


def sdk_readiness(sdk_source: str | None = None) -> RuntimeReadiness:
    identity = f"mobilerun@{PINNED_COMMIT}"
    try:
        root, modules = _sdk_import(sdk_source)
        required = ("MobileConfig", "AgentConfig", "DeviceConfig", "TelemetryConfig", "TracingConfig", "LoggingConfig", "ToolsConfig")
        missing = tuple(name for name in required if not hasattr(modules["config"], name))
        for module_name, cls_name in (("manager", "ManagerAgent"), ("executor", "ExecutorAgent"), ("fast", "FastAgent"), ("registry", "ToolRegistry")):
            if not hasattr(modules[module_name], cls_name):
                missing += (f"{module_name}.{cls_name}",)
        version = str(getattr(root, "__version__", "")) or None
        compatible = "compatible" if version == PINNED_VERSION and not missing else "version-or-hook-mismatch"
        return RuntimeReadiness(not missing and version == PINNED_VERSION, identity, version, compatible, (
            "portal_mode=disabled", "auto_setup=false", "telemetry=false", "tracing=false",
            "trajectory=none", "screenshots=local-only", "shell/code/arbitrary-url/MCP tools=disabled",
        ), missing)
    except RuntimeUnavailable as exc:
        return RuntimeReadiness(False, identity, None, "sdk-unavailable", (), (str(exc),))


class MobilerunRuntime:
    def __init__(self, *, sdk_source: str | None = None, policy: RuntimePolicy | None = None,
                 callbacks: RuntimeCallbacks | None = None) -> None:
        self.sdk_source, self.policy, self.callbacks = sdk_source, policy or RuntimePolicy(), callbacks or RuntimeCallbacks()

    async def run_child(self, job: JobDetail, child: ChildSummary, store: JobStore, bridge: NativeBridge,
                        profiles: ProfileGateway | Mapping[str, Any] | Callable[..., Any], cancel: Any) -> Mapping[str, Any]:
        """Run one leased child; caller must claim the lease before entering.

        The worker contract is ``run_child(job, child, store, bridge, profiles, cancel)``.
        ``child.lease_worker_id`` is the owner identity and the lease is re-read
        from ``claim_next`` by the worker entrypoint; this method never claims scope.
        """
        _check_cancel(cancel)
        readiness = sdk_readiness(self.sdk_source)
        if not readiness.ready:
            raise RuntimeUnavailable("Mobilerun hooks are not ready: " + ", ".join(readiness.missing))
        if not child.lease_worker_id:
            raise RuntimeUnavailable("child has no active worker lease")
        self._receipt_sequence = 0
        lease = SimpleNamespace(child_id=child.child_id, serial=child.target.serial, fence=getattr(child, "fence", None), worker_id=child.lease_worker_id)
        # ChildSummary intentionally omits fencing data; worker entrypoint may attach the lease.
        if not isinstance(lease.fence, int):
            raise RuntimeUnavailable("worker must attach the claimed lease fence")
        serial = lease.serial
        goal = redact_for_cloud(job.intent.text)
        role_map = job.profile_snapshot.role_bindings
        planner_id = role_map.get(Role.PLANNER)
        executor_id = role_map.get(Role.EXECUTOR)
        verifier_id = role_map.get(Role.VERIFIER)
        if not planner_id or not executor_id or not verifier_id:
            raise RuntimeUnavailable("immutable plan must bind planner, executor, and verifier profiles")
        models = {
            "planner": _resolve_llm(profiles, "planner", planner_id, "manager", redact_for_cloud),
            "executor": _resolve_llm(profiles, "executor", executor_id, "executor", redact_for_cloud),
            "fast_agent": _resolve_llm(profiles, "executor", executor_id, "fast_agent", redact_for_cloud),
            "verifier": _resolve_llm(profiles, "verifier", verifier_id, "analysis_verifier", redact_for_cloud),
        }
        raw_driver = bridge.driver_for(serial=serial, fence=lease.fence, cancelled=lambda: _cancelled(cancel))
        raw_state = bridge.state_provider_for(serial=serial, fence=lease.fence, cancelled=lambda: _cancelled(cancel))
        if inspect.isawaitable(raw_driver):
            raw_driver = await raw_driver
        if inspect.isawaitable(raw_state):
            raw_state = await raw_state
        if raw_driver is None or raw_state is None:
            raise BridgeUnavailable("guarded driver and state provider are required")
        driver = GuardedDriver(raw_driver, store, child, lease, bridge, cancel)
        state_provider = GuardedStateProvider(raw_state, store, child, lease, cancel)
        try:
            result = await asyncio.wait_for(self._run_sdk(goal, models, driver, state_provider, bridge, store, child, lease, cancel), self.policy.timeout_seconds)
            return redact_for_cloud(result)
        except asyncio.TimeoutError as exc:
            raise RuntimeErrorBase("Mobilerun child timed out") from exc

    async def _run_sdk(self, goal: str, models: Mapping[str, Any], driver: Any, state_provider: Any,
                       bridge: NativeBridge, store: JobStore, child: ChildSummary, lease: Any, cancel: Any) -> Mapping[str, Any]:
        _, modules = _sdk_import(self.sdk_source)
        config = _safe_config(modules["config"], self.policy)
        registry = modules["registry"].ToolRegistry()
        tool_specs = bridge.register_mobilerun_tools(
            serial=lease.serial,
            capability_authorizer=lambda cap: store.authorize(child.child_id, lease.serial, cap, lease.fence),
            cancelled=lambda: _cancelled(cancel),
        )
        if inspect.isawaitable(tool_specs):
            tool_specs = await tool_specs
        if not isinstance(tool_specs, Mapping):
            raise BridgeUnavailable("native tool registration did not return typed mappings")
        async def complete(*, success: bool, message: str, ctx: Any) -> str:
            _check_cancel(cancel)
            if not isinstance(success, bool) or not isinstance(message, str) or len(message) > 2000:
                raise BridgeUnavailable("invalid bounded completion")
            shared.finished = True
            shared.success = success
            shared.answer = redact_for_cloud(message)
            return "completed"
        for name, spec in tool_specs.items():
            if name in FORBIDDEN_TOOL_NAMES or not isinstance(spec, Mapping) or not callable(spec.get("function")):
                raise BridgeUnavailable("native tool registration contains an unsafe or untyped tool")
        tool_specs = dict(tool_specs)
        tool_specs["complete"] = {
            "description": "Finish the task with a bounded success result.",
            "parameters": {"type": "object", "required": ["success", "message"],
                            "properties": {"success": {"type": "boolean"}, "message": {"type": "string"}}},
            "function": complete,
        }
        tool_specs = _adapt_tool_specs(tool_specs)
        registry.register_from_dict(tool_specs)
        if not registry.tools:
            raise BridgeUnavailable("no guarded native actions were registered")
        state_cls = modules["state"].MobileAgentState
        shared = state_cls(instruction=goal, telemetry_config_enabled=False, platform="Android")
        action_ctx = modules["action"].ActionContext(driver=driver, ui=None, shared_state=shared, state_provider=state_provider, streaming=False)
        callbacks = self.callbacks
        if self.policy.reasoning:
            manager = modules["manager"].ManagerAgent(
                llm=models["planner"], action_ctx=action_ctx, state_provider=state_provider,
                save_trajectory="none", shared_state=shared, agent_config=config.agent,
                registry=registry, tracing_config=config.tracing,
                timeout=self.policy.timeout_seconds,
            )
            executor = modules["executor"].ExecutorAgent(
                llm=models["executor"], registry=registry, action_ctx=action_ctx,
                shared_state=shared, agent_config=config.agent,
                timeout=self.policy.timeout_seconds,
            )
            last: Mapping[str, Any] = {"success": False, "reason": "no manager result"}
            for _ in range(self.policy.max_turns):
                _check_cancel(cancel)
                manager_handler = manager.run()
                async for event in manager_handler.stream_events():
                    await self._emit_event(event, child, lease.serial, cancel)
                manager_result = await manager_handler
                if manager_result.get("answer"):
                    return {"success": bool(manager_result.get("success", False)), "reason": manager_result["answer"],
                            "steps": int(shared.step_number)}
                subgoal = str(manager_result.get("current_subgoal", "")).strip()
                if not subgoal:
                    return {"success": False, "reason": "manager returned no guarded subgoal", "steps": int(shared.step_number)}
                executor_handler = executor.run(subgoal=subgoal)
                async for event in executor_handler.stream_events():
                    await self._emit_event(event, child, lease.serial, cancel)
                last = await executor_handler
                shared.action_history.append(last.get("action", {}))
                shared.summary_history.append(str(last.get("summary", "")))
                shared.action_outcomes.append(bool(last.get("outcome", False)))
                shared.error_descriptions.append(str(last.get("error", "")))
                if last.get("outcome") is True:
                    # Manager must independently verify completion on its next turn;
                    # do not claim phone success merely from one executor response.
                    continue
            return {"success": False, "reason": "manager/executor turn limit reached", "steps": int(shared.step_number)}
        else:
            allowed_tool_names = frozenset(registry.tools)
            fast_llm = _SdkFastAgentLLM(models["fast_agent"], allowed_tool_names)
            prompt_resolver = modules["prompt_resolver"].PromptResolver(
                custom_prompts={"fast_agent_system": _FAST_AGENT_SYSTEM_PROMPT}
            )
            agent = modules["fast"].FastAgent(llm=fast_llm, agent_config=config.agent, registry=registry, action_ctx=action_ctx,
                state_provider=state_provider, save_trajectory="none", debug=False, shared_state=shared,
                tracing_config=config.tracing, prompt_resolver=prompt_resolver,
                timeout=self.policy.timeout_seconds)
        handler = agent.run(input=goal)
        async for event in handler.stream_events():
            await self._emit_event(event, child, lease.serial, cancel)
        result = await handler
        _check_cancel(cancel)
        return {"sdk": f"mobilerun@{PINNED_COMMIT}", "role": "executor", "success": bool(result.get("success", False)),
                "reason": _safe_text(result.get("reason", result.get("answer", ""))), "steps": int(shared.step_number)}

    async def _emit_event(self, event: Any, child: Any, serial: str, cancel: Any) -> None:
        _check_cancel(cancel)
        summary = _event_summary(event)
        if self.callbacks.on_receipt:
            self._receipt_sequence = getattr(self, "_receipt_sequence", 0) + 1
            tool_name = getattr(event, "tool_name", None)
            capability = (_READ_CAPABILITIES | _MUTATING_CAPABILITIES).get(
                tool_name, Capability.OBSERVE
            )
            success = getattr(event, "success", None)
            outcome = (
                StepOutcome.SUCCEEDED if success is True else
                StepOutcome.FAILED if success is False else StepOutcome.STARTED
            )
            receipt = StepReceipt(
                step_id=f"mobilerun_{uuid.uuid4().hex}",
                child_id=child.child_id,
                sequence=self._receipt_sequence,
                capability=capability,
                outcome=outcome,
                summary=summary,
                evidence={"event": type(event).__name__, "serial": serial},
            )
            value = self.callbacks.on_receipt(receipt)
            if inspect.isawaitable(value):
                await value
        if self.callbacks.on_progress:
            value = self.callbacks.on_progress({"child_id": child.child_id, "serial": serial,
                                                "event": type(event).__name__, "summary": summary})
            if inspect.isawaitable(value):
                await value


def _resolve_llm(profiles: Any, role: str, profile_id: str, component: str, redactor: Callable[[Any], Any]) -> Any:
    if hasattr(profiles, "build_llm"):
        return profiles.build_llm(role=role, profile_id=profile_id, component=component, redactor=redactor)
    if hasattr(profiles, "get_llm"):
        return profiles.get_llm(role=role, profile_id=profile_id, component=component, redactor=redactor)
    if callable(profiles):
        return profiles(role=role, profile_id=profile_id, component=component, redactor=redactor)
    if isinstance(profiles, Mapping):
        llm = profiles.get(component)
        if llm is not None:
            return llm
    raise RuntimeUnavailable("profile gateway cannot build the assigned LLM")


def _safe_config(cm: Any, policy: RuntimePolicy) -> Any:
    required = ("AgentConfig", "FastAgentConfig", "ManagerConfig", "ExecutorConfig", "DeviceConfig", "TelemetryConfig", "TracingConfig", "LoggingConfig", "ToolsConfig", "MobileConfig")
    if any(not hasattr(cm, name) for name in required):
        raise RuntimeUnavailable("SDK configuration hooks are incomplete")
    agent = cm.AgentConfig(max_steps=policy.max_turns, reasoning=policy.reasoning, streaming=False,
        fast_agent=cm.FastAgentConfig(vision=False, parallel_tools=False), manager=cm.ManagerConfig(vision=False),
        executor=cm.ExecutorConfig(vision=False), app_cards=cm.AppCardConfig(enabled=False))
    return cm.MobileConfig(agent=agent, device=cm.DeviceConfig(portal_mode="disabled", auto_setup=False),
        telemetry=cm.TelemetryConfig(enabled=False), tracing=cm.TracingConfig(enabled=False, langfuse_screenshots=False),
        logging=cm.LoggingConfig(save_trajectory="none", trajectory_gifs=False),
        tools=cm.ToolsConfig(disabled_tools=list(FORBIDDEN_TOOL_NAMES)))


def _event_summary(event: Any) -> str:
    fields = {}
    for name in ("success", "reason", "summary", "description", "outcome", "tool_name"):
        if hasattr(event, name):
            fields[name] = redact_for_cloud(getattr(event, name))
    return _safe_text(fields or type(event).__name__)


def _adapt_tool_specs(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Convert the bridge's closed JSON-schema shape to SDK ToolRegistry's map.

    Mobilerun 0.7.0 expects ``parameters`` to be ``{name: {type, ...}}`` while
    the application boundary intentionally publishes a conventional object
    schema.  The adapter is explicit and rejects arbitrary parameter shapes.
    """
    adapted: dict[str, dict[str, Any]] = {}
    for name, spec in specs.items():
        params = spec.get("parameters", {})
        if isinstance(params, Mapping) and isinstance(params.get("properties"), Mapping):
            required = set(params.get("required", ())) if isinstance(params.get("required", ()), (list, tuple, set)) else set()
            properties = params["properties"]
            params = {str(key): dict(value, required=str(key) in required) for key, value in properties.items() if isinstance(value, Mapping)}
        if not isinstance(params, Mapping) or any(not isinstance(value, Mapping) for value in params.values()):
            raise BridgeUnavailable("native tool parameters are not a closed typed map")
        function = spec["function"]
        object_parameters = frozenset(
            key for key, value in params.items() if value.get("type") == "object"
        )
        if object_parameters:
            async def typed_function(*, _function=function, _object_parameters=object_parameters, **kwargs: Any) -> Any:
                for key in _object_parameters:
                    value = kwargs.get(key)
                    if isinstance(value, str):
                        if len(value) > 16000:
                            raise BridgeUnavailable("object parameter exceeds bounded JSON size")
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError as exc:
                            raise BridgeUnavailable("object parameter is not valid JSON") from exc
                        if not isinstance(value, Mapping):
                            raise BridgeUnavailable("object parameter must decode to an object")
                        kwargs[key] = dict(value)
                result = _function(**kwargs)
                return await result if inspect.isawaitable(result) else result
            function = typed_function
        adapted[name] = {"description": str(spec.get("description", ""))[:500], "parameters": dict(params), "function": function}
    return adapted


async def run_child(job: JobDetail, child: ChildSummary, store: JobStore, bridge: NativeBridge,
                    profiles: ProfileGateway | Mapping[str, Any] | Callable[..., Any], cancel: Any) -> Mapping[str, Any]:
    """Module-level worker contract used by ``ops.ai_worker_entry``."""
    return await MobilerunRuntime().run_child(job, child, store, bridge, profiles, cancel)


__all__ = ["MobilerunRuntime", "RuntimePolicy", "RuntimeCallbacks", "RuntimeReadiness", "RuntimeUnavailable", "BridgeUnavailable", "RuntimeCancelled", "NativeBridge", "ProfileGateway", "PINNED_COMMIT", "PINNED_VERSION", "redact_for_cloud", "sdk_readiness", "run_child"]
