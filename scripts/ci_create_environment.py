"""Install CI tooling into a fresh runner-private virtual environment."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def create(root: Path, destination: Path, *, development: bool) -> Path:
    if destination.exists():
        raise FileExistsError('CI environment target must be new.')
    venv.EnvBuilder(with_pip=True).create(destination)
    binary = destination / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    for lock in ['build.lock', 'dev.lock' if development else 'backend.lock']:
        subprocess.run([str(binary), '-m', 'pip', 'install', '--require-hashes', '-r', str(root / 'requirements' / lock)], cwd=root, check=True)
    subprocess.run([str(binary), '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', str(root)], cwd=root, check=True)
    subprocess.run([str(binary), '-m', 'pip', 'check'], cwd=root, check=True)
    if os.environ.get('GITHUB_PATH'):
        with Path(os.environ['GITHUB_PATH']).open('a', encoding='utf-8') as handle:
            handle.write(str(binary.parent)+'\n')
    print(f'Isolated CI Python ready: {binary}')
    return binary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--destination', type=Path, default=Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir())) / 'd3m0n-ci-tools')
    parser.add_argument('--development', action='store_true')
    args = parser.parse_args()
    create(args.root.resolve(), args.destination.resolve(), development=args.development)
