"""Private pipe client used only inside the isolated Mobilerun interpreter."""
from __future__ import annotations
import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any, Mapping
from ai_contracts import JobDetail, Capability, Authorization
from gitd.services.sdk_worker_protocol import WorkerRequest, WorkerResponse


class RemoteRejected(RuntimeError):
    pass


class WorkerPipe:
    def __init__(self, bootstrap: Mapping[str, Any], incoming: Any, outgoing: Any):
        self.bootstrap, self.incoming, self.outgoing = bootstrap, incoming, outgoing
        self.sequence = 0

    def call(self, kind: str, payload: Mapping[str, Any], role: str = "executor") -> Mapping[str, Any]:
        self.sequence += 1
        b = self.bootstrap
        request = WorkerRequest(request_id=f"call-{self.sequence}", nonce=b["nonce"], kind=kind,
            job_id=b["job"]["job_id"], child_id=b["child_id"], serial=b["serial"], fence=b["fence"], role=role, payload=payload)
        self.outgoing.write(request.as_line() + "\n")
        self.outgoing.flush()
        raw = self.incoming.readline(256 * 1024 + 1)
        if not raw or len(raw.encode("utf-8")) > 256 * 1024:
            raise RemoteRejected("parent pipe disconnected or exceeded bounds")
        response = WorkerResponse.from_line(raw)
        if (response.nonce, response.request_id, response.child_id, response.fence) != (request.nonce, request.request_id, request.child_id, request.fence):
            raise RemoteRejected("parent response identity mismatch")
        if response.kind == "error":
            raise RemoteRejected("owner policy rejected the SDK operation")
        return response.payload


class RemoteStore:
    def __init__(self, pipe: WorkerPipe):
        self.pipe = pipe
    def authorize(self, child_id: str, serial: str, capability: Capability, fence: int) -> Authorization:
        if (child_id, serial, fence) != (self.pipe.bootstrap["child_id"], self.pipe.bootstrap["serial"], self.pipe.bootstrap["fence"]):
            raise RemoteRejected("SDK authorization target drift")
        return Authorization.model_validate(self.pipe.call("authorize", {"capability": capability.value}))


class RemoteState:
    requires_coordinate_tools = False
    screenshot_matches_input_coords = False
    resize_model_screenshot = False
    def __init__(self, pipe: WorkerPipe):
        self.pipe = pipe
    async def get_state(self):
        from gitd.services.native_observation import canonical_from_public, to_sdk_ui_state
        value = self.pipe.call("observe", {})
        return to_sdk_ui_state(canonical_from_public(value))
    async def get_final_state(self):
        return await self.get_state()


class RemoteBridge:
    def __init__(self, pipe: WorkerPipe):
        self.pipe = pipe
    def driver_for(self, **kwargs):
        return self
    def state_provider_for(self, **kwargs):
        return RemoteState(self.pipe)
    async def authorized_driver_call(self, *, serial, capability, fence, operation, args, kwargs):
        if args:
            raise RemoteRejected("SDK driver requires normalized native arguments")
        if (serial, fence) != (self.pipe.bootstrap["serial"], self.pipe.bootstrap["fence"]):
            raise RemoteRejected("SDK driver target drift")
        return self.pipe.call("action", {"driver_operation": operation, "params": kwargs, "capability": capability.value})
    def register_mobilerun_tools(self, *, serial, capability_authorizer, cancelled):
        if serial != self.pipe.bootstrap["serial"]:
            raise RemoteRejected("SDK tools target drift")
        result = {}
        for name, spec in self.pipe.bootstrap["tools"].items():
            def bind(tool_name, tool_spec):
                async def invoke(**kwargs):
                    kwargs.pop("ctx", None)
                    if cancelled():
                        raise RemoteRejected("SDK cancelled")
                    properties = tool_spec.get("parameters", {}).get("properties", {})
                    for field, metadata in properties.items():
                        if field in kwargs and isinstance(kwargs[field], str) and metadata.get("type") in {"object", "array"}:
                            kwargs[field] = json.loads(kwargs[field])
                    return self.pipe.call("action", {"tool": tool_name, "params": kwargs})
                return invoke
            result[name] = dict(spec, function=bind(name, spec))
        return result


class RemoteGateway:
    def __init__(self, pipe: WorkerPipe):
        self.pipe = pipe
    def estimate_reservation(self, profile_id, messages, max_tokens):
        # Parent ignores this placeholder and computes/reserves its own trusted
        # upper bound before any provider request. Worker cannot authorize spend.
        return "parent-reserved"
    async def invoke(self, profile_id, *, role, component, messages, max_tokens, **kwargs):
        from gitd.services.cloud_gateway import GatewayResult, UIEnvelope
        payload = {"profile_id": profile_id, "component": component, "messages": list(messages), "max_tokens": max_tokens}
        if kwargs.get("response_format") is not None:
            payload["response_format"] = kwargs["response_format"]
        value = self.pipe.call("llm", payload, role=role)
        envelope = UIEnvelope(**value["envelope"])
        return GatewayResult(content=value["content"], raw_json=None, envelope=envelope)


class RemoteProfiles:
    def __init__(self, pipe: WorkerPipe):
        self.pipe = pipe
    def build_llm(self, *, role, profile_id, component, redactor):
        from gitd.services.sdk_gateway_llm import build_sdk_llm
        bound = self.pipe.bootstrap["profile_metadata"].get(profile_id)
        if not bound or role not in bound["roles"]:
            raise RemoteRejected("SDK profile binding mismatch")
        return build_sdk_llm(RemoteGateway(self.pipe), profile_id=profile_id, role=role, component=component,
            model_name=bound["model"], max_output_tokens=bound["max_tokens"])


async def execute(bootstrap: Mapping[str, Any], incoming: Any, outgoing: Any) -> Mapping[str, Any]:
    from gitd.services.mobilerun_runtime import MobilerunRuntime, RuntimePolicy
    pipe = WorkerPipe(bootstrap, incoming, outgoing)
    job = JobDetail.model_validate(bootstrap["job"])
    original = next(c for c in job.children if c.child_id == bootstrap["child_id"])
    child = SimpleNamespace(**original.model_dump(), fence=bootstrap["fence"])
    # model_dump() converts target to dict; retain the actual typed target.
    child.target = original.target
    result = await MobilerunRuntime(policy=RuntimePolicy(reasoning=bootstrap["mode"] == "reasoning", max_turns=15, max_cloud_calls=30)).run_child(
        job, child, RemoteStore(pipe), RemoteBridge(pipe), RemoteProfiles(pipe), lambda: False)
    pipe.call("finish", {"success": result.get("success") is True, "summary": str(result.get("reason", "SDK finished"))[:240]})
    return result
