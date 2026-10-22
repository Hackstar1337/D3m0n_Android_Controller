#!/usr/bin/env bash
# Explicit root-owned Linux FleetAI runtime installer. It never installs OS
# packages, starts a model/worker, discovers a phone, contacts a provider, or
# enables the socket unless --enable-socket was explicitly supplied.
set -euo pipefail
umask 022

readonly PREFIX=/opt/d3m0n-ai-worker
readonly SOCKET=/run/d3m0n-ai-worker.sock
readonly WRAPPER=/usr/local/libexec/d3m0n-ai-worker
readonly CLIENT=/usr/local/libexec/d3m0n-ai-worker-client
readonly DISPATCHER=/usr/local/libexec/d3m0n-ai-worker-dispatcher.py
readonly UNITDIR=/etc/systemd/system
readonly SOCKET_UNIT=$UNITDIR/d3m0n-ai-worker.socket
readonly SERVICE_UNIT=$UNITDIR/d3m0n-ai-worker@.service
readonly SUDOERS=/etc/sudoers.d/d3m0n-ai-worker
readonly CONFIG=/etc/d3m0n-ai-worker/runtime.json
readonly BACKUP=/var/backups/d3m0n-ai-worker
readonly HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
readonly HOSTDIR=$HERE/../runtime/host-isolation
readonly MANIFEST=$HOSTDIR/source-manifest.sha256
readonly SDK_COMMIT=301856138c6edeb2ae692bfab85b279ace520e59
readonly SDK_VERSION=0.7.0

DRY_RUN=0; ROLLBACK=0; ENABLE_SOCKET=0; PROBE=0
SOURCE=; SDK_SOURCE=; SDK_VENV=; OPERATOR=; OPERATOR_GROUP=
usage() { cat <<'EOF'
usage: sudo install-fleet-runtime.sh --operator ACCOUNT --source DIR --sdk-source DIR --sdk-venv DIR [--enable-socket] [--probe] [--dry-run]
       sudo install-fleet-runtime.sh --rollback [--dry-run]
EOF
}
log() { printf '%s\n' "[fleet-runtime] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "missing prerequisite: $1"; }
take_arg() { (($# >= 2)) || fail "missing value for $1"; printf '%s' "$2"; }

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --rollback) ROLLBACK=1 ;;
    --enable-socket) ENABLE_SOCKET=1 ;;
    --probe) PROBE=1 ;;
    --operator) OPERATOR=$(take_arg "$@"); shift ;;
    --source) SOURCE=$(take_arg "$@"); shift ;;
    --sdk-source) SDK_SOURCE=$(take_arg "$@"); shift ;;
    --sdk-venv) SDK_VENV=$(take_arg "$@"); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
  shift
done

if (( ! DRY_RUN && EUID != 0 )); then fail 'the installation step must be invoked explicitly as root (sudo)'; fi
if (( PROBE && ! ENABLE_SOCKET )); then fail '--probe requires the explicit --enable-socket gate'; fi
for tool in id getent find install cp rm mv mkdir mktemp chmod chown stat sha256sum awk grep git systemctl systemd-analyze visudo sudo; do need "$tool"; done

