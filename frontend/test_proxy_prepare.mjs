// Contract test: one-click client setup in the Proxy Manager (source-level, no network).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const panel = readFileSync(new URL('./src/components/ProxyManagerPanel.vue', import.meta.url), 'utf8')
const session = readFileSync(new URL('./src/composables/proxySession.ts', import.meta.url), 'utf8')

assert.match(session, /export type JobAction = 'test' \| 'apply' \| 'disable' \| 'prepare'/, 'prepare is a job action')
assert.match(session, /vpn_consent\?: boolean \| null/, 'client consent state is typed')
assert.match(session, /installed_now\?: boolean/, 'prepare results carry install state')

// Prepare is one click: the button itself is the explicit request, no saved profile required.
assert.ok(panel.includes("void startJob('prepare', serials, true)"), 'prepare starts with explicit confirmation from the click')
assert.ok(panel.includes("action !== 'disable' && action !== 'prepare' && missingProfiles.length"), 'prepare does not require a proxy profile')
assert.ok(panel.includes("item.action === 'prepare'"), 'prepare jobs are parsed (re-attach after reload)')

// Targets: never other-VPN or offline phones; includes not-installed, missing consent and unchecked phones.
const needs = panel.slice(panel.indexOf('function needsPrepare'), panel.indexOf('function jobActiveFor'))
assert.ok(needs.includes("if (!device.online) return false"), 'offline phones are skipped')
assert.ok(needs.includes("client.status === 'other_vpn'"), 'phones with another VPN are never touched')
assert.ok(needs.includes("client.vpn_consent === false"), 'missing VPN consent is prepared')

// Execute the target selection logic on representative states.
const clients = {
  a: { status: 'client_missing' }, b: { status: 'ready', vpn_consent: true }, c: { status: 'ready', vpn_consent: false },
  d: { status: 'other_vpn' }, e: { status: 'unknown' }, g: { status: 'offline' },
}
const clientOf = serial => clients[serial] ?? null
const body = needs.replace(/function needsPrepare\(device: Device\): boolean/, 'return function needsPrepare(device)')
const needsPrepare = new Function('clientOf', body)(clientOf)
const fleet = ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(serial => ({ serial, online: true })).concat([{ serial: 'h', online: false }])
assert.deepEqual(fleet.filter(needsPrepare).map(d => d.serial), ['a', 'c', 'e', 'f'])

// UI entry points and truthful copy.
for (const label of ['Prepare all clients', 'Prepare selected', "'Prepare client'", 'routing unchanged', "return 'Client setup'"]) {
  assert.ok(panel.includes(label), `missing UI text: ${label}`)
}
assert.ok(!panel.includes('so device-wide Apply will report setup required'), 'the old dead-end message is gone')
assert.ok(panel.includes("['Explicit confirmation is required for device changes'"), 'new server detail is mapped to safe text')

console.log('proxy prepare contracts: OK')
