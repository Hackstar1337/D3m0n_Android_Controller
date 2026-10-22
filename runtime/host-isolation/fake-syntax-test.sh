#!/usr/bin/env bash
# Safe local gate: syntax/parsing only; no host command, worker, sudo, or network.
set -euo pipefail
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v bash >/dev/null 2>&1 || { echo 'bash unavailable' >&2; exit 1; }
for script in "$HERE"/*.sh; do bash -n "$script"; done
if command -v python3 >/dev/null 2>&1; then PY=python3; elif command -v python >/dev/null 2>&1; then PY=python; else echo 'python unavailable' >&2; exit 1; fi
"$PY" - "$HERE/bootstrap.py" <<'PY'
import ast
import pathlib
import sys
for name in sys.argv[1:]:
    ast.parse(pathlib.Path(name).read_text(encoding="utf-8"), filename=name)
print("python bootstrap syntax passed")
PY
printf '%s\n' 'shell syntax passed; no host runtime was executed'
