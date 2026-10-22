#!/usr/bin/env bash
# PREPARED ARTIFACT: default mode is static and does not start the worker.
# Optional --runtime runs only the fixed sudoers-allowed --probe command.
set -euo pipefail
umask 077

readonly PREFIX=/opt/d3m0n-ai-worker
readonly WRAPPER=/usr/local/libexec/d3m0n-ai-worker
readonly SUDOERS=/etc/sudoers.d/d3m0n-ai-worker
readonly MANIFEST="${1:-/opt/d3m0n-ai-worker/host-isolation/source-manifest.sha256}"
readonly CONFIG=/etc/d3m0n-ai-worker/runtime.json

fail() { printf 'validate-host-isolation: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "unsupported host: missing $1"; }
for command_name in find stat sha256sum awk grep sort id systemd-run systemctl sudo visudo; do need "$command_name"; done
[[ $(id -u) -eq 0 ]] || fail 'static host validation must run as root'
[[ -r "$CONFIG" ]] || fail 'generated runtime config is missing'
OPERATOR=$(awk -F'"' '/"worker_user"[[:space:]]*:/ {print $4; exit}' "$CONFIG")
[[ "$OPERATOR" =~ ^[A-Za-z][A-Za-z0-9._-]{0,62}$ && "$OPERATOR" != root ]] || fail 'generated non-root operator is invalid'
[[ -x "$WRAPPER" && -f "$PREFIX/bootstrap.py" ]] || fail 'worker wrapper/bootstrap missing'
[[ -x "$PREFIX/venv/bin/python" ]] || fail 'worker venv interpreter missing'
[[ -d "$PREFIX/source" && -d "$PREFIX/sdk" && -d "$PREFIX/venv" ]] || fail 'worker staging roots missing'
[[ "$(stat -c '%u:%g' "$WRAPPER" "$PREFIX/bootstrap.py" "$PREFIX/source" "$PREFIX/sdk" "$PREFIX/venv" | sort -u)" == "0:0" ]] || fail 'worker artifacts are not root-owned'
[[ "$(stat -c '%a' "$WRAPPER")" == 755 ]] || fail 'wrapper mode is not 0755'
[[ "$(stat -c '%a' "$SUDOERS")" == 440 ]] || fail 'sudoers mode is not 0440'
[[ ! -w "$PREFIX/source" && ! -w "$PREFIX/sdk" && ! -w "$PREFIX/venv" ]] || fail 'worker trees are writable'
if find -P "$PREFIX/source" "$PREFIX/sdk" "$PREFIX/venv" -type l -print -quit | grep -q .; then fail 'worker trees contain symlinks'; fi

for required in DynamicUser=yes ProtectHome=yes ProtectSystem=strict PrivateTmp=yes PrivateNetwork=yes NoNewPrivileges=yes RestrictAddressFamilies=AF_UNIX MemoryMax=512M TasksMax=32 LimitNPROC=32 CPUQuota=50% RuntimeMaxSec=180s StandardError=null; do
  grep -F -- "--property=$required" "$WRAPPER" >/dev/null || fail "wrapper missing property: $required"
done
[[ "$(grep -F --count 'case "$#"' "$WRAPPER")" -eq 1 ]] || fail 'wrapper argument gate missing'
grep -F -- '"$PYTHON" -E -s -X utf8 "$BOOTSTRAP"' "$WRAPPER" >/dev/null || fail 'wrapper uses an unexpected interpreter invocation'
grep -F -- "$OPERATOR ALL=(root) NOPASSWD: D3M0N_AI_WORKER_CMDS" "$SUDOERS" >/dev/null || fail 'sudoers principal/rule mismatch'
grep -F -- '/usr/local/libexec/d3m0n-ai-worker --probe' "$SUDOERS" >/dev/null || fail 'sudoers probe rule missing'
visudo -cf "$SUDOERS" >/dev/null || fail 'installed sudoers does not parse'
systemd-run --version >/dev/null 2>&1 || fail 'systemd-run is not functional'
systemctl --version >/dev/null 2>&1 || fail 'systemd is not available'
sudo -V >/dev/null 2>&1 || fail 'sudo is not available'

[[ -r "$MANIFEST" ]] || fail "manifest not found: $MANIFEST"
while read -r relative expected _; do
  [[ -z "$relative" || "$relative" == \#* ]] && continue
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail "invalid manifest line: $relative"
  file="$PREFIX/source/$relative"
  [[ -f "$file" && ! -L "$file" ]] || fail "manifest file absent: $relative"
  [[ "$(sha256sum -- "$file" | awk '{print $1}')" == "$expected" ]] || fail "source hash mismatch: $relative"
done < "$MANIFEST"

if [[ "${1:-}" == --runtime ]]; then
  command -v runuser >/dev/null 2>&1 || fail 'runuser is required for the fixed probe'
  result=$(runuser -u "$OPERATOR" -- /usr/local/libexec/d3m0n-ai-worker-client --probe) || fail 'fixed --probe invocation failed'
  grep -F '"sdk_identity":"mobilerun@301856138c6edeb2ae692bfab85b279ace520e59"' <<<"$result" >/dev/null || fail 'probe identity mismatch'
  grep -E 'api[_-]?key|bearer|secret|password|token|cookie' <<<"$result" >/dev/null && fail 'probe emitted secret-like material' || true
  printf '%s\n' 'fixed --probe passed; this does not establish physical device or provider access'
else
  printf '%s\n' 'static host-isolation checks passed; worker was not started'
fi
