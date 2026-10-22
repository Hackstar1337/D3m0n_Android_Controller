import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
SERVICE_PATH = ROOT / "gitd" / "services" / "agent_chat.py"
ROUTER_PATH = ROOT / "gitd" / "routers" / "agent_chat.py"


def install_stubs():
    gitd = types.ModuleType("gitd"); gitd.__path__ = [str(ROOT / "gitd")]
    bots = types.ModuleType("gitd.bots"); bots.__path__ = []
    common = types.ModuleType("gitd.bots.common"); common.__path__ = []
    device = types.ModuleType("gitd.bots.common.device")
    device.is_ios_ref = lambda value: str(value).startswith("ios:")
    services = types.ModuleType("gitd.services"); services.__path__ = [str(ROOT / "gitd" / "services")]
    tools = types.ModuleType("gitd.services.agent_tools")
    tools.TOOL_CALLS = []
    tools.execute_tool = lambda name, args: tools.TOOL_CALLS.append((name, args)) or "tool-ok"
    tools.get_screenshot_b64 = lambda device: "image-b64"
    tools.tool_prompt_list = lambda values: "screenshot(device)"
    tools.tools_for_device = lambda device: [{"name": "screenshot", "description": "shot", "input_schema": {"type": "object", "properties": {"device": {"type": "string"}}}}]
    context = types.ModuleType("gitd.services.device_context")
    context.get_screen_tree = lambda device: "tree"
    context.get_phone_state = lambda device: {"currentApp": "mock"}
    backoff = types.ModuleType("gitd.services.llm_backoff")
    backoff.backoff_stream = lambda fn, **kwargs: [{"__result__": fn()}]
    backoff.effort_timeout = lambda model: 5
    config = types.ModuleType("gitd.config")
    config.settings = types.SimpleNamespace(ollama_base_url="http://ollama.test", default_provider="claude-code")
    for name, module in {
        "gitd": gitd, "gitd.bots": bots, "gitd.bots.common": common,
        "gitd.bots.common.device": device, "gitd.services": services,
        "gitd.services.agent_tools": tools, "gitd.services.device_context": context,
        "gitd.services.llm_backoff": backoff, "gitd.config": config,
    }.items():
        sys.modules[name] = module
    return tools


class Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.text = text
        self.closed = False
    def json(self): return self._data
    def close(self): self.closed = True
    def iter_lines(self, decode_unicode=True): return []


class PullResponse(Response):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames
    def iter_lines(self, decode_unicode=True):
        return [json.dumps(frame) for frame in self.frames]


class FakeRequests(types.ModuleType):
    class ConnectionError(Exception): pass
    def __init__(self):
        super().__init__("requests")
        self.chat_count = 0
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if url.endswith("/api/show"):
            return Response(data={"capabilities": ["completion", "tools", "vision"]})
        if url.endswith("/api/pull"):
            return PullResponse([{"status": "pulling", "completed": 5, "total": 10}, {"status": "success"}])
        return Response(data={})
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/api/show"):
            return Response(data={"capabilities": ["completion", "tools", "vision"]})
        if url.endswith("/api/chat"):
            self.chat_count += 1
            if self.chat_count == 1:
                return Response(data={"message": {"content": "", "tool_calls": [{"id": "call-1", "function": {"name": "screenshot", "arguments": "{}"}}]}})
            return Response(data={"message": {"content": "finished"}})
        if url.endswith("/api/tags"):
            return Response(data={"models": [{"name": "qwen3.5:4b", "size": 1000000000, "details": {"parameter_size": "4B"}}]})
        if url.endswith("/api/ps"):
            return Response(data={"models": [{"name": "qwen3.5:4b", "size_vram": 900000000}]})
        return Response(data={})


class AgentConsoleBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = install_stubs()
        cls.requests = FakeRequests()
        sys.modules["requests"] = cls.requests
        spec = importlib.util.spec_from_file_location("gitd.services.agent_chat", SERVICE_PATH)
        cls.service = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.service
        spec.loader.exec_module(cls.service)
        routers = types.ModuleType("gitd.routers"); routers.__path__ = [str(ROOT / "gitd" / "routers")]
        sys.modules["gitd.routers"] = routers
        router_spec = importlib.util.spec_from_file_location("gitd.routers.agent_chat", ROUTER_PATH)
        cls.router = importlib.util.module_from_spec(router_spec)
        sys.modules[router_spec.name] = cls.router
        router_spec.loader.exec_module(cls.router)

    def setUp(self):
        self.service._sessions.clear()
        self.service._active_http_responses.clear()
        self.tools.TOOL_CALLS.clear()
        self.requests.chat_count = 0
        self.requests.calls.clear()
        # Production now owns abortable native HTTP connections. Patch only its
        # transport seam to retain these existing requests-shaped fixtures.
        transport = patch.object(self.service, "_ollama_http_request",
                                 side_effect=lambda handle, method, url, **kw: self.requests.request(method, url, **kw))
        transport.start()
        self.addCleanup(transport.stop)

    def test_native_tools_and_actual_vision_attachment(self):
        session = self.service.create_session("android-1", provider="ollama", model="qwen3.5:4b")
        events = list(self.service._chat_ollama(session, "inspect"))
        self.assertIn({"type": "tool_call", "name": "screenshot", "args": {"device": "android-1"}}, events)
        self.assertEqual(self.tools.TOOL_CALLS, [("screenshot", {"device": "android-1"})])
        self.assertFalse(any("Tool error" in event.get("result", "") for event in events))
        self.assertTrue(any(event.get("type") == "screenshot" and event.get("image") == "image-b64" for event in events))
        chat_payloads = [call[2]["json"] for call in self.requests.calls if call[1].endswith("/api/chat")]
        self.assertEqual(chat_payloads[0]["tools"][0]["function"]["name"], "screenshot")
        self.assertEqual(chat_payloads[0]["messages"][-1]["images"], ["image-b64"])
        self.assertEqual(session.messages[-1].content, "finished")

    def test_bounded_history_preserves_native_pair_and_omits_old_images(self):
        session = self.service.create_session("android-1", provider="ollama", model="qwen3.5:4b")
        session.messages = [
            self.service.ChatMessage("user", "old"),
            self.service.ChatMessage("assistant", "thinking"),
            self.service.ChatMessage("tool_call", "", "screenshot", {"device": "android-1"}, "pair-1", "old-image"),
            self.service.ChatMessage("tool_result", "result", "screenshot", {}, "pair-1", "old-image"),
            self.service.ChatMessage("user", "new"),
        ]
        history = self.service._ollama_history(session, "system", "new", include_images=False)
        serialized = json.dumps(history)
        self.assertIn("pair-1", serialized)
        self.assertNotIn("old-image", serialized)
        self.assertEqual(sum(1 for item in history if item.get("role") == "tool"), 1)

    def test_stop_is_session_scoped_and_tool_device_is_guarded(self):
        first = self.service.create_session("android-1", provider="ollama")
        second = self.service.create_session("android-2", provider="ollama")
        self.service.stop_agent(first.id)
        self.assertTrue(first.cancel_event.is_set())
        self.assertFalse(second.cancel_event.is_set())
        with self.assertRaises(ValueError):
            self.service._dispatch_tool(first, "screenshot", {"device": "android-2"})
        self.assertEqual(self.tools.TOOL_CALLS, [])
        with self.assertRaises(RuntimeError):
            self.service._dispatch_tool(first, "screenshot", {"device": "android-1"})

    def test_one_active_run_guard(self):
        session = self.service.create_session("android-1", provider="ollama")
        self.service.begin_agent_run(session)
        with self.assertRaises(RuntimeError):
            self.service.begin_agent_run(session)
        self.service.end_agent_run(session)

    def test_status_is_live_and_pull_is_asynchronous(self):
        status = self.router.ollama_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["models"][0]["status"], "loaded")
        self.assertEqual(status["models"][0]["capabilities"], ["completion", "tools", "vision"])
        started = self.router.ollama_pull(self.router.OllamaModelRequest(model="demo:1b"))
        self.assertEqual(started["status"], "pulling")
        for _ in range(20):
            state = self.router.ollama_pull_status("demo:1b")
            if state["status"] == "done":
                break
            time.sleep(0.01)
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["percent"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
