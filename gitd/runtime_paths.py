"""Runtime artifacts belong in configured private state, never in Docker source mounts."""
from __future__ import annotations
import os
from pathlib import Path


def runtime_directory(name: str, legacy: Path) -> Path:
    if not name or Path(name).name != name or name in {'.', '..'} or '/' in name or '\\' in name:
        raise ValueError('Runtime directory name must be one safe path component.')
    override = os.environ.get('D3M0N_STATE_DIR', '').strip()
    if override:
        from ops.platform_config import ensure_private_dir
        return ensure_private_dir(Path(override).expanduser() / name)
    # Preserve existing native recordings/results; do not silently migrate/delete.
    directory = Path(legacy)
    directory.mkdir(parents=True, exist_ok=True)
    return directory
