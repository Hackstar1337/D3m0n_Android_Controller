#!/bin/sh
set -eu

APP_DIR=${D3M0N_FRONTEND_DIR:-/app/frontend}
MODULE_DIR=${D3M0N_FRONTEND_MODULE_DIR:-$APP_DIR/node_modules}
LOCKFILE=$APP_DIR/package-lock.json
FINGERPRINT=$MODULE_DIR/.d3m0n-lock-fingerprint

[ -f "$LOCKFILE" ] || { echo "frontend_entrypoint: missing $LOCKFILE" >&2; exit 1; }
mkdir -p "$MODULE_DIR"
EXPECTED=$(sha256sum "$LOCKFILE" | awk '{print $1}')
ACTUAL=''
[ -f "$FINGERPRINT" ] && ACTUAL=$(cat "$FINGERPRINT" 2>/dev/null || true)

if [ "$EXPECTED" != "$ACTUAL" ] || [ ! -x "$MODULE_DIR/.bin/vite" ]; then
  echo "frontend_entrypoint: installing dependencies for lockfile $EXPECTED"
  cd "$APP_DIR"
  npm ci
  TMP="$FINGERPRINT.tmp.$$"
  printf '%s\n' "$EXPECTED" > "$TMP"
  mv "$TMP" "$FINGERPRINT"
else
  echo "frontend_entrypoint: dependency volume is current ($EXPECTED)"
fi

cd "$APP_DIR"
exec npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
