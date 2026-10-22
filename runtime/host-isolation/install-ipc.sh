#!/usr/bin/env bash
# Install only the IPC boundary.  This script never installs or edits the SDK,
# wrapper, bootstrap, source manifest, sudoers, or any application source.
set -euo pipefail
umask 077

readonly HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
readonly LIBEXEC=/usr/local/libexec
readonly UNITDIR=/etc/systemd/system
readonly DISPATCHER=/usr/local/libexec/d3m0n-ai-worker-dispatcher.py
readonly CLIENT=/usr/local/libexec/d3m0n-ai-worker-client
readonly SOCKET_UNIT=/etc/systemd/system/d3m0n-ai-worker.socket
readonly SERVICE_UNIT=/etc/systemd/system/d3m0n-ai-worker@.service
readonly SOCKET_PATH=/run/d3m0n-ai-worker.sock
OPERATOR=
fail() { printf 'install-ipc: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

[[ $# -eq 2 && "$1" == --operator ]] || fail 'usage: install-ipc.sh --operator ACCOUNT'
OPERATOR=$2
[[ "$OPERATOR" =~ ^[A-Za-z][A-Za-z0-9._-]{0,62}$ && "$OPERATOR" != root ]] || fail 'operator must be a non-root account name'

[[ "$(id -u)" -eq 0 ]] || fail 'must be run as root by the parent installer'
for c in id getent install stat readlink systemctl systemd-analyze; do need "$c"; done
id "$OPERATOR" >/dev/null 2>&1 || fail "trusted peer account does not exist: $OPERATOR"
getent group "$OPERATOR" >/dev/null 2>&1 || fail "trusted peer group does not exist: $OPERATOR"

# Refuse symlinked or writable installation parents.  Existing target files are
# replaced only when they are regular root-owned files; unexpected paths fail.
secure_dir() {
  local path=$1 mode owner group
  [[ -d "$path" && ! -L "$path" ]] || fail "required directory is not real: $path"
  owner=$(stat -c '%u' -- "$path")
  group=$(stat -c '%g' -- "$path")
  mode=$(stat -c '%a' -- "$path")
  [[ "$owner" == 0 && "$group" == 0 ]] || fail "directory is not root:root: $path"
  (( (8#$mode & 0022) == 0 )) || fail "directory is group/world writable: $path"
}
for directory in /usr /usr/local "$LIBEXEC" /etc "$UNITDIR" /run; do secure_dir "$directory"; done

for source in dispatcher.py client.py d3m0n-ai-worker.socket d3m0n-ai-worker@.service; do
  [[ -f "$HERE/$source" && ! -L "$HERE/$source" ]] || fail "missing or symlinked artifact: $source"
done

check_target() {
  local path=$1
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -f "$path" && ! -L "$path" ]] || fail "refusing non-regular target: $path"
    [[ "$(stat -c '%u:%g' -- "$path")" == 0:0 ]] || fail "existing target is not root-owned: $path"
  fi
}
for target in "$DISPATCHER" "$CLIENT" "$SOCKET_UNIT" "$SERVICE_UNIT"; do check_target "$target"; done
if [[ -e "$SOCKET_PATH" || -L "$SOCKET_PATH" ]]; then
  [[ -S "$SOCKET_PATH" ]] || fail "socket path is occupied by a non-socket"
  [[ "$(stat -c '%u:%g %a' -- "$SOCKET_PATH")" == '0:'"$(id -g ash)"' 660' ]] || fail 'existing socket has unexpected ownership/mode'
fi

install -o root -g root -m 0755 -- "$HERE/dispatcher.py" "$DISPATCHER"
install -o root -g root -m 0755 -- "$HERE/client.py" "$CLIENT"
sed "s/__OPERATOR__/$OPERATOR/g" "$HERE/d3m0n-ai-worker.socket" | install -o root -g root -m 0644 /dev/stdin "$SOCKET_UNIT"
install -o root -g root -m 0644 -- "$HERE/d3m0n-ai-worker@.service" "$SERVICE_UNIT"

# Parser compatibility is checked before activation; no worker or SDK is run.
systemd-analyze verify "$SOCKET_UNIT" "$SERVICE_UNIT" || fail 'systemd unit verification failed'
systemctl daemon-reload
printf '%s\n' 'IPC units installed but left disabled; use the integrated installer --enable-socket gate.'
