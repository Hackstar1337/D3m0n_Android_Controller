"""Local setup and owned background dashboard lifecycle (Python stdlib only).

OS prerequisite installation belongs to install.sh/install.ps1. This module
never installs OS packages, changes a firewall, scans a LAN or runs phone tools.
A private loopback supervisor owns its child; stop never kills a stored PID.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
START_TIMEOUT = 90


def runtime_dir() -> Path:
    from ops.platform_config import ensure_private_dir, state_dir
    key = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    return ensure_private_dir(state_dir() / 'launcher' / key)


def venv_python() -> Path:
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def private_write(path: Path, payload: dict) -> None:
    from ops.platform_config import ensure_private_file
    tmp = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    try:
        with tmp.open('x', encoding='utf-8') as handle:
            ensure_private_file(tmp)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        ensure_private_file(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def lifecycle_lock():
    """Native advisory lock; no stale PID deletion or user-process termination."""
    path = runtime_dir() / 'lifecycle.lock'
    with path.open('a+b') as handle:
        from ops.platform_config import ensure_private_file
        ensure_private_file(path)
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('Another setup/start/stop is in progress. Retry when it finishes.') from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('Another setup/start/stop is in progress. Retry when it finishes.') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load_record() -> dict | None:
    try:
        data = json.loads((runtime_dir() / 'instance.json').read_text(encoding='utf-8'))
        if data.get('repo') != str(ROOT) or not isinstance(data.get('control_port'), int):
            return None
        if not 1 <= data['control_port'] <= 65535 or not isinstance(data.get('control_key'), str):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def control(record: dict | None, action: str = 'status') -> dict | None:
    if not record or action not in {'status', 'stop'}:
        return None
    conn = http.client.HTTPConnection('127.0.0.1', record['control_port'], timeout=2)
    try:
        conn.request('POST', '/' + action, body=b'', headers={
            'X-D3m0n-Launcher': record['control_key'], 'Content-Length': '0',
        })
        response = conn.getresponse()
        if response.status != 200:
            return None
        value = json.loads(response.read(4096))
        if value.get('instance') != record.get('instance'):
            return None
        return value
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def choose_port(preferred: int) -> int:
    if not 1 <= preferred <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    # Never take over an unknown listener or relabel its URL as ours.
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1', preferred))
        except OSError:
            sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def health(port: int, instance: str | None = None) -> bool:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/auth/status', timeout=2) as result:
            payload = json.loads(result.read(4096))
            return (result.status == 200 and payload.get('configured') is True
                    and (instance is None or result.headers.get('X-D3m0n-Instance') == instance))
    except (OSError, ValueError, http.client.HTTPException):
        return False


def clean_child_environment(port: int) -> dict[str, str]:
    # Only OS runtime and explicit application settings cross this boundary.
    # Provider credentials belong in the protected model-profile store, not an
    # unrelated interactive shell's long-lived child environment.
    os_keys = {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','HOME','USERPROFILE',
               'LOCALAPPDATA','APPDATA','TEMP','TMP','TMPDIR','LANG','LC_ALL',
               'XDG_STATE_HOME','XDG_CACHE_HOME','PYTHONPATH','PYTHONUTF8',
               'PYTHONIOENCODING','PYTHONDONTWRITEBYTECODE','DB_PATH','GITD_ENABLE_IOS'}
    sensitive = ('PASSWORD','SECRET','TOKEN','API_KEY','ACCESS_KEY','CREDENTIAL')
    env = {k:v for k,v in os.environ.items() if k.upper() in os_keys or (
        k.startswith('D3M0N_') and not any(word in k.upper() for word in sensitive))}
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8:replace'
    env['D3M0N_HOST'] = '127.0.0.1'
    env['D3M0N_PORT'] = str(port)
    env['PYTHONUNBUFFERED'] = '1'
    # Managed launch is local-only. Do not overwrite .env or existing configs.
    env['D3M0N_ALLOWED_ORIGINS'] = f'http://127.0.0.1:{port},http://localhost:{port}'
    env.pop('D3M0N_PUBLIC_ORIGIN', None)
    return env


def supervise(port: int, instance: str) -> int:
    directory = runtime_dir()
    key = secrets.token_urlsafe(32)
    stopped = threading.Event()
    child = None
    from ops.setup_ownership import WindowsChildJob
    child_job = WindowsChildJob()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            supplied = self.headers.get('X-D3m0n-Launcher', '')
            if (self.path not in {'/status', '/stop'} or self.headers.get('Origin')
                    or not hmac.compare_digest(supplied.encode('utf-8'), key.encode('ascii'))):
                self.send_error(403)
                return
            result = {'instance': instance, 'running': child is not None and child.poll() is None,
                      'port': port}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if self.path == '/stop':
                stopped.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    record = {'repo': str(ROOT), 'instance': instance, 'control_port': server.server_port,
              'control_key': key, 'port': port, 'pid': os.getpid()}
    try:
        env = clean_child_environment(port)
        env['D3M0N_LAUNCH_INSTANCE'] = instance
        child = subprocess.Popen([str(venv_python()), '-m', 'ops.launch', '--host', '127.0.0.1',
                                  '--port', str(port)], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL,
                                 **({'start_new_session': True} if os.name != 'nt' else {}))
        child_job.assign(child)
        thread.start()
        private_write(directory / 'instance.json', record)
        while child.poll() is None and not stopped.wait(0.25):
            pass
        if child.poll() is None:
            if os.name == 'nt':
                child.terminate()
            else:
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    child_job.close()
                else:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        return child.returncode or 0
    finally:
        server.shutdown() if thread.is_alive() else None
        server.server_close()
        child_job.close()
        if child is not None and child.poll() is None:
            if os.name == 'nt':
                child.terminate()
            else:
                os.killpg(child.pid, signal.SIGTERM)
        current = load_record()
        if current and current.get('instance') == instance:
            (directory / 'instance.json').unlink(missing_ok=True)


def ensure_auth() -> None:
    # Runs on the interactive terminal; a password never appears in argv/env/logs.
    subprocess.run([str(venv_python()), '-c',
                    'from ops.launch import _load_env, _auth_setup; _load_env(); _auth_setup(); print("Dashboard authentication ready.")'],
                   cwd=ROOT, check=True)


def start(port: int, no_browser: bool = False) -> str:
    if not venv_python().is_file() or not (ROOT / 'frontend/dist/index.html').is_file():
        raise RuntimeError('Installation is incomplete. Run install.cmd or bash install.sh first.')
    with lifecycle_lock():
        existing = load_record()
        active = control(existing)
        if active and active.get('running'):
            port = active['port']
            if not health(port, existing.get('instance')):
                raise RuntimeError('Owned dashboard is starting or unhealthy. Check its private launcher log.')
        else:
            ensure_auth()
            port = choose_port(port)
            instance = secrets.token_hex(16)
            log = runtime_dir() / 'dashboard.log'
            from ops.platform_config import ensure_private_file
            with log.open('ab') as handle:
                ensure_private_file(log)
                options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session': True}
                runner = subprocess.Popen([str(venv_python()), '-m', 'ops.setup_manager', '_serve',
                                           '--port', str(port), '--instance', instance],
                                          cwd=ROOT, stdin=subprocess.DEVNULL, stdout=handle,
                                          stderr=subprocess.STDOUT, **options)
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                if runner.poll() is not None:
                    raise RuntimeError(f'Dashboard launch failed. Private diagnostics: {log}')
                record = load_record()
                if record and record.get('instance') == instance:
                    active = control(record)
                    if active and active.get('running') and health(port, instance) and runner.poll() is None:
                        break
                time.sleep(0.3)
            else:
                control(load_record(), 'stop')
                raise RuntimeError(f'Dashboard readiness timed out. Private diagnostics: {log}')
    url = f'http://127.0.0.1:{port}/?discover'
    print('\nDASHBOARD READY: ' + url, flush=True)
    print('Use Discover devices to authorize/pair phones. No phone action or AI job was started.')
    print('Stop: python -m ops.setup_manager stop (from this installed checkout).')
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            print('Browser could not be opened automatically; copy the URL above.')
    return url


def install(args) -> int:
    if args.dry_run:
        print('Dry run: create/reuse project .venv; install .[d3m0n]; npm ci/build; prompt for password; health-check and print local URL.')
        return 0
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise RuntimeError('Run the application installer as your normal user, not root. Scoped package-manager commands can use sudo.')
    from ops.setup_ownership import checkout_lock
    with lifecycle_lock(), checkout_lock():
        if control(load_record()):
            raise RuntimeError('This checkout is running. Stop its owned dashboard before updating dependencies.')
        if (ROOT / '.venv').exists() and not (runtime_dir() / 'installed.json').exists():
            if input('An existing environment may belong to an older/foreground service. Have you stopped ALL services using this checkout? [y/N] ').strip().lower() not in {'y','yes'}:
                raise RuntimeError('Existing environment preserved. Stop its services, then rerun setup.')
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
        py = str(venv_python())
        subprocess.run([py, '-m', 'pip', 'install', '--upgrade', 'pip'], cwd=ROOT, check=True)
        subprocess.run([py, '-m', 'pip', 'install', '.[d3m0n]'], cwd=ROOT, check=True)
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not npm:
            raise RuntimeError('npm is missing. Rerun the guided OS prerequisite installer.')
        subprocess.run([npm, 'ci', '--prefix', str(ROOT / 'frontend')], cwd=ROOT, check=True)
        subprocess.run([npm, '--prefix', str(ROOT / 'frontend'), 'run', 'build'], cwd=ROOT, check=True)
        ensure_auth()
        private_write(runtime_dir() / 'installed.json', {'repo': str(ROOT), 'setup_version': 1})
    print('Installation complete; existing passwords, .env and phone/profile state were preserved.')
    if not args.no_start:
        start(args.port, args.no_browser)
    else:
        print('Start later: .venv Python -m ops.setup_manager start')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'start', 'status', 'stop', '_serve'])
    parser.add_argument('--port', type=int, default=5055)
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--instance', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        if args.command == 'install':
            return install(args)
        if args.dry_run:
            print('Dry run: no processes, state, network requests or installations started.')
            return 0
        if args.command == 'start':
            start(args.port, args.no_browser)
        elif args.command == '_serve':
            if not args.instance or len(args.instance) != 32:
                raise RuntimeError('Internal supervisor requires an installation instance identifier')
            return supervise(args.port, args.instance)
        elif args.command == 'status':
            record = load_record()
            result = control(record)
            if result and result.get('running') and health(result['port'], record.get('instance')):
                print(f"DASHBOARD READY: http://127.0.0.1:{result['port']}/?discover")
            else:
                print('This checkout has no healthy owned dashboard running.')
                return 1
        elif args.command == 'stop':
            with lifecycle_lock():
                record = load_record()
                result = control(record, 'stop')
                if not result:
                    print('No owned dashboard was stopped; no PID-based kill was attempted.')
                    return 1
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and control(record):
                    time.sleep(0.25)
                if control(record):
                    raise RuntimeError('Owned supervisor is still stopping; no unrelated process was touched.')
                print('Owned dashboard stopped.')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('Setup error: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
