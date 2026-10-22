from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from ai_contracts import Capability, StepOutcome  # noqa: E402
from gitd.services.mobilerun_runtime import (  # noqa: E402
    MobilerunRuntime,
    PINNED_COMMIT,
    PINNED_VERSION,
    RuntimePolicy,
    RuntimeCallbacks,
    RuntimeUnavailable,
    _SdkFastAgentLLM,
    _adapt_tool_specs,
    _native_calls_as_xml,
    redact_for_cloud,
    sdk_readiness,
)
from ops.ai_fleet_jobs import JobStore  # noqa: E402
from ops.ai_worker_entry import run_worker_once  # noqa: E402


class FakeBridge:
    def driver_for(self, **kwargs):
        return object()

    def state_provider_for(self, **kwargs):
        return object()

    def register_mobilerun_tools(self, **kwargs):
        return {
            "observe": {
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                "description": "Read a redacted state observation.",
                "function": self.observe,
            }
        }

    async def observe(self, *, ctx):
        return "observation"

    def authorized_driver_call(self, **kwargs):
        raise AssertionError("fake contract must not call a native operation")


class RuntimeContractTests(unittest.TestCase):
    def test_redaction_is_recursive_and_never_keeps_credential_like_values(self):
        value = {
            "screen_text": "Welcome",
            "api_key": "sk-test-secret-value",
            "nested": ["Bearer very-secret-token", {"password": "hidden"}],
            "count": 3,
        }
        cleaned = redact_for_cloud(value)
        self.assertEqual(cleaned["api_key"], "[redacted]")
        self.assertEqual(cleaned["nested"][0], "[redacted]")
        self.assertEqual(cleaned["nested"][1]["password"], "[redacted]")
        self.assertEqual(cleaned["count"], 3)

    def test_policy_rejects_vision_until_explicitly_supported(self):
        with self.assertRaises(ValueError):
            RuntimePolicy(vision=True)
        policy = RuntimePolicy(max_turns=2, max_cloud_calls=4)
        self.assertEqual(policy.max_turns, 2)

    def test_readiness_reports_pinned_identity_and_does_not_fake_availability(self):
        readiness = sdk_readiness()
        self.assertEqual(readiness.sdk_identity, f"mobilerun@{PINNED_COMMIT}")
        if readiness.ready:
            self.assertEqual(readiness.sdk_version, PINNED_VERSION)
        else:
            self.assertTrue(readiness.missing)

    def test_manifest_is_explicitly_disabled_and_pinned(self):
        manifest_path = ROOT / "gitd" / "services" / "mobilerun_runtime_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["commit"], PINNED_COMMIT)
        self.assertEqual(manifest["version"], PINNED_VERSION)
        self.assertFalse(manifest["auto_setup"])
        self.assertFalse(manifest["telemetry"])
        self.assertFalse(manifest["tracing"])
        self.assertEqual(manifest["trajectory"], "none")

    def test_worker_cancellation_happens_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "isolated.sqlite3")
            result = asyncio.run(run_worker_once(store, "fake-worker", FakeBridge(), {}, lambda: True))
        self.assertIsNone(result.child_id)
        self.assertEqual(result.summary, "worker cancelled before claim")

    def test_fake_native_tool_shape_is_closed(self):
        specs = FakeBridge().register_mobilerun_tools()
        self.assertIn("observe", specs)
        self.assertTrue(callable(specs["observe"]["function"]))
        self.assertNotIn("shell", specs)
        self.assertEqual(Capability.OBSERVE.value, "observe")

    def test_realistic_provider_native_call_is_normalized_to_sdk_xml(self):
        response = SimpleNamespace(
            message=SimpleNamespace(
                content=None,
                blocks=[],
                additional_kwargs={
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "input",
                            "arguments": '{"action":"tap","params":{"x":10,"y":20}}',
                        },
                    }]
                },
            ),
            raw={},
            additional_kwargs={},
        )
        xml = _native_calls_as_xml(response, frozenset({"input", "complete"}))
        self.assertEqual(
            xml,
            '<function_calls>\n<invoke name="input">\n'
            '<parameter name="action">tap</parameter>\n'
            '<parameter name="params">{"x":10,"y":20}</parameter>\n'
            '</invoke>\n</function_calls>',
        )

        class Provider:
            async def achat(self, messages, **kwargs):
                response.message.content = "I will inspect the device first."
                return response

        normalized = asyncio.run(
            _SdkFastAgentLLM(Provider(), frozenset({"input", "complete"})).achat([])
        )
        self.assertTrue(normalized.message.content.startswith("<function_calls>"))

    def test_normalizer_rejects_unknown_or_unbounded_native_output(self):
        unknown = SimpleNamespace(
            message=SimpleNamespace(content=None, additional_kwargs={
                "tool_calls": [{"function": {"name": "shell", "arguments": "{}"}}]
            })
        )
        self.assertIsNone(_native_calls_as_xml(unknown, frozenset({"observe"})))
        oversized = SimpleNamespace(
            message=SimpleNamespace(content=None, additional_kwargs={
                "tool_calls": [{"function": {"name": "observe", "arguments": "{}"}}] * 9
            })
        )
        self.assertIsNone(_native_calls_as_xml(oversized, frozenset({"observe"})))

    def test_empty_invalid_provider_output_fails_closed_in_adapter(self):
        class InvalidProvider:
            async def achat(self, messages, **kwargs):
                return SimpleNamespace(message=SimpleNamespace(content=None, additional_kwargs={}))

        with self.assertRaises(ValueError):
            asyncio.run(_SdkFastAgentLLM(InvalidProvider(), frozenset({"observe"})).achat([]))

    def test_xml_object_parameter_is_decoded_before_typed_native_wrapper(self):
        seen = {}

        async def input_tool(*, action, params):
            seen.update(action=action, params=params)
            return "native input accepted"

        adapted = _adapt_tool_specs({
            "input": {
                "description": "typed input",
                "parameters": {
                    "type": "object",
                    "required": ["action", "params"],
                    "properties": {
                        "action": {"type": "string"},
                        "params": {"type": "object"},
                    },
                },
                "function": input_tool,
            }
        })
        result = asyncio.run(adapted["input"]["function"](
            action="tap", params='{"x":10,"y":20}'
        ))
        self.assertEqual(result, "native input accepted")
        self.assertEqual(seen, {"action": "tap", "params": {"x": 10, "y": 20}})

    def test_native_tool_event_receipt_keeps_native_capability(self):
        receipts = []
        runtime = MobilerunRuntime(callbacks=RuntimeCallbacks(on_receipt=receipts.append))
        asyncio.run(runtime._emit_event(
            SimpleNamespace(tool_name="input", success=True, summary="input accepted"),
            SimpleNamespace(child_id="child-1"), "serial-1", False,
        ))
        self.assertEqual(receipts[0].capability, Capability.INPUT)
        self.assertEqual(receipts[0].outcome, StepOutcome.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
