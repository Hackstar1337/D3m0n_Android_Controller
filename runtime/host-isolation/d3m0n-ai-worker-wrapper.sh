#!/usr/bin/env bash
# Installed as /usr/local/libexec/d3m0n-ai-worker (root:root, 0755).
# The only caller-visible inputs are the private stdin/stdout JSON pipe and an
# optional literal --probe. No caller argument is forwarded to Python.
set -euo pipefail

readonly PYTHON=/opt/d3m0n-ai-worker/venv/bin/python
readonly BOOTSTRAP=/opt/d3m0n-ai-worker/bootstrap.py

case "$#" in
  0) argv=() ;;
  1)
    [[ "$1" == "--probe" ]] || { printf '%s\n' 'd3m0n-ai-worker: only --probe is accepted' >&2; exit 64; }
    argv=(--probe)
    ;;
  *)
    printf '%s\n' 'd3m0n-ai-worker: unexpected arguments' >&2
    exit 64
    ;;
esac

[[ -x "$PYTHON" && -f "$BOOTSTRAP" ]] || {
  printf '%s\n' 'd3m0n-ai-worker: immutable runtime is not installed' >&2
  exit 70
}

exec /usr/bin/systemd-run \
  --pipe --wait --collect --quiet \
  --property=DynamicUser=yes \
  --property=ProtectHome=yes \
  --property=ProtectSystem=strict \
  --property=PrivateTmp=yes \
  --property=PrivateNetwork=yes \
  --property=NoNewPrivileges=yes \
  --property=RestrictAddressFamilies=AF_UNIX \
  --property=MemoryMax=512M \
  --property=TasksMax=32 \
  --property=LimitNPROC=32 \
  --property=CPUQuota=50% \
  --property=RuntimeMaxSec=180s \
  --property=LimitNOFILE=256 \
  --property=WorkingDirectory=/opt/d3m0n-ai-worker \
  --property=ReadOnlyPaths=/opt/d3m0n-ai-worker \
  --property=StandardError=null \
  --property="Environment=PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONNOUSERSITE=1 HOME=/tmp OTEL_SDK_DISABLED=true POSTHOG_DISABLED=true D3M0N_AI_WORKER_CONFIG=/etc/d3m0n-ai-worker/runtime.json" \
  "$PYTHON" -E -s -X utf8 "$BOOTSTRAP" "${argv[@]}"
