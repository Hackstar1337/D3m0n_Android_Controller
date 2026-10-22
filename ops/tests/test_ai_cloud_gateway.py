import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
try:
    from llama_index.core.base.llms.types import ChatMessage, TextBlock
except ImportError as exc:
    raise unittest.SkipTest("Adapter suite requires the isolated pinned Mobilerun SDK venv; do not add SDK dependencies to the backend") from exc

from ops.ai_fleet_settings import ProfileStore
from gitd.services.cloud_gateway import (
    CloudGateway,
    GatewayCancelled,
    GatewayPolicyError,
    GatewayResponseError,
    SDKModelFactory,
)
from gitd.services.sdk_gateway_llm import build_sdk_llm


def fake_resolver(host, port):
    return ["93.184.216.34"]


class CloudGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ProfileStore(self.temp.name, resolver=fake_resolver)
        self.store.create_profile("p", "openai", "test-model", "https://api.openai.com/v1", "provider-secret")

    async def asyncTearDown(self):
        self.temp.cleanup()

    @staticmethod
    def success_transport(request):
        body = json.dumps({
            "id": "req-safe-1",
            "choices": [{"message": {"content": "provider output must stay server-side"}}],
            "usage": {"completion_tokens": 3},
        }).encode()
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body, request=request)

    async def test_factory_routes_all_sdk_components_and_ui_is_redacted(self):
        gateway = CloudGateway(self.store, transport=httpx.MockTransport(self.success_transport), resolver=fake_resolver)
        factory = SDKModelFactory(gateway, capabilities=("observe",))
        handles = [factory.for_manager("p"), factory.for_executor("p"), factory.for_fast_agent("p"),
                   factory.for_app_opener("p"), factory.for_structured_output("p"), factory.for_sdk_agent("p")]
        self.assertEqual([item.component for item in handles], ["manager", "executor", "fast_agent", "app_opener", "structured_output", "sdk_agent"])
        result = await handles[0].complete([{"role": "user", "content": "hello"}], estimated_cost_usd="0.01")
        self.assertIn("provider output", result.content)
        envelope = result.ui_envelope()
        self.assertNotIn("provider output", json.dumps(envelope))
        self.assertNotIn("provider-secret", json.dumps(envelope))
        self.assertFalse(envelope["telemetry"])
        self.assertFalse(envelope["traces"])

    async def test_analysis_verifier_is_distinct_and_structured_output_has_format(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return self.success_transport(request)
        gateway = CloudGateway(self.store, transport=httpx.MockTransport(handler), resolver=fake_resolver)
        factory = SDKModelFactory(gateway)
        verifier = factory.for_analysis_verifier("p")
        with self.assertRaises(GatewayPolicyError):
            await verifier.complete([{"role": "user", "content": "verify"}])
        await verifier.complete([{"role": "user", "content": "verify"}], verifier_mode="analysis")
        structured = factory.for_structured_output("p")
        await structured.complete([{"role": "user", "content": "json"}], response_format={"type": "json_object"})
        self.assertEqual(seen[0]["response_format"] if "response_format" in seen[0] else None, None)
        self.assertEqual(seen[1]["response_format"]["type"], "json_object")

    async def test_cancellation_vision_and_redirect_fail_closed(self):
        gateway = CloudGateway(self.store, transport=httpx.MockTransport(self.success_transport), resolver=fake_resolver)
        cancelled = asyncio.Event()
        cancelled.set()
        with self.assertRaises(GatewayCancelled):
            await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}], cancellation=cancelled)
        with self.assertRaises(GatewayPolicyError):
            await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/x"}}]}])
        redirect = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test"}, request=request))
        gateway = CloudGateway(self.store, transport=redirect, resolver=fake_resolver)
        with self.assertRaises(GatewayResponseError):
            await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}])

    async def test_response_limit_and_request_dns_policy(self):
        huge = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 20, request=request))
        from gitd.services.cloud_gateway import GatewayLimits
        gateway = CloudGateway(self.store, transport=huge, resolver=fake_resolver, limits=GatewayLimits(max_response_bytes=5))
        with self.assertRaises(GatewayResponseError):
            await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}])
        with patch.object(self.store, "reserve", side_effect=Exception("should not reach")):
            # A canceled operation is checked before budget reservation/provider access.
            event = asyncio.Event(); event.set()
            with self.assertRaises(GatewayCancelled):
                await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}], cancellation=event)

    async def test_normalizes_openai_text_parts_and_single_tool_call(self):
        responses = [
            {"choices": [{"message": {"content": [{"type": "text", "text": "### Thought"}, {"type": "text", "text": "ready"}]}}]},
            {"id": "tool-1", "choices": [{"message": {
                "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "press_home", "arguments": '{"reason":"return"}'
                }}],
            }}], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
        ]

        def handler(request):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(responses.pop(0)).encode(), request=request)

        gateway = CloudGateway(self.store, transport=httpx.MockTransport(handler), resolver=fake_resolver)
        text_result = await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(text_result.content, "### Thought\nready")
        tool_result = await gateway.invoke("p", role="executor", component="executor", messages=[{"role": "user", "content": "x"}])
        self.assertIn('### Action\n{"action":"press_home","reason":"return"}', tool_result.content)
        self.assertEqual(tool_result.envelope.output_tokens, 7)

    async def test_sdk_adapter_returns_pinned_chatresponse_shape_and_history(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            body = {"id": "sdk-1", "choices": [{"message": {"content": "sdk text"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(body).encode(), request=request)

        gateway = CloudGateway(self.store, transport=httpx.MockTransport(handler), resolver=fake_resolver)
        llm = build_sdk_llm(gateway, profile_id="p", role="executor", component="executor")
        response = await llm.achat([
            ChatMessage(role="system", blocks=[TextBlock(text="system instructions")]),
            ChatMessage(role="user", blocks=[TextBlock(text="user request")]),
        ])
        self.assertEqual(response.message.content, "sdk text")
        self.assertEqual(response.raw["usage"]["completion_tokens"], 2)
        self.assertEqual([m["role"] for m in seen[0]["messages"]], ["system", "user"])


if __name__ == "__main__":
    unittest.main()
