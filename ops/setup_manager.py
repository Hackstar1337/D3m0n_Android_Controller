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
import platform
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


def _run_install_command(command: list[str], *, logger, label: str) -> None:
    """Run an installer command without copying its output to the terminal."""
    from ops.install_state import redact_text
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError as exc:
        logger.write(f'{label} could not start: {redact_text(exc)}', level='error')
        raise RuntimeError(f'{label} could not start. Private diagnostics: {logger.path}') from exc
    output = (result.stdout or '') + (('\n' + result.stderr) if result.stderr else '')
    logger.command_output(output, exit_code=result.returncode)
    if result.returncode:
        raise RuntimeError(f'{label} failed with exit code {result.returncode}. Private diagnostics: {logger.path}')


def _frontend_build_recovery(root: Path, directory: Path) -> None:
    """Recover an interrupted isolated frontend build without touching a good dist."""
    marker = Path(directory) / 'frontend-build.json'
    if not marker.is_file():
        return
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
        destination = (root / 'frontend' / 'dist').resolve()
        if Path(str(value.get('destination', ''))).resolve() != destination:
            return
        staging_value = value.get('staging', '')
        staging = Path(str(staging_value)).resolve() if staging_value else None
        backup_value = value.get('backup', '')
        backup = Path(str(backup_value)).resolve() if backup_value else None
        if staging and staging.parent not in {Path(directory).resolve(), destination.parent}:
            return
        if staging and staging.parent == destination.parent and not staging.name.startswith('.d3m0n-frontend-staged-'):
            return
        if backup and backup.parent != destination.parent:
            return
        # Older markers may describe the pre-build move; restore that artifact
        # only when the live destination is absent. New markers leave dist alone.
        if not destination.exists() and backup and backup.exists():
            os.replace(backup, destination)
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        marker.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Do not guess at arbitrary directories when a recovery marker is malformed.
        return


def _build_frontend_staged(npm: str, *, directory: Path, logger) -> Path:
    """Build away from the live artifact, then atomically publish it."""
    from ops.install_state import atomic_promote_directory
    frontend = ROOT / 'frontend'
    destination = frontend / 'dist'
    _frontend_build_recovery(ROOT, directory)
    staging = frontend / f'.d3m0n-frontend-staged-{secrets.token_hex(8)}'
    marker = directory / 'frontend-build.json'
    from ops.platform_config import atomic_write_json
    atomic_write_json(marker, {'destination': str(destination.resolve()), 'backup': None,
                               'staging': str(staging.resolve())})
    try:
        _run_install_command([npm, '--prefix', str(frontend), 'run', 'type-check'], logger=logger,
                              label='Frontend type check')
        _run_install_command([npm, '--prefix', str(frontend), 'run', 'build-only', '--',
                              '--outDir', str(staging)], logger=logger, label='Frontend build')
        if not staging.is_dir() or not (staging / 'index.html').is_file():
            raise RuntimeError(f'Frontend build produced no valid index.html. Private diagnostics: {logger.path}')
        atomic_promote_directory(staging, destination, directory)
        marker.unlink(missing_ok=True)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        marker.unlink(missing_ok=True)
        raise


def _install_provenance() -> dict[str, str]:
    from ops.install_state import source_revision
    node = shutil.which('node.exe' if os.name == 'nt' else 'node')
    node_version = 'unavailable'
    if node:
        try:
            node_version = subprocess.run([node, '--version'], capture_output=True, text=True,
                                          check=False, timeout=5).stdout.strip() or 'unknown'
        except (OSError, subprocess.SubprocessError):
            node_version = 'unknown'
    return {'source_revision': source_revision(ROOT), 'python': platform.python_version(),
            'platform': platform.platform(), 'node': node_version}


def _install_stage(state: dict, state_path: Path, name: str, fingerprint: str, action, *,
                   outputs=(), output_root: Path | None = None, validator=None) -> None:
    from ops.install_state import StageLogger, mark_stage, stage_complete
    log_dir = state_path.parent / 'logs'
    if stage_complete(state, name, fingerprint, root=output_root):
        if validator is None or validator():
            return
    with StageLogger(log_dir, name) as logger:
        logger.write('stage started', fingerprint=fingerprint)
        try:
            action(logger)
            mark_stage(state, name, fingerprint, outputs=outputs, root=output_root, log_path=logger.path)
            from ops.install_state import save_install_state
            save_install_state(state, state_path)
            logger.write('stage completed')
        except BaseException as exc:
            logger.write(f'stage failed: {exc}', level='error')
            raise


