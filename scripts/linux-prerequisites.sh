#!/usr/bin/env bash
# Linux prerequisites for install.sh. No phone discovery or device commands.
set -euo pipefail
umask 022

readonly NODE_VERSION='22.23.4'
readonly NODE_BASE_URL="https://nodejs.org/download/release/v${NODE_VERSION}"
readonly NODE_HOME_ROOT="${HOME}/.local/share/d3m0n"
DRY_RUN=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: bash scripts/linux-prerequisites.sh [--dry-run] [--yes]

Checks Python 3.11/3.12, venv, pip, Node.js/npm, adb, curl, tar, xz and
sha256sum. Missing OS packages are proposed for Debian/Ubuntu or Fedora only;
no package-repository additions, firewall changes, WSL changes, or phone actions
are performed.
EOF
}
log() { printf '[linux-prerequisites] %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
add_pkg() {
  local package=$1
  local existing
  for existing in "${PLAN_PKGS[@]}"; do
    [[ "$existing" == "$package" ]] && return 0
  done
  PLAN_PKGS+=("$package")
}
command_exists() { command -v "$1" >/dev/null 2>&1; }

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

if [[ ! -r /etc/os-release ]]; then
  fail 'cannot identify Linux distribution (/etc/os-release is unavailable); no changes made.'
fi
# shellcheck disable=SC1091
. /etc/os-release
OS_FAMILY=''
case "${ID:-}" in
  ubuntu|debian) OS_FAMILY='debian' ;;
  fedora) OS_FAMILY='fedora' ;;
  *) fail "unsupported Linux distribution: ${PRETTY_NAME:-${ID:-unknown}}. Supported: Debian/Ubuntu and Fedora. No changes made." ;;
esac

if [[ "$OS_FAMILY" == debian ]]; then
  command_exists apt-get || fail 'Debian/Ubuntu was detected but apt-get is unavailable; no changes made.'
else
  command_exists dnf || fail 'Fedora was detected but dnf is unavailable; no changes made.'
fi

PLAN_PKGS=()
PYTHON=''
for candidate in python3.12 python3.11 python3; do
  if command_exists "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' >/dev/null 2>&1; then
    PYTHON=$candidate
    break
  fi
done
PYTHON_VENV_OK=0
PYTHON_PIP_OK=0
if [[ -n "$PYTHON" ]]; then
  "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1 && PYTHON_VENV_OK=1 || true
  "$PYTHON" -c 'import ensurepip' >/dev/null 2>&1 && PYTHON_PIP_OK=1 || true
fi

NODE_CMD=''
NODE_VERSION_TEXT=''
for candidate in node "$NODE_HOME_ROOT"/node-v22.23.4-linux-*/bin/node; do
  candidate=$(command -v "$candidate" 2>/dev/null || true)
  [[ -n "$candidate" && -x "$candidate" ]] || continue
  NODE_CMD=$candidate
  NODE_VERSION_TEXT=$("$candidate" --version 2>/dev/null || true)
  break
done
node_version_ok() {
  local text=${1#v} major minor
  IFS=. read -r major minor _ <<< "$text"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] || return 1
  if (( major == 20 )); then
    (( minor >= 19 ))
  elif (( major >= 22 )); then
    if (( major == 22 )); then (( minor >= 12 )); else return 0; fi
  else
    return 1
  fi
}
NODE_OK=0
NPM_OK=0
if [[ -n "$NODE_CMD" ]] && node_version_ok "$NODE_VERSION_TEXT"; then
  NODE_OK=1
  "$NODE_CMD" --version >/dev/null 2>&1 && "$NODE_CMD" -p 'process.execPath' >/dev/null 2>&1 || NODE_OK=0
  "$NODE_CMD" --version >/dev/null 2>&1 && command_exists npm && npm --version >/dev/null 2>&1 && NPM_OK=1 || true
  # A user-local archive has npm beside node even when npm was not initially on PATH.
  if (( NODE_OK )) && [[ "$NODE_CMD" == "$NODE_HOME_ROOT"/* ]]; then
    [[ -x "$(dirname "$NODE_CMD")/../bin/npm" ]] && NPM_OK=1 || true
  fi
fi
if (( NODE_OK == 0 )); then
  for candidate in "$NODE_HOME_ROOT"/node-v22.23.4-linux-*/bin/node; do
    [[ -x "$candidate" ]] || continue
    candidate_version=$($candidate --version 2>/dev/null || true)
    if node_version_ok "$candidate_version" && [[ -x "$(dirname "$candidate")/npm" ]]; then
      NODE_CMD=$candidate
      NODE_VERSION_TEXT=$candidate_version
      NODE_OK=1
      NPM_OK=1
      break
    fi
  done
fi

ADB_OK=0
command_exists adb && adb version >/dev/null 2>&1 && ADB_OK=1 || true
CURL_OK=0; command_exists curl && CURL_OK=1 || true
TAR_OK=0; command_exists tar && TAR_OK=1 || true
XZ_OK=0; command_exists xz && XZ_OK=1 || true
SHA_OK=0; command_exists sha256sum && SHA_OK=1 || true

# OS packages are only proposed here. apt-cache/dnf list are local metadata reads;
# they do not refresh indexes or contact a network.
if [[ -z "$PYTHON" || $PYTHON_VENV_OK -eq 0 ]]; then
  if [[ "$OS_FAMILY" == debian ]]; then
    if apt-cache show python3.12 >/dev/null 2>&1; then
      add_pkg python3.12; add_pkg python3.12-venv
    elif apt-cache show python3.11 >/dev/null 2>&1; then
      add_pkg python3.11; add_pkg python3.11-venv
    else
      add_pkg python3.12; add_pkg python3.12-venv
    fi
  else
    add_pkg python3.12
  fi
fi
if [[ -z "$PYTHON" || $PYTHON_PIP_OK -eq 0 ]]; then
  case "$PYTHON" in
    python3.11) add_pkg python3.11-venv ;;
    python3.12) add_pkg python3.12-venv ;;
    *) add_pkg python3-pip ;;
  esac
