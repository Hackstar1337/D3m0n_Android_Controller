import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI

from ops import dashboard_auth as da

PUBLIC = {"host": "dashboard.example.test", "x-real-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9", "x-forwarded-proto": "https"}
PASSWORD = __import__("secrets").token_urlsafe(24)  # throwaway test only


def scope(path, method="GET", headers=None, kind="http", client=("203.0.113.9", 4321)):
    return {
        "type": kind, "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "scheme": "https" if kind == "http" else "wss", "http_version": "1.1",
        "server": ("127.0.0.1", 5055), "client": client,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }


async def call(app, sc, body=b""):
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if sc["type"] == "websocket":
            if not delivered:
                delivered = True
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(sc, receive, send)
    return sent


def status_of(messages):
    for m in messages:
        if m["type"] == "http.response.start":
            return m["status"]
    return None


def header(messages, name):
    for m in messages:
        if m["type"] == "http.response.start":
            return [v.decode() for k, v in m.get("headers", []) if k.decode().lower() == name]
    return []


def body_json(messages):
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return json.loads(raw or b"{}")


class Recorder:
    def __init__(self):
        self.seen = []

    async def __call__(self, sc, receive, send):
        self.seen.append(sc.get("d3m0n_auth"))
        if sc["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


class DashboardAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_env = {name: os.environ.get(name) for name in ("D3M0N_ALLOWED_ORIGINS", "GITD_CORS_ORIGINS")}
        # Keep origin policy hermetic: this suite must not inherit a developer's
        # live public origin or mutate it for subsequent tests.
        os.environ["D3M0N_ALLOWED_ORIGINS"] = "https://dashboard.example.test,http://127.0.0.1:5055"
        os.environ["GITD_CORS_ORIGINS"] = "https://dashboard.example.test,http://127.0.0.1:5055"
        root = Path(self.temp.name)
        self.auth_file = root / "state" / "dashboard-auth.json"
        self.dist = root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<html></html>")
        (self.dist / "favicon.ico").write_bytes(b"x")
        (self.dist / "mascot").mkdir()
        (self.dist / "mascot" / "01-front-view.png").write_bytes(b"png")
        da.install_hash(self.auth_file, da.hash_password(PASSWORD, iterations=1000), min_iterations=1)
        self.previous_state = da.state
        da.state = da.AuthState(self.auth_file, min_iterations=1)
        self.inner = Recorder()
        self.gate = da.DashboardAuthMiddleware(self.inner, auth_state=da.state, dist=self.dist)

    def tearDown(self):
        da.state = self.previous_state
        for name, value in self.previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def full_app(self):
        app = FastAPI()
        app.include_router(da.router)

        @app.get("/api/health")
        async def health():
            return {"status": "ok"}

        @app.get("/api/phone/devices")
        async def devices():
            return {"devices": []}

        app.add_middleware(da.DashboardAuthMiddleware, auth_state=da.state, dist=self.dist)
        return app

    async def unlock(self, app, password, ip="203.0.113.9"):
        headers = {**PUBLIC, "x-real-ip": ip, "x-forwarded-for": ip, "content-type": "application/json", "origin": "https://dashboard.example.test"}
        return await call(app, scope("/api/auth/unlock", "POST", headers), json.dumps({"password": password}).encode())

    def test_password_is_stored_only_as_hash_with_private_mode(self):
        text = self.auth_file.read_text()
        self.assertNotIn(PASSWORD, text)
        data = json.loads(text)
        self.assertEqual(data["kdf"], "pbkdf2_sha256")
        self.assertIn("session_secret", data)
        if __import__("os").name == "posix":
            self.assertEqual(oct(self.auth_file.stat().st_mode & 0o777), "0o600")
        config = da.state.config()
        self.assertTrue(config.verify_password(PASSWORD))
        self.assertFalse(config.verify_password("wrong"))
        self.assertFalse(config.verify_password(""))
        self.assertFalse(config.verify_password("x" * 300))
        self.assertFalse(config.verify_password(None))
        with self.assertRaises(ValueError):
            da.install_hash(self.auth_file, {"kdf": "md5", "iterations": 1, "salt": "a", "hash": "b"}, min_iterations=1)

    def test_tokens_are_signed_expiring_and_revocable(self):
        config = da.state.config()
        token = config.issue_token()
        self.assertTrue(config.token_valid(token))
        self.assertFalse(config.token_valid(token[:-2] + ("AA" if not token.endswith("AA") else "BB")))
        self.assertFalse(config.token_valid(config.issue_token(now=time.time() - da.SESSION_SECONDS - 5)))
        self.assertFalse(config.token_valid("v1.9999999999.nonce"))
        da.rotate_sessions(self.auth_file)
        self.assertFalse(da.state.config().token_valid(token))

    def test_config_reloads_after_rapid_same_size_rewrite(self):
        # Two writes inside one coarse filesystem clock tick must still reload (revocation applies).
        first = da.state.config()
        before = self.auth_file.stat()
        da.rotate_sessions(self.auth_file)
        os.utime(self.auth_file, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.auth_file.stat().st_size, before.st_size)
        self.assertNotEqual(da.state.config().secret, first.secret)

    def test_trusted_local_requires_loopback_without_forwarding_headers(self):
        self.assertTrue(da.is_trusted_local(scope("/api/x", headers={"host": "127.0.0.1:5055"}, client=("127.0.0.1", 1))))
        self.assertTrue(da.is_trusted_local(scope("/api/x", headers={"host": "localhost:5055"}, client=("127.0.0.1", 1))))
        self.assertFalse(da.is_trusted_local(scope("/api/x", headers={**PUBLIC}, client=("127.0.0.1", 1))))
        self.assertFalse(da.is_trusted_local(scope("/api/x", headers={"host": "127.0.0.1:5055", "x-real-ip": "1.2.3.4"}, client=("127.0.0.1", 1))))
        self.assertFalse(da.is_trusted_local(scope("/api/x", headers={"host": "127.0.0.1:5055", "x-forwarded-for": "1.2.3.4"}, client=("127.0.0.1", 1))))
        self.assertFalse(da.is_trusted_local(scope("/api/x", headers={"host": "dashboard.example.test"}, client=("127.0.0.1", 1))))
        self.assertFalse(da.is_trusted_local(scope("/api/x", headers={"host": "127.0.0.1:5055"}, client=("192.168.50.5", 1))))

    async def test_public_requests_are_locked_except_shell_health_and_auth(self):
        locked = await call(self.gate, scope("/api/phone/devices", headers=PUBLIC))
        self.assertEqual(status_of(locked), 401)
        self.assertEqual(header(locked, "x-d3m0n-auth"), ["locked"])
        self.assertEqual(body_json(locked)["detail"], da.LOCKED_DETAIL)
        self.assertEqual(status_of(await call(self.gate, scope("/api/proxy/devices", headers=PUBLIC))), 401)
        self.assertEqual(status_of(await call(self.gate, scope("/api/server/restart", "POST", PUBLIC))), 401)
        for path in ("/", "/index.html", "/assets/index-abc.js", "/favicon.ico", "/mascot/01-front-view.png", "/api/health", "/api/auth/status"):
            self.assertEqual(status_of(await call(self.gate, scope(path, headers=PUBLIC))), 200, path)
        self.assertEqual(status_of(await call(self.gate, scope("/missing.txt", headers=PUBLIC))), 401)
        self.assertEqual(status_of(await call(self.gate, scope("/mascot/missing.png", headers=PUBLIC))), 401)
        self.assertEqual(status_of(await call(self.gate, scope("/mascot/01-front-view.png", "POST", PUBLIC))), 401)
        self.assertEqual(status_of(await call(self.gate, scope("/assets/../secret", headers=PUBLIC))), 401)
        redirect = await call(self.gate, scope("/docs", headers={**PUBLIC, "accept": "text/html"}))
        self.assertEqual(status_of(redirect), 303)
        self.assertEqual(header(redirect, "location"), ["/"])
        self.assertNotIn("session", self.inner.seen)

    async def test_local_services_stay_trusted(self):
        result = await call(self.gate, scope("/api/phone/android-h264/status", headers={"host": "127.0.0.1:5055"}, client=("127.0.0.1", 9)))
        self.assertEqual(status_of(result), 200)
        self.assertEqual(self.inner.seen[-1], "local")

    async def test_websocket_requires_session_and_same_origin(self):
        denied = await call(self.gate, scope("/api/phone/android-h264/a", headers=PUBLIC, kind="websocket"))
        self.assertEqual(denied[-1], {"type": "websocket.close", "code": 1008})
        self.assertEqual(self.inner.seen, [])
        token = da.state.config().issue_token()
        cookie = {**PUBLIC, "cookie": f"other=1; {da.COOKIE_NAME}={token}"}
        cross = await call(self.gate, scope("/api/phone/android-h264/a", headers={**cookie, "origin": "https://evil.example"}, kind="websocket"))
        self.assertEqual(cross[-1]["type"], "websocket.close")
        allowed = await call(self.gate, scope("/api/phone/android-h264/a", headers={**cookie, "origin": "https://dashboard.example.test"}, kind="websocket"))
        self.assertEqual(allowed[-1]["type"], "websocket.accept")
        self.assertEqual(self.inner.seen, ["session"])

    async def test_unconfigured_fails_closed_for_public_but_not_local(self):
        self.auth_file.unlink()
        self.assertEqual(status_of(await call(self.gate, scope("/api/phone/devices", headers=PUBLIC))), 503)
        self.assertEqual(status_of(await call(self.gate, scope("/api/phone/devices", headers={"host": "127.0.0.1:5055"}, client=("127.0.0.1", 9)))), 200)

    async def test_unlock_sets_secure_cookie_and_unlocks_api(self):
        app = self.full_app()
        wrong = await self.unlock(app, "nope")
        self.assertEqual(status_of(wrong), 401)
        self.assertEqual(header(wrong, "set-cookie"), [])
        good = await self.unlock(app, PASSWORD)
        self.assertEqual(status_of(good), 200)
        cookies = header(good, "set-cookie")
        self.assertEqual(len(cookies), 1)
        lowered = cookies[0].lower()
        for flag in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={da.SESSION_SECONDS}"):
            self.assertIn(flag, lowered)
        self.assertNotIn(PASSWORD.lower(), lowered)
        token = cookies[0].split(";", 1)[0].split("=", 1)[1]
        authed = {**PUBLIC, "cookie": f"{da.COOKIE_NAME}={token}"}
        self.assertEqual(status_of(await call(app, scope("/api/phone/devices", headers=authed))), 200)
        status = body_json(await call(app, scope("/api/auth/status", headers=authed)))
        self.assertEqual(status, {"configured": True, "unlocked": True, "local": False, "session_days": 30})
        anon = body_json(await call(app, scope("/api/auth/status", headers=PUBLIC)))
        self.assertFalse(anon["unlocked"])
        lock = await call(app, scope("/api/auth/lock", "POST", {**authed, "origin": "https://dashboard.example.test"}))
        self.assertEqual(status_of(lock), 200)
        self.assertTrue(any("max-age=0" in c.lower() or "expires=" in c.lower() for c in header(lock, "set-cookie")))

    async def test_repeated_failures_are_rate_limited_per_client(self):
        app = self.full_app()
        for _ in range(da.MAX_FAILURES):
            self.assertEqual(status_of(await self.unlock(app, "bad")), 401)
        blocked = await self.unlock(app, PASSWORD)
        self.assertEqual(status_of(blocked), 429)
        self.assertTrue(int(header(blocked, "retry-after")[0]) > 0)
        self.assertEqual(status_of(await self.unlock(app, PASSWORD, ip="198.51.100.7")), 200)


if __name__ == "__main__":
    unittest.main()
