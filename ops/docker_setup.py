"""Local Docker lifecycle; no phone discovery, provider calls, or volume deletion."""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
COMPOSE_DEV = ROOT / 'compose.dev.yaml'
COMPOSE_PROD = ROOT / 'compose.prod.yaml'
PROJECT_NAME = os.environ.get('D3M0N_COMPOSE_PROJECT', 'd3m0n-droid')
DEFAULT_API_PORT = 5055
DEFAULT_UI_PORT = 5173
START_TIMEOUT = 120.0


def project_name() -> str:
    value = os.environ.get('D3M0N_COMPOSE_PROJECT', PROJECT_NAME)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', value):
        raise ValueError('Invalid Compose project; use lowercase letters, numbers, underscores, or hyphens.')
    return value


def _state_root() -> Path:
    explicit = os.environ.get('D3M0N_DOCKER_STATE_DIR', '').strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path == ROOT or ROOT in path.parents:
            raise ValueError('Docker host state must be outside the source checkout.')
        return path
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    else:
        base = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
    return base / 'd3m0n' / 'docker'


def state_path() -> Path:
    key = hashlib.sha256((str(ROOT.resolve()) + '\0' + project_name()).encode()).hexdigest()[:16]
    return _state_root() / key / 'state.json'


def _write_state(data: dict[str, Any]) -> None:
    from ops.platform_config import atomic_write_json
    atomic_write_json(state_path(), data)


def load_state() -> dict[str, Any] | None:
    try:
        data = json.loads(state_path().read_text(encoding='utf-8'))
        api, ui = int(data['api_port']), int(data['ui_port'])
        if data.get('repo') != str(ROOT.resolve()) or data.get('project', project_name()) != project_name():
            return None
        if not (1 <= api <= 65535 and 1 <= ui <= 65535):
            return None
        if data.get('profile', 'development') not in {'development', 'production'}:
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _port_free(port: int, host: str = '127.0.0.1') -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(preferred: int, *, reserved: Iterable[int] = ()) -> int:
    if not 1 <= preferred <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    reserved_set = set(reserved)
    if preferred not in reserved_set and _port_free(preferred):
        return preferred
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            selected = int(sock.getsockname()[1])
        if selected not in reserved_set:
            return selected
    raise RuntimeError('Could not reserve distinct local ports; retry after inspecting local listeners.')


def select_ports(api_port: int | None = None, ui_port: int | None = None, *, production: bool = False) -> tuple[int, int]:
    previous = load_state() or {}
    api = choose_port(int(api_port or previous.get('api_port', DEFAULT_API_PORT)))
    return (api, api) if production else (api, choose_port(int(ui_port or previous.get('ui_port', DEFAULT_UI_PORT)), reserved=(api,)))


def _run(command: list[str], *, check: bool = True, capture: bool = False,
         input_text: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=check, text=True, input=input_text,
                          capture_output=capture, env=env)


def compose_command(compose_file: Path, args: list[str], *, project: str | None = None) -> list[str]:
    return ['docker', 'compose', '-p', project or project_name(), '-f', str(compose_file), *args]