def _tool_version(command: str, argument: str = '--version') -> str:
    try:
        result = subprocess.run([command, argument], cwd=ROOT, capture_output=True, text=True,
                                check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (result.stdout or result.stderr or '').strip()


def _validate_prerequisites(logger) -> None:
    """Validate the guided runtime contract without installing host software."""
    from ops.compatibility import load_compatibility, node_supported, python_supported
    manifest = load_compatibility(ROOT / 'ops' / 'install-compatibility.json')
    python_version = platform.python_version()
    if not python_supported(python_version, guided=True):
        raise RuntimeError(f'Python {python_version} is outside the guided compatibility range; use Python 3.11 or 3.12.')
    node = shutil.which('node.exe' if os.name == 'nt' else 'node')
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not node or not npm:
        raise RuntimeError('Node.js and npm are required by the native installer; run the platform prerequisite installer first.')
    node_version = _tool_version(node)
    if not node_supported(node_version):
        raise RuntimeError(f'Node.js {node_version or "unknown"} is outside the compatibility range in install-compatibility.json.')
    logger.write('Prerequisite compatibility validated', python=python_version, node=node_version,
                 npm=_tool_version(npm), profiles=manifest.get('diagnostic_profiles', {}))


_BACKEND_IMPORTS = (
    'fastapi', 'uvicorn', 'sqlalchemy', 'alembic', 'pydantic', 'pydantic_settings',
    'requests', 'dotenv', 'yaml', 'psutil', 'docker', 'openai', 'mcp', 'langfuse',
    'cryptography', 'httpx', 'multipart', 'PIL', 'websocket', 'websockets', 'gitd', 'ops',
)


def _backend_is_healthy(python: Path) -> bool:
    """Check the installed interpreter, dependency metadata, and import surface."""
    if not python.is_file():
        return False
    try:
        checked = subprocess.run([str(python), '-m', 'pip', 'check'], cwd=ROOT,
                                 capture_output=True, text=True, check=False, timeout=60)
        if checked.returncode:
            return False
        script = 'import ' + ','.join(_BACKEND_IMPORTS)
        imported = subprocess.run([str(python), '-c', script], cwd=ROOT,
                                  capture_output=True, text=True, check=False, timeout=60)
        return imported.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
        from ops.install_state import (fingerprint_bytes, fingerprint_paths, fingerprint_tree,
                                       load_install_state, mark_stage, new_install_state,
                                       save_install_state, sha256_file, recover_atomic_promotion)
        runtime = runtime_dir()
        recover_atomic_promotion(runtime, destination=ROOT / 'frontend' / 'dist')
        state_path = runtime / 'installed.json'
        state = load_install_state(state_path, expected_repo=ROOT)
        if state is None:
            state = new_install_state(ROOT, provenance=_install_provenance())
            save_install_state(state, state_path)
        else:
            state['provenance'] = _install_provenance()
            save_install_state(state, state_path)
        pyproject = ROOT / 'pyproject.toml'
        compat = ROOT / 'ops' / 'install-compatibility.json'
        backend_lock = ROOT / 'requirements' / 'backend.lock'
        build_lock = ROOT / 'requirements' / 'build.lock'
        if not backend_lock.is_file() or not build_lock.is_file():
            raise RuntimeError('Native install requires requirements/backend.lock and requirements/build.lock; no unpinned fallback is allowed.')
        prereq_fp = fingerprint_bytes((fingerprint_paths([pyproject, compat]) + json.dumps(_install_provenance(), sort_keys=True)).encode())
        _install_stage(state, state_path, 'prerequisites', prereq_fp,
                       _validate_prerequisites, outputs=[pyproject, compat], output_root=ROOT)
        backend_inputs = [pyproject, backend_lock, build_lock]
        backend_fp = fingerprint_bytes((fingerprint_paths(backend_inputs) + platform.python_version()).encode())
        py = str(venv_python())
        def install_backend(logger):
            _run_install_command([sys.executable, '-m', 'venv', str(ROOT / '.venv')], logger=logger,
                                 label='Python environment creation')
            _run_install_command([py, '-m', 'pip', 'install', '--require-hashes', '-r', str(backend_lock)],
                                 logger=logger, label='Backend dependency installation')
            _run_install_command([py, '-m', 'pip', 'install', '--require-hashes', '-r', str(build_lock)],
                                 logger=logger, label='Locked build dependency installation')
            _run_install_command([py, '-m', 'pip', 'install', '--no-deps',
                                  '--no-build-isolation', '-e', str(ROOT)], logger=logger,
                                 label='Editable project installation')
            if not _backend_is_healthy(venv_python()):
                raise RuntimeError('Backend pip check/import validation failed after installation.')
        _install_stage(state, state_path, 'backend_dependencies', backend_fp, install_backend,
                       outputs=[venv_python()], output_root=ROOT,
                       validator=lambda: _backend_is_healthy(venv_python()))
        py = str(venv_python())
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not npm:
            raise RuntimeError('npm is missing. Rerun the guided OS prerequisite installer.')
        package_json = ROOT / 'frontend' / 'package.json'
        package_lock = ROOT / 'frontend' / 'package-lock.json'
        frontend_fp = fingerprint_bytes((fingerprint_paths([package_json, package_lock]) +
                                         platform.python_version()).encode())
        _install_stage(state, state_path, 'frontend_dependencies', frontend_fp,
                       lambda logger: _run_install_command([npm, 'ci', '--prefix', str(ROOT / 'frontend')],
                                                           logger=logger, label='Frontend dependency installation'),
                       outputs=[ROOT / 'frontend' / 'node_modules' / '.package-lock.json',
                                ROOT / 'frontend' / 'node_modules' / 'vite' / 'package.json'], output_root=ROOT,
                       validator=lambda: subprocess.run([npm, 'ls', '--prefix', str(ROOT / 'frontend'), '--depth=0'],
                                                        capture_output=True, check=False).returncode == 0)
        frontend_source_fp = fingerprint_tree(ROOT / 'frontend', exclude=('node_modules', 'dist'))
        _install_stage(state, state_path, 'frontend_build', frontend_source_fp,
                       lambda logger: _build_frontend_staged(npm, directory=runtime, logger=logger),
                       outputs=[ROOT / 'frontend' / 'dist'], output_root=ROOT)
        auth_file = Path(os.environ.get('D3M0N_DASHBOARD_AUTH_FILE', str(runtime.parent.parent / 'dashboard-auth.json')))
        auth_fp = fingerprint_bytes((str(auth_file) + (sha256_file(auth_file) if auth_file.is_file() else 'missing')).encode())
        _install_stage(state, state_path, 'authentication', auth_fp,
                       lambda logger: (ensure_auth(), logger.write('Dashboard authentication is configured.')),
                       outputs=[auth_file])
    print('Installation complete; existing passwords, .env and phone/profile state were preserved.')
    if not args.no_start:
        start(args.port, args.no_browser)
        # Readiness is intentionally recorded only after the owned supervisor passes
        # its authenticated health check; --no-start leaves it pending for start.
        state = load_install_state(state_path, expected_repo=ROOT) or state
        from ops.install_state import mark_stage, save_install_state
        readiness_fp = fingerprint_bytes((str(args.port) + str(state.get('stages', {}).get('authentication', {}))).encode())
        mark_stage(state, 'readiness', readiness_fp, outputs=[ROOT / 'frontend' / 'dist'], root=ROOT)
        save_install_state(state, state_path)
    else:
        print('Start later: .venv Python -m ops.setup_manager start')
    return 0


def backup_state(source: str | None, destination: str | None) -> int:
    from ops.state_backup import create_backup
    if source is None:
        active = control(load_record())
        if active and active.get('running'):
            raise RuntimeError('The native supervisor is running. Stop it, create the backup, then start it again: '
                               'python -m ops.setup_manager stop; python -m ops.setup_manager backup; '
                               'python -m ops.setup_manager start')
    path = create_backup(Path(source) if source else None, Path(destination) if destination else None)
    print(f'Private state backup created: {path}')
    print('This archive contains secret-bearing state; store it securely and never commit it.')
    return 0


def restore_state(archive: str | None, target: str | None) -> int:
    if not archive or not target:
        raise ValueError('restore requires --archive ARCHIVE and --target NEW_TARGET')
    from ops.state_backup import restore_backup
    path = restore_backup(Path(archive), Path(target))
    print(f'Private state restored to new target: {path}')
    print('The live installation was not overwritten.')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'start', 'status', 'stop', 'backup', 'restore', '_serve'])
    parser.add_argument('--port', type=int, default=5055)
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--instance', help=argparse.SUPPRESS)
    parser.add_argument('--source')
    parser.add_argument('--destination')
    parser.add_argument('--archive')
    parser.add_argument('--target')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        if args.command == 'install':
            return install(args)
        if args.command == 'backup':
            if args.dry_run:
                print('Dry run: no state was read or archive was created.')
                return 0
            return backup_state(args.source, args.destination)
        if args.command == 'restore':
            if args.dry_run:
                print('Dry run: no archive was read and no target was created.')
                return 0
            return restore_state(args.archive, args.target)
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
