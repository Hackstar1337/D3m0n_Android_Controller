"""Focused regressions for the metadata-only agent activity status snapshot."""

import importlib.util
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
SERVICE_PATH = ROOT / "gitd" / "services" / "agent_chat.py"
ROUTER_PATH = ROOT / "gitd" / "routers" / "agent_chat.py"


def _install_dependency_stubs():
    """Install only the service/router dependencies; no phone or provider IO."""
    gitd = types.ModuleType("gitd")
    gitd.__path__ = [str(ROOT / "gitd")]
    bots = types.ModuleType("gitd.bots")
    bots.__path__ = []
    common = types.ModuleType("gitd.bots.common")
    common.__path__ = []
    device = types.ModuleType("gitd.bots.common.device")
    device.is_ios_ref = lambda value: str(value).startswith("ios:")
    services = types.ModuleType("gitd.services")
    services.__path__ = [str(ROOT / "gitd" / "services")]
    tools = types.ModuleType("gitd.services.agent_tools")
    tools.execute_tool = lambda *args, **kwargs: "stubbed"
    tools.get_screenshot_b64 = lambda *args, **kwargs: ""
    tools.tool_prompt_list = lambda values: ""
    tools.tools_for_device = lambda device_name: []
    context = types.ModuleType("gitd.services.device_context")
    context.get_screen_tree = lambda device_name: ""
    context.get_phone_state = lambda device_name: {}
    backoff = types.ModuleType("gitd.services.llm_backoff")
    backoff.backoff_stream = lambda fn: fn()
    backoff.effort_timeout = lambda model: 5
    config = types.ModuleType("gitd.config")
    config.settings = types.SimpleNamespace(ollama_base_url="http://127.0.0.1:11434")
    for name, module in {
        "gitd": gitd,
        "gitd.bots": bots,
        "gitd.bots.common": common,
        "gitd.bots.common.device": device,
        "gitd.services": services,
        "gitd.services.agent_tools": tools,
        "gitd.services.device_context": context,
        "gitd.services.llm_backoff": backoff,
        "gitd.config": config,
    }.items():
        sys.modules[name] = module

    # The production checkout normally supplies these.  Keep this test runnable
    # in the dependency-light review environment as well.
    if "fastapi" not in sys.modules:
        try:
            import fastapi  # noqa: F401
        except ImportError:
            fastapi = types.ModuleType("fastapi")

            class APIRouter:
                def __init__(self, *args, **kwargs):
                    pass

                def _route(self, *args, **kwargs):
                    return lambda function: function

                get = post = put = delete = _route

            class HTTPException(Exception):
                def __init__(self, status_code, detail=None, headers=None):
                    super().__init__(detail)
                    self.status_code = status_code
                    self.detail = detail
                    self.headers = headers or {}

            fastapi.APIRouter = APIRouter
            fastapi.Body = lambda default=None, **kwargs: default
            fastapi.HTTPException = HTTPException
            fastapi.Query = lambda default=None, **kwargs: default
            fastapi.Request = object
            sys.modules["fastapi"] = fastapi
    if "pydantic" not in sys.modules:
        try:
            import pydantic  # noqa: F401
        except ImportError:
            pydantic = types.ModuleType("pydantic")

            class BaseModel:
                def __init__(self, **values):
                    self.__dict__.update(values)

            pydantic.BaseModel = BaseModel
            sys.modules["pydantic"] = pydantic
    if "starlette.responses" not in sys.modules:
        try:
            from starlette.responses import JSONResponse  # noqa: F401
        except ImportError:
            starlette = types.ModuleType("starlette")
            responses = types.ModuleType("starlette.responses")

            class JSONResponse:
                def __init__(self, content, status_code=200, headers=None):
                    self.body = content
                    self.status_code = status_code
                    self.headers = {k.lower(): v for k, v in (headers or {}).items()}

            class StreamingResponse(JSONResponse):
                pass

            responses.JSONResponse = JSONResponse
            responses.StreamingResponse = StreamingResponse
            starlette.responses = responses
            sys.modules["starlette"] = starlette
            sys.modules["starlette.responses"] = responses


def _load_modules():
    _install_dependency_stubs()
    service_spec = importlib.util.spec_from_file_location("gitd.services.agent_chat", SERVICE_PATH)
    service = importlib.util.module_from_spec(service_spec)
    sys.modules[service_spec.name] = service
    service_spec.loader.exec_module(service)

    routers = types.ModuleType("gitd.routers")
    routers.__path__ = [str(ROOT / "gitd" / "routers")]
    sys.modules["gitd.routers"] = routers
    router_spec = importlib.util.spec_from_file_location("gitd.routers.agent_chat", ROUTER_PATH)
    router = importlib.util.module_from_spec(router_spec)
    sys.modules[router_spec.name] = router
    router_spec.loader.exec_module(router)
    return service, router


