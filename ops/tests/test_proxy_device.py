import asyncio
import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "proxy_device.py"
if not MODULE_PATH.is_file(): MODULE_PATH = pathlib.Path(__file__).with_name("proxy_device.py")
spec = importlib.util.spec_from_file_location("proxy_device", MODULE_PATH)
assert spec and spec.loader
proxy_device = importlib.util.module_from_spec(spec)
sys.modules["proxy_device"] = proxy_device
spec.loader.exec_module(proxy_device)


CONFIG = {
    "serial": "emulator-5554",
    "label": "test phone",
    "proxy_type": "https",
    "host": "proxy.example.test",
    "port": 443,
    "username": "alice",
    "password": "correct horse battery staple",
}


class ProxyDeviceTests(unittest.IsolatedAsyncioTestCase):
    def test_profile_is_single_upstream_loopback_and_lan_direct(self):
        payload = proxy_device.build_profile_yaml(CONFIG)
        # Credentials are needed by the transient client profile; command-line
        # and result-message tests below ensure they are not exposed elsewhere.
        self.assertIn(b"correct horse battery staple", payload)
        document = proxy_device.yaml.safe_load(payload)
        self.assertEqual(document["mixed-port"], 7890)
        self.assertFalse(document["allow-lan"])
        self.assertEqual(document["bind-address"], "127.0.0.1")
        self.assertEqual(len(document["proxies"]), 1)
        self.assertEqual(document["proxies"][0]["type"], "http")
        self.assertTrue(document["proxies"][0]["tls"])
        self.assertFalse(document["proxies"][0]["skip-cert-verify"])
        self.assertIn("IP-CIDR,192.168.0.0/16,DIRECT,no-resolve", document["rules"])
        self.assertEqual(document["rules"][-1], "MATCH,D3MON")

    async def test_test_is_server_only_and_does_not_use_adb(self):
        publish = AsyncMock()
        revoke = AsyncMock()
        driver = proxy_device.ProxyDeviceDriver(publish, revoke)
        with patch.object(proxy_device, "_probe_sync", return_value="203.0.113.9"):
            result = await driver.test(CONFIG)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_ip"], "203.0.113.9")
        self.assertFalse(result["verified_device"])
        self.assertIn("Server-side", result["message"])
        publish.assert_not_awaited()

    async def test_apply_verifies_phone_and_revokes_token_in_finally(self):
        published = []

        def publish(serial, payload):
            published.append((serial, payload))
            return "opaque-token-123456", "/api/proxy/bootstrap/opaque-token-123456"

        revoke = AsyncMock()
        driver = proxy_device.ProxyDeviceDriver(publish, revoke, backend_port=8123)
        driver._existing_vpn_is_other_app = AsyncMock(return_value=False)
        driver.prepare_client = AsyncMock(return_value={"ok": True, "status": "ready"})
        driver._run_adb = AsyncMock(return_value=(0, ""))
        driver._import_and_start = AsyncMock()
        driver._vpn_state = AsyncMock(return_value=("null", False, True))
        driver._reverse_devices.add(CONFIG["serial"])
        driver._verify_device = AsyncMock(return_value="198.51.100.7")
        driver._stop_clash = AsyncMock()
        with patch.object(proxy_device, "_probe_sync", return_value="198.51.100.7"):
            result = await driver.apply(CONFIG["serial"], CONFIG)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified_device"])
        self.assertEqual(result["exit_ip"], "198.51.100.7")
        self.assertEqual(published[0][0], CONFIG["serial"])
        commands = [call.args[1:] for call in driver._run_adb.await_args_list]
        self.assertTrue(any("reverse" in command and "--remove" in command for command in commands))
        self.assertNotIn(CONFIG["password"], repr(commands))
        revoke.assert_awaited_once_with("opaque-token-123456")

    async def test_unverified_apply_is_setup_required_and_never_success(self):
        revoke = AsyncMock()
        driver = proxy_device.ProxyDeviceDriver(
            lambda serial, payload: ("opaque-token-123456", "/api/proxy/bootstrap/opaque-token-123456"),
            revoke,
        )
        driver._existing_vpn_is_other_app = AsyncMock(return_value=False)
        driver.prepare_client = AsyncMock(return_value={"ok": True, "status": "ready"})
        driver._run_adb = AsyncMock(return_value=(0, ""))
        driver._import_and_start = AsyncMock()
        driver._changed_devices.add(CONFIG["serial"])
        driver._reverse_devices.add(CONFIG["serial"])
        driver._verify_device = AsyncMock(side_effect=proxy_device.SetupRequired("consent required"))
        driver._stop_clash = AsyncMock()
        with patch.object(proxy_device, "_probe_sync", return_value="203.0.113.8"):
            result = await driver.apply(CONFIG["serial"], CONFIG)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "setup-required")
        self.assertFalse(result["verified_device"])
        revoke.assert_awaited_once_with("opaque-token-123456")
        driver._stop_clash.assert_awaited_once_with(CONFIG["serial"])

    async def test_apply_does_not_publish_when_server_preflight_fails(self):
        publish = AsyncMock()
        revoke = AsyncMock()
        driver = proxy_device.ProxyDeviceDriver(publish, revoke)
        with patch.object(proxy_device, "_probe_sync", side_effect=ConnectionError("bad")):
            result = await driver.apply(CONFIG["serial"], CONFIG)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified_device"])
        publish.assert_not_awaited()
        revoke.assert_not_awaited()

    async def test_disable_only_uses_documented_stop_control(self):
        driver = proxy_device.ProxyDeviceDriver(AsyncMock(), AsyncMock())
        driver._existing_vpn_is_other_app = AsyncMock(return_value=False)
        driver._run_adb = AsyncMock(return_value=(0, ""))
        driver._vpn_state = AsyncMock(return_value=("null", False, False))
        result = await driver.disable(CONFIG["serial"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified_device"])
        calls = [call.args[1:] for call in driver._run_adb.await_args_list]
        self.assertEqual(len(calls), 2)
        self.assertIn("-a", calls[0])
        self.assertIn(proxy_device.STOP_ACTION, calls[0])

    async def test_import_requires_save_select_and_vpn_before_success(self):
        driver = proxy_device.ProxyDeviceDriver(lambda *a: None, lambda *a: None)
        driver._existing_vpn_is_other_app = AsyncMock(return_value=False)
        driver._run_adb = AsyncMock(return_value=(0, ""))
        driver._save_and_select_profile = AsyncMock()
        driver._vpn_state = AsyncMock(return_value=("null", False, False))
        with self.assertRaises(proxy_device.SetupRequired):
            await driver._import_and_start(CONFIG["serial"], "opaque-token-123456", "/api/proxy/bootstrap/opaque-token-123456")
        driver._save_and_select_profile.assert_awaited_once()
        self.assertIn(CONFIG["serial"], driver._reverse_devices)
        self.assertEqual(driver.backend_port, 5055)

    async def test_existing_other_vpn_is_never_stopped(self):
        driver = proxy_device.ProxyDeviceDriver(lambda *a: ("opaque-token-123456", "/api/proxy/bootstrap/opaque-token-123456"), lambda *a: None)
        driver.test = AsyncMock(return_value={"ok": True})
        driver._existing_vpn_is_other_app = AsyncMock(return_value=True)
        driver._stop_clash = AsyncMock()
        result = await driver.apply(CONFIG["serial"], CONFIG)
        self.assertFalse(result["ok"])
        driver._stop_clash.assert_not_awaited()

    async def test_profile_selection_uses_observed_native_radio_state(self):
        ET = proxy_device.ET
        name = "D3m0n-test-profile"
        properties = ET.fromstring(f'<hierarchy><node package="{proxy_device.PACKAGE}" text="{name}"/><node package="{proxy_device.PACKAGE}" content-desc="Save" clickable="true" bounds="[0,0][10,10]"/></hierarchy>')
        main = ET.fromstring(f'<hierarchy><node package="{proxy_device.PACKAGE}" text="Profiles" clickable="true" bounds="[0,0][10,10]"/></hierarchy>')
        profile = lambda checked: ET.fromstring(f'<hierarchy><node resource-id="{proxy_device.PACKAGE}:id/root_view" clickable="true" bounds="[0,0][10,10]"><node package="{proxy_device.PACKAGE}" text="{name}"/><node class="android.widget.RadioButton" checked="{checked}"/></node></hierarchy>')
        driver = proxy_device.ProxyDeviceDriver(lambda *a: None, lambda *a: None)
        driver._ui_tree = AsyncMock(side_effect=[properties, main, profile("false"), profile("true")])
        driver._run_adb = AsyncMock(return_value=(0, ""))
        driver._tap_node = AsyncMock()
        with patch.object(proxy_device.asyncio, "sleep", new=AsyncMock()):
            await driver._save_and_select_profile(CONFIG["serial"], name)
        self.assertEqual(driver._tap_node.await_count, 3)
        self.assertIn(CONFIG["serial"], driver._changed_devices)


class ProxyClientStatusTests(unittest.IsolatedAsyncioTestCase):
    async def run_status(self, outputs):
        driver = proxy_device.ProxyDeviceDriver(AsyncMock(), AsyncMock())
        seen = []
        async def fake_adb(serial, *args, timeout=None):
            seen.append(args)
            return outputs[len(seen) - 1]
        driver._run_adb = fake_adb
        return await driver.client_status("192.168.50.37:5555"), seen
    async def test_client_status_is_read_only(self):
        result, seen = await self.run_status([(1, ""), (0, "null\n")])
        self.assertEqual(result["status"], "client_missing")
        self.assertFalse(result["client_installed"])
        commands = [" ".join(args) for args in seen]
        self.assertEqual(commands, ["shell pm path " + proxy_device.PACKAGE, "shell settings get secure always_on_vpn_app"])
        self.assertFalse(any(word in command for command in commands for word in (" put ", "am start", "input ", "monkey")))
    async def test_client_status_ready_and_other_vpn(self):
        ready, _ = await self.run_status([(0, "package:/data/app/base.apk\n"), (0, proxy_device.PACKAGE + "\n")])
        self.assertEqual(ready["status"], "ready")
        other, _ = await self.run_status([(0, "package:/data/app/base.apk\n"), (0, "com.other.vpn\n")])
        self.assertEqual(other["status"], "other_vpn")
        self.assertNotIn("com.other.vpn", other["message"])
    async def test_client_status_failure_is_unknown(self):
        driver = proxy_device.ProxyDeviceDriver(AsyncMock(), AsyncMock())
        async def broken(serial, *args, timeout=None):
            raise proxy_device.SetupRequired("Android command timed out")
        driver._run_adb = broken
        result = await driver.client_status("192.168.50.37:5555")
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["client_installed"])


class ProxyPrepareTests(unittest.IsolatedAsyncioTestCase):
    """One-click client setup. No real ADB, phones or network are used."""

    def setUp(self):
        import hashlib, tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.cache = pathlib.Path(self.temp.name) / "cache"
        self.apk_bytes = b"PK\x03\x04" + b"fake-client" * 100
        self.digest = hashlib.sha256(self.apk_bytes).hexdigest()
        self.pinned = {
            "arm64-v8a": ("client-arm64.apk", len(self.apk_bytes), self.digest),
            "universal": ("client-universal.apk", 999, "0" * 64),
        }
        self.patches = [
            patch.object(proxy_device, "CLIENT_CACHE_DIR", self.cache),
            patch.object(proxy_device, "CLIENT_APKS", self.pinned),
        ]
        for item in self.patches:
            item.start()
        proxy_device._apk_lock = None
        proxy_device._digest_cache.clear()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def make_driver(self, installed_after_install=True, install_output="Success\n", consent="ACTIVATE_VPN: allow; time=+1s\n", donors=None):
        driver = proxy_device.ProxyDeviceDriver(AsyncMock(), AsyncMock())
        driver._existing_vpn_is_other_app = AsyncMock(return_value=False)
        state = {"installed": False}
        commands = []
        apk = self.apk_bytes
        donors = donors or {}

        async def fake_adb(serial, *args, timeout=None):
            commands.append((serial,) + args)
            joined = " ".join(args)
            if joined == "shell pm path " + proxy_device.PACKAGE:
                if serial in donors:
                    return 0, "package:/data/app/" + proxy_device.PACKAGE + "-abc==/base.apk\n"
                return (0, "package:/data/app/x/base.apk\n") if state["installed"] else (1, "")
            if joined.startswith("shell dumpsys package"):
                return 0, "    versionCode=211031 minSdk=21\n    versionName=2.11.31.Meta\n"
            if joined == "shell getprop ro.product.cpu.abilist":
                return 0, "arm64-v8a,armeabi-v7a,armeabi\n"
            if args and args[0] == "install":
                if installed_after_install:
                    state["installed"] = True
                return 0, install_output
            if joined.startswith("shell sha256sum"):
                return 0, donors.get(serial, "") + "  " + args[-1] + "\n"
            if args and args[0] == "pull":
                pathlib.Path(args[2]).write_bytes(apk)
                return 0, "1 file pulled\n"
            if joined.startswith("shell appops get"):
                return 0, consent
            return 0, ""

        driver._run_adb = fake_adb
        driver._run_host_adb = AsyncMock(return_value=(0, "List of devices attached\n" + "".join(f"{s}\tdevice\n" for s in donors)))
        return driver, commands, state

    def seed_cache(self, data=None):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / "client-arm64.apk").write_bytes(self.apk_bytes if data is None else data)

    async def test_installs_verified_cached_client_and_preapproves_vpn_without_routing(self):
        self.seed_cache()
        driver, commands, state = self.make_driver()
        with patch.object(proxy_device, "_download_sync", side_effect=AssertionError("no download")):
            result = await driver.prepare_client("192.168.50.40:5555")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["installed_now"])
        self.assertEqual(result["version"], "2.11.31.Meta")
        self.assertIn("vpn-consent", result["steps"])
        joined = [" ".join(c[1:]) for c in commands]
        self.assertTrue(any(c.startswith("install -r -g ") and c.endswith("client-arm64.apk") for c in joined))
        self.assertIn("shell appops set " + proxy_device.PACKAGE + " ACTIVATE_VPN allow", joined)
        for forbidden in ("am start", "START_CLASH", "settings put", "reverse", "input tap"):
            self.assertFalse(any(forbidden in c for c in joined), forbidden)

    async def test_tampered_cache_is_never_installed_and_download_must_match_digest(self):
        self.seed_cache(b"PK\x03\x04tampered")
        driver, commands, _ = self.make_driver()
        def bad_download(url, dest, size, digest, timeout=30.0):
            raise proxy_device.SetupRequired("Downloaded client failed checksum verification")
        with patch.object(proxy_device, "_download_sync", side_effect=bad_download):
            result = await driver.prepare_client("192.168.50.40:5555")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "setup-required")
        self.assertIn("checksum", result["message"])
        self.assertFalse(any(c[1] == "install" for c in commands if len(c) > 1))

    async def test_copies_pinned_build_from_fleet_phone_only_when_digest_matches(self):
        driver, commands, _ = self.make_driver(donors={"192.168.50.50:5555": "f" * 64, "192.168.50.37:5555": self.digest})
        with patch.object(proxy_device, "_download_sync", side_effect=AssertionError("no download")):
            result = await driver.prepare_client("192.168.50.40:5555")
        self.assertTrue(result["ok"], result)
        pulls = [c for c in commands if len(c) > 1 and c[1] == "pull"]
        self.assertEqual([c[0] for c in pulls], ["192.168.50.37:5555"], "only the digest-matching donor is copied")
        self.assertTrue((self.cache / "client-arm64.apk").is_file())
        self.assertEqual(list(self.cache.glob(".partial-*")), [])

    async def test_already_installed_only_preapproves_consent(self):
        driver, commands, state = self.make_driver()
        state["installed"] = True
        result = await driver.prepare_client("192.168.50.40:5555")
        self.assertTrue(result["ok"])
        self.assertFalse(result["installed_now"])
        self.assertFalse(any(len(c) > 1 and c[1] == "install" for c in commands))

    async def test_other_vpn_owner_is_left_alone(self):
        driver, commands, _ = self.make_driver()
        driver._existing_vpn_is_other_app = AsyncMock(return_value=True)
        result = await driver.prepare_client("192.168.50.40:5555")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "other_vpn")
        self.assertEqual(commands, [])

    async def test_install_rejection_reports_android_reason_and_missing_consent_fails(self):
        self.seed_cache()
        driver, _, _ = self.make_driver(installed_after_install=False, install_output="Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE: /data/x]\n")
        result = await driver.prepare_client("192.168.50.40:5555")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Android rejected the Clash Meta install (INSTALL_FAILED_INSUFFICIENT_STORAGE)")
        driver, _, state = self.make_driver(consent="ACTIVATE_VPN: ignore\n")
        state["installed"] = True
        result = await driver.prepare_client("192.168.50.40:5555")
        self.assertFalse(result["ok"])
        self.assertIn("consent", result["message"])

    async def test_apply_stops_before_publishing_when_prepare_fails(self):
        publish = AsyncMock()
        driver = proxy_device.ProxyDeviceDriver(publish, AsyncMock())
        driver.prepare_client = AsyncMock(return_value={"ok": False, "status": "setup-required", "message": "Clash Meta could not be fetched"})
        driver._import_and_start = AsyncMock()
        with patch.object(proxy_device, "_probe_sync", return_value="203.0.113.8"):
            result = await driver.apply(CONFIG["serial"], CONFIG)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "setup-required")
        self.assertIn("routing was not changed", result["message"])
        publish.assert_not_awaited()
        driver._import_and_start.assert_not_awaited()

    def test_pinned_release_digests_are_complete(self):
        self.patches[1].stop()
        try:
            for abi, (name, size, digest) in proxy_device.CLIENT_APKS.items():
                self.assertTrue(name.startswith("cmfa-2.11.31-meta-") and name.endswith(".apk"), abi)
                self.assertGreater(size, 40_000_000)
                self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertTrue(proxy_device.CLIENT_RELEASE_BASE.startswith("https://github.com/MetaCubeX/ClashMetaForAndroid/releases/download/v2.11.31/"))
        finally:
            self.patches[1].start()


if __name__ == "__main__":
    unittest.main()