fi
(( ADB_OK )) || { [[ "$OS_FAMILY" == debian ]] && add_pkg adb || add_pkg android-tools; }
(( CURL_OK )) || add_pkg curl
(( TAR_OK )) || add_pkg tar
(( XZ_OK )) || { [[ "$OS_FAMILY" == debian ]] && add_pkg xz-utils || add_pkg xz; }
(( SHA_OK )) || { [[ "$OS_FAMILY" == debian ]] && add_pkg coreutils || add_pkg coreutils; }

NODE_NEEDS_DOWNLOAD=0
if (( NODE_OK == 0 || NPM_OK == 0 )); then NODE_NEEDS_DOWNLOAD=1; fi
NODE_ARCH=''
if (( NODE_NEEDS_DOWNLOAD )); then
  case "$(uname -m)" in
    x86_64|amd64) NODE_ARCH=x64 ;;
    aarch64|arm64) NODE_ARCH=arm64 ;;
    *) fail "unsupported Linux architecture $(uname -m) for the official Node.js archive; no changes made." ;;
  esac
fi

log "Detected: ${PRETTY_NAME:-unknown} (${OS_FAMILY}); Python=${PYTHON:-missing}; Node=${NODE_VERSION_TEXT:-missing}; adb=$([[ $ADB_OK == 1 ]] && echo ok || echo missing)"
log "Checks: venv=$([[ $PYTHON_VENV_OK == 1 ]] && echo ok || echo missing), pip=$([[ $PYTHON_PIP_OK == 1 ]] && echo ok || echo missing), npm=$([[ $NPM_OK == 1 ]] && echo ok || echo missing), curl=$([[ $CURL_OK == 1 ]] && echo ok || echo missing), tar=$([[ $TAR_OK == 1 ]] && echo ok || echo missing), xz=$([[ $XZ_OK == 1 ]] && echo ok || echo missing), sha256sum=$([[ $SHA_OK == 1 ]] && echo ok || echo missing)"

