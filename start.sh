#!/usr/bin/env bash
set -euo pipefail
REPO="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$REPO"
if [[ "${1:-}" == "--mode" && "${2:-}" == "docker" ]] || [[ "${1:-}" == "--mode=docker" ]]; then
  command -v python3 >/dev/null 2>&1 || { echo 'Docker start needs an existing Python 3 interpreter for the dependency-free bootstrap.' >&2; exit 1; }
  exec python3 ops/docker_setup.py start
fi
[[ -x .venv/bin/python ]] || { echo 'Run bash install.sh first.' >&2; exit 1; }
exec .venv/bin/python -m ops.setup_manager start "$@"
