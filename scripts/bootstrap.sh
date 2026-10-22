#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
SKIP_INSTALL=0
for arg in "$@"; do case "$arg" in --dry-run) DRY_RUN=1;; --skip-install) SKIP_INSTALL=1;; *) echo "Unknown option: $arg" >&2; exit 2;; esac; done
run() { if (( DRY_RUN )); then printf '+ '; printf '%q ' "$@"; echo; else "$@"; fi; }
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (sys.version_info[:2] in ((3,10),(3,11),(3,12))) else 1)' 2>/dev/null; then PYTHON="$candidate"; break; fi
done
[[ -n "$PYTHON" ]] || { echo 'Supported Python 3.10, 3.11, or 3.12 was not found; no machine-level install is performed.' >&2; exit 1; }
NODE_VERSION="$(node --version 2>/dev/null || true)"
if [[ ! "$NODE_VERSION" =~ ^v?(20\.(1[9]|[2-9][0-9])|2[2-9]\.|[3-9][0-9]\.) ]]; then
  echo "Unsupported or missing Node.js: ${NODE_VERSION:-missing} (need ^20.19.0 or >=22.12.0)." >&2
  exit 1
fi
VENV="$REPO/.venv"
V_PY="$VENV/bin/python"
echo "Repository: $REPO"
echo "Python: $PYTHON ($($PYTHON --version 2>&1))"
echo "Node: $NODE_VERSION"
if (( ! SKIP_INSTALL )); then
  [[ -x "$V_PY" ]] || run "$PYTHON" -m venv "$VENV"
  run "$V_PY" -m pip install --upgrade pip
  pushd "$REPO" >/dev/null
  run "$V_PY" -m pip install '.[d3m0n]'
  popd >/dev/null
  run npm ci --prefix "$REPO/frontend"
  run npm --prefix "$REPO/frontend" run build
else
  echo 'Skip-install: venv/package/frontend writes were skipped.'
fi
if [[ -e "$REPO/.env" ]]; then echo 'Existing .env preserved.'; else echo 'No .env exists; leaving configuration uncreated.'; fi
echo "Next: $V_PY -m ops.doctor; $V_PY -m ops.launch --setup-password"
