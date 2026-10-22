"""Closed provider transport for Mobilerun fleet AI calls.

All SDK-facing components use :class:`SDKModelFactory`; no Manager, Executor,
FastAgent, app-opener, or structured-output call should construct an HTTP client
or provider model directly.  This module does not perform a call at import time.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from ops.ai_fleet_settings import ProfileStore, normalize_endpoint

MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_MESSAGES = 64
MAX_MESSAGE_CHARS = 32_000
_ALLOWED_ROLES = {"planner", "executor", "verifier"}
_ALLOWED_COMPONENTS = {
    "manager", "executor", "fast_agent", "app_opener", "structured_output",
    "analysis_verifier", "sdk_agent",
}
_SECRET_WORDS = ("api_key", "apikey", "authorization", "bearer", "password", "secret", "cookie", "token")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_VERIFIED_PRICING = {
    "gpt-6-luna": {"input_usd_per_million": 0.10, "output_usd_per_million": 0.50,
                    "context_window": 1_050_000, "max_output_tokens": 128_000},
    # Test-only deterministic transport fixture; never selected by production
    # profile defaults or the live model catalog.
    "test-model": {"input_usd_per_million": 0.10, "output_usd_per_million": 0.50,
                    "context_window": 128_000, "max_output_tokens": 16_384},
}


class CloudGatewayError(RuntimeError):
    """Safe transport or policy failure; raw provider content is not attached."""


class GatewayCancelled(CloudGatewayError):
    pass


class GatewayPolicyError(CloudGatewayError):
    pass


class GatewayResponseError(CloudGatewayError):
    pass


@dataclass(frozen=True)
class GatewayLimits:
    timeout_connect: float = 3.0
    timeout_read: float = 30.0
    timeout_write: float = 10.0
    timeout_pool: float = 3.0
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_request_bytes: int = MAX_REQUEST_BYTES
    max_tokens: int = 4096
    max_calls: int = 20
    max_price_usd: float = 20.0


@dataclass(frozen=True)
class UIEnvelope:
    """Redacted metadata suitable for dashboard status/history."""

    ok: bool
    profile_id: str
    provider: str
    model: str
    role: str
    component: str
    status: str
    request_id: str | None = None
    input_messages: int = 0
    output_tokens: int | None = None
    response_bytes: int | None = None
    usage_known: bool = False
    vision_used: bool = False
    telemetry: bool = False
    traces: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "profile_id": self.profile_id, "provider": self.provider,
            "model": self.model, "role": self.role, "component": self.component,
            "status": self.status, "request_id": self.request_id,
            "input_messages": self.input_messages, "output_tokens": self.output_tokens,
            "response_bytes": self.response_bytes, "usage_known": self.usage_known,
            "vision_used": self.vision_used, "telemetry": False, "traces": False,
        }


@dataclass(frozen=True)
class GatewayResult:
    content: str
    raw_json: Mapping[str, Any] | None
    envelope: UIEnvelope

    def ui_envelope(self) -> dict[str, Any]:
        return self.envelope.as_dict()


@dataclass(frozen=True)
class SDKModelHandle:
    """A role/component-bound model; it can only call through CloudGateway."""

    gateway: "CloudGateway"
    profile_id: str
    role: str
    component: str
    scope: str = "navigation"
    capabilities: frozenset[str] = frozenset()

    async def complete(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        allow_vision: bool = False,
        estimated_cost_usd: str | None = None,
        cancellation: asyncio.Event | None = None,
        operation_guard: Callable[[], Any] | Callable[[], Awaitable[Any]] | None = None,
        verifier_mode: str | None = None,
    ) -> GatewayResult:
        return await self.gateway.invoke(
            self.profile_id,
            role=self.role,
            component=self.component,
            messages=messages,
            max_tokens=max_tokens,
            response_format=response_format,
            scope=self.scope,
            capabilities=self.capabilities,
            allow_vision=allow_vision,
            estimated_cost_usd=estimated_cost_usd,
            cancellation=cancellation,
            operation_guard=operation_guard,
            verifier_mode=verifier_mode,
            reasoning_effort="none",
        )


def _text_content(value: Any) -> str:
    """Extract only documented OpenAI text parts; never stringify arbitrary objects."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if not isinstance(part, Mapping) or part.get("type") not in {"text", "output_text"}:
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _tool_call_action(call: Any) -> str:
    """Convert one provider function call to the SDK's existing action envelope.

    Arguments are JSON-decoded only (never evaluated) and remain subject to the
    parent native-tool validator after the LLM response is returned.
    """
    if not isinstance(call, Mapping):
        raise GatewayResponseError("Provider returned an invalid tool call")
    function = call.get("function")
    if not isinstance(function, Mapping):
        raise GatewayResponseError("Provider returned an invalid tool call")
    name = function.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise GatewayResponseError("Provider returned an invalid tool name")
    arguments: Any = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GatewayResponseError("Provider returned invalid tool arguments") from exc
    if not isinstance(arguments, Mapping) or "action" in arguments:
        raise GatewayResponseError("Provider returned invalid tool arguments")
    action: dict[str, Any] = {"action": name, **dict(arguments)}
    try:
        encoded = json.dumps(action, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise GatewayResponseError("Provider returned invalid tool arguments") from exc
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_CHARS:
        raise GatewayResponseError("Provider tool call exceeds the response limit")
    return encoded


def _normalize_completion_message(message: Mapping[str, Any]) -> str:
    """Normalize Chat Completions message variants consumed by Mobilerun.

    OpenAI-compatible servers may return string content, text-part arrays, or
    a null content plus ``tool_calls``/legacy ``function_call``.  The previous
    implementation accepted only the first form, silently losing real action
    responses at the LlamaIndex boundary.
    """
    text = _text_content(message.get("content"))
    calls = message.get("tool_calls")
    if calls is None and message.get("function_call") is not None:
        calls = [message["function_call"]]
    if calls is not None:
        if not isinstance(calls, list) or len(calls) != 1:
            raise GatewayResponseError("Provider returned an unsupported tool-call count")
        action = _tool_call_action(calls[0])
        tool_text = f"### Thought\nProvider selected a native action.\n### Action\n{action}\n### Description\nProvider native action"
        text = f"{text}\n{tool_text}" if text else tool_text
    if not text:
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            text = refusal
    if not text:
        raise GatewayResponseError("Provider returned no usable completion")
    return text


class CloudGateway:
    """Bounded OpenAI-compatible transport backed by encrypted fleet profiles."""

    def __init__(
        self,
        profiles: ProfileStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str, int | None], Iterable[str]] | None = None,
        limits: GatewayLimits | None = None,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.profiles = profiles
        self.transport = transport
        self.resolver = resolver or self._resolve
        self.limits = limits or GatewayLimits()
        self._client_factory = client_factory or httpx.AsyncClient

    @staticmethod
    def _resolve(host: str, port: int | None) -> list[str]:
        rows = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(row[4][0] for row in rows if row[4]))

    def _check_dns(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        try:
            addresses = list(self.resolver(parsed.hostname or "", parsed.port or 443))
        except (OSError, socket.gaierror, ValueError) as exc:
            raise GatewayPolicyError("Provider endpoint DNS resolution failed") from exc
        if not addresses:
            raise GatewayPolicyError("Provider endpoint DNS resolution failed")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except ValueError as exc:
                raise GatewayPolicyError("Provider endpoint DNS resolution failed") from exc
            if not address.is_global:
                raise GatewayPolicyError("Provider endpoint resolved to a non-public address")

    @staticmethod
    def _check_cancelled(cancellation: asyncio.Event | None) -> None:
        if cancellation is not None and cancellation.is_set():
            raise GatewayCancelled("Provider operation cancelled")

    @staticmethod
    async def _guard(operation_guard: Callable[[], Any] | Callable[[], Awaitable[Any]] | None) -> None:
        if operation_guard is None:
            return
        result = operation_guard()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise GatewayPolicyError("Operation guard rejected provider call")

    def estimate_reservation(self, profile_id: str, messages: Iterable[Mapping[str, Any]], max_tokens: int) -> str:
        """Return a conservative USD reservation from verified model metadata."""
        runtime = self.profiles.get_runtime(profile_id)
        from ops.ai_model_policy import policy_for
        pricing = policy_for(self.profiles, profile_id, runtime)
        if pricing is None:
            raise GatewayPolicyError("verified pricing is unavailable for the selected model")
        rows, _ = self._safe_messages(messages)
        input_chars = sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))) for row in rows)
        # UTF-8 bytes + per-message framing safely upper-bound text tokenization;
        # chars/4 is not conservative for CJK, code, arbitrary unicode or short tokens.
        input_tokens = max(1, sum(len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 64 for row in rows))
        amount = (input_tokens / 1_000_000) * pricing["input_usd_per_million"] + (max_tokens / 1_000_000) * pricing["output_usd_per_million"] + pricing.get("request_usd", 0)
        from decimal import Decimal, ROUND_CEILING
        return str(Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_CEILING))

    def usage_cost(self, profile_id: str, usage: Mapping[str, Any]):
        from decimal import Decimal, ROUND_CEILING
        from ops.ai_model_policy import policy_for
        pricing = policy_for(self.profiles, profile_id)
        if pricing is None: return None
        counts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts): return None
        amount = (Decimal(counts[0]) * Decimal(str(pricing["input_usd_per_million"])) + Decimal(counts[1]) * Decimal(str(pricing["output_usd_per_million"]))) / Decimal(1000000)
        return (amount + Decimal(str(pricing.get("request_usd", 0)))).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)

    @staticmethod
    def _safe_messages(messages: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        rows = list(messages)
        if not rows or len(rows) > MAX_MESSAGES:
            raise GatewayPolicyError("Message count is outside the bounded request policy")
        vision = False
        output: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise GatewayPolicyError("Invalid message")
            role = row.get("role")
            content = row.get("content")
            if role not in {"system", "user", "assistant", "tool"}:
                raise GatewayPolicyError("Invalid message role")
            if isinstance(content, str):
                if len(content) > MAX_MESSAGE_CHARS:
                    raise GatewayPolicyError("Message exceeds the request limit")
                output.append({"role": role, "content": content})
            elif isinstance(content, list):
                # Keep only the OpenAI image/text shape; raw UI observations are
                # never copied into the UI envelope or persisted by this module.
                safe_parts = []
                for part in content:
                    if not isinstance(part, Mapping) or part.get("type") not in {"text", "image_url"}:
                        raise GatewayPolicyError("Unsupported message content")
                    if part["type"] == "text":
                        text = part.get("text", "")
                        if not isinstance(text, str) or len(text) > MAX_MESSAGE_CHARS:
                            raise GatewayPolicyError("Message exceeds the request limit")
                        safe_parts.append({"type": "text", "text": text})
                    else:
                        image = part.get("image_url")
                        if not isinstance(image, Mapping) or not isinstance(image.get("url"), str):
                            raise GatewayPolicyError("Invalid image content")
                        safe_parts.append({"type": "image_url", "image_url": {"url": image["url"]}})
                        vision = True
                output.append({"role": role, "content": safe_parts})
            else:
                raise GatewayPolicyError("Invalid message content")
        return output, vision

    async def invoke(
        self,
        profile_id: str,
        *,
        role: str,
        component: str,
        messages: Iterable[Mapping[str, Any]],
        model: str | None = None,
        max_tokens: int | None = None,
        response_format: Mapping[str, Any] | None = None,
        scope: str = "navigation",
        capabilities: Iterable[str] = (),
        allow_vision: bool = False,
        estimated_cost_usd: str | None = None,
        cancellation: asyncio.Event | None = None,
        operation_guard: Callable[[], Any] | Callable[[], Awaitable[Any]] | None = None,
        verifier_mode: str | None = None,
        reasoning_effort: str = "none",
    ) -> GatewayResult:
        role = str(role).lower()
        component = str(component).lower()
        if role not in _ALLOWED_ROLES or component not in _ALLOWED_COMPONENTS:
            raise GatewayPolicyError("Invalid SDK role or component")
        if reasoning_effort != "none":
            raise GatewayPolicyError("Only reviewed reasoning_effort=none is supported")
        if component == "analysis_verifier" and (role != "verifier" or verifier_mode != "analysis"):
            raise GatewayPolicyError("Analysis verifier must be explicitly selected as verifier analysis")
        if component == "sdk_agent" and role == "verifier" and verifier_mode not in {"sdk_agent", None}:
            raise GatewayPolicyError("SDK verifier mode is distinct from analysis verification")
        if allow_vision and (not scope or "observe" not in set(capabilities)):
            raise GatewayPolicyError("Vision requires explicit observe capability")
        self._check_cancelled(cancellation)
        await self._guard(operation_guard)
        runtime = self.profiles.get_runtime(profile_id)
        endpoint = normalize_endpoint(
            runtime["endpoint_origin"], runtime["provider"],
            approved_generic_origins=self.profiles.approved_generic_origins, resolver=self.profiles.resolver,
        )
        self._check_dns(endpoint)
        selected_model = (model or runtime["model"]).strip()
        if not selected_model or len(selected_model) > 200:
            raise GatewayPolicyError("Invalid model")
        tokens = runtime["max_tokens"] if max_tokens is None else max_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= min(runtime["max_tokens"], self.limits.max_tokens):
            raise GatewayPolicyError("Token limit exceeds profile policy")
        if estimated_cost_usd is None:
            estimated_cost_usd = self.estimate_reservation(profile_id, messages, tokens)
        try:
            estimated = float(estimated_cost_usd)
        except (TypeError, ValueError) as exc:
            raise GatewayPolicyError("Invalid spend reservation") from exc
        if estimated < 0 or estimated > min(float(runtime["max_price_usd"]), self.limits.max_price_usd):
            raise GatewayPolicyError("Spend reservation exceeds policy")
        safe_messages, has_vision = self._safe_messages(messages)
        if has_vision and not allow_vision:
            raise GatewayPolicyError("Vision is disabled by default")
        self._check_cancelled(cancellation)
        # Reserve immediately before construction of the request; the profile
        # store rejects expired/revoked profiles and bounded call/spend overflow.
        self.profiles.reserve(profile_id, calls=1, amount_usd=str(estimated))
        from ops.ai_model_policy import policy_for
        model_policy = policy_for(self.profiles, profile_id, runtime)
        if model_policy is None or selected_model != runtime["model"]:
            raise GatewayPolicyError("Verified model policy is required; overrides are not authorized")
        if tokens > model_policy["max_output_tokens"]:
            raise GatewayPolicyError("Output limit exceeds verified model capability")
        payload: dict[str, Any] = {"model": selected_model, "messages": safe_messages,
                                  model_policy["token_parameter"]: tokens}
        if model_policy.get("reasoning_effort") is not None:
            payload["reasoning_effort"] = model_policy["reasoning_effort"]
        if response_format is not None:
            if not model_policy["structured_output"]:
                raise GatewayPolicyError("Structured output is not verified for this model")
            if not isinstance(response_format, Mapping) or len(json.dumps(response_format)) > 16_384:
                raise GatewayPolicyError("Invalid structured-output format")
            payload["response_format"] = dict(response_format)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.limits.max_request_bytes:
            raise GatewayPolicyError("Provider request exceeds the size limit")
        headers = {"Authorization": f"Bearer {runtime['secret']}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(
            connect=self.limits.timeout_connect, read=self.limits.timeout_read,
            write=self.limits.timeout_write, pool=self.limits.timeout_pool,
        )
        client_kwargs = {"timeout": timeout, "follow_redirects": False}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        client = self._client_factory(**client_kwargs)
        url = endpoint.rstrip("/") + "/chat/completions"
        response = None
        try:
            self._check_cancelled(cancellation)
            await self._guard(operation_guard)
            request = client.build_request("POST", url, headers=headers, content=encoded)
            response = await client.send(request, stream=True)
            if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
                raise GatewayResponseError("Provider redirects are not permitted")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                self._check_cancelled(cancellation)
                if len(body) + len(chunk) > self.limits.max_response_bytes:
                    raise GatewayResponseError("Provider response exceeded the size limit")
                body.extend(chunk)
            if response.status_code >= 400:
                raise GatewayResponseError("Provider request was rejected")
            try:
                data = json.loads(bytes(body).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise GatewayResponseError("Provider returned invalid JSON") from exc
            if not isinstance(data, Mapping):
                raise GatewayResponseError("Provider returned invalid JSON")
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise GatewayResponseError("Provider returned no completion")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise GatewayResponseError("Provider returned no completion message")
            content = _normalize_completion_message(message)
            usage = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
            output_tokens = usage.get("completion_tokens")
            if not isinstance(output_tokens, int):
                output_tokens = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None
            request_id = data.get("id") if isinstance(data.get("id"), str) and len(data["id"]) < 200 else None
            envelope = UIEnvelope(True, profile_id, runtime["provider"], selected_model, role, component, "succeeded", request_id,
                                  len(safe_messages), output_tokens, len(body), output_tokens is not None, has_vision)
            return GatewayResult(content=content, raw_json=data, envelope=envelope)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GatewayResponseError("Provider transport failed") from exc
        finally:
            if response is not None:
                await response.aclose()
            await client.aclose()

    async def structured_output(self, *args: Any, **kwargs: Any) -> GatewayResult:
        kwargs["component"] = "structured_output"
        kwargs["response_format"] = kwargs.get("response_format") or {"type": "json_object"}
        return await self.invoke(*args, **kwargs)


class SDKModelFactory:
    """Single factory/transport seam for all Mobilerun SDK model construction."""

    def __init__(self, gateway: CloudGateway, *, scope: str = "navigation", capabilities: Iterable[str] = ()) -> None:
        self.gateway = gateway
        self.scope = scope
        self.capabilities = frozenset(capabilities)

    def create_model(self, profile_id: str, *, role: str, component: str, scope: str | None = None,
                     capabilities: Iterable[str] | None = None) -> SDKModelHandle:
        if component not in _ALLOWED_COMPONENTS:
            raise GatewayPolicyError("Unknown SDK component")
        if role not in _ALLOWED_ROLES:
            raise GatewayPolicyError("Unknown SDK role")
        return SDKModelHandle(self.gateway, profile_id, role, component,
                              scope or self.scope,
                              frozenset(self.capabilities if capabilities is None else capabilities))

    def build_llm(self, *, role: str, profile_id: str, component: str = "sdk_agent",
                  redactor: Callable[[Any], Any] | None = None) -> Any:
        from gitd.services.sdk_gateway_llm import build_sdk_llm
        handle = self.create_model(profile_id, role=role, component=component)
        runtime = self.gateway.profiles.get_runtime(profile_id)
        if role not in set(runtime.get("roles", ())):
            raise GatewayPolicyError("profile is not assigned to requested role")
        return build_sdk_llm(self.gateway, profile_id=profile_id, role=role, component=component,
                             model_name=str(runtime["model"]),
                             max_output_tokens=min(int(runtime["max_tokens"]), self.gateway.limits.max_tokens))

    get_llm = build_llm

    def for_manager(self, profile_id: str, *, role: str = "planner") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="manager")

    def for_executor(self, profile_id: str, *, role: str = "executor") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="executor")

    def for_fast_agent(self, profile_id: str, *, role: str = "executor") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="fast_agent")

    def for_app_opener(self, profile_id: str, *, role: str = "executor") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="app_opener")

    def for_structured_output(self, profile_id: str, *, role: str = "planner") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="structured_output")

    def for_analysis_verifier(self, profile_id: str) -> SDKModelHandle:
        return self.create_model(profile_id, role="verifier", component="analysis_verifier")

    def for_sdk_agent(self, profile_id: str, *, role: str = "executor") -> SDKModelHandle:
        return self.create_model(profile_id, role=role, component="sdk_agent")

    # Explicit alias for adapters that call the seam a transport.
    transport = create_model


MobilerunModelFactory = SDKModelFactory


def build_sdk_model_factory(
    profiles: ProfileStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str, int | None], Iterable[str]] | None = None,
    limits: GatewayLimits | None = None,
    scope: str = "navigation",
    capabilities: Iterable[str] = (),
) -> SDKModelFactory:
    """Build the one approved model seam used by every SDK component."""
    return SDKModelFactory(
        CloudGateway(profiles, transport=transport, resolver=resolver, limits=limits),
        scope=scope,
        capabilities=capabilities,
    )

__all__ = [
    "CloudGateway", "CloudGatewayError", "GatewayCancelled", "GatewayLimits",
    "GatewayPolicyError", "GatewayResponseError", "GatewayResult", "MobilerunModelFactory",
    "SDKModelFactory", "SDKModelHandle", "UIEnvelope", "build_sdk_model_factory",
]
