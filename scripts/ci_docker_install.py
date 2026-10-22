"""Executable clean Docker/ZIP acceptance; uses only a new explicit project."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


def request(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers={'content-type':'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b'{}')
    except HTTPError as exc:
        return exc.code, {}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(root: Path, project: str, *, skip_build: bool = False) -> None:
    if not project.startswith('d3m0n-ci-'):
        raise ValueError('Disposable acceptance project must start with d3m0n-ci-.')
    env = os.environ.copy()
    api, ui = free_port(), free_port()
    while ui == api:
        ui = free_port()
    env.update({'D3M0N_COMPOSE_PROJECT':project, 'D3M0N_HOST_API_PORT':str(api), 'D3M0N_HOST_UI_PORT':str(ui)})
    compose = ['docker','compose','-p',project,'-f',str(root/'compose.dev.yaml')]
    # Refuse to adopt/delete someone else's named acceptance project.
    existing = subprocess.run(compose + ['ps','-aq'], cwd=root, env=env, capture_output=True, text=True, check=True)
    volumes = subprocess.run(['docker','volume','ls','--format','{{.Name}}'], capture_output=True, text=True, check=True).stdout.split()
    if existing.stdout.strip() or any(name.startswith(project+'_') for name in volumes):
        raise RuntimeError('Acceptance project already exists; choose a new name.')
    def cmd(args, **kwargs):
        return subprocess.run(compose + args, cwd=root, env=env, check=True, **kwargs)
    password = secrets.token_urlsafe(24)
    # Hash inside the locked backend image, not the dependency-free host runner.
    hash_code = "import json,sys; from ops.dashboard_auth import hash_password; print(json.dumps(hash_password(sys.stdin.read())))"
    started = False
    try:
        cmd(['config','--quiet'])
        if not skip_build:
            cmd(['build'])
        started = True
        cmd(['up','-d','--wait','--wait-timeout','180'])
        for port in [api, ui]:
            if request(f'http://127.0.0.1:{port}/api/health')[0] != 200:
                raise RuntimeError('API/frontend proxy liveness failed.')
            status, payload = request(f'http://127.0.0.1:{port}/api/auth/status')
            if status != 200 or payload.get('configured') is not False:
                raise RuntimeError('Fresh disposable state unexpectedly contains authentication.')
            if request(f'http://127.0.0.1:{port}/api/phone/devices')[0] != 503:
                raise RuntimeError('Unconfigured protected API was not denied.')
        hashed = cmd(['exec','-T','backend','python','-c',hash_code], input=password, capture_output=True, text=True).stdout
        cmd(['exec','-T','backend','python','-m','ops.dashboard_auth','install-hash'], input=hashed, text=True)
        with tempfile.TemporaryDirectory(prefix='d3m0n-docker-state-') as host_state:
            env['D3M0N_DOCKER_STATE_DIR'] = host_state
            subprocess.run([sys.executable,'-m','ops.docker_setup','start','--no-browser','--api-port',str(api),'--ui-port',str(ui)], cwd=root, env=env, check=True)
            cmd(['exec','-T','frontend','npm','run','type-check'])
            cmd(['exec','-T','frontend','npm','test'])
            tests = ['tests/test_compatibility.py','tests/test_docker_setup.py','tests/test_docker_lifecycle.py','tests/test_setup_manager.py','tests/test_install_state.py','tests/test_state_backup.py','tests/test_release_package.py','tests/test_ci_contract.py','tests/test_guided_install_contract.py','tests/test_portable_install.py','tests/test_platform_config.py','ops/tests/test_dashboard_auth.py']
            cmd(['exec','-T','backend','python','-m','pytest','-q',*tests])
            read_hash = ['exec','-T','backend','python','-c',"import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/state/dashboard-auth.json').read_bytes()).hexdigest())"]
            before = cmd(read_hash, capture_output=True, text=True).stdout.strip()
            cmd(['restart'])
            deadline = time.monotonic()+90
            while time.monotonic()<deadline:
                try:
                    if request(f'http://127.0.0.1:{api}/api/health')[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(1)
            subprocess.run([sys.executable,'-m','ops.docker_setup','start','--no-browser'], cwd=root, env=env, check=True)
            after = cmd(read_hash, capture_output=True, text=True).stdout.strip()
            if before != after:
                raise RuntimeError('Restart changed existing authentication.')
            if request(f'http://127.0.0.1:{api}/api/auth/unlock', {'password':password})[0] != 200:
                raise RuntimeError('Fresh dashboard login failed.')
            print(f'CLEAN DOCKER INSTALL ACCEPTED: {project}')
    finally:
        if started:
            subprocess.run(compose+['down','--volumes','--remove-orphans'], cwd=root, env=env, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--project',required=True)
    parser.add_argument('--skip-build',action='store_true')
    args = parser.parse_args()
    run(args.root.resolve(),args.project,skip_build=args.skip_build)
