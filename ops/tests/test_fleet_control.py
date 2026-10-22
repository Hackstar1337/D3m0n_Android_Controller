"""Unittest coverage for the stage1 fleet backend.

All ADB-facing helpers are mocked; this suite never starts a subprocess or
requires a connected device.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

import ops.fleet_control as fleet_control


class FleetControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_state = os.environ.get("D3M0N_FLEET_STATE")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["D3M0N_FLEET_STATE"] = self._tmp.name
        fleet_control.reset_state_for_tests()

    async def asyncTearDown(self) -> None:
        fleet_control.reset_state_for_tests()
        if self._old_state is None:
            os.environ.pop("D3M0N_FLEET_STATE", None)
        else:
            os.environ["D3M0N_FLEET_STATE"] = self._old_state
        self._tmp.cleanup()

    def app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(fleet_control.router)
        return app

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_group_crud_casefold_dedup_and_persistence_reload(self) -> None:
        response = await self.request("POST", "/api/fleet/groups", json={"name": " Phones ", "devices": ["a", "a", "b"]})
        self.assertEqual(response.status_code, 200)
        group = response.json()["group"]
        self.assertEqual(group["name"], "Phones")
        self.assertEqual(group["devices"], ["a", "b"])

        duplicate = await self.request("POST", "/api/fleet/groups", json={"name": "pHoNeS", "devices": []})
        self.assertEqual(duplicate.status_code, 409)
        updated = await self.request("PATCH", f"/api/fleet/groups/{group['id']}", json={"devices": ["b", "c", "c"]})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["group"]["devices"], ["b", "c"])

        # Reloading the module models a process restart while retaining SQLite state.
        reloaded = importlib.reload(fleet_control)
        persisted_app = FastAPI()
        persisted_app.include_router(reloaded.router)
        transport = httpx.ASGITransport(app=persisted_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            persisted = await client.get("/api/fleet/groups")
            deleted = await client.delete(f"/api/fleet/groups/{group['id']}")
        self.assertEqual(persisted.status_code, 200)
        self.assertEqual(persisted.json()["groups"][0]["devices"], ["b", "c"])
        self.assertEqual(deleted.status_code, 200)

    async def test_group_and_action_input_limits(self) -> None:
        invalid_serial = await self.request("POST", "/api/fleet/groups", json={"name": "bad", "devices": ["ios"]})
        self.assertEqual(invalid_serial.status_code, 422)
        too_many = await self.request(
            "POST", "/api/fleet/groups", json={"name": "many", "devices": [f"d{i}" for i in range(51)]}
        )
        self.assertEqual(too_many.status_code, 422)
        empty = await self.request(
            "POST", "/api/fleet/actions", json={"targets": {"devices": [], "group_ids": [], "all": False}, "action": "key", "params": {"key": "HOME"}}
        )
        self.assertEqual(empty.status_code, 422)
        action_too_many = await self.request(
            "POST", "/api/fleet/actions", json={"targets": {"devices": [f"d{i}" for i in range(51)]}, "action": "key", "params": {"key": "HOME"}}
        )
        self.assertEqual(action_too_many.status_code, 422)

    async def test_origin_rejection_and_capabilities(self) -> None:
        response = await self.request(
            "POST",
            "/api/fleet/groups",
            headers={"Origin": "https://evil.example"},
            json={"name": "blocked", "devices": []},
        )
        self.assertEqual(response.status_code, 403)
        cross_site = await self.request(
            "POST",
            "/api/fleet/groups",
            headers={"Sec-Fetch-Site": "cross-site"},
            json={"name": "blocked2", "devices": []},
        )
        self.assertEqual(cross_site.status_code, 403)
        capabilities = await self.request("GET", "/api/fleet/capabilities")
        self.assertTrue(capabilities.json()["unicode"])
        self.assertTrue(capabilities.json()["files"])
        self.assertTrue(capabilities.json()["batch_install"])
        self.assertEqual(capabilities.json()["sync"], "best-effort")

    async def test_dry_run_is_device_free(self) -> None:
        executor = AsyncMock()
        fleet_control.action_executor = executor
        response = await self.request(
            "POST",
            "/api/fleet/actions",
            json={"targets": {"devices": ["preview-1", "preview-2"], "group_ids": [], "all": False}, "action": "key", "params": {"key": "HOME"}, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(all(row.get("dry_run") for row in response.json()["results"]))
        executor.assert_not_awaited()

    async def test_normalized_resolution_and_safe_ascii_quote(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def fake_run_adb(*args: str, timeout: float = 12.0) -> str:
            calls.append(args)
            if args[-2:] == ("wm", "size"):
                return "Physical size: 1080x1920\nOverride size: 720x1280\n"
            return ""

        fleet_control.run_adb = fake_run_adb
        fleet_control.reset_state_for_tests()
        await fleet_control.action_executor("device-1", "tap", {"x": 1.0, "y": 0.5})
        self.assertEqual(calls[-1][-3:], ("tap", "719", "640"))

        malicious = "hello; rm -rf / 'quoted'"
        params = fleet_control._validate_action("text", {"text": malicious})
        command = fleet_control.build_action_command("device-1", "text", params, (720, 1280))
        self.assertEqual(command[0:4], ("adb", "-s", "device-1", "shell"))
        self.assertIn("input text ", command[-1])
        self.assertNotEqual(command[-1], "input text " + malicious)
        self.assertEqual(fleet_control._validate_action("text", {"text": "café"})["text"], "café")
        with self.assertRaises(Exception):
            fleet_control._validate_action("text", {"text": "bad\0text"})

    async def test_overlap_offline_failure_and_failure_isolation(self) -> None:
        created = await self.request("POST", "/api/fleet/groups", json={"name": "one", "devices": ["offline", "shared"]})
        group_id = created.json()["group"]["id"]
        calls: list[str] = []

        async def executor(serial: str, action: str, params: dict) -> None:
            calls.append(serial)
            if serial == "offline":
                raise fleet_control.AdbCommandError("offline: device unavailable")
            await asyncio.sleep(0.01)

        fleet_control.action_executor = executor
        response = await self.request(
            "POST",
            "/api/fleet/actions",
            json={"targets": {"devices": ["shared"], "group_ids": [group_id], "all": False}, "action": "key", "params": {"key": "HOME"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(calls.count("shared"), 1)
        self.assertEqual(calls.count("offline"), 1)
        results = {row["device"]: row for row in response.json()["results"]}
        self.assertTrue(results["shared"]["ok"])
        self.assertFalse(results["offline"]["ok"])
        self.assertIn("unavailable", results["offline"]["error"])

    async def test_bounded_concurrent_fanout(self) -> None:
        active = 0
        maximum = 0
        lock = asyncio.Lock()

        async def executor(serial: str, action: str, params: dict) -> None:
            nonlocal active, maximum
            async with lock:
                active += 1
                maximum = max(maximum, active)
            await asyncio.sleep(0.015)
            async with lock:
                active -= 1

        fleet_control.action_executor = executor
        serials = [f"d{i}" for i in range(50)]
        response = await self.request(
            "POST",
            "/api/fleet/actions",
            json={"targets": {"devices": serials, "group_ids": [], "all": False}, "action": "key", "params": {"key": "HOME"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(response.json()["results"]), 50)
        self.assertLessEqual(maximum, 24)
        self.assertGreater(response.json()["dispatch_spread_ms"], 0)


if __name__ == "__main__":
    unittest.main()