if (( ${#PLAN_PKGS[@]} == 0 && NODE_NEEDS_DOWNLOAD == 0 )); then
  log 'All Linux prerequisites are ready; no OS or network changes are needed.'
  exit 0
fi

log 'Planned changes (nothing has been changed yet):'
if (( ${#PLAN_PKGS[@]} )); then
  if [[ "$OS_FAMILY" == debian ]]; then
    printf '  - sudo apt-get update\n  - sudo apt-get install -y --no-install-recommends'
  else
    printf '  - sudo dnf install -y'
  fi
  printf ' %q' "${PLAN_PKGS[@]}"
  printf '\n'
fi
if (( NODE_NEEDS_DOWNLOAD )); then
  log "  - download official Node.js v${NODE_VERSION} Linux archive for $(uname -m) into ${NODE_HOME_ROOT} (user-local; existing system node is not replaced)"
  log "  - verify its SHA256 against ${NODE_BASE_URL}/SHASUMS256.txt before extraction"
fi
if (( DRY_RUN )); then
  log 'Dry-run: the plan was reported; no writes, package operations, privilege escalation, or network requests were performed.'
  exit 0
fi

if (( EUID == 0 )); then
  fail 'refusing to continue as root; run install.sh as the app owner. OS package operations use scoped sudo.'
fi
if (( ASSUME_YES == 0 )); then
  printf 'Proceed with the listed OS/network changes? [y/N] '
  answer=''
  IFS= read -r answer || true
  case "$answer" in
    y|Y|yes|YES|Yes) ;;
    *) log 'Declined; no changes made. Re-run with --yes only after reviewing the plan.'; exit 1 ;;
  esac
fi

if (( ${#PLAN_PKGS[@]} )); then
  if [[ "$OS_FAMILY" == debian ]]; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PLAN_PKGS[@]}"
  else
    sudo dnf install -y "${PLAN_PKGS[@]}"
  fi
fi

# Refresh command discovery after the explicitly consented package operation.
PATH="${NODE_HOME_ROOT}/node-v${NODE_VERSION}-linux-x64/bin:${NODE_HOME_ROOT}/node-v${NODE_VERSION}-linux-arm64/bin:$PATH"
export PATH

if (( NODE_NEEDS_DOWNLOAD )); then
  command_exists curl || fail 'curl is still missing after package installation; cannot download Node.js.'
  command_exists tar || fail 'tar is still missing after package installation; cannot install Node.js.'
  command_exists xz || fail 'xz is still missing after package installation; cannot install Node.js.'
  command_exists sha256sum || fail 'sha256sum is still missing after package installation; cannot verify Node.js.'
  NODE_FILE="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  NODE_ROOT="node-v${NODE_VERSION}-linux-${NODE_ARCH}"
  NODE_DEST="${NODE_HOME_ROOT}/${NODE_ROOT}"
  if [[ -x "$NODE_DEST/bin/node" && -x "$NODE_DEST/bin/npm" ]]; then
    log "User-local Node.js v${NODE_VERSION} already exists; preserving it."
  else
    [[ ! -e "$NODE_DEST" ]] || fail "refusing to replace an unexpected existing path: $NODE_DEST"
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/d3m0n-node.XXXXXX")
    cleanup() { rm -rf "$tmpdir"; }
    trap cleanup EXIT
    archive="$tmpdir/$NODE_FILE"
    sums="$tmpdir/SHASUMS256.txt"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$archive" "${NODE_BASE_URL}/${NODE_FILE}"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$sums" "${NODE_BASE_URL}/SHASUMS256.txt"
    expected=$(awk -v file="$NODE_FILE" '$2 == file {print $1; exit}' "$sums")
    [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || fail "official SHASUMS256.txt did not contain a valid checksum for $NODE_FILE"
    printf '%s  %s\n' "$expected" "$archive" | sha256sum -c - >/dev/null
    entries=$(tar -tJf "$archive")
    while IFS= read -r entry; do
      [[ -z "$entry" ]] && continue
      [[ "$entry" != /* && "$entry" != *'..'* ]] || fail "unsafe path in Node archive: $entry"
      [[ "$entry" == "$NODE_ROOT" || "$entry" == "$NODE_ROOT/" || "$entry" == "$NODE_ROOT"/* ]] || fail "unexpected path in Node archive: $entry"
    done <<< "$entries"
    mkdir -p "$tmpdir/extract"
    tar -xJf "$archive" --no-same-owner --no-same-permissions -C "$tmpdir/extract"
    extracted_root="$tmpdir/extract/$NODE_ROOT"
    while IFS= read -r -d '' link_path; do
      link_target=$(readlink "$link_path")
      resolved_target=$(realpath -m -- "$(dirname "$link_path")/$link_target")
      case "$resolved_target" in
        "$extracted_root"/*) ;;
        *) fail "unsafe symlink target in Node archive: $link_path -> $link_target" ;;
      esac
    done < <(find "$extracted_root" -type l -print0)
    [[ -x "$tmpdir/extract/$NODE_ROOT/bin/node" && -x "$tmpdir/extract/$NODE_ROOT/bin/npm" ]] || fail 'Node archive did not contain expected executable paths'
    mkdir -p "$NODE_HOME_ROOT"
    mv "$tmpdir/extract/$NODE_ROOT" "$NODE_DEST"
    log "Installed verified Node.js v${NODE_VERSION} under $NODE_DEST (user-local)."
  fi
fi

# A final read-only check is useful for a rerun/resume, but adb version is the
# only adb operation permitted by this installer.
PATH="${NODE_HOME_ROOT}/node-v${NODE_VERSION}-linux-x64/bin:${NODE_HOME_ROOT}/node-v${NODE_VERSION}-linux-arm64/bin:$PATH"
export PATH
for required in python3.11 python3.12 node npm adb curl tar xz sha256sum; do
  : # report below; Python aliases are checked separately to avoid requiring both.
done
final_python=''
for candidate in python3.12 python3.11 python3; do
  if command_exists "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' >/dev/null 2>&1; then final_python=$candidate; break; fi
done
[[ -n "$final_python" ]] || fail 'supported Python 3.11/3.12 is still unavailable; resume after fixing the package manager result.'
"$final_python" -c 'import venv, ensurepip' >/dev/null 2>&1 || fail 'Python venv/ensurepip support is still unavailable; install the matching -venv package and resume.'
node --version >/dev/null 2>&1 || fail 'Node.js is still unavailable after installation.'
npm --version >/dev/null 2>&1 || fail 'npm is still unavailable after installation.'
adb version >/dev/null 2>&1 || fail 'adb is still unavailable after installation.'
log 'Prerequisites are ready. Existing project files, .env, and state were not modified.'
