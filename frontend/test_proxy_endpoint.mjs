// Tests for the local proxy endpoint parser and the memory-only proxy session.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createRequire } from 'node:module'

const root = dirname(fileURLToPath(import.meta.url))
const require = createRequire(pathToFileURL(join(root, 'package.json')))
const ts = require('typescript')

const parserSource = await readFile(join(root, 'src/composables/proxyEndpoint.ts'), 'utf8')
const compiled = ts.transpileModule(parserSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText
const { parseProxyEndpoint, looksLikeEndpoint } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

function ok(input, expected, fallback) {
  const result = parseProxyEndpoint(input, fallback)
  assert.equal(result.ok, true, `${input} should parse: ${result.error}`)
  assert.deepEqual(result.value, expected, input)
}
function bad(input) {
  const result = parseProxyEndpoint(input)
  assert.equal(result.ok, false, `${input} should be rejected`)
  assert.equal(typeof result.error, 'string')
  assert.ok(!result.error.includes('placeholder-pass'), 'errors never echo credentials')
}

ok('http://alice:placeholder-pass@proxy.example.net:8080', { proxy_type: 'http', host: 'proxy.example.net', port: 8080, username: 'alice', password: 'placeholder-pass' })
ok('https://proxy.example.net:443/', { proxy_type: 'https', host: 'proxy.example.net', port: 443, username: null, password: null })
ok('socks5h://u:placeholder-pass%40@10.0.0.5:1080', { proxy_type: 'socks5', host: '10.0.0.5', port: 1080, username: 'u', password: 'placeholder-pass@' })
ok('alice:placeholder-pass@proxy.example.net:3128', { proxy_type: 'http', host: 'proxy.example.net', port: 3128, username: 'alice', password: 'placeholder-pass' })
ok('proxy.example.net:3128', { proxy_type: 'socks5', host: 'proxy.example.net', port: 3128, username: null, password: null }, 'socks5')
ok('proxy.example.net:3128:alice:placeholder-pass:', { proxy_type: 'http', host: 'proxy.example.net', port: 3128, username: 'alice', password: 'placeholder-pass:' })
ok('alice:placeholder-pass:proxy.example.net:3128', { proxy_type: 'http', host: 'proxy.example.net', port: 3128, username: 'alice', password: 'placeholder-pass' })
ok('alice:placeholder-pass:proxy.example.net:3128', { proxy_type: 'http', host: 'proxy.example.net', port: 3128, username: 'alice', password: 'placeholder-pass' })
ok('[2001:db8::1]:8080', { proxy_type: 'http', host: '[2001:db8::1]', port: 8080, username: null, password: null })
bad('')
bad('ftp://proxy.example.net:21')
bad('proxy.example.net')
bad('proxy.example.net:99999')
bad('http://alice:placeholder-pass@bad host:80')
bad('http://alice:placeholder-pass@-bad-.example:80x')

assert.equal(looksLikeEndpoint('proxy.example.net'), false)
assert.equal(looksLikeEndpoint('proxy.example.net:8080'), true)
assert.equal(looksLikeEndpoint('http://proxy.example.net'), true)
assert.equal(looksLikeEndpoint('u:p@proxy.example.net:1'), true)

// Session store and panel: memory-only, cleared when the dashboard locks, no browser storage.
const session = await readFile(join(root, 'src/composables/proxySession.ts'), 'utf8')
const panel = await readFile(join(root, 'src/components/ProxyManagerPanel.vue'), 'utf8')
const auth = await readFile(join(root, 'src/composables/dashboardAuth.ts'), 'utf8')
for (const text of [session, panel, auth]) {
  assert.ok(!text.includes('localStorage') && !text.includes('sessionStorage') && !text.includes('indexedDB'), 'no browser storage')
}
assert.ok(!session.includes('adminKey') && !panel.includes('adminKey'), 'no proxy admin key is held in memory')
assert.ok(/watch\(\(\) => dashboardAuth\.phase, phase => \{ if \(phase !== 'unlocked'\) clearProxySession\(\) \}\)/.test(session), 'dashboard lock clears the proxy session')
assert.ok(/for \(const serial of Object\.keys\(s\.editors\)\) delete s\.editors\[serial\]/.test(session), 'clear wipes unsaved credential drafts')
assert.ok(auth.includes("credentials: 'same-origin'") && !auth.includes('document.cookie'), 'session cookie stays HttpOnly and server-managed')
assert.ok(panel.includes("'/api/proxy/clients?refresh=1'") && panel.includes("'/api/proxy/clients'"), 'client readiness endpoint')
assert.ok(panel.includes('SAFE_SERVER_DETAILS.get('), 'only allow-listed server messages are shown')
assert.ok(/finally \{[\s\S]{0,400}scheduleDeviceRefresh\(\)/.test(panel), 'device refresh loop always reschedules')
assert.ok(/watch\(unlocked, value => \{ if \(!value\) stopAllPolling\(\) \}\)/.test(panel), 'locking stops all proxy polling')
assert.ok(panel.includes('bulkPayloads = new Map()'), 'bulk credentials are discarded')
assert.ok(!/\{\{[^}]*(password|username)[^}]*\}\}/.test(panel.replace(/username_set|password_set/g, '')), 'credentials are never interpolated into the template')
console.log('PROXY_ENDPOINT_AND_SESSION_TESTS=PASS')
