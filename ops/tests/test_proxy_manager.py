"""Unit tests for proxy_manager; no ADB, phones, network, or live services are used."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import HTTPException
from starlette.requests import Request

MODULE_PATH = Path(__file__).resolve().parents[1] / "proxy_manager.py"
if not MODULE_PATH.is_file(): MODULE_PATH = Path(__file__).with_name("proxy_manager.py")
_spec = importlib.util.spec_from_file_location("proxy_manager", MODULE_PATH)
assert _spec and _spec.loader
pm = importlib.util.module_from_spec(_spec)
sys.modules["proxy_manager"] = pm
_spec.loader.exec_module(pm)


class FakeDriver:
    def __init__(self) -> None:
        self.tests: list[str] = []
        self.applies: list[str] = []
        self.disables: list[str] = []

    async def test(self, config):
        self.tests.append(config["serial"])
        return {"ok": True, "status": "server_preflight", "message": "authenticated", "exit_ip": "203.0.113.10", "verified_device": False}

    async def apply(self, serial, config):
        self.applies.append(serial)
        return {"ok": True, "status": "device_verified", "message": "routing active", "exit_ip": "203.0.113.10", "verified_device": True}

    async def disable(self, serial):
        self.disables.append(serial)
        return {"ok": True, "status": "disabled", "message": "normal internet"}


class ProxyManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.test_store = pm.ProxyStore(Path(self.temp.name) / "state")
        pm.set_store(self.test_store)
        pm.inventory_provider = lambda: [{"serial": "phone-a", "label": "A", "online": True}, "phone-live"]
        pm.driver_factory = lambda: self.driver
        self.driver = FakeDriver()
        pm.reset_state_for_tests()

    def tearDown(self) -> None:
        pm.inventory_provider = None
        pm.driver_factory = None
        pm.set_store(pm.ProxyStore(Path(self.temp.name) / "reset-state"))
        self.temp.cleanup()

    @staticmethod
    def request(body=None, headers=None, auth="session") -> Request:
        raw = b"" if body is None else __import__("json").dumps(body).encode()
        scope = {"type": "http", "method": "POST", "path": "/", "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()], "d3m0n_auth": auth}
        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        return Request(scope, receive)

    def admin_headers(self) -> dict[str, str]:
        return {"origin": "http://localhost:5055"}

    def save_direct(self, serial="phone-a", username="alice", password=("sec" + "ret")):
        return self.test_store.save({"serial": serial, "label": serial, "proxy_type": "http", "host": "proxy.example", "port": 8080, "username": username, "password": password})

    def test_credentials_are_encrypted_and_public_shape_has_no_secrets(self):
        public = self.save_direct()
        self.assertTrue(public["username_set"])
        self.assertTrue(public["password_set"])
        self.assertNotIn("username", public)
        self.assertNotIn("password", public)
        raw_db = Path(self.test_store.db_file).read_bytes()
        self.assertNotIn(b"alice", raw_db)
        self.assertNotIn(b"secret", raw_db)
        self.assertEqual(self.test_store.get_config("phone-a")["password"], "secret")
        import os
        if os.name == "nt":
            from ops.windows_security import owner_only
            self.assertTrue(owner_only(Path(self.test_store.state_dir)))
            self.assertTrue(owner_only(Path(self.test_store.key_file)))
        else:
            self.assertEqual((Path(self.test_store.state_dir).stat().st_mode & 0o777), 0o700)
            self.assertEqual((Path(self.test_store.key_file).stat().st_mode & 0o777), 0o600)

    def test_null_preserves_credentials_and_empty_clears_them(self):
        self.save_direct()
        data = {"serial": "phone-a", "label": "renamed", "proxy_type": "http", "host": "proxy.example", "port": 8081, "username": None, "password": None}
        self.test_store.save(data)
        self.assertEqual(self.test_store.get_config("phone-a")["password"], "secret")
        data["password"] = ""
        self.test_store.save(data)
        # An explicit empty value clears the saved credential instead of storing empty Basic auth.
        self.assertIsNone(self.test_store.get_config("phone-a")["password"])
        self.assertFalse(self.test_store.get_public("phone-a")["password_set"])
        self.assertEqual(self.test_store.get_config("phone-a")["username"], "alice")
        data["username"] = ""
        self.test_store.save(data)
        self.assertFalse(self.test_store.get_public("phone-a")["username_set"])

    async def test_routing_summary_reports_applied_exit_ip_without_secrets(self):
        self.save_direct()
        self.save_direct(serial="phone-b")
        self.test_store.update_runtime("phone-a", desired="proxy", applied="proxy",
                                       result={"ok": True, "status": "device_verified", "message": "ok", "exit_ip": "203.0.113.10", "verified_device": True})
        summary = await pm.routing()
        self.assertEqual(summary["total"], 2)
        a, b = summary["devices"]["phone-a"], summary["devices"]["phone-b"]
        self.assertEqual((a["mode"], a["exit_ip"], a["verified"]), ("proxy", "203.0.113.10", True))
        self.assertEqual((b["mode"], b["exit_ip"], b["verified"]), ("normal", None, False))
        dumped = __import__("json").dumps(summary)
        for secret in ("alice", "secret", "proxy.example", "8080"):
            self.assertNotIn(secret, dumped)
        route = next(r for r in pm.router.routes if getattr(r, "path", "") == "/api/proxy/routing")
        self.assertTrue(route.dependencies, "routing must require the dashboard session")

    async def wait_job(self, job_id):
        for _ in range(100):
            await asyncio.sleep(0.005)
            if pm._jobs[job_id]["status"] in {"completed", "failed"}:
                return

    async def test_recent_jobs_are_listed_newest_first_without_credentials(self):
        self.save_direct()
        first = (await pm.create_job(self.request({"action": "test", "devices": ["phone-a"], "confirmed": False}, self.admin_headers())))["job"]["id"]
        await self.wait_job(first)
        await asyncio.sleep(0.01)
        second = (await pm.create_job(self.request({"action": "test", "devices": ["phone-a"], "confirmed": False}, self.admin_headers())))["job"]["id"]
        await self.wait_job(second)
        listed = (await pm.list_jobs())["jobs"]
        self.assertEqual([job["id"] for job in listed][:2], [second, first])
        dumped = __import__("json").dumps(listed)
        self.assertNotIn("secret", dumped)
        self.assertNotIn("alice", dumped)

    async def test_client_readiness_is_read_only_cached_and_reported_per_device(self):
        calls: list[str] = []

        class ClientDriver(FakeDriver):
            async def client_status(inner, serial):
                calls.append(serial)
                missing = serial == "phone-a"
                return {"status": "client_missing" if missing else "ready", "client_installed": not missing, "always_on_vpn": False, "message": "checked"}

        self.driver = ClientDriver()
        result = await pm.clients()
        statuses = {item["serial"]: item["status"] for item in result["clients"]}
        self.assertEqual(statuses["phone-a"], "client_missing")
        self.assertFalse(result["cached"])
        first_calls = len(calls)
        self.assertGreaterEqual(first_calls, 1)
        again = await pm.clients(refresh=True)
        self.assertTrue(again["cached"], "refresh is rate-limited to protect ADB")
        self.assertEqual(len(calls), first_calls)
        self.assertEqual((self.driver.tests, self.driver.applies, self.driver.disables), ([], [], []))

    async def test_client_readiness_without_driver_support_is_unknown(self):
        result = await pm.clients()
        self.assertTrue(result["clients"])
        self.assertTrue(all(item["status"] in {"unknown", "offline"} for item in result["clients"]))

    async def test_dashboard_session_and_same_origin_are_required(self):
        with self.assertRaises(HTTPException) as missing:
            await pm._admin(self.request(headers={}, auth=None))
        self.assertEqual(missing.exception.status_code, 401)
        with self.assertRaises(HTTPException) as bogus:
            await pm._admin(self.request(headers=self.admin_headers(), auth="anything-else"))
        self.assertEqual(bogus.exception.status_code, 401)
        with self.assertRaises(HTTPException) as cross:
            await pm._admin(self.request(headers={**self.admin_headers(), "origin": "https://evil.example"}))
        self.assertEqual(cross.exception.status_code, 403)
        await pm._admin(self.request(headers=self.admin_headers()))
        await pm._admin(self.request(headers={}, auth="local"))
        self.assertFalse((Path(self.temp.name) / "state" / "admin.key").exists())
        result = await pm.devices()
        self.assertEqual(result["total"], 2)

    async def test_dynamic_inventory_unions_saved_profiles_without_cap(self):
        self.save_direct("saved-only")
        result = await pm.devices()
        serials = {item["serial"] for item in result["devices"]}
        self.assertEqual(serials, {"phone-a", "phone-live", "saved-only"})
        saved = next(item for item in result["devices"] if item["serial"] == "saved-only")
        self.assertFalse(saved["online"])
        self.assertIsNotNone(saved["profile"])

    async def test_profile_endpoint_forbids_unknown_fields_and_never_returns_credentials(self):
        body = {"serial": "phone-a", "label": "A", "proxy_type": "https", "host": "proxy.example", "port": 443, "username": "u", "password": "p", "unexpected": "do not accept"}
        with self.assertRaises(HTTPException) as error:
            await pm.save_profile(self.request(body, self.admin_headers()))
        self.assertEqual(error.exception.status_code, 422)
        self.assertNotIn("p", str(error.exception.detail))
        body.pop("unexpected")
        result = await pm.save_profile(self.request(body, self.admin_headers()))
        self.assertNotIn("password", result["profile"])
        with self.assertRaises(HTTPException):
            await pm.save_profile(self.request({**body, "host": "https://user:p@host/path"}, self.admin_headers()))

    async def test_bootstrap_is_transient_no_store_and_revocable(self):
        token, path = pm.publish_config("phone-a", b"secret-yaml")
        self.assertTrue(path.endswith(token))
        response = await pm.bootstrap(token)
        self.assertEqual(response.body, b"secret-yaml")
        self.assertEqual(response.headers["cache-control"], "no-store")
        pm.revoke_config(token)
        with self.assertRaises(HTTPException) as error:
            await pm.bootstrap(token)
        self.assertEqual(error.exception.status_code, 404)

    async def test_apply_preflights_then_verifies_and_is_bounded_by_two(self):
        self.save_direct("phone-a")
        self.save_direct("phone-live", username=None, password=None)
        body = {"action": "apply", "devices": ["phone-a", "phone-live"], "confirmed": True}
        created = await pm.create_job(self.request(body, self.admin_headers()))
        job_id = created["job"]["id"]
        for _ in range(50):
            await asyncio.sleep(0.005)
            if pm._jobs[job_id]["status"] in {"completed", "failed"}:
                break
        job = (await pm.get_job(job_id))["job"]
        self.assertEqual(job["status"], "completed")
        self.assertTrue(all(item["verified_device"] for item in job["results"]))
        self.assertEqual(self.driver.tests, ["phone-a", "phone-live"])
        self.assertEqual(self.driver.applies, ["phone-a", "phone-live"])
        self.assertEqual(self.test_store.get_public("phone-a")["applied_mode"], "proxy")

    async def test_apply_requires_explicit_confirmation_and_disable_updates_state(self):
        self.save_direct()
        with self.assertRaises(HTTPException) as error:
            await pm.create_job(self.request({"action": "apply", "devices": ["phone-a"], "confirmed": False}, self.admin_headers()))
        self.assertEqual(error.exception.status_code, 422)
        self.test_store.update_runtime("phone-a", desired="proxy", applied="proxy")
        created = await pm.create_job(self.request({"action": "disable", "devices": ["phone-a"], "confirmed": True}, self.admin_headers()))
        for _ in range(50):
            await asyncio.sleep(0.005)
            if pm._jobs[created["job"]["id"]]["status"] in {"completed", "failed"}:
                break
        self.assertEqual(self.test_store.get_public("phone-a")["applied_mode"], "normal")

    async def test_driver_is_lazy_and_test_result_is_not_device_verified(self):
        self.save_direct()
        created = await pm.create_job(self.request({"action": "test", "devices": ["phone-a"], "confirmed": False}, self.admin_headers()))
        for _ in range(50):
            await asyncio.sleep(0.005)
            if pm._jobs[created["job"]["id"]]["status"] in {"completed", "failed"}:
                break
        result = pm._jobs[created["job"]["id"]]["results"][0]
        self.assertFalse(result.get("verified_device", False))
        self.assertEqual(pm.capabilities.__name__, "capabilities")

    async def test_prepare_needs_no_profile_never_routes_and_refreshes_readiness(self):
        prepared: list[str] = []

        class PrepareDriver(FakeDriver):
            async def prepare_client(inner, serial):
                prepared.append(serial)
                return {"ok": True, "status": "ready", "message": "Clash Meta installed", "installed_now": True, "version": "2.11.31.Meta", "steps": ["installed", "vpn-consent"]}

        self.driver = PrepareDriver()
        with self.assertRaises(HTTPException) as error:
            await pm.create_job(self.request({"action": "prepare", "devices": ["phone-a"], "confirmed": False}, self.admin_headers()))
        self.assertEqual(error.exception.status_code, 422)
        pm._client_cache.update(at=10_000_000.0, items=[{"serial": "phone-a"}], checked_at="x")
        created = await pm.create_job(self.request({"action": "prepare", "devices": ["phone-a", "phone-live"], "confirmed": True}, self.admin_headers()))
        await self.wait_job(created["job"]["id"])
        job = (await pm.get_job(created["job"]["id"]))["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(sorted(prepared), ["phone-a", "phone-live"])
        self.assertTrue(all(item["installed_now"] and item["version"] == "2.11.31.Meta" for item in job["results"]))
        self.assertTrue(all(item["verified_device"] is False for item in job["results"]))
        self.assertEqual((self.driver.tests, self.driver.applies, self.driver.disables), ([], [], []))
        self.assertIsNone(self.test_store.get_public("phone-a"), "prepare must not create or change a proxy profile")
        self.assertEqual(pm._client_cache["at"], 0.0, "readiness is re-checked after setup")

    async def test_prepare_failure_and_missing_adapter_support_are_reported(self):
        created = await pm.create_job(self.request({"action": "prepare", "devices": ["phone-a"], "confirmed": True}, self.admin_headers()))
        await self.wait_job(created["job"]["id"])
        job = pm._jobs[created["job"]["id"]]
        self.assertEqual(job["status"], "failed")
        self.assertIn("not supported", job["results"][0]["message"])

        class BrokenDriver(FakeDriver):
            async def prepare_client(inner, serial):
                raise RuntimeError("/data/app/secret path")

        self.driver = BrokenDriver()
        created = await pm.create_job(self.request({"action": "prepare", "devices": ["phone-a"], "confirmed": True}, self.admin_headers()))
        await self.wait_job(created["job"]["id"])
        result = pm._jobs[created["job"]["id"]]["results"][0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Client setup failed")


if __name__ == "__main__":
    unittest.main()
