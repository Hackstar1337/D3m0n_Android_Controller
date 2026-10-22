#!/usr/bin/env python3
"""Unprivileged bridge for the existing worker stdin/stdout pipe.

The client has no authority to select a command, path, environment, or peer.
It sends one hello byte and then copies opaque bytes in both directions.  The
parent's newline-JSON protocol is intentionally not parsed or logged here.
"""
from __future__ import annotations

import errno
import json
import os
import re
import selectors
import socket
import sys
import time
from typing import BinaryIO

CONFIG_PATH = "/etc/d3m0n-ai-worker/runtime.json"
SOCKET_PATH = "/run/d3m0n-ai-worker.sock"
HELLO_RUN = b"R"
HELLO_PROBE = b"P"
HELLO_ACK = b"+"
MAX_CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5.0


def load_socket_path() -> str:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict) or config.get("config_path") != CONFIG_PATH:
            raise ValueError
        expected = {
            "prefix": "/opt/d3m0n-ai-worker",
            "socket_path": SOCKET_PATH,
            "wrapper_path": "/usr/local/libexec/d3m0n-ai-worker",
            "client_path": "/usr/local/libexec/d3m0n-ai-worker-client",
            "source_root": "/opt/d3m0n-ai-worker/source",
            "sdk_root": "/opt/d3m0n-ai-worker/sdk",
            "venv_root": "/opt/d3m0n-ai-worker/venv",
        }
        if any(config.get(key) != value for key, value in expected.items()):
            raise ValueError
        for key in ("worker_user", "worker_group"):
            if not isinstance(config.get(key), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,62}", config[key]) or config[key] == "root":
                raise ValueError
        return SOCKET_PATH
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError("runtime configuration is unavailable or invalid")


def _fail(message: str, code: int = 70) -> int:
    print(f"d3m0n-ai-worker-client: {message}", file=sys.stderr)
    return code


def _copy_once(source: BinaryIO, destination: socket.socket | BinaryIO) -> bool:
    data = source.read(MAX_CHUNK)
    if not data:
        return False
    if isinstance(destination, socket.socket):
        destination.sendall(data)
    else:
        destination.write(data)
        destination.flush()
    return True


def relay(sock: socket.socket, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Copy stdin->socket and socket->stdout without interpreting either stream."""
    selector = selectors.DefaultSelector()
    stdin_fd = stdin.fileno()
    sock_fd = sock.fileno()
    selector.register(stdin_fd, selectors.EVENT_READ, "stdin")
    selector.register(sock_fd, selectors.EVENT_READ, "socket")
    stdin_open = True
    socket_open = True
    try:
        while socket_open:
            for key, _events in selector.select(timeout=CONNECT_TIMEOUT):
                if key.data == "stdin" and stdin_open:
                    # BufferedReader.read(n) waits for n bytes or EOF on a pipe.
                    # os.read returns currently available bytes for interactive RPC.
                    data = os.read(stdin_fd, MAX_CHUNK)
                    if data:
                        sock.sendall(data)
                    else:
                        stdin_open = False
                        selector.unregister(stdin_fd)
                        # Half-close only the client->worker direction.  This
                        # is a protocol EOF, not cancellation; output remains
                        # readable until the worker exits.
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                elif key.data == "socket":
                    data = sock.recv(MAX_CHUNK)
                    if not data:
                        socket_open = False
                        break
                    stdout.write(data)
                    stdout.flush()
    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        if getattr(exc, "errno", None) not in (errno.EPIPE, errno.ECONNRESET, None):
            return _fail("relay failed")
        return 0
    finally:
        selector.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--probe"]):
        return _fail("only the literal --probe argument is accepted", 64)
    hello = HELLO_PROBE if args == ["--probe"] else HELLO_RUN
    try:
        socket_path = load_socket_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(socket_path)
            sock.sendall(hello)
            ack = sock.recv(1)
            if ack != HELLO_ACK:
                return _fail("root handler rejected handshake")
            sock.settimeout(None)
            return relay(sock, sys.stdin.buffer, sys.stdout.buffer)
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError, RuntimeError):
        return _fail("worker socket is unavailable")


if __name__ == "__main__":
    raise SystemExit(main())
