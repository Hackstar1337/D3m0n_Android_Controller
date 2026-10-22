import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(root, 'src/components/ProxyManagerPanel.vue'), 'utf8')

assert.ok(source.includes('const capabilitiesUnavailable = ref(false)'), 'capability availability has explicit state')
assert.ok(source.includes('capabilities.value = null'), 'failed capability fetch clears stale capability data')
assert.ok(source.includes('capabilitiesUnavailable.value = true'), 'failed capability fetch is marked unavailable')
assert.ok(source.includes("return 'Setup/status unavailable'"), 'failed capability fetch renders unavailable status')
assert.ok(!source.includes("capabilities.value = { device_wide: true"), 'failure never infers device-wide support')

const capabilityText = source.match(/const capabilityText = computed\(\(\) => \{([\s\S]*?)\n\}\)/)
assert.ok(capabilityText, 'capability badge text computed state exists')
assert.ok(capabilityText[1].indexOf('capabilitiesUnavailable.value') < capabilityText[1].indexOf('capabilities.value?.device_wide'), 'unavailable state wins over capability rendering')

const loadCapabilities = source.match(/async function loadCapabilities\(\) \{([\s\S]*?)\n\}/)
assert.ok(loadCapabilities, 'capability loader exists')
assert.ok(loadCapabilities[1].includes('capabilitiesUnavailable.value = false'), 'successful capability fetch clears unavailable state')
assert.ok(loadCapabilities[1].includes('capabilitiesUnavailable.value = true'), 'failed capability fetch sets unavailable state')

console.log('test_proxy_capabilities: unavailable and success contracts passed')
