"""Integration check (phone host only): the production app resolves phone input
routes to ops.fast_control before the upstream gitd handlers."""
from __future__ import annotations

import importlib.util
import unittest
from unittest import mock

HAS_GITD = importlib.util.find_spec("gitd") is not None


def load_app():
    """Import the production app; the static mount may point at a frontend that is not built yet
    (e.g. in a staging tree), which is irrelevant to route order."""
    from starlette import staticfiles
    original = staticfiles.StaticFiles.__init__

    def lenient(self, *args, **kwargs):
        kwargs["check_dir"] = False
        original(self, *args, **kwargs)

    with mock.patch.object(staticfiles.StaticFiles, "__init__", lenient):
        from ops.server import app
    return app


def scope_for(path, method):
    return {"type": "http", "path": path, "method": method, "root_path": "", "path_params": {},
            "query_string": b"", "headers": []}


def endpoint_for(route, scope):
    """Endpoint a matched route dispatches to. FastAPI >= 0.150 wraps included routers in
    _IncludedRouter (children under .original_router); older versions flatten APIRoutes."""
    from starlette.routing import Match
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        return endpoint
    nested = getattr(route, "routes", None)
    if nested is None:
        nested = getattr(getattr(route, "original_router", None), "routes", None)
    for child in nested or []:
        match, _ = child.matches(scope)
        if match == Match.FULL:
            found = endpoint_for(child, scope)
            if found is not None:
                return found
    return None


def first_match(app, path, method):
    """(top-level index, matched route) of the first full match, i.e. what Starlette dispatches to."""
    from starlette.routing import Match
    scope = scope_for(path, method)
    for index, route in enumerate(app.router.routes):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return index, route
    return None, None


@unittest.skipUnless(HAS_GITD, "gitd is only installed on the phone host")
class ServerRouteOrderTests(unittest.TestCase):
    def test_phone_input_routes_resolve_to_fast_control(self):
        app = load_app()
        expected = {
            "/api/phone/tap": "fast_tap", "/api/phone/swipe": "fast_swipe",
            "/api/phone/key": "fast_key", "/api/phone/input": "fast_input",
        }
        fast_indexes = set()
        for path, name in expected.items():
            index, route = first_match(app, path, "POST")
            self.assertIsNotNone(route, path)
            endpoint = endpoint_for(route, scope_for(path, "POST"))
            self.assertIsNotNone(endpoint, path)
            self.assertEqual(endpoint.__name__, name, path)
            self.assertEqual(endpoint.__module__, "ops.fast_control", path)
            fast_indexes.add(index)
        index, route = first_match(app, "/api/phone/control/status", "GET")
        self.assertEqual(endpoint_for(route, scope_for("/api/phone/control/status", "GET")).__name__, "control_status")
        # Other upstream phone routes stay with gitd.
        index, launch = first_match(app, "/api/phone/launch", "POST")
        self.assertIsNotNone(launch)
        self.assertNotIn(index, fast_indexes)
        endpoint = endpoint_for(launch, scope_for("/api/phone/launch", "POST"))
        if endpoint is not None:
            self.assertNotEqual(endpoint.__module__, "ops.fast_control")


if __name__ == "__main__":
    unittest.main()