def check_engine() -> dict[str, str]:
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker client missing; install Docker Desktop and rerun Docker setup.')
    try:
        _run([docker, 'version', '--format', '{{.Client.Version}}'], capture=True)
        compose = _run([docker, 'compose', 'version', '--short'], capture=True)
        info = _run([docker, 'info', '--format', '{{.OSType}} {{.Architecture}}'], capture=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError('Docker/Compose engine unavailable; start Docker Desktop with Linux containers, then retry.') from exc
    engine = info.stdout.strip().split()
    if len(engine) < 2 or engine[0].lower() != 'linux':
        raise RuntimeError('Docker must use a Linux engine; switch to Linux containers.')
    from ops.compatibility import platform_tools_spec
    try:
        platform_tools_spec('linux', engine[1])
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return {'docker': docker, 'compose': compose.stdout.strip(), 'architecture': engine[1]}


def _compose_file(production: bool) -> Path:
    selected = COMPOSE_PROD if production else COMPOSE_DEV
    if not selected.is_file():
        raise RuntimeError(f'Missing Compose file: {selected.name}')
    return selected


def owned_containers() -> list[dict[str, Any]]:
    result = _run(['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={project_name()}'], capture=True)
    ids = result.stdout.split()
    if not ids:
        return []
    inspected = _run(['docker', 'inspect', *ids], capture=True)
    containers = json.loads(inspected.stdout)
    for container in containers:
        labels = container.get('Config', {}).get('Labels', {})
        working = labels.get('com.docker.compose.project.working_dir', '')
        if not working or Path(working).resolve() != ROOT.resolve():
            raise RuntimeError(f'Compose project {project_name()} belongs to another checkout. Set D3M0N_COMPOSE_PROJECT to a new name; no existing service was changed.')
    return containers


def _running_ports(containers: list[dict[str, Any]], *, production: bool) -> tuple[int, int] | None:
    mapping: dict[str, int] = {}
    for container in containers:
        if not container.get('State', {}).get('Running'):
            continue
        service = container['Config']['Labels'].get('com.docker.compose.service')
        key = '5055/tcp' if service == 'backend' else '5173/tcp'
        ports = container.get('NetworkSettings', {}).get('Ports', {}).get(key) or []
        if ports:
            if ports[0].get('HostIp') != '127.0.0.1':
                raise RuntimeError('Existing project is not localhost-only; stop it and review its bind settings.')
            mapping[service] = int(ports[0]['HostPort'])
    if 'backend' not in mapping:
        return None
    return (mapping['backend'], mapping['backend']) if production else ((mapping['backend'], mapping['frontend']) if 'frontend' in mapping else None)


def _image_available(tag: str) -> bool:
    return _run(['docker', 'image', 'inspect', tag], check=False, capture=True).returncode == 0


def _volume_writable(compose_file: Path, *, production: bool = False) -> None:
    probe = "from pathlib import Path; import tempfile; roots=[Path('/state'),Path('/home/droid/.android')]; [(Path(tempfile.NamedTemporaryFile(dir=p,delete=False).name).unlink()) for p in roots]; print('private volumes writable')"
    if _run(compose_command(compose_file, ['exec', '-T', 'backend', 'python', '-c', probe]), check=False, capture=True).returncode:
        raise RuntimeError('Private state/ADB volume is unwritable by the non-root runtime; repair ownership without deleting state.')
    if not production:
        probe_js = "const f=require('fs'); for(const p of ['node_modules','dist']){f.accessSync(p,f.constants.W_OK)}"
        if _run(compose_command(compose_file, ['exec', '-T', 'frontend', 'node', '-e', probe_js]), check=False, capture=True).returncode:
            raise RuntimeError('Frontend dependency/build volume is unwritable; repair ownership without removing private state.')


def _http_status(url: str, timeout: float = 2.0) -> tuple[int, dict[str, Any] | None]:
    try:
        from urllib.parse import urlsplit
        accept = 'application/json' if urlsplit(url).path.startswith('/api/') else 'text/html'
        with build_opener(ProxyHandler({})).open(Request(url, headers={'Accept': accept}), timeout=timeout) as response:
            raw = response.read(64 * 1024)
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                payload = None
            return int(response.status), payload
    except HTTPError as exc:
        return int(exc.code), None
    except (OSError, URLError, ValueError):
        return 0, None


def liveness(api_port: int, ui_port: int) -> bool:
    return _http_status(f'http://127.0.0.1:{api_port}/api/health')[0] == 200 and _http_status(f'http://127.0.0.1:{ui_port}/')[0] == 200


def readiness(api_port: int, ui_port: int) -> dict[str, Any]:
    results = []
    for port in dict.fromkeys((api_port, ui_port)):
        health, _ = _http_status(f'http://127.0.0.1:{port}/api/health')
        page, _ = _http_status(f'http://127.0.0.1:{port}/')
        auth, payload = _http_status(f'http://127.0.0.1:{port}/api/auth/status')
        protected, _ = _http_status(f'http://127.0.0.1:{port}/api/phone/devices')
        results.append({'port': port, 'health': health, 'page': page,
                        'auth_configured': auth == 200 and isinstance(payload, dict) and payload.get('configured') is True,
                        'protected_api': protected in (401, 403)})
    live = all(r['health'] == 200 and r['page'] == 200 for r in results)
    configured = all(r['auth_configured'] for r in results)
    protected = all(r['protected_api'] for r in results)
    return {'liveness': live, 'auth_configured': configured, 'protected_api': protected,
            'ready': live and configured and protected, 'origins': results}


def wait_readiness(api_port: int, ui_port: int, *, timeout: float = START_TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = readiness(api_port, ui_port)
        if last.get('ready'):
            return last
        time.sleep(1)
    return last


def _configure_auth(compose_file: Path) -> None:
    if not sys.stdin.isatty():
        raise RuntimeError('Password is unconfigured. Use an interactive terminal: dev.ps1 password; no default password was generated.')
    print('Create the dashboard password now (hidden input).')
    if _run(compose_command(compose_file, ['exec', 'backend', 'python', '-m', 'ops.dashboard_auth', 'set-password']), check=False).returncode:
        raise RuntimeError('Password setup cancelled/failed; rerun setup to complete the hidden prompt.')


def _image_fingerprint() -> str:
    from ops.install_state import fingerprint_paths
    names = ['Dockerfile.dev', '.dockerignore', 'requirements/backend.lock', 'requirements/dev.lock', 'requirements/build.lock',
             'pyproject.toml', 'ops/install-compatibility.json', 'ops/compatibility.py', 'scripts/install_platform_tools.py',
             'scripts/install_container_platform_tools.py', 'scripts/frontend_entrypoint.sh', 'frontend/package.json', 'frontend/package-lock.json']
    return fingerprint_paths([ROOT / name for name in names], root=ROOT)


def _save_running_state(api_port: int, ui_port: int, *, production: bool, engine: dict[str, str]) -> dict[str, Any]:
    from ops.install_state import source_revision
    project = project_name()
    data = {'version': 2, 'repo': str(ROOT.resolve()), 'project': project, 'mode': 'docker',
            'profile': 'production' if production else 'development', 'compose_file': str(_compose_file(production)),
            'api_port': api_port, 'ui_port': ui_port, 'engine_architecture': engine['architecture'],
            'source_revision': source_revision(ROOT), 'image_fingerprint': _image_fingerprint(),
            'updated_at': _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}
    for name in ('private_state', 'adb_identity', 'frontend_modules', 'frontend_dist'):
        data[f'{name}_volume'] = f'{project}_{name}'
    _write_state(data)
    return data


def bootstrap(*, api_port: int | None = None, ui_port: int | None = None,
              production: bool = False, no_start: bool = False, dry_run: bool = False,
              rebuild: bool = False, no_browser: bool = False) -> int:
    compose_file = _compose_file(production)
    if dry_run:
        api, ui = select_ports(api_port, ui_port, production=production)
        print(f'Dry run: project {project_name()}, candidate API {api}, UI {ui}; no engine, state, password, or service changes.')
        return 0
    engine = check_engine()
    containers = owned_containers()
    previous = load_state() or {}
    running = _running_ports(containers, production=production)
    if previous and previous.get('profile') != ('production' if production else 'development') and any(c.get('State', {}).get('Running') for c in containers):
        raise RuntimeError('Stop this project before switching development/production profiles; private volumes are preserved.')
    reuse = running and (api_port is None or api_port == running[0]) and (ui_port is None or ui_port == running[1])
    api, ui = running if reuse else select_ports(api_port, ui_port, production=production)
    env = os.environ.copy()
    env.update({'D3M0N_COMPOSE_PROJECT': project_name(), 'D3M0N_HOST_API_PORT': str(api), 'D3M0N_HOST_UI_PORT': str(ui)})
    required = (f'{project_name()}-backend:prod',) if production else (f'{project_name()}-backend:dev', f'{project_name()}-frontend:dev')
    if rebuild or previous.get('image_fingerprint') != _image_fingerprint() or not all(_image_available(tag) for tag in required):
        _run(compose_command(compose_file, ['build', '--pull=false']), env=env)
    if no_start:
        print('Installation images built; --no-start preserved all services. Start later from this checkout.')
        return 0
    if _run(compose_command(compose_file, ['up', '-d', '--wait', '--wait-timeout', '180']), check=False, env=env).returncode:
        raise RuntimeError('Docker startup failed. Run dev.ps1 logs; do not delete volumes to repair it.')
    # Record actual owned mapping before a possible first-password cancellation,
    # ensuring reruns rejoin the same services instead of choosing new ports.
    _save_running_state(api, ui, production=production, engine=engine)
    _volume_writable(compose_file, production=production)
    first = readiness(api, ui)
    if not first.get('liveness'):
        raise RuntimeError('Dashboard or frontend API proxy is not responding; inspect service logs.')
    if not first['auth_configured']:
        _configure_auth(compose_file)
    result = wait_readiness(api, ui)
    if not result.get('ready'):
        raise RuntimeError('Readiness failed: both origins need a page, API proxy, configured authentication, and a denied unauthenticated protected API.')
    url = f'http://127.0.0.1:{ui}/'
    print(f'D3M0N DOCKER READY: {url}')
    print(f'Built dashboard/API: http://127.0.0.1:{api}/')
    if not no_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            print('Open the printed URL in your browser.')
    return 0


def status() -> int:
    data = load_state()
    if not data:
        print('No recorded Docker install; run the Docker start command to adopt this checkout\'s services.')
        return 1
    result = readiness(int(data['api_port']), int(data['ui_port']))
    print(json.dumps({'project': data['project'], 'profile': data['profile'], 'api_url': f"http://127.0.0.1:{data['api_port']}/", 'ui_url': f"http://127.0.0.1:{data['ui_port']}/", 'readiness': result}, indent=2))
    return 0 if result['ready'] else 1


def lifecycle(action: str, *, production: bool = False) -> int:
    check_engine()
    owned_containers()
    data = load_state() or {}
    compose = _compose_file(data.get('profile') == 'production' if data else production)
    args = ['logs', '--follow', '--tail', '80'] if action == 'logs' else ['stop']
    return _run(compose_command(compose, args), check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'start', 'status', 'stop', 'logs'])
    parser.add_argument('--mode', choices=['native', 'docker'], default='docker')
    parser.add_argument('--production', action='store_true')
    parser.add_argument('--api-port', type=int)
    parser.add_argument('--ui-port', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'native':
            from ops.setup_manager import main as native
            forwarded = [args.command]
            for flag, yes in [('--dry-run', args.dry_run), ('--no-start', args.no_start), ('--no-browser', args.no_browser)]:
                if yes:
                    forwarded.append(flag)
            if args.api_port:
                forwarded += ['--port', str(args.api_port)]
            return native(forwarded)
        if args.command in {'install', 'start'}:
            return bootstrap(api_port=args.api_port, ui_port=args.ui_port, production=args.production,
                             no_start=args.no_start, dry_run=args.dry_run, rebuild=args.rebuild, no_browser=args.no_browser)
        if args.dry_run:
            print('Dry run: no Docker lifecycle effects.')
            return 0
        return status() if args.command == 'status' else lifecycle(args.command, production=args.production)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Docker setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
