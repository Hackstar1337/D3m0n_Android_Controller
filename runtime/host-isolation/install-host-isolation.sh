#!/usr/bin/env bash
# PREPARED ARTIFACT: parent runs this on the target Linux host after review.
# Usage: install-host-isolation.sh <d3m0n-checkout> <sdk-source> <sdk-venv>
set -euo pipefail
umask 022

readonly PREFIX=/opt/d3m0n-ai-worker
readonly WRAPPER=/usr/local/libexec/d3m0n-ai-worker
readonly SUDOERS=/etc/sudoers.d/d3m0n-ai-worker
readonly HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
readonly MANIFEST="$HERE/source-manifest.sha256"

fail() { printf 'install-host-isolation: %s\n' "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "unsupported host: missing $1"; }

[[ $# -eq 3 ]] || fail 'usage: install-host-isolation.sh <d3m0n-checkout> <sdk-source> <sdk-venv>'
[[ $(id -u) -eq 0 ]] || fail 'must be run as root; do not use an unbounded sudo shell'
for command_name in bash cp find install sha256sum awk mv rm mkdir mktemp chown chmod id stat grep systemd-run systemctl sudo visudo; do
  need_cmd "$command_name"
done
[[ -r "$MANIFEST" ]] || fail "missing audited manifest: $MANIFEST"
[[ -x /usr/bin/systemd-run || -x "$(command -v systemd-run)" ]] || fail 'systemd-run is not executable'
systemd-run --version >/dev/null 2>&1 || fail 'systemd-run is not functional'
systemctl --version >/dev/null 2>&1 || fail 'systemd is not available'
sudo -V >/dev/null 2>&1 || fail 'sudo is not available'
visudo -V >/dev/null 2>&1 || fail 'visudo is not available'
id ash >/dev/null 2>&1 || fail 'required sudoers principal ash does not exist'

readonly CHECKOUT=$(CDPATH= cd -- "$1" && pwd)
readonly SDK_SOURCE=$(CDPATH= cd -- "$2" && pwd)
readonly SDK_VENV=$(CDPATH= cd -- "$3" && pwd)
[[ -d "$CHECKOUT" && -d "$SDK_SOURCE" && -d "$SDK_VENV" ]] || fail 'all three inputs must be directories'
[[ -x "$SDK_VENV/bin/python" ]] || fail 'SDK venv must contain bin/python'
[[ ! -e "$PREFIX" && ! -e "$WRAPPER" && ! -e "$SUDOERS" ]] || fail 'refusing to replace an existing installation; rollback first'

# Reject symlinks in all input trees. This makes the copy boundary auditable and
# prevents an input checkout from smuggling user-owned files into /opt.
for tree in "$CHECKOUT" "$SDK_SOURCE" "$SDK_VENV"; do
  if find -P "$tree" -type l -print -quit | grep -q .; then
    fail "input tree contains a symlink: $tree"
  fi
done

# Verify every application file against the reviewed Windows snapshot before it
# is copied. The SDK tree is separately pinned/verified by the parent process.
while read -r relative expected _; do
  [[ -z "$relative" || "$relative" == \#* ]] && continue
  [[ "$relative" == */* || "$relative" == *.py ]] || fail "invalid manifest path: $relative"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || fail "invalid manifest hash: $relative"
  file="$CHECKOUT/$relative"
  [[ -f "$file" && ! -L "$file" ]] || fail "manifest file missing or symlink: $relative"
  actual=$(sha256sum -- "$file" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || fail "source hash mismatch: $relative"
done < "$MANIFEST"

readonly STAGE=$(mktemp -d /tmp/d3m0n-ai-worker.XXXXXX)
cleanup() { rm -rf -- "$STAGE"; }
trap cleanup EXIT
mkdir -p "$STAGE/source" "$STAGE/sdk" "$STAGE/venv"

while read -r relative _ _; do
  [[ -z "$relative" || "$relative" == \#* ]] && continue
  mkdir -p "$STAGE/source/$(dirname -- "$relative")"
  install -o root -g root -m 0644 -- "$CHECKOUT/$relative" "$STAGE/source/$relative"
done < "$MANIFEST"
cp -a -- "$SDK_SOURCE/." "$STAGE/sdk/"
cp -a -- "$SDK_VENV/." "$STAGE/venv/"
install -o root -g root -m 0644 -- "$HERE/bootstrap.py" "$STAGE/bootstrap.py"

# Private input/mktemp modes must not block DynamicUser traversal/imports.
# Grant read/execute only; keep all installed code root-owned and non-writable.
find "$STAGE" -type d -exec chmod a+rx,a-w {} +
find "$STAGE" -type f -perm /0111 -exec chmod a+rx,a-w {} +
find "$STAGE" -type f ! -perm /0111 -exec chmod a+r,a-w {} +
chown -R root:root "$STAGE"

mkdir -p /opt /usr/local/libexec /etc/sudoers.d
install -o root -g root -m 0755 -- "$HERE/d3m0n-ai-worker-wrapper.sh" "$STAGE/wrapper"
install -o root -g root -m 0440 -- "$HERE/d3m0n-ai-worker.sudoers" "$STAGE/sudoers"
visudo -cf "$STAGE/sudoers" >/dev/null || fail 'sudoers validation failed'

mv -- "$STAGE" "$PREFIX"
install -o root -g root -m 0755 -- "$PREFIX/wrapper" "$WRAPPER"
rm -f -- "$PREFIX/wrapper"
install -o root -g root -m 0440 -- "$PREFIX/sudoers" "$SUDOERS"
rm -f -- "$PREFIX/sudoers"
trap - EXIT

# Confirm immutable ownership/modes after the move. Do not start the worker.
[[ "$(stat -c '%u:%g' "$PREFIX/source" "$PREFIX/sdk" "$PREFIX/venv" "$WRAPPER" "$SUDOERS" | sort -u)" == "0:0" ]] || fail 'ownership check failed'
[[ -z "$(find "$PREFIX/source" "$PREFIX/sdk" "$PREFIX/venv" -perm /0222 -print -quit)" ]] || fail 'read-only staging check failed'
visudo -cf "$SUDOERS" >/dev/null || fail 'installed sudoers validation failed'
printf '%s\n' 'staged; no worker, probe, network, or provider command was executed'
