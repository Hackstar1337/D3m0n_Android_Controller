#!/usr/bin/env bash
set -euo pipefail
REPO="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$REPO"
[[ -x .venv/bin/python ]] || { echo 'Run bash install.sh first.' >&2; exit 1; }
exec .venv/bin/python -m ops.setup_manager start "$@"
