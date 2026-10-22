#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Missing $PYTHON; run scripts/bootstrap.sh first." >&2; exit 1; }
cd "$REPO"
exec "$PYTHON" -m ops.launch "$@"
