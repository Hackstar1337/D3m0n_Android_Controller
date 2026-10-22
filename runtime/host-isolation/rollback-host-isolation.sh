#!/usr/bin/env bash
# PREPARED ARTIFACT: parent runs this on the target Linux host if rollback is approved.
set -euo pipefail
umask 077

readonly PREFIX=/opt/d3m0n-ai-worker
readonly WRAPPER=/usr/local/libexec/d3m0n-ai-worker
readonly SUDOERS=/etc/sudoers.d/d3m0n-ai-worker
fail() { printf 'rollback-host-isolation: %s\n' "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'must be run as root'
for command_name in visudo stat rm; do command -v "$command_name" >/dev/null 2>&1 || fail "unsupported host: missing $command_name"; done
[[ ! -e "$SUDOERS" || "$(stat -c '%u:%g %a' "$SUDOERS")" == '0:0 440' ]] || fail 'sudoers ownership/mode is unexpected; inspect manually'
if [[ -e "$SUDOERS" ]]; then visudo -cf "$SUDOERS" >/dev/null || fail 'refusing to remove an invalid sudoers file'; fi
rm -f -- "$SUDOERS" "$WRAPPER"
rm -rf -- "$PREFIX"
printf '%s\n' 'worker wrapper, sudoers drop-in, and /opt staging removed; application source was not modified'