validate_operator() {
  [[ "$OPERATOR" =~ ^[A-Za-z][A-Za-z0-9._-]{0,62}$ ]] || fail '--operator must be an existing account name, not a path or shell expression'
  [[ "$OPERATOR" != root ]] || fail '--operator must be non-root'
  local uid gid group
  uid=$(id -u -- "$OPERATOR") || fail "operator account does not exist: $OPERATOR"
  gid=$(id -g -- "$OPERATOR") || fail "operator primary group is unavailable: $OPERATOR"
  group=$(id -gn -- "$OPERATOR") || fail "operator primary group is unavailable: $OPERATOR"
  (( uid > 0 && gid > 0 )) || fail '--operator must resolve to a non-root account/group'
  getent group "$group" >/dev/null || fail "operator group is unavailable: $group"
  OPERATOR_GROUP=$group
}
real_tree() { [[ -d "$1" && ! -L "$1" ]] || fail "input must be a real directory: $1"; }
reject_links() { find -P "$1" -type l -print -quit | grep -q . && fail "input tree contains a symlink: $1" || true; }
validate_inputs() {
  [[ -n "$SOURCE" && -n "$SDK_SOURCE" && -n "$SDK_VENV" ]] || { usage >&2; exit 64; }
  real_tree "$SOURCE"; real_tree "$SDK_SOURCE"; real_tree "$SDK_VENV"
  # Bootstrap creates unrelated node_modules/.bin symlinks. Only the reviewed
  # manifest files are staged; each one is hash-checked and may not be a symlink.
  reject_links "$SDK_SOURCE"; reject_links "$SDK_VENV"
  [[ -x "$SDK_VENV/bin/python" && ! -L "$SDK_VENV/bin/python" ]] || fail 'SDK venv must be a copy-based venv with a real bin/python'
  local commit
  commit=$(git -C "$SDK_SOURCE" rev-parse --verify HEAD 2>/dev/null) || fail 'SDK source must be a git checkout'
  [[ "$commit" == "$SDK_COMMIT" ]] || fail "SDK commit is not the reviewed pin (got $commit)"
  grep -R -E --include='pyproject.toml' --include='setup.py' --include='setup.cfg' --include='PKG-INFO' "0\.7\.0" "$SDK_SOURCE" >/dev/null || fail 'SDK source does not declare the reviewed version 0.7.0'
  [[ -r "$MANIFEST" ]] || fail "reviewed source manifest is missing: $MANIFEST"
  while read -r relative expected _; do
    [[ -z "$relative" || "$relative" == \#* ]] && continue
    [[ "$relative" != /* && "$relative" != *'..'* ]] || fail "unsafe manifest path: $relative"
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail "invalid manifest hash: $relative"
    local file="$SOURCE/$relative" actual
    [[ -f "$file" && ! -L "$file" ]] || fail "manifest file missing or symlinked: $relative"
    actual=$(sha256sum -- "$file" | awk '{print $1}')
    [[ "$actual" == "$expected" ]] || fail "source hash mismatch: $relative"
  done < "$MANIFEST"
}
active_workers() {
  local rows
  rows=$(systemctl list-units --all --no-legend 'd3m0n-ai-worker@*.service' 2>/dev/null || true)
  [[ -z "$(printf '%s\n' "$rows" | awk '$3 ~ /^(active|activating|deactivating)$/ || $4 ~ /^(running|start|stop)/ {print}')" ]]
}
backup_one() {
  local source=$1 relative=$2
  if [[ -e "$source" || -L "$source" ]]; then
    mkdir -p -- "$BACKUP_DIR/$(dirname -- "$relative")"
    cp -a -- "$source" "$BACKUP_DIR/$relative"
  fi
}
rollback() {
  active_workers || fail 'refusing rollback while a worker service is active'
  local latest
  latest=$(find "$BACKUP" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)
  [[ -n "$latest" && -d "$latest" ]] || fail 'no bounded runtime backup is available'
  if (( DRY_RUN )); then log "dry-run: would restore $latest (runtime, helpers, units, sudoers, and config)"; return; fi
  systemctl disable --now d3m0n-ai-worker.socket >/dev/null 2>&1 || true
  rm -rf -- "$PREFIX" "$CONFIG" "$WRAPPER" "$CLIENT" "$DISPATCHER" "$SOCKET_UNIT" "$SERVICE_UNIT" "$SUDOERS"
  [[ ! -e "$latest/runtime/prefix" ]] || cp -a -- "$latest/runtime/prefix" "$PREFIX"
  [[ ! -e "$latest/installed/etc/d3m0n-ai-worker/runtime.json" ]] || { install -d -m 0755 /etc/d3m0n-ai-worker; cp -a -- "$latest/installed/etc/d3m0n-ai-worker/runtime.json" "$CONFIG"; }
  for pair in \
    "$latest/installed/usr/local/libexec/d3m0n-ai-worker:$WRAPPER" \
    "$latest/installed/usr/local/libexec/d3m0n-ai-worker-client:$CLIENT" \
    "$latest/installed/usr/local/libexec/d3m0n-ai-worker-dispatcher.py:$DISPATCHER" \
    "$latest/installed/etc/systemd/system/d3m0n-ai-worker.socket:$SOCKET_UNIT" \
    "$latest/installed/etc/systemd/system/d3m0n-ai-worker@.service:$SERVICE_UNIT" \
    "$latest/installed/etc/sudoers.d/d3m0n-ai-worker:$SUDOERS"; do
    src=${pair%%:*}; dst=${pair#*:}; [[ -e "$src" ]] || continue; install -D -o root -g root -m 0644 -- "$src" "$dst"; [[ "$dst" == "$WRAPPER" || "$dst" == "$CLIENT" || "$dst" == "$DISPATCHER" ]] && chmod 0755 "$dst" || true
  done
  systemctl daemon-reload; visudo -cf "$SUDOERS" >/dev/null 2>&1 || true
  log "restored bounded backup: $latest; socket remains disabled"
}

if (( ROLLBACK )); then rollback; exit 0; fi
validate_operator
validate_inputs
systemd-analyze --version >/dev/null 2>&1 || fail 'systemd is required; nothing was staged'
systemctl --version >/dev/null 2>&1 || fail 'systemd is unavailable; nothing was staged'
active_workers || fail 'refusing upgrade while a worker service is active'
log "validated operator=$OPERATOR, SDK=$SDK_COMMIT/$SDK_VERSION, and every reviewed manifest entry"
if (( DRY_RUN )); then
  log 'dry-run: no root-owned files, units, socket state, credentials, or password setup were changed'
  exit 0
fi
STAMP=$(date +%Y%m%d%H%M%S); BACKUP_DIR=$BACKUP/$STAMP; mkdir -p -m 0700 -- "$BACKUP_DIR"
backup_one "$PREFIX" runtime/prefix
backup_one "$WRAPPER" installed/usr/local/libexec/d3m0n-ai-worker
backup_one "$CLIENT" installed/usr/local/libexec/d3m0n-ai-worker-client
backup_one "$DISPATCHER" installed/usr/local/libexec/d3m0n-ai-worker-dispatcher.py
backup_one "$SOCKET_UNIT" installed/etc/systemd/system/d3m0n-ai-worker.socket
backup_one "$SERVICE_UNIT" installed/etc/systemd/system/d3m0n-ai-worker@.service
backup_one "$SUDOERS" installed/etc/sudoers.d/d3m0n-ai-worker
backup_one "$CONFIG" installed/etc/d3m0n-ai-worker/runtime.json

STAGE=$(mktemp -d /tmp/d3m0n-fleet-runtime.XXXXXX); trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/prefix/source" "$STAGE/prefix/sdk" "$STAGE/prefix/venv" "$STAGE/prefix/host-isolation" "$STAGE/libexec" "$STAGE/units" "$STAGE/etc/sudoers.d" "$STAGE/etc/d3m0n-ai-worker"
while read -r relative _ _; do
  [[ -z "$relative" || "$relative" == \#* ]] && continue
  mkdir -p "$STAGE/prefix/source/$(dirname -- "$relative")"
  install -o root -g root -m 0644 -- "$SOURCE/$relative" "$STAGE/prefix/source/$relative"
done < "$MANIFEST"
cp -a -- "$SDK_SOURCE/." "$STAGE/prefix/sdk/"
cp -a -- "$SDK_VENV/." "$STAGE/prefix/venv/"
install -o root -g root -m 0644 -- "$HOSTDIR/bootstrap.py" "$STAGE/prefix/bootstrap.py"
install -o root -g root -m 0644 -- "$MANIFEST" "$STAGE/prefix/host-isolation/source-manifest.sha256"
install -o root -g root -m 0755 -- "$HOSTDIR/d3m0n-ai-worker-wrapper.sh" "$STAGE/libexec/d3m0n-ai-worker"
install -o root -g root -m 0755 -- "$HOSTDIR/client.py" "$STAGE/libexec/d3m0n-ai-worker-client"
install -o root -g root -m 0755 -- "$HOSTDIR/dispatcher.py" "$STAGE/libexec/d3m0n-ai-worker-dispatcher.py"
sed "s/__OPERATOR__/$OPERATOR_GROUP/g" "$HOSTDIR/d3m0n-ai-worker.socket" > "$STAGE/units/d3m0n-ai-worker.socket"
install -o root -g root -m 0644 -- "$HOSTDIR/d3m0n-ai-worker@.service" "$STAGE/units/d3m0n-ai-worker@.service"
sed "s/__OPERATOR__/$OPERATOR/g" "$HOSTDIR/d3m0n-ai-worker.sudoers" > "$STAGE/etc/sudoers.d/d3m0n-ai-worker"
cat > "$STAGE/etc/d3m0n-ai-worker/runtime.json" <<EOF
{
  "client_path": "/usr/local/libexec/d3m0n-ai-worker-client",
  "config_path": "/etc/d3m0n-ai-worker/runtime.json",
  "mode": "linux-host-isolation",
  "prefix": "/opt/d3m0n-ai-worker",
  "sdk_commit": "$SDK_COMMIT",
  "sdk_root": "/opt/d3m0n-ai-worker/sdk",
  "sdk_version": "$SDK_VERSION",
  "socket_path": "/run/d3m0n-ai-worker.sock",
  "source_root": "/opt/d3m0n-ai-worker/source",
  "venv_root": "/opt/d3m0n-ai-worker/venv",
  "worker_group": "$OPERATOR_GROUP",
  "worker_user": "$OPERATOR",
  "wrapper_path": "/usr/local/libexec/d3m0n-ai-worker"
}
EOF
visudo -cf "$STAGE/etc/sudoers.d/d3m0n-ai-worker" >/dev/null || fail 'generated sudoers failed validation'
systemd-analyze verify "$STAGE/units/d3m0n-ai-worker.socket" "$STAGE/units/d3m0n-ai-worker@.service" || fail 'generated systemd units failed validation'

systemctl disable --now d3m0n-ai-worker.socket >/dev/null 2>&1 || true
rm -rf -- "$PREFIX" "$WRAPPER" "$CLIENT" "$DISPATCHER" "$SOCKET_UNIT" "$SERVICE_UNIT" "$SUDOERS" "$CONFIG"
install -d -o root -g root -m 0755 /opt /usr/local/libexec /etc/systemd/system /etc/sudoers.d /etc/d3m0n-ai-worker
mv -- "$STAGE/prefix" "$PREFIX"
install -o root -g root -m 0755 -- "$STAGE/libexec/d3m0n-ai-worker" "$WRAPPER"
install -o root -g root -m 0755 -- "$STAGE/libexec/d3m0n-ai-worker-client" "$CLIENT"
install -o root -g root -m 0755 -- "$STAGE/libexec/d3m0n-ai-worker-dispatcher.py" "$DISPATCHER"
install -o root -g root -m 0644 -- "$STAGE/units/d3m0n-ai-worker.socket" "$SOCKET_UNIT"
install -o root -g root -m 0644 -- "$STAGE/units/d3m0n-ai-worker@.service" "$SERVICE_UNIT"
install -o root -g root -m 0440 -- "$STAGE/etc/sudoers.d/d3m0n-ai-worker" "$SUDOERS"
install -o root -g root -m 0644 -- "$STAGE/etc/d3m0n-ai-worker/runtime.json" "$CONFIG"
chown -R root:root "$PREFIX"; find "$PREFIX" -type d -exec chmod a+rx,a-w {} +; find "$PREFIX" -type f -perm /0111 -exec chmod a+rx,a-w {} +; find "$PREFIX" -type f ! -perm /0111 -exec chmod a+r,a-w {} +
systemd-analyze verify "$SOCKET_UNIT" "$SERVICE_UNIT"; visudo -cf "$SUDOERS" >/dev/null; systemctl daemon-reload
if (( ENABLE_SOCKET )); then systemctl enable --now d3m0n-ai-worker.socket; log 'socket enabled explicitly; no worker/model was started'; else log 'socket installed but left disabled (use --enable-socket explicitly)'; fi
if (( PROBE )); then
  command -v runuser >/dev/null 2>&1 || fail 'runuser is required for the non-credential probe'
  probe=$(runuser -u "$OPERATOR" -- "$CLIENT" --probe) || fail 'fixed no-credential probe failed'
  grep -Eiq 'api[_-]?key|bearer|secret|password|token|cookie' <<<"$probe" && fail 'probe emitted secret-like material' || true
  log 'fixed no-credential probe passed; no model, provider, device, or live job was started'
fi
log 'FleetAI runtime installed with manifest-only source staging and generated root-owned config; activation remains owner-controlled'
