#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${D3M0N_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$PYTHON" ]]; then
  echo 'Guided Python 3.11/3.12 is required. Set D3M0N_PYTHON; this script does not install global toolchains.' >&2
  exit 1
fi
version="$($PYTHON -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
case "$version" in
  3.11|3.12) ;;
  *) echo "Unsupported lock-maintenance Python $version; use 3.11 or 3.12." >&2; exit 1;;
esac
args=("$ROOT/scripts/update_dependency_locks.py")
if [[ "${1:-}" == "--upgrade" ]]; then args+=(--upgrade); elif [[ -n "${1:-}" ]]; then echo 'Usage: update_dependency_locks.sh [--upgrade]' >&2; exit 2; fi
exec "$PYTHON" "${args[@]}"
