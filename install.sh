#!/usr/bin/env bash
# Guided, user-owned Linux installer. Run with: bash install.sh [options]
set -euo pipefail

REPO="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PREREQ="$REPO/scripts/linux-prerequisites.sh"
MODE=native
DRY_RUN=0
ASSUME_YES=0
NO_START=0
PORT=""
UI_PORT=""
NO_BROWSER=0
PRODUCTION=0
REBUILD=0

usage() {
  cat <<'EOF'
Usage: bash install.sh [--mode native|docker] [--dry-run] [--yes] [--no-start] [--port N] [--ui-port N] [--no-browser] [--production] [--rebuild]

Native mode is the default and retains the guided OS prerequisite consent flow.
Docker mode uses the existing Docker engine and a dependency-free local Python
bootstrap; it never silently installs a host toolchain.
EOF
}
fail() { printf 'install.sh: ERROR: %s\n' "$*" >&2; exit 1; }
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] || fail 'port must be an integer'; (( $1 >= 1 && $1 <= 65535 )) || fail 'port must be between 1 and 65535'; }

while (($#)); do
  case "$1" in
    --mode)
      (($# >= 2)) || fail '--mode needs native or docker'
      MODE=${2,,}; shift
      [[ "$MODE" == native || "$MODE" == docker ]] || fail '--mode must be native or docker'
      ;;
    --mode=*) MODE=${1#*=}; MODE=${MODE,,}; [[ "$MODE" == native || "$MODE" == docker ]] || fail '--mode must be native or docker' ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --no-start) NO_START=1 ;;
    --production) PRODUCTION=1 ;;
    --rebuild) REBUILD=1 ;;
    --no-browser) NO_BROWSER=1 ;;
    --port)
      (($# >= 2)) || fail '--port needs a value'; PORT=$2; valid_port "$PORT"; shift ;;
    --port=*) PORT=${1#*=}; valid_port "$PORT" ;;
    --ui-port)
      (($# >= 2)) || fail '--ui-port needs a value'; UI_PORT=$2; valid_port "$UI_PORT"; shift ;;
    --ui-port=*) UI_PORT=${1#*=}; valid_port "$UI_PORT" ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

if [[ "$MODE" == docker ]]; then
  PYTHON=""
  for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' >/dev/null 2>&1; then
      PYTHON=$candidate; break
    fi
  done
  [[ -n "$PYTHON" ]] || fail 'Docker setup needs existing Python 3.11/3.12 for its dependency-free bootstrap; choose the native prerequisite plan if missing. No global toolchain was installed.'
  APP_ARGS=("$REPO/ops/docker_setup.py" install)
  (( DRY_RUN )) && APP_ARGS+=(--dry-run)
  (( NO_START )) && APP_ARGS+=(--no-start)
  [[ -n "$PORT" ]] && APP_ARGS+=(--api-port "$PORT")
  [[ -n "$UI_PORT" ]] && APP_ARGS+=(--ui-port "$UI_PORT")
  (( NO_BROWSER )) && APP_ARGS+=(--no-browser)
  (( PRODUCTION )) && APP_ARGS+=(--production)
  (( REBUILD )) && APP_ARGS+=(--rebuild)
  exec "$PYTHON" "${APP_ARGS[@]}"
fi

if (( EUID == 0 && !DRY_RUN )); then
  fail 'refusing to launch or install the application as root; run as a normal user (OS package steps may use scoped sudo).'
fi
[[ -f "$PREREQ" ]] || fail "missing prerequisite helper: $PREREQ"

PREREQ_ARGS=()
(( DRY_RUN )) && PREREQ_ARGS+=(--dry-run)
(( ASSUME_YES )) && PREREQ_ARGS+=(--yes)

printf 'D3m0n Linux guided installer (native mode)\nRepository: %s\n' "$REPO"
bash "$PREREQ" "${PREREQ_ARGS[@]}"

APP_ARGS=()
(( NO_START )) && APP_ARGS+=(--no-start)
[[ -n "$PORT" ]] && APP_ARGS+=(--port "$PORT")
(( NO_BROWSER )) && APP_ARGS+=(--no-browser)

if (( DRY_RUN )); then
  printf '\nDry-run: no application command was executed.\n'
  printf 'Planned application command: python -m ops.setup_manager install'
  for app_arg in "${APP_ARGS[@]}"; do printf ' %q' "$app_arg"; done
  printf '\n'
  exit 0
fi

# Prefer the supported interpreter selected by the prerequisite check. Do not
# use dashboard Python 3.10 for this guided installer.
PYTHON=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' >/dev/null 2>&1; then
    PYTHON=$candidate; break
  fi
done
[[ -n "$PYTHON" ]] || fail 'supported Python 3.11 or 3.12 was not found after prerequisite handling'

# The fallback Node archive is deliberately user-local. Add it only for this
# process; never replace a system node or edit shell startup files.
for node_bin in "$HOME"/.local/share/d3m0n/node-v22.23.4-linux-*/bin; do
  if [[ -x "$node_bin/node" ]]; then PATH="$node_bin:$PATH"; export PATH; break; fi
done

cd "$REPO"
printf 'Running setup manager as the current user: %s -m ops.setup_manager install\n' "$PYTHON"
exec "$PYTHON" -m ops.setup_manager install "${APP_ARGS[@]}"
