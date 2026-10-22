import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
let source
try { source = await readFile(join(root, 'ProxyManagerPanel.vue'), 'utf8') }
catch { source = await readFile(join(root, 'src/components/ProxyManagerPanel.vue'), 'utf8') }

function includes(text, message) {
  assert.ok(source.includes(text), `missing ${message}: ${text}`)
}

// This is intentionally a source-level test: the parent project supplies Vue/Vite remotely.
assert.ok(source.startsWith('<script setup lang="ts">'), 'component uses script setup TypeScript')
includes("'/api/proxy/capabilities'", 'public capabilities endpoint')
includes("'/api/proxy/devices'", 'admin device endpoint')
includes("'/api/proxy/profiles'", 'profile save endpoint')
includes("'/api/proxy/jobs'", 'job creation endpoint')
includes("`/api/proxy/jobs/${encodeURIComponent(id)}`", 'job polling endpoint')
// Access follows the dashboard password session; there is no separate proxy key.
assert.ok(!source.includes('X-Proxy-Admin-Key'), 'no separate proxy admin key header')
assert.ok(!source.includes('importKey') && !source.includes('keyDraft'), 'no proxy key import/unlock flow')
includes("credentials: 'same-origin'", 'session cookie is sent with proxy requests')
includes("dashboardAuth.phase === 'unlocked'", 'proxy access follows the dashboard unlock')
includes("{ action, devices: devicesForJob, confirmed }", 'literal job request payload')

for (const requiredAction of ['test', 'apply', 'disable']) {
  assert.ok(source.includes(`'${requiredAction}'`), `supports ${requiredAction} action`)
}
for (const requiredLabel of ['Save profile only', 'Test selected (server preflight)', 'Apply selected routing', 'Disable selected routing', 'Lock dashboard']) {
  assert.ok(source.includes(requiredLabel), `renders ${requiredLabel}`)
}

// Contract security invariants.
assert.ok(!source.includes('localStorage'), 'does not use localStorage')
assert.ok(!source.includes('sessionStorage'), 'does not use sessionStorage')
assert.ok(source.includes('autocomplete="new-password"'), 'credential inputs use new-password autocomplete')
assert.ok(source.includes('Blank fields preserve an existing credential'), 'preserve-existing credential hint is visible')
assert.ok(source.includes('Save profile only'), 'save-only operation is distinct from apply')
assert.ok(source.includes('Routing was not changed'), 'save does not imply routing')
assert.ok(source.includes('Protected by the dashboard password'), 'protection notice is visible')
assert.ok(source.includes('cannot prove anonymity'), 'detection limitation is disclosed')
assert.ok(source.includes('server preflight'), 'server-test scope is disclosed')
assert.ok(source.includes('Device-side routing verified'), 'device verification is distinguished')
assert.ok(source.includes('setup required'), 'unsupported/setup-required state is represented')

// Polling must have a finite bound and explicit cancellation paths.
includes('const maxJobPolls = 900', 'bounded job poll count for multi-device batches')
includes('clearTimeout', 'timer cleanup')
includes('onUnmounted', 'unmount cleanup')
includes('stopAllPolling()', 'poller release')
includes('AbortController', 'cancellable fetches')
assert.match(source, /state\.attempts\s*>=\s*maxJobPolls/, 'job polling stops at its bound')

// Credential values are not copied into error strings or rendered from response payloads.
assert.ok(!source.includes('error.message}'))
assert.ok(!source.includes('payload.detail'), 'does not display arbitrary API detail that could reflect credentials')
assert.ok(source.includes("throw new Error('The proxy service could not be reached.')"), 'network errors are generic')

console.log('test_proxy_panel: all source contract checks passed')
