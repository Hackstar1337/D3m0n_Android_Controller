#!/opt/d3m0n-ai-worker/venv/bin/python
"""Fixed bootstrap for the DynamicUser SDK worker.

The wrapper supplies the interpreter and this file by absolute path. Python is
started with -E -s -X utf8; this module removes the script directory/current
working-directory import slot and inserts exactly one application source root.
The SDK itself is installed in the immutable worker venv, not imported from an
ambient checkout or PYTHONPATH.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
import stat
import sys
from typing import NoReturn

_CONFIG = Path("/etc/d3m0n-ai-worker/runtime.json")
_BOOTSTRAP_DIR = Path(__file__).resolve().parent


def _fail(message: str) -> "NoReturn":
    # Keep diagnostics on stderr; the service unit discards worker stderr.
    print(f"d3m0n-ai-worker bootstrap: {message}", file=sys.stderr)
    raise SystemExit(70)


def _root_owned_directory(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            _fail(f"source root is not a real directory: {path}")
        info = path.stat()
    except OSError:
        _fail(f"source root cannot be inspected: {path}")
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode):
        _fail("source root must be owned by root and remain a directory")


def _load_config() -> dict[str, str]:
    try:
        with _CONFIG.open(encoding="utf-8") as handle:
            config = json.load(handle)
        expected = {
            "prefix": "/opt/d3m0n-ai-worker",
            "socket_path": "/run/d3m0n-ai-worker.sock",
            "wrapper_path": "/usr/local/libexec/d3m0n-ai-worker",
            "client_path": "/usr/local/libexec/d3m0n-ai-worker-client",
            "source_root": "/opt/d3m0n-ai-worker/source",
            "sdk_root": "/opt/d3m0n-ai-worker/sdk",
            "venv_root": "/opt/d3m0n-ai-worker/venv",
            "config_path": str(_CONFIG),
        }
        if not isinstance(config, dict) or any(config.get(key) != value for key, value in expected.items()):
            raise ValueError
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _fail("root-owned runtime configuration is unavailable or invalid")
        raise AssertionError from exc


_RUNTIME = _load_config()
_SOURCE = Path(_RUNTIME["source_root"])
_root_owned_directory(_SOURCE)

if sys.argv[1:] not in ([], ["--probe"]):
    _fail("only the literal --probe argument is accepted")

# -E prevents PYTHONPATH/PYTHONHOME injection; -s prevents user site packages.
# Remove the script directory and the empty CWD slot as defense in depth, then
# put only the audited root-owned application source at the front.
sys.path[:] = [item for item in sys.path if item not in ("", str(_BOOTSTRAP_DIR))]
sys.path.insert(0, os.fspath(_SOURCE))

from ops.ai_fleet_sidecar import main  # noqa: E402

raise SystemExit(main())
