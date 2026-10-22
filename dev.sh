#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
action=${1:-start}
shift || true
python_cmd=''
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<(3,13) else 1)' >/dev/null 2>&1; then python_cmd=$candidate; break; fi
done
[[ -n "$python_cmd" ]] || { echo 'Use an existing Python 3.11/3.12 interpreter for the Docker helper.' >&2; exit 1; }
project=${D3M0N_COMPOSE_PROJECT:-d3m0n-droid}
compose=(docker compose -p "$project" -f compose.dev.yaml)
case "$action" in
  start|stop|status|logs) exec "$python_cmd" -m ops.docker_setup "$action" "$@" ;;
  rebuild) exec "$python_cmd" -m ops.docker_setup start --rebuild "$@" ;;
  shell) exec "${compose[@]}" exec backend bash ;;
  frontend-shell) exec "${compose[@]}" exec frontend bash ;;
  build-ui) exec "${compose[@]}" exec frontend npm run build ;;
  password) exec "${compose[@]}" exec backend python -m ops.dashboard_auth set-password ;;
  test)
    "${compose[@]}" exec -T frontend npm run type-check
    "${compose[@]}" exec -T frontend npm test
    "${compose[@]}" exec -T backend python -m pytest -q tests/test_compatibility.py tests/test_docker_setup.py tests/test_docker_lifecycle.py tests/test_setup_manager.py tests/test_install_state.py tests/test_state_backup.py tests/test_runtime_paths.py tests/test_guided_install_contract.py tests/test_portable_install.py tests/test_platform_config.py tests/test_ci_contract.py tests/test_release_package.py ops/tests/test_dashboard_auth.py
    ;;
  backup|restore) exec "$python_cmd" -m ops.docker_backup "$action" "$@" ;;
  edit) command -v code >/dev/null 2>&1 && exec code D3m0n-Droid.code-workspace || { echo 'Open D3m0n-Droid.code-workspace in your editor.'; } ;;
  *) echo 'Actions: start stop status logs rebuild shell frontend-shell build-ui password test edit backup restore' >&2; exit 2 ;;
esac
