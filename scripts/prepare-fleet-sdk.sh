#!/usr/bin/env bash
# Prepare the reviewed SDK in a user-selected task directory. This script never
# accepts an arbitrary URL/ref, runs a model, contacts a provider, or touches a
# system installation. Network access is only the fixed upstream git/pip step.
set -euo pipefail
umask 077
readonly UPSTREAM=https://github.com/droidrun/mobilerun.git
readonly COMMIT=301856138c6edeb2ae692bfab85b279ace520e59
readonly VERSION=0.7.0
TASK_DIR=; PYTHON=python3; DRY_RUN=0
usage() { echo 'usage: prepare-fleet-sdk.sh --task-dir DIR [--python PYTHON] [--dry-run]'; }
fail() { echo "prepare-fleet-sdk: $*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --task-dir) (($# >= 2)) || fail 'missing --task-dir value'; TASK_DIR=$2; shift ;;
    --python) (($# >= 2)) || fail 'missing --python value'; PYTHON=$2; shift ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
  esac
  shift
done
[[ -n "$TASK_DIR" && "$TASK_DIR" != */ && "$TASK_DIR" != *'..'* ]] || fail '--task-dir is required and may not contain traversal'
command -v git >/dev/null 2>&1 || fail 'git is required'
command -v "$PYTHON" >/dev/null 2>&1 || fail "python is unavailable: $PYTHON"
if (( DRY_RUN )); then
  printf '%s\n' "dry-run: would clone $UPSTREAM at $COMMIT, build mobilerun==$VERSION as a wheel, install it non-editably with --copies, and preserve LICENSE under $TASK_DIR"
  exit 0
fi
mkdir -p -- "$TASK_DIR"
readonly CHECKOUT=$TASK_DIR/mobilerun-source
readonly WHEELS=$TASK_DIR/wheels
readonly VENV=$TASK_DIR/venv
[[ ! -e "$CHECKOUT" && ! -e "$WHEELS" && ! -e "$VENV" ]] || fail 'task directory already contains prepared SDK outputs; use a new task directory'
git clone --no-checkout --filter=blob:none "$UPSTREAM" "$CHECKOUT"
git -C "$CHECKOUT" fetch --depth 1 origin "$COMMIT"
git -C "$CHECKOUT" checkout --detach "$COMMIT"
[[ "$(git -C "$CHECKOUT" rev-parse HEAD)" == "$COMMIT" ]] || fail 'upstream checkout is not the reviewed commit'
grep -q "version = \"$VERSION\"" "$CHECKOUT/pyproject.toml" || fail 'upstream package version is not 0.7.0'
[[ -f "$CHECKOUT/LICENSE" ]] || fail 'upstream LICENSE is missing'
mkdir -p -- "$WHEELS"
"$PYTHON" -m pip wheel --wheel-dir "$WHEELS" "$CHECKOUT"
"$PYTHON" -m venv --copies "$VENV"
"$VENV/bin/python" -m pip install --no-index --find-links "$WHEELS" "mobilerun==$VERSION"
"$VENV/bin/python" -m pip check
# venv --copies still creates the conventional lib64 -> lib alias on Linux.
# Python's actual site-packages live in lib; remove only this exact internal
# alias so the root installer can continue rejecting every other symlink.
if [[ -L "$VENV/lib64" && "$(readlink "$VENV/lib64")" == lib ]]; then
  rm -- "$VENV/lib64"
fi
install -m 0644 -- "$CHECKOUT/LICENSE" "$TASK_DIR/MOBILERUN-LICENSE"
printf '%s\n' "prepared pinned Mobilerun $COMMIT / $VERSION" "source=$CHECKOUT" "sdk_source=$CHECKOUT" "sdk_venv=$VENV" "license=$TASK_DIR/MOBILERUN-LICENSE"