SERVICE, ROUTER = _load_modules()


class AgentActivityStatusTests(unittest.TestCase):
    def setUp(self):
        with SERVICE._sessions_lock:
            SERVICE._sessions.clear()

    def _session(self, device):
        return SERVICE.create_session(device, provider="ollama", model="test-model")

    def test_idle_and_loaded_sessions_never_report_running(self):
        idle = self._session("serial-idle")
        loaded = self._session("serial-loaded")
        rows = {row["id"]: row for row in SERVICE.list_sessions()}
        for session in (idle, loaded):
            self.assertFalse(rows[session.id]["running"])
            self.assertFalse(rows[session.id]["stopping"])

    def test_begin_end_and_immediate_stop_status(self):
        session = self._session("serial-run")
        SERVICE.begin_agent_run(session, run_id="run-1")
        row = next(row for row in SERVICE.list_sessions() if row["id"] == session.id)
        self.assertTrue(row["running"])
        self.assertFalse(row["stopping"])

        SERVICE.stop_agent(session.id, run_id="run-1")
        row = next(row for row in SERVICE.list_sessions() if row["id"] == session.id)
        self.assertFalse(row["running"])
        # Stop has already released the local run; provider computation is a separate limit.
        self.assertFalse(row["stopping"])
        self.assertFalse(session.run_lock.locked())

        SERVICE.end_agent_run(session)
        row = next(row for row in SERVICE.list_sessions() if row["id"] == session.id)
        self.assertFalse(row["running"])
        self.assertFalse(row["stopping"])

    def test_stale_stop_does_not_stop_current_run(self):
        session = self._session("serial-stale")
        SERVICE.begin_agent_run(session, run_id="old")
        SERVICE.end_agent_run(session)
        SERVICE.begin_agent_run(session, run_id="new")
        SERVICE.stop_agent(session.id, run_id="old")
        row = next(row for row in SERVICE.list_sessions() if row["id"] == session.id)
        self.assertTrue(row["running"])
        self.assertFalse(row["stopping"])
        SERVICE.end_agent_run(session)

    def test_multiple_sessions_same_serial_and_isolation(self):
        first = self._session("serial-shared")
        second = self._session("serial-shared")
        other = self._session("serial-other")
        SERVICE.begin_agent_run(first, run_id="first")
        SERVICE.begin_agent_run(other, run_id="other")
        rows = {row["id"]: row for row in SERVICE.list_sessions()}
        self.assertTrue(rows[first.id]["running"])
        self.assertFalse(rows[second.id]["running"])
        self.assertTrue(rows[other.id]["running"])
        SERVICE.stop_agent(first.id, run_id="first")
        rows = {row["id"]: row for row in SERVICE.list_sessions()}
        self.assertFalse(rows[first.id]["stopping"])
        self.assertFalse(first.run_lock.locked())
        self.assertTrue(rows[other.id]["running"])
        SERVICE.end_agent_run(first)
        SERVICE.end_agent_run(other)

    def test_status_rows_are_metadata_only(self):
        session = self._session("serial-private")
        session.messages.append(SERVICE.ChatMessage("user", "secret prompt"))
        row = SERVICE.list_sessions()[0]
        self.assertEqual(
            set(row), {"id", "device", "provider", "model", "messages", "running", "stopping"}
        )
        self.assertEqual(row["messages"], 1)
        self.assertNotIn("secret prompt", row.values())
        self.assertNotIn("api", row)
        self.assertNotIn("prompt", row)
        self.assertNotIn("history", row)

    def test_snapshot_survives_concurrent_create_and_delete(self):
        errors = []

        def churn():
            try:
                for index in range(250):
                    session = self._session(f"serial-{index % 4}")
                    SERVICE.delete_session(session.id)
            except Exception as exc:  # pragma: no cover - failure is asserted below
                errors.append(exc)

        worker = threading.Thread(target=churn)
        worker.start()
        while worker.is_alive():
            rows = SERVICE.snapshot_sessions()
            self.assertTrue(all({"id", "running", "stopping"}.issubset(row) for row in rows))
        worker.join()
        self.assertEqual(errors, [])

    def test_sessions_route_uses_existing_auth_and_no_store(self):
        calls = []
        request = object()
        with patch.object(ROUTER, "_ai_access", side_effect=lambda req: calls.append(req)):
            response = ROUTER.list_sessions(request)
        self.assertEqual(calls, [request])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        payload = json.loads(response.body) if isinstance(response.body, (bytes, str)) else response.body
        self.assertIsInstance(payload, list)
        self.assertTrue(all(isinstance(row.get("running"), bool) and isinstance(row.get("stopping"), bool) for row in payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
