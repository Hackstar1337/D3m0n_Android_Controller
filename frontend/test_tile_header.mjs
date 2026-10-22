import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const fleet = fs.readFileSync('src/views/PhoneAdminView.vue', 'utf8')
const transpile = source => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }
}).outputText
const extract = (start, end) => {
  const a = fleet.indexOf(start)
  assert.notEqual(a, -1, `missing helper anchor: ${start}`)
  const b = fleet.indexOf(end, a)
  assert.notEqual(b, -1, `missing helper end: ${end}`)
  return fleet.slice(a, b)
}

// Execute the actual helpers with the same ref-shaped values used by the component.
const syncSnippet = extract("type TileSyncState =", 'const syncInstantTitle = computed')
const ctx = {
  syncEnabled: { value: false },
  syncPool: { value: [] },
  syncStatus: { value: {} },
  syncStatusLoaded: { value: false },
}
vm.runInNewContext(transpile(`${syncSnippet}\nglobalThis.actions = { tileSyncState, tileSyncTitle }`), ctx)
const { tileSyncState, tileSyncTitle } = ctx.actions
const serial = '192.168.50.37:5555'
const other = '192.168.50.38:5555'

assert.equal(tileSyncState(serial), 'inactive', 'disabled Sync has no tile state')
ctx.syncEnabled.value = true
assert.equal(tileSyncState(other), 'inactive', 'non-member has no tile state')
ctx.syncPool.value = [serial]
assert.equal(tileSyncState(serial), 'pending', 'member remains pending until status loads')
ctx.syncStatusLoaded.value = true
ctx.syncStatus.value = { [serial]: { instant: true, path: 'stream', lastOk: null, lastAge: Infinity, lastError: '' } }
assert.equal(tileSyncState(serial), 'ready', 'stream-backed member is ready')
ctx.syncStatus.value = { [serial]: { instant: false, path: 'adb', lastOk: null, lastAge: Infinity, lastError: '' } }
assert.equal(tileSyncState(serial), 'fallback', 'ADB member is visibly a fallback')
assert.match(tileSyncTitle(serial), /ADB fallback/)
ctx.syncStatus.value = { [serial]: { instant: true, path: 'stream', lastOk: false, lastAge: 12, lastError: 'timeout' } }
assert.equal(tileSyncState(serial), 'failed', 'recent mirrored-input failure is alert state')
assert.match(tileSyncTitle(serial), /last mirrored input failed: timeout/)
ctx.syncStatus.value = { [serial]: { instant: true, path: 'stream', lastOk: false, lastAge: 120, lastError: 'old timeout' } }
assert.equal(tileSyncState(serial), 'stale-fail', 'stale failure is not presented as current failure')
assert.match(tileSyncTitle(serial), /previous input failure is stale/)

// Header structure and preserved interaction contracts.
for (const needle of [
  'class="multi-card-header glass-hub"',
  'class="tile-select hub-chip"',
  'class="hub-signals"',
  'syncEnabled && syncPool.includes(d.serial)',
  `'is-' + hubSignal(d.serial, slot.key)`,
  'class="tile-number"',
  'class="tile-ip__addr"',
  'type="checkbox"',
  '@click="openDirectControl(d.serial)"',
  "startMultiStream(d.serial, isIosDevice(d.serial) ? 'mjpeg' : 'rtc')",
  '@click="stopMultiStream(d.serial);',
]) assert.ok(fleet.includes(needle), `header hook preserved: ${needle}`)
assert.ok(!fleet.includes('class="tile-ip__tag"'), 'outlined Proxy badge removed')
assert.ok(!fleet.includes('class="tile-sync-fail"'), 'duplicate failure badge removed')
assert.ok(!fleet.includes('multi-card > .multi-card-header::after'), 'old forced wrap hack removed')
assert.ok(fleet.includes('class="tile-select hub-chip"'), 'number chip retains the labeled selection checkbox')
assert.ok(fleet.includes('<details class="tile-quick"'), 'one disclosure replaces crowded permanent action buttons')
assert.ok(fleet.includes("'Phone actions ' + d.serial"), 'actions summary has a per-device accessible label')
assert.ok(fleet.includes('.tile-select:focus-within'), 'numbered selection chip has keyboard focus feedback')
assert.match(fleet, /\.multi-card-header\.glass-hub \{[^\n]*border-radius: 0; border: 0; background-color: var\(--phone-surface, #07090c\);[^\n]*box-shadow: none/)
assert.match(fleet, /\.tile-quick__btn:focus-visible[^\n]*outline/)
assert.match(fleet, /prefers-reduced-motion: reduce/)
console.log('TILE_HEADER_TESTS=PASS (truthful Sync states, member-only icon, Glass Hub identity chip, controls and accessibility contracts)')
