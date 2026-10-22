#!/usr/bin/env python3
"""Root side of the d3m0n worker IPC boundary.

This module is installed only by the parent as a root-owned executable.  It is
socket-activated with Accept=yes, so fd 0 is exactly one AF_UNIX stream.  The
only protocol byte consumed here is the one-byte hello (P=probe, R=run).  The
parent protocol is deliberately opaque and remains a pipe protocol owned by
the worker; the dispatcher never parses or logs it.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import time
try:
    import pwd
except ImportError:  # Offline tests run on Windows; production is Ubuntu.
    class _PwdCompat:
        @staticmethod
        def getpwnam(_name: str):
            raise KeyError(_name)
    pwd = _PwdCompat()

CONFIG_PATH = "/etc/d3m0n-ai-worker/runtime.json"
EXPECTED = {
    "prefix": "/opt/d3m0n-ai-worker",
    "socket_path": "/run/d3m0n-ai-worker.sock",
    "wrapper_path": "/usr/local/libexec/d3m0n-ai-worker",
    "client_path": "/usr/local/libexec/d3m0n-ai-worker-client",
    "source_root": "/opt/d3m0n-ai-worker/source",
    "sdk_root": "/opt/d3m0n-ai-worker/sdk",
    "venv_root": "/opt/d3m0n-ai-worker/venv",
    "config_path": CONFIG_PATH,
}
HELLO_RUN = b"R"
HELLO_PROBE = b"P"
HELLO_ACK = b"+"
HANDSHAKE_TIMEOUT = 5.0
CHILD_GRACE_SECONDS = 2.0
PEERCRED_SIZE = struct.calcsize("3i")
SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
EXEC_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}


def load_config() -> dict[str, str]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict) or any(config.get(key) != value for key, value in EXPECTED.items()):
            raise ValueError("fixed runtime paths do not match")
        for key in ("worker_user", "worker_group"):
            value = config.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,62}", value) or value == "root":
                raise ValueError("invalid dedicated worker identity")
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("runtime configuration is unavailable or invalid") from exc


class ProtocolError(Exception):
    """A malformed or incomplete fixed handshake."""


def peer_is_operator(conn: socket.socket, worker_user: str) -> bool:
    """Accept exactly the UID resolved from the generated dedicated account."""
    try:
        operator_uid = pwd.getpwnam(worker_user).pw_uid
        raw = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, PEERCRED_SIZE)
        _pid, peer_uid, _gid = struct.unpack("3i", raw)
    except (KeyError, OSError, struct.error):
        return False
    return peer_uid == operator_uid


def read_hello(conn: socket.socket) -> bytes:
    """Read exactly one bounded hello byte; never consume parent payload."""
    conn.settimeout(HANDSHAKE_TIMEOUT)
    hello = conn.recv(1)
    if hello not in (HELLO_RUN, HELLO_PROBE):
        raise ProtocolError("invalid worker handshake")
    return hello


def _argv_for(hello: bytes, wrapper: str) -> list[str]:
    if hello == HELLO_PROBE:
        return [wrapper, "--probe"]
    if hello == HELLO_RUN:
        return [wrapper]
    raise ProtocolError("invalid worker mode")


def _set_parent_death_signal() -> None:
    """Make the exec'd wrapper terminate if this socket handler dies."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        os._exit(127)
    # Linux PR_SET_PDEATHSIG is operation 1.  Do not retain arbitrary caller
    # state across the root boundary; failure is fatal before exec.
    if prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        os._exit(127)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + CHILD_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def exec_child(conn: socket.socket, argv: list[str], wrapper: str) -> None:
    """Run the fixed wrapper while the root parent watches socket hangup."""
    monitor_fd = os.dup(conn.fileno())
    pid = os.fork()
    if pid == 0:
        try:
            os.close(monitor_fd)
            _set_parent_death_signal()
            os.setpgid(0, 0)
            os.dup2(conn.fileno(), 0)
            os.dup2(conn.fileno(), 1)
            if conn.fileno() > 1:
                conn.detach()
            # argv and environment are constants selected only by the one-byte
            # hello.  No client-controlled shell, path, or variable is passed.
            os.execve(wrapper, argv, EXEC_ENV)
        except BaseException:
            os._exit(127)

    try:
        conn.close()
        poller = select.poll()
        poller.register(monitor_fd, select.POLLHUP | select.POLLERR)
        while True:
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return
            events = poller.poll(100)
            if any(mask & (select.POLLHUP | select.POLLERR) for _fd, mask in events):
                _kill_group(pid)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
                return
    finally:
        os.close(monitor_fd)


def dispatch(conn: socket.socket) -> None:
    config = load_config()
    if not peer_is_operator(conn, config["worker_user"]):
        raise PermissionError("untrusted peer")
    hello = read_hello(conn)
    argv = _argv_for(hello, config["wrapper_path"])
    # The ack is the only response before exec.  Crucially, no recv() occurs
    # between this point and the wrapper child starting, so bootstrap bytes
    # remain exclusively the worker's stdin.
    conn.sendall(HELLO_ACK)
    conn.settimeout(None)
    exec_child(conn, argv, config["wrapper_path"])


def main() -> int:
    try:
        conn = socket.socket(fileno=os.dup(0))
        try:
            dispatch(conn)
        finally:
            conn.close()
    except (PermissionError, ProtocolError, TimeoutError, OSError):
        print("d3m0n-ai-worker-dispatcher: rejected IPC connection", file=sys.stderr)
        return 77
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
