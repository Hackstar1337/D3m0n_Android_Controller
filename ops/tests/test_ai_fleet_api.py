from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_contracts import ProfileRef, Role
from ops.ai_fleet_api import router
from ops.ai_fleet_controller import FleetController, set_controller
from ops.ai_fleet_jobs import JobStore


class LocalAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope["d3m0n_auth"] = "local"
        await self.app(scope, receive, send)


class FakeRuntime:
    def status(self):
        return {"state": "configured", "activation": "disabled", "configured": True, "ready": False}

    def capabilities(self):
        return [{"name": "observe", "available": True, "state": "configured", "reason": "fake observer"}]


class FakeProfiles:
    def __init__(self):
        self.data = {
            "fake-profile": {
                "profile_id": "fake-profile",
                "provider": "fake",
                "model": "fake-model",
                "endpoint_origin": "https://fake.invalid",
                "configured": True,
                "expires_at": None,
                "capabilities": ["text"],
                "roles": ["planner", "executor", "verifier"],
                "version": 1,
            }
        }

    def list_profiles(self):
        return list(self.data.values())

    def get_profile(self, profile_id):
        return self.data.get(profile_id)

    def create_profile(self, data):
        self.data[data["profile_id"]] = dict(data, configured=False)
        return self.data[data["profile_id"]]

    def update_profile(self, profile_id, data):
        if profile_id not in self.data:
            raise RuntimeError("profile not found")
        self.data[profile_id].update(data)
        return self.data[profile_id]

    def delete_profile(self, profile_id):
        self.data.pop(profile_id, None)

    def role_bindings(self):
        return {"planner": "fake-profile", "executor": "fake-profile", "verifier": "fake-profile"}

    def readiness(self, profile_id=None):
        return {"profile_id": profile_id, "ready": bool(profile_id is None or profile_id in self.data), "key_configured": False}


class FleetApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = FleetController(
            store=JobStore(Path(self.tmp.name) / "jobs.sqlite3"),
            profiles=FakeProfiles(),
            runtime=FakeRuntime(),
        )
        set_controller(self.controller)
        app = FastAPI()
        app.include_router(router)
        app.add_middleware(LocalAuth)
        self.client = TestClient(app)

    def tearDown(self):
        set_controller(None)
        self.tmp.cleanup()

    @staticmethod
    def _plan_body(count=1):
        return {
            "intent": {"text": "Open Settings and return Home", "metadata": {"api_key": "never-public"}},
            "targets": [
                {"target_id": f"phone-{i}", "serial": f"fake-{i}", "label": f"Phone {i}", "online": True}
                for i in range(1, count + 1)
            ],
            "profiles": [{"profile_id": "fake-profile", "provider": "fake", "model": "fake-model", "endpoint_origin": "https://fake.invalid", "configured": True}],
            "role_bindings": {"planner": "fake-profile", "executor": "fake-profile", "verifier": "fake-profile"},
            "idempotency_key": "plan-unit-key-123456",
        }

    def test_status_is_disabled_and_capabilities_fail_closed(self):
        response = self.client.get("/api/ai-fleet/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["activation"], "disabled")
        self.assertFalse(body["runtime"]["ready"])
        capabilities = self.client.get("/api/ai-fleet/capabilities").json()["capabilities"]
        self.assertTrue(capabilities)
        self.assertTrue(all(item["available"] is False for item in capabilities))

    def test_plan_approval_jobs_and_controls_are_durable(self):
        created = self.client.post("/api/ai-fleet/plans", json=self._plan_body(2))
        self.assertEqual(created.status_code, 201)
        payload = created.json()
        token = payload["approval_token"]
        plan_id = payload["plan"]["plan_id"]
        approved = self.client.post(
            f"/api/ai-fleet/plans/{plan_id}/approve",
            json={"approval_token": token, "idempotency_key": "approve-unit-key-123"},
        )
        self.assertEqual(approved.status_code, 200)
        job = approved.json()
        self.assertEqual(job["target_count"], 2)
        self.assertNotIn("approval_token", json.dumps(job))
        listed = self.client.get("/api/ai-fleet/jobs?limit=10")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["jobs"]), 1)
        paused = self.client.post(f"/api/ai-fleet/jobs/{job['job_id']}/actions", json={"action": "pause"})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["state"], "paused")
        events = self.client.get(f"/api/ai-fleet/jobs/{job['job_id']}/events?limit=10")
        self.assertEqual(events.status_code, 200)
        self.assertTrue(events.json()["bounded"])

    def test_cross_site_and_secret_profile_fields_are_rejected(self):
        response = self.client.get("/api/ai-fleet/status", headers={"origin": "https://evil.invalid"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/api/ai-fleet/profiles",
            json={"profile_id": "bad", "provider": "fake", "model": "m", "endpoint_origin": "https://fake.invalid", "api_key": "secret"},
        )
        self.assertEqual(response.status_code, 422)

    def test_profile_metadata_never_exposes_secret_like_values(self):
        listed = self.client.get("/api/ai-fleet/profiles")
        self.assertEqual(listed.status_code, 200)
        text = listed.text
        self.assertNotIn("api_key", text)
        readiness = self.client.get("/api/ai-fleet/profiles/fake-profile/readiness")
        self.assertEqual(readiness.status_code, 200)
        self.assertNotIn("secret", readiness.text.lower())

    def test_unconfigured_or_expired_profile_cannot_create_activation_plan(self):
        body = self._plan_body()
        body["profiles"][0]["configured"] = False
        self.assertEqual(self.client.post("/api/ai-fleet/plans", json=body).status_code, 409)
        body = self._plan_body()
        body["profiles"][0]["expires_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(self.client.post("/api/ai-fleet/plans", json=body).status_code, 409)


if __name__ == "__main__":
    unittest.main()
