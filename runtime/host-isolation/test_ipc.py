#!/usr/bin/env python3
"""Offline tests for the root/ash IPC boundary; no systemd, SSH, SDK, or network.

Run with: python3 host-isolation/test_ipc.py
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import socket
import struct
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent

def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

DISPATCHER = load("ipc_dispatcher", HERE / "dispatcher.py")
CLIENT = load("ipc_client", HERE / "client.py")

class FakePeer:
    def __init__(self, uid: int):
        self.uid = uid
        self.calls: list[int] = []
    def getsockopt(self, level, option, size):
        self.calls.append(size)
        return struct.pack("3i", 1234, self.uid, 1000)

class HelloSocket:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.recv_sizes: list[int] = []
    def settimeout(self, value):
        pass
    def recv(self, size: int) -> bytes:
        self.recv_sizes.append(size)
        return self.payload[:size]

class IPCContractTests(unittest.TestCase):
    def test_hello_is_one_byte_and_only_two_values(self):
        for hello in (b"P", b"R"):
            sock = HelloSocket(hello + b'{"should_not_be_read":true}')
            self.assertEqual(DISPATCHER.read_hello(sock), hello)
            self.assertEqual(sock.recv_sizes, [1])
        for invalid in (b"", b"X", b"run"):
            with self.assertRaises(DISPATCHER.ProtocolError):
                DISPATCHER.read_hello(HelloSocket(invalid))

    def test_peer_uid_is_exact_ash_uid(self):
        with mock.patch.object(DISPATCHER.pwd, "getpwnam", return_value=mock.Mock(pw_uid=1000)):
            self.assertTrue(DISPATCHER.peer_is_ash(FakePeer(1000)))
            self.assertFalse(DISPATCHER.peer_is_ash(FakePeer(0)))
            self.assertFalse(DISPATCHER.peer_is_ash(FakePeer(1001)))

    def test_exact_wrapper_argv_and_fixed_environment(self):
        calls = []
        for hello, expected in ((b"R", [DISPATCHER.WRAPPER]), (b"P", [DISPATCHER.WRAPPER, "--probe"])):
            sock = HelloSocket(hello)
            sock.sendall = lambda value: calls.append(("ack", value))
            with mock.patch.object(DISPATCHER, "peer_is_ash", return_value=True), mock.patch.object(
                DISPATCHER, "exec_child", side_effect=lambda s, mode: calls.append(("exec", mode))
            ):
                DISPATCHER.dispatch(sock)
            self.assertEqual(calls[-2:], [("ack", b"+"), ("exec", expected)])

    def test_client_argument_gate(self):
        self.assertEqual(CLIENT.main(["--bad"]), 64)

    def test_units_pin_boundary(self):
        socket_unit = (HERE / "d3m0n-ai-worker.socket").read_text()
        service_unit = (HERE / "d3m0n-ai-worker@.service").read_text()
        for text in (socket_unit, service_unit):
            self.assertNotIn("ExecStart=/bin/sh", text)
        self.assertIn("ListenStream=/run/d3m0n-ai-worker.sock", socket_unit)
        self.assertIn("SocketMode=0660", socket_unit)
        self.assertIn("SocketUser=root", socket_unit)
        self.assertIn("SocketGroup=ash", socket_unit)
        self.assertIn("Accept=yes", socket_unit)
        self.assertIn("MaxConnections=3", socket_unit)
        self.assertIn("ProtectHome=yes", service_unit)
        self.assertIn("ProtectSystem=strict", service_unit)
        self.assertIn("PrivateNetwork=yes", service_unit)
        self.assertIn("KillMode=control-group", service_unit)

    def test_no_protocol_logging_or_arbitrary_exec_surface(self):
        source = (HERE / "dispatcher.py").read_text()
        self.assertIn("recv(1)", source)
        self.assertIn("os.execve(WRAPPER", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.environ", source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
