import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

/* 1. Functional: the H.264 player requests a quality profile and sends visibility hints. */
const source = fs.readFileSync('src/composables/useH264Stream.ts', 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
let clock = 0, id = 0
const timers = new Map(), sockets = []
const later = (fn, ms, interval = false) => { const key = ++id; timers.set(key, { fn, at: clock + ms, ms, interval }); return key }
const clear = key => { timers.delete(key) }
class Socket {
  static OPEN = 1
  constructor(url) { this.url = String(url); this.sent = []; this.readyState = 1; sockets.push(this) }
  send(data) { this.sent.push(JSON.parse(data)) }
  close() { this.closed = true; this.readyState = 3; if (this.onclose) this.onclose() }
}
const module = { exports: {} }
const timerApi = { setTimeout: (f, m) => later(f, m), setInterval: (f, m) => later(f, m, true), clearTimeout: clear, clearInterval: clear }
const context = {
  exports: module.exports, module, console,
  require: name => { assert.equal(name, 'vue'); return { ref: value => ({ value }) } },
  window: { VideoDecoder: function () {}, ...timerApi }, ...timerApi,
  WebSocket: Socket, URL, URLSearchParams, TextDecoder, TextEncoder,
  crypto: { randomUUID: () => 'grid-viewer' }, location: { search: '' },
  document: { hidden: false, addEventListener() {}, removeEventListener() {} },
  performance: { now: () => clock },
}
vm.runInNewContext(js, context)
const canvas = { getContext: () => null }
let profile = 'grid', shown = true
const tile = module.exports.startH264Stream('wss://dashboard.example.test/api/phone/android-h264/192.168.50.37%3A5555', canvas, { profile: () => profile, visible: () => shown, kind: 'dashboard' })
const ws = sockets.at(-1)
const url = new URL(ws.url)
assert.equal(url.searchParams.get('profile'), 'grid', 'grid tiles must request the grid profile on connect')
assert.equal(url.searchParams.get('viewer'), 'grid-viewer')
ws.onopen()
assert.equal(ws.sent.length, 0, 'no redundant hint right after connecting with the requested profile')
shown = false; tile.update()
assert.deepEqual(ws.sent.at(-1), { mode: 'key' }, 'off-screen tiles switch to keyframe-only refresh')
profile = 'hd'; tile.update()
assert.deepEqual(ws.sent.at(-1), { profile: 'hd' }, 'quality changes are sent immediately')
shown = true; tile.update()
assert.deepEqual(ws.sent.at(-1), { mode: 'full' }, 'tiles back on screen resume full frame rate')
const count = ws.sent.length; tile.update()
assert.equal(ws.sent.length, count, 'unchanged hints are not re-sent')
assert.equal(typeof tile.update, 'function')
tile.stop()
const hd = module.exports.startH264Stream('wss://dashboard.example.test/api/phone/android-h264/192.168.50.38%3A5555', canvas, {})
assert.equal(new URL(sockets.at(-1).url).searchParams.has('profile'), false, 'device windows keep the default HD stream (no profile param)')
hd.stop()
assert.ok(source.includes('decoder.reset(); decoder.configure('), 'a new SPS reconfigures the decoder (profile switch)')

/* 2. Source contracts: Live grid scale, Sync All safety, control desk mirroring. */
const fleet = fs.readFileSync('src/views/PhoneAdminView.vue', 'utf8')
const windowSource = fs.readFileSync('src/views/DeviceWindowView.vue', 'utf8')
const checks = [
  [fleet.includes("{ value: '200', label: 'Wall', density: 'wall'"), 'Wall layout for ~50 phones'],
  [fleet.includes('repeat(auto-fill, minmax(min(100%, ${gridDensityInfo.value.minColumn}px), 1fr))'), 'auto columns follow the layout preset'],
  [fleet.includes("&& tileOnScreen(serial), kind: scope === 'multi' ? 'dashboard' : 'single', profile: () => scope === 'single' ? 'hd' : tileProfile() })"), 'tiles send visibility and quality hints'],
  [fleet.includes("subTab.value === 'multi' && (!controlDevice.value || focusedGridDevice.value === serial)"), 'original visibility contract kept'],
  [fleet.includes('new IntersectionObserver(') && fleet.includes(':ref="tileRef(d.serial)"') && fleet.includes('bindTile(serial, element)'), 'off-screen tiles are detected'],
  [fleet.includes('const syncEnabled = ref(false)'), 'Sync All always starts off'],
  [!/localStorage\.setItem\('d3m0n\.dashboard-interaction\.v1'[^\n]*syncEnabled/.test(fleet), 'Sync All on/off is never persisted'],
  [fleet.includes('@click="requestSyncAll"') && fleet.includes('@click="confirmSyncAll"') && fleet.includes('role="alertdialog" aria-label="Confirm Sync All"'), 'Sync All needs an explicit confirmation'],
  [fleet.includes("body: JSON.stringify({ device: serial, ...withSync(serial, body) })"), 'direct tile input carries the sync list'],
  [(fleet.match(/\.\.\.withSync\(serial, \{\}\)/g) || []).length === 2, 'tile hardware keys carry the sync list'],
  [fleet.includes('.slice(0, MAX_SYNC_DEVICES)') && fleet.includes('const MAX_SYNC_DEVICES = 100'), 'sync list bounded to the server limit'],
  [fleet.includes(':sync-devices="syncEnabled ? syncPool : []"'), 'control desk input is mirrored while Sync All is on'],
  [fleet.includes("'input is never broadcast'"), 'help text stays truthful when Sync All is off'],
  [fleet.includes('send: (path, body) => sendTileInput(serial, path, body)'), 'tile actions keep serial identity'],
  [fleet.includes('aria-label="Sync All scope"') && fleet.includes('aria-label="Grid stream quality"') && fleet.includes('aria-label="Filter devices by stream state"'), 'grid controls are labelled'],
  [windowSource.includes('syncDevices?: string[]') && windowSource.includes('SYNC_PATHS.has(item.path)'), 'device window accepts sync targets'],
  [windowSource.includes('body: JSON.stringify({ device: props.deviceSerial, ...item.body })'), 'device window request line unchanged'],
]
for (const [ok, label] of checks) assert.ok(ok, label)
console.log(`LIVE_GRID_SCALE_AND_SYNC_TESTS=${checks.length + 8} PASS`)
