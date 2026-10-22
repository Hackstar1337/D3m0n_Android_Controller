"""Real LlamaIndex model handles for the pinned Mobilerun SDK.

The SDK receives a concrete ``CustomLLM`` implementation.  It has no dynamic
attribute forwarding and all calls are routed through the owner-controlled
CloudGateway; provider secrets never enter this module's public objects.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from llama_index.core.llms import CustomLLM, LLMMetadata
from llama_index.core.base.llms.types import ChatMessage, ChatResponse, CompletionResponse

from gitd.services.cloud_gateway import CloudGateway, GatewayResult

Role = Literal["planner", "executor", "verifier"]
Component = Literal[
    "manager", "executor", "fast_agent", "app_opener", "structured_output",
    "analysis_verifier", "sdk_agent",
]
_COMPONENTS = frozenset({"manager", "executor", "fast_agent", "app_opener", "structured_output", "analysis_verifier", "sdk_agent"})
_ROLES = frozenset({"planner", "executor", "verifier"})


def _text(message: ChatMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in getattr(message, "blocks", ()) or ():
        value = getattr(block, "text", None)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        role = getattr(getattr(message, "role", None), "value", getattr(message, "role", "user"))
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        result.append({"role": role, "content": _text(message)})
    return result


class SDKGatewayLLM(CustomLLM):
    """Concrete LlamaIndex LLM backed exclusively by :class:`CloudGateway`."""

    gateway: Any
    profile_id: str
    role: str
    component: str
    context_window: int = 1_050_000
    max_output_tokens: int = 4096
    model_name: str = "gpt-6-luna"

    class Config:
        arbitrary_types_allowed = True
        extra = "forbid"

    @classmethod
    def class_name(cls) -> str:
        return "d3m0n_sdk_gateway_llm"

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=self.context_window,
            num_output=self.max_output_tokens,
            is_chat_model=True,
            is_function_calling_model=True,
            model_name=self.model_name,
        )

    def _validate(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"max_tokens", "max_completion_tokens", "response_format", "cancellation", "operation_guard", "verifier_mode", "reasoning_effort", "formatted"}
        unknown = set(kwargs) - allowed
        if unknown:
            raise ValueError("unsupported LLM parameter")
        reasoning = kwargs.get("reasoning_effort", "none")
        if reasoning != "none":
            raise ValueError("gpt-6-luna Chat Completions requires reasoning_effort='none'")
        first = kwargs.get("max_completion_tokens")
        second = kwargs.get("max_tokens")
        if first is not None and second is not None and first != second:
            raise ValueError("conflicting completion token limits")
        tokens = first if first is not None else second
        if tokens is None:
            tokens = self.max_output_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= self.max_output_tokens:
            raise ValueError("completion token limit outside verified profile bounds")
        return {"max_tokens": tokens, "response_format": kwargs.get("response_format"), "cancellation": kwargs.get("cancellation"), "operation_guard": kwargs.get("operation_guard"), "verifier_mode": kwargs.get("verifier_mode"), "reasoning_effort": "none"}

    async def _gateway_chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> GatewayResult:
        params = self._validate(kwargs)
        safe_messages = _messages(messages)
        reservation = self.gateway.estimate_reservation(self.profile_id, safe_messages, params["max_tokens"])
        return await self.gateway.invoke(
            self.profile_id, role=self.role, component=self.component,
            messages=safe_messages, max_tokens=params["max_tokens"], estimated_cost_usd=reservation,
            response_format=params["response_format"], cancellation=params["cancellation"],
            operation_guard=params["operation_guard"], verifier_mode=params["verifier_mode"],
            reasoning_effort="none",
        )

    async def achat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        result = await self._gateway_chat(messages, **kwargs)
        raw = dict(result.raw_json or {})
        usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
        return ChatResponse(
            message=ChatMessage(role="assistant", content=result.content),
            raw=raw,
            additional_kwargs={"usage": dict(usage)},
            delta=None,
        )

    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.achat(messages, **kwargs))
        raise RuntimeError("use achat inside an active event loop")

    async def acomplete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        result = await self._gateway_chat([ChatMessage(role="user", content=prompt)], **kwargs)
        raw = dict(result.raw_json or {})
        usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
        return CompletionResponse(text=result.content, raw=raw, additional_kwargs={"usage": dict(usage)})

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acomplete(prompt, formatted=formatted, **kwargs))
        raise RuntimeError("use acomplete inside an active event loop")

    def _complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        return self.complete(prompt, formatted=formatted, **kwargs)

    def _stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any):
        raise NotImplementedError("streaming is disabled for the fleet runtime")

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any):
        raise NotImplementedError("streaming is disabled for the fleet runtime")


def build_sdk_llm(gateway: CloudGateway, *, profile_id: str, role: Role, component: Component, model_name: str = "gpt-6-luna", max_output_tokens: int = 4096, context_window: int = 1_050_000) -> SDKGatewayLLM:
    if role not in _ROLES or component not in _COMPONENTS:
        raise ValueError("closed SDK role/component required")
    if isinstance(max_output_tokens, bool) or not 1 <= max_output_tokens <= 128_000:
        raise ValueError("invalid verified output bound")
    return SDKGatewayLLM(gateway=gateway, profile_id=profile_id, role=role, component=component, model_name=model_name, max_output_tokens=max_output_tokens, context_window=context_window)

__all__ = ["SDKGatewayLLM", "build_sdk_llm"]
