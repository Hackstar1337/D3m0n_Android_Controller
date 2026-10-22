"""Real native install/reinstall acceptance in temporary private state."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(root: Path) -> None:
    # Use only a disposable clone/ZIP tree, never an existing developer .venv.
    if (root / '.venv').exists():
        raise RuntimeError('Native acceptance requires a clean disposable checkout without .venv.')
    with tempfile.TemporaryDirectory(prefix='d3m0n-native-acceptance-') as temporary:
        env = os.environ.copy()
        env.update({'D3M0N_STATE_DIR': temporary, 'D3M0N_DASHBOARD_AUTH_FILE': str(Path(temporary) / 'dashboard-auth.json')})
        from ops.dashboard_auth import hash_password
        fixture = json.dumps(hash_password(secrets.token_urlsafe(24)))
        subprocess.run([sys.executable, '-m', 'ops.dashboard_auth', 'install-hash'], cwd=root, env=env, input=fixture, text=True, check=True)
        command = [sys.executable, '-m', 'ops.setup_manager']
        try:
            subprocess.run(command + ['install', '--no-browser'], cwd=root, env=env, check=True)
            paths = list(Path(temporary).glob('launcher/*/installed.json'))
            if len(paths) != 1:
                raise RuntimeError('Native install did not publish its private resumable stage record.')
            first = json.loads(paths[0].read_text())
            before = (Path(temporary) / 'dashboard-auth.json').read_bytes()
            subprocess.run(command + ['status'], cwd=root, env=env, check=True)
            subprocess.run(command + ['stop'], cwd=root, env=env, check=True)
            subprocess.run(command + ['install', '--no-browser'], cwd=root, env=env, check=True)
            second = json.loads(paths[0].read_text())
            for stage in ['backend_dependencies', 'frontend_dependencies', 'frontend_build']:
                if first['stages'][stage]['completed_at'] != second['stages'][stage]['completed_at']:
                    raise RuntimeError(f'Unchanged stage was unnecessarily rebuilt: {stage}')
            if before != (Path(temporary) / 'dashboard-auth.json').read_bytes():
                raise RuntimeError('Reinstall changed the existing auth fixture.')
            subprocess.run(command + ['status'], cwd=root, env=env, check=True)
            print('NATIVE INSTALL AND RESUME ACCEPTED')
        finally:
            subprocess.run(command + ['stop'], cwd=root, env=env, check=False)


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    run(args.root.resolve())
