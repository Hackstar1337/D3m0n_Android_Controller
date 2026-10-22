"""Explicit private Docker backups; restore creates a new, isolated project only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from ops import docker_setup
from ops.platform_config import ensure_private_file


def _archive_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError('Private backup archives must be outside the checkout.')
    return path


def _volume_exists(name: str) -> bool:
    return docker_setup._run(['docker', 'volume', 'inspect', name], check=False, capture=True).returncode == 0


def _image() -> str:
    data = docker_setup.load_state() or {}
    profile = 'prod' if data.get('profile') == 'production' else 'dev'
    return f'{docker_setup.project_name()}-backend:{profile}'


def backup(destination: Path, *, include_secrets: bool) -> Path:
    if not include_secrets:
        raise ValueError('This backup contains password hashes, encrypted profiles, and ADB identities; explicitly pass --include-secrets.')
    docker_setup.check_engine()
    containers = docker_setup.owned_containers()
    if any(c.get('State', {}).get('Running') for c in containers):
        raise RuntimeError('Stop this Docker project before backing up for a consistent database snapshot; then start it again.')
    destination = _archive_path(str(destination))
    if destination.exists():
        raise FileExistsError('Backup destination exists; choose a new filename.')
    if not destination.parent.is_dir():
        destination.parent.mkdir(parents=True, exist_ok=True)
    project = docker_setup.project_name()
    for suffix in ('private_state', 'adb_identity'):
        if not _volume_exists(f'{project}_{suffix}'):
            raise RuntimeError(f'Private volume {suffix} is missing; backup was not created.')
    code = "from ops.state_backup import create_backup; from pathlib import Path; import sys; p=create_backup(Path('/source'),Path('/tmp/private-backup.tar.gz')); sys.stdout.buffer.write(p.read_bytes())"
    command = ['docker', 'run', '--rm', '--user', '1000:1000', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--mount', f'type=volume,src={project}_private_state,dst=/source/state,readonly',
               '--mount', f'type=volume,src={project}_adb_identity,dst=/source/adb,readonly',
               _image(), 'python', '-c', code]
    temporary = destination.with_name(f'.{destination.name}.{os.getpid()}.tmp')
    try:
        with temporary.open('xb') as output:
            ensure_private_file(temporary)
            result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('Docker backup failed; no final archive was published. Check image availability and volume permissions.')
        from ops.state_backup import validate_backup
        validate_backup(temporary)
        os.replace(temporary, destination)
        ensure_private_file(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore(archive: Path, target_project: str, *, include_secrets: bool) -> str:
    if not include_secrets:
        raise ValueError('Restoring credentials requires --include-secrets.')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', target_project) or target_project == docker_setup.project_name():
        raise ValueError('Restore needs a different, valid --target-project; live state is never overwritten.')
    archive = _archive_path(str(archive))
    from ops.state_backup import validate_backup
    manifest = validate_backup(archive)
    if any(not (e['path'].startswith('state/') or e['path'].startswith('adb/')) for e in manifest['files']):
        raise ValueError('This is not a Docker state/ADB backup archive.')
    docker_setup.check_engine()
    names = [f'{target_project}_private_state', f'{target_project}_adb_identity']
    if any(_volume_exists(name) for name in names):
        raise FileExistsError('Restore project volumes already exist; use a fresh namespace.')
    for name in names:
        docker_setup._run(['docker', 'volume', 'create', '--label', 'd3m0n.restore=isolated', name], capture=True)
    mounts = ['--mount', f'type=volume,src={names[0]},dst=/target/state', '--mount', f'type=volume,src={names[1]},dst=/target/adb']
    # Root is used only to initialize OWNERSHIP of the two newly-created volumes;
    # the application and restore file copy run non-root. No existing volumes touched.
    docker_setup._run(['docker', 'run', '--rm', '--user', '0:0', '--cap-drop=ALL', '--cap-add=CHOWN',
                       '--security-opt=no-new-privileges', *mounts, _image(), 'chown', '1000:1000', '/target/state', '/target/adb'])
    code = "from pathlib import Path; import sys,shutil; from ops.state_backup import restore_backup; p=Path('/tmp/input.tar.gz'); f=p.open('wb'); shutil.copyfileobj(sys.stdin.buffer,f); f.close(); t=restore_backup(p,Path('/tmp/restored')); [shutil.copytree(t/n,Path('/target')/n,dirs_exist_ok=True) for n in ['state','adb'] if (t/n).exists()]"
    with archive.open('rb') as stream:
        result = subprocess.run(['docker', 'run', '--rm', '-i', '--user', '1000:1000', '--cap-drop=ALL',
                                 '--security-opt=no-new-privileges', *mounts, _image(), 'python', '-c', code],
                                cwd=ROOT, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Restore failed in its isolated new volumes; live installation is unchanged. Inspect the new namespace before retrying with another name.')
    return target_project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--target-project')
    parser.add_argument('--include-secrets', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'backup':
            print(f'Private Docker backup created: {backup(args.archive, include_secrets=args.include_secrets)}')
        else:
            project = restore(args.archive, args.target_project or '', include_secrets=args.include_secrets)
            print(f'Restored only into new project {project}; set D3M0N_COMPOSE_PROJECT to inspect it. Live project unchanged.')
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Docker backup/restore refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
