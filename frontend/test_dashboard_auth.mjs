// Source-level contracts for the dashboard-wide password unlock.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const read = path => readFile(join(root, path), 'utf8')
const [main, rootView, lock, auth, app] = await Promise.all([
  read('src/main.ts'), read('src/DashboardRoot.vue'), read('src/components/DashboardLock.vue'),
  read('src/composables/dashboardAuth.ts'), read('src/App.vue'),
])

// The application (streams, polling, sockets) only mounts after unlock.
assert.ok(main.includes('createApp(DashboardRoot)'), 'main mounts the lock-aware root')
assert.ok(main.includes('installDashboardAuthGuard()'), 'session-expiry guard is installed')
assert.ok(/<App v-if="dashboardAuth\.phase === 'unlocked'" \/>\s*<DashboardLock v-else \/>/.test(rootView), 'App renders only when unlocked')

// Password handling: sent once, never stored, never echoed.
for (const text of [lock, auth, rootView]) {
  assert.ok(!/localStorage|sessionStorage|indexedDB|document\.cookie/.test(text), 'no browser storage or script-readable cookies')
}
assert.ok(lock.includes('autocomplete="current-password"'), 'password manager friendly input')
assert.ok(/const value = password\.value\s+password\.value = ''/.test(lock), 'password field is cleared on submit')
assert.ok(auth.includes("'/api/auth/unlock'") && auth.includes("'/api/auth/status'") && auth.includes("'/api/auth/lock'"), 'uses the server auth endpoints')
assert.ok(auth.includes("JSON.stringify({ password })"), 'password is only sent in the unlock request body')
assert.ok(!/console\.(log|info|debug)/.test(auth + lock), 'nothing is logged')

// Lockout and session expiry are surfaced, not silently retried.
assert.ok(auth.includes('response.status === 429') && lock.includes('Locked out'), 'rate-limit lockout is shown')
assert.ok(auth.includes("response.headers.get('x-d3m0n-auth') === 'locked'"), 'guard reacts only to the gate marker')
assert.ok(auth.includes("!path.startsWith('/api/auth/')"), 'guard ignores auth endpoints')
assert.ok(lock.includes("dashboardAuth.phase === 'unconfigured'") && lock.includes("dashboardAuth.phase === 'offline'"), 'unconfigured and offline states are handled')

// Users can lock the dashboard from the shell.
assert.ok(app.includes('@click="lockDashboard"') && app.includes('Lock dashboard'), 'sidebar exposes Lock dashboard')
console.log('DASHBOARD_AUTH_FRONTEND_TESTS=PASS')
