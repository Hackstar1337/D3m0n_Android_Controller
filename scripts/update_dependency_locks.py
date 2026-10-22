"""Maintain reviewed, hashed Python 3.11/3.12 Windows/Linux dependency locks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / 'requirements'
OVERLAYS = REQUIREMENTS / 'platform-overlays.json'
STANZA = re.compile(r'(?ms)^([a-zA-Z][a-zA-Z0-9_.-]*)==([^;\s]+).*?(?=^[a-zA-Z][a-zA-Z0-9_.-]*==|\Z)')


def compile_command() -> list[str]:
    configured = os.environ.get('PIP_COMPILE')
    return shlex.split(configured, posix=os.name != 'nt') if configured else [sys.executable, '-m', 'piptools', 'compile']


def compile_lock(output: Path, *extras: str, build: bool, upgrade: bool) -> None:
    command = compile_command() + ['pyproject.toml', '--generate-hashes', '--allow-unsafe', '--resolver', 'backtracking', '--strip-extras', '--output-file', str(output.relative_to(ROOT))]
    for extra in extras:
        command.extend(['--extra', extra])
    if build:
        command += ['--all-build-deps', '--only-build-deps']
    if upgrade:
        command.append('--upgrade')
    env = os.environ.copy()
    env['CUSTOM_COMPILE_COMMAND'] = 'python scripts/update_dependency_locks.py' + (' --upgrade' if upgrade else '')
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def overlay_text(name: str, spec: dict) -> str:
    hashes = sorted(set(spec['sha256']))
    if not hashes or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in hashes):
        raise ValueError(f'Invalid hash overlay for {name}')
    header = f'{name}=={spec["version"]}; {spec["marker"]} \\\n'
    lines = [f'    --hash=sha256:{value}' + (' \\' if i < len(hashes)-1 else '') for i, value in enumerate(hashes)]
    return header + '\n'.join(lines) + '\n    # reviewed platform-specific dependency overlay\n'


def apply_platform_overlay(path: Path, overlays: dict | None = None) -> None:
    overlays = overlays or json.loads(OVERLAYS.read_text())
    text = path.read_text(encoding='utf-8')
    for name in overlays:
        text = STANZA.sub(lambda match: '' if match.group(1).lower() == name else match.group(0), text)
    text = text.rstrip() + '\n\n' + ''.join(overlay_text(name, overlays[name]) for name in sorted(overlays))
    path.write_text(text, encoding='utf-8', newline='\n')


def validate_lock(path: Path) -> None:
    text = path.read_text(encoding='utf-8')
    entries = list(STANZA.finditer(text))
    if not entries:
        raise ValueError(f'No pinned dependencies in {path.name}')
    names = [m.group(1).lower().replace('_','-') for m in entries]
    if len(names) != len(set(names)):
        raise ValueError(f'Duplicate requirement in {path.name}')
    for match in entries:
        if not re.search(r'--hash=sha256:[0-9a-f]{64}', match.group(0)):
            raise ValueError(f'Unhashed requirement {match.group(1)} in {path.name}')
    if path.name != 'build.lock':
        for name, marker in [('pywin32', 'sys_platform == "win32"'), ('uvloop', 'sys_platform != "win32"')]:
            matches = [m for m in entries if m.group(1).lower() == name]
            if len(matches) != 1 or marker not in matches[0].group(0).splitlines()[0]:
                raise ValueError(f'Missing universal marker for {name} in {path.name}')


def refresh_overlay_hashes(overlays: dict) -> dict:
    result = {}
    for name, spec in overlays.items():
        with urlopen(f'https://pypi.org/pypi/{name}/json', timeout=30) as response:
            metadata = json.load(response)
        version = metadata['info']['version']
        hashes = [file['digests']['sha256'] for file in metadata['releases'][version] if file.get('packagetype') in {'bdist_wheel','sdist'} and not file.get('yanked')]
        result[name] = {'version': version, 'marker': spec['marker'], 'sha256': hashes}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade', action='store_true')
    parser.add_argument('--check', action='store_true', help='validate checked-in pins/markers/hashes without resolving or downloading')
    args = parser.parse_args(argv)
    if args.check:
        for name in ['backend.lock','dev.lock','build.lock']:
            validate_lock(REQUIREMENTS / name)
        print('Dependency locks: pins, hashes, and cross-platform markers valid.')
        return 0
    if sys.version_info[:2] not in {(3,11),(3,12)}:
        parser.error('Maintenance requires Python 3.11 or 3.12.')
    overlays = json.loads(OVERLAYS.read_text())
    if args.upgrade:
        overlays = refresh_overlay_hashes(overlays)
    # All outputs must resolve and validate before any live lockfile is replaced.
    with tempfile.TemporaryDirectory(prefix='.lock-refresh-', dir=REQUIREMENTS) as temporary:
        staging = Path(temporary)
        for name, extras, build in [('backend.lock',('d3m0n',),False), ('dev.lock',('d3m0n','dev'),False), ('build.lock',(),True)]:
            output = staging / name
            output.write_bytes((REQUIREMENTS / name).read_bytes())
            compile_lock(output,*extras,build=build,upgrade=args.upgrade)
            if not build:
                apply_platform_overlay(output,overlays)
            validate_lock(output)
        for name in ['backend.lock','dev.lock','build.lock']:
            os.replace(staging/name, REQUIREMENTS/name)
        if args.upgrade:
            OVERLAYS.write_text(json.dumps(overlays,indent=2)+'\n', encoding='utf-8')
    print('Locks refreshed; validate installations on the full Windows/Linux Python matrix before release.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
