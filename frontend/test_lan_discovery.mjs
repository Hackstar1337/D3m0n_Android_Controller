import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = await readFile(new URL('./src/components/DeviceDiscoveryPanel.vue', import.meta.url), 'utf8')
for (const required of [
  '/api/phone/discovery/interfaces',
  '/api/phone/discovery/status',
  '/api/phone/discovery/scan',
  '/api/phone/discovery/enroll',
  'Scan selected LAN',
  'Pair &amp; connect',
  "emit('enrolled'",
  'adb connect',
  'unauthorized',
]) assert.ok(source.includes(required), `missing discovery contract: ${required}`)
assert.ok(source.includes('window.confirm'), 'LAN probing must require confirmation')
assert.ok(source.includes("scan(['mdns'])"), 'initial discovery must be passive mDNS')
assert.ok(source.includes('pairing_code'), 'pairing code must be submitted only to enrollment')
console.log('frontend LAN discovery contract passed')
