import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "proxy_device.py"
spec = importlib.util.spec_from_file_location("proxy_device_hardening", MODULE_PATH)
assert spec and spec.loader
proxy_device = importlib.util.module_from_spec(spec)
sys.modules["proxy_device_hardening"] = proxy_device
spec.loader.exec_module(proxy_device)


class _TlsResponse:
    def __init__(self, body):
        self.body = body

    def settimeout(self, timeout):
        pass

    def sendall(self, payload):
        pass

    def recv(self, size):
        if self.body is None:
            return b""
        body, self.body = self.body, None
        return body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class _TlsContext:
    def __init__(self, response):
        self.response = response
        self.server_hostname = None

    def wrap_socket(self, sock, server_hostname=None):
        self.server_hostname = server_hostname
        return self.response


class ProxyHardeningTests(unittest.TestCase):
    def test_every_exit_ip_requires_a_global_address(self):
        for value in ("10.0.0.7", "127.0.0.1", "192.0.2.7", "2001:db8::7"):
            with self.assertRaises(ValueError, msg=value):
                proxy_device._validated_global_exit_ip(value)
        self.assertEqual(proxy_device._validated_global_exit_ip("8.8.8.8"), "8.8.8.8")

    def test_allow_local_probe_rejects_non_global_exit_and_accepts_global_exit(self):
        connect_response = _TlsResponse(b"HTTP/1.1 200 Connection established\r\n\r\n")
        probe_response = _TlsResponse(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n{\"ip\":\"10.0.0.7\"}")
        context = _TlsContext(probe_response)
        raw = _TlsResponse(None)
        with patch.object(proxy_device.socket, "create_connection", return_value=raw), patch.object(proxy_device.ssl, "create_default_context", return_value=context):
            raw.body = connect_response.body
            with self.assertRaises(ConnectionError):
                proxy_device._probe_sync(proxy_device._Proxy("http", "127.0.0.1", 7890, None, None), allow_local=True)

        probe_response = _TlsResponse(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 14\r\n\r\n{\"ip\":\"8.8.8.8\"}")
        context = _TlsContext(probe_response)
        raw = _TlsResponse(None)
        with patch.object(proxy_device.socket, "create_connection", return_value=raw), patch.object(proxy_device.ssl, "create_default_context", return_value=context):
            raw.body = connect_response.body
            self.assertEqual(proxy_device._probe_sync(proxy_device._Proxy("http", "127.0.0.1", 7890, None, None), allow_local=True), "8.8.8.8")

    def test_socks5_probe_rejects_non_global_exit(self):
        probe_response = _TlsResponse(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\n\r\n{\"ip\":\"10.0.0.7\"}")
        context = _TlsContext(probe_response)
        sock = _TlsResponse(None)
        with patch.object(proxy_device, "_socks5_connect", return_value=sock), patch.object(proxy_device.ssl, "create_default_context", return_value=context):
            with self.assertRaises(ConnectionError):
                proxy_device._probe_sync(proxy_device._Proxy("socks5", "proxy.example.test", 1080, None, None))

    def test_bracketed_ipv6_is_bare_for_sockets_and_single_bracketed_in_url(self):
        ipv6 = "2001:4860:4860::8888"
        config = {"proxy_type": "https", "host": f"[{ipv6}]", "port": 443}
        proxy = proxy_device._validate_config(config)
        self.assertEqual(proxy.host, ipv6)

        calls = []
        upstream = _TlsResponse(b"HTTP/1.1 200 Connection established\r\n\r\n")
        context = _TlsContext(upstream)
        with patch.object(proxy_device, "_reject_local_addresses"), patch.object(proxy_device.socket, "create_connection", side_effect=lambda address, timeout: calls.append((address, timeout)) or upstream), patch.object(proxy_device.ssl, "create_default_context", return_value=context):
            proxy_device._connect_upstream(proxy_device._Proxy("https", f"[{ipv6}]", 443, None, None), 2.0)
        self.assertEqual(calls[0][0], (ipv6, 443))
        self.assertEqual(context.server_hostname, ipv6)

        captured = {}

        class FakeProxy:
            def __init__(self, url, auth=None):
                captured["url"] = url
                captured["auth"] = auth

        class FakeStream:
            def __enter__(self):
                return types.SimpleNamespace(status_code=200, iter_bytes=lambda: [b'{"ip":"8.8.8.8"}'])

            def __exit__(self, *args):
                return False

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args):
                return FakeStream()

        fake_httpx = types.SimpleNamespace(Proxy=FakeProxy, Client=FakeClient)
        with patch.dict(sys.modules, {"httpx": fake_httpx}), patch.object(proxy_device, "_reject_local_addresses"):
            self.assertEqual(proxy_device._probe_sync(proxy_device._Proxy("https", f"[{ipv6}]", 443, None, None)), "8.8.8.8")
        self.assertEqual(captured["url"], f"https://[{ipv6}]:443")

    def test_mismatched_or_nested_ipv6_brackets_are_rejected(self):
        for host in ("[2001:db8::1", "2001:db8::1]", "[[2001:db8::1]]"):
            with self.assertRaises(proxy_device.AdapterError, msg=host):
                proxy_device._validate_config({"proxy_type": "http", "host": host, "port": 8080})


if __name__ == "__main__":
    unittest.main()
