import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const read = file => fs.readFileSync(file, 'utf8')
const transpile = source => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }
}).outputText
const run = (source, context, actions) => {
  vm.runInNewContext(`${transpile(source)}\nglobalThis.actions = ${actions}`, context)
  return context.actions
}

const widget = read('src/components/PhoneStreamWidget.vue')
const guardStart = widget.indexOf('function canCommitStream')
const guardEnd = widget.indexOf('function defaultMjpegUrl', guardStart)
assert.ok(guardStart >= 0 && guardEnd > guardStart)
const { canCommitStream } = run(widget.slice(guardStart, guardEnd), {}, '{ canCommitStream }')
assert.equal(canCommitStream(1, 'A', 1, 'A', true), true)
assert.equal(canCommitStream(1, 'A', 2, 'B', true), false, 'old device response cannot commit after switch')
assert.equal(canCommitStream(1, 'A', 1, 'A', false), false, 'response cannot commit after stop')

const overview = read('src/components/StreamOverview.vue')
const overviewStart = overview.indexOf('type StatusPayload =')
const overviewEnd = overview.indexOf('async function refresh', overviewStart)
assert.ok(overviewStart >= 0 && overviewEnd > overviewStart)
const { normalizeStatusPayload, summarizeStatus } = run(overview.slice(overviewStart, overviewEnd), {}, '{ normalizeStatusPayload, summarizeStatus }')
for (const malformed of [{ streams: {}, viewers: [] }, { streams: [], viewers: {} }, null, {}]) {
  assert.equal(normalizeStatusPayload(malformed), null, 'malformed status arrays are rejected')
  assert.equal(summarizeStatus(malformed), null, 'malformed status does not produce KPIs')
}
const summary = summarizeStatus({
  streams: [{ running: true, frames: 3, last_frame_age: 4 }],
  viewers: [{ events: { metrics: { at: Date.now() / 1000, detail: { queue: 2, kbps: 512 } } } }, { events: {} }],
})
assert.equal(JSON.stringify(summary), JSON.stringify({ encoders: 1, viewers: 2, errors: 0, idle: 1, maxQueue: 2, kbps: 512 }))

const admin = read('src/views/PhoneAdminView.vue')
const tileStart = admin.indexOf('function tileState')
const tileEnd = admin.indexOf('function matchesDevice', tileStart)
assert.ok(tileStart >= 0 && tileEnd > tileStart)
const tileCtx = {
  multiStreaming: { value: { A: true } },
  streamFrozen: { value: {} },
  blackWarning: { value: {} },
  rtcStatus: { value: { A: 'H.264 reconnecting...' } },
  multiStreamMode: { value: { A: 'rtc' } },
}
const { tileState } = run(admin.slice(tileStart, tileEnd), tileCtx, '{ tileState }')
assert.equal(tileState('A'), 'attention')
tileCtx.rtcStatus.value.A = 'H.264 connecting...'
assert.equal(tileState('A'), 'attention')
tileCtx.rtcStatus.value.A = 'Streaming'
assert.equal(tileState('A'), 'live')
tileCtx.rtcStatus.value.A = ''
tileCtx.multiStreamMode.value.A = 'mjpeg'
assert.equal(tileState('A'), 'live', 'healthy MJPEG remains live')

const overlayStart = admin.indexOf('const overlayOn = ref<Record<string, boolean>>({})')
const overlayEnd = admin.indexOf('/* ── multi-device', overlayStart)
assert.ok(overlayStart >= 0 && overlayEnd > overlayStart)
const overlayCtx = {
  ref: value => ({ value }),
  alive: true,
  isIosDevice: () => false,
  tileActionMessage: { value: {} },
  api: () => Promise.reject(new Error('API 500: backend detail must stay hidden')),
}
const overlayActions = run(admin.slice(overlayStart, overlayEnd), overlayCtx, '{ toggleOverlay, overlayOn, overlayBusy, tileActionMessage }')
await overlayActions.toggleOverlay('A')
assert.equal(overlayActions.overlayOn.value.A, undefined, 'failed overlay POST does not commit optimistic state')
assert.equal(overlayActions.tileActionMessage.value.A, 'UI grid update failed', 'overlay failure is visible and concise')
overlayCtx.api = async () => ({ ok: true })
await overlayActions.toggleOverlay('A')
assert.equal(overlayActions.overlayOn.value.A, true, 'only confirmed overlay response commits state')

const pendingOverlay = new Map()
const overlayCalls = []
overlayCtx.api = url => {
  const serial = decodeURIComponent(url.split('/').pop())
  overlayCalls.push(serial)
  return new Promise((resolve, reject) => pendingOverlay.set(serial, { resolve, reject }))
}
const rejectedA = overlayActions.toggleOverlay('A')
const ignoredA = overlayActions.toggleOverlay('A')
const successfulB = overlayActions.toggleOverlay('B')
assert.deepEqual(overlayCalls, ['A', 'B'], 'busy clicks are ignored per device while another phone remains independent')
assert.equal(overlayActions.overlayBusy.value.A, true)
assert.equal(overlayActions.overlayBusy.value.B, true)
pendingOverlay.get('A').reject(new Error('temporary failure'))
await rejectedA
await ignoredA
assert.equal(overlayActions.overlayOn.value.A, true, 'rejected concurrent toggle preserves the prior committed state')
assert.equal(overlayActions.tileActionMessage.value.A, 'UI grid update failed')
assert.equal(overlayActions.overlayBusy.value.A, undefined, 'rejected device releases only its own busy guard')
assert.equal(overlayActions.overlayBusy.value.B, true, 'other device remains busy until its own response')
pendingOverlay.get('B').resolve({ ok: true })
await successfulB
assert.equal(overlayActions.overlayOn.value.B, true, 'successful concurrent toggle commits only after confirmation')
assert.equal(overlayActions.overlayBusy.value.B, undefined, 'successful device releases its busy guard')

const app = read('src/App.vue')
const navStart = app.indexOf('function normalizePremiumTabs')
const navEnd = app.indexOf('const premiumTabs =', navStart)
assert.ok(navStart >= 0 && navEnd > navStart)
const { normalizePremiumTabs } = run(app.slice(navStart, navEnd), {}, '{ normalizePremiumTabs }')
assert.equal(JSON.stringify(normalizePremiumTabs(undefined, new Set())), '[]')
assert.equal(JSON.stringify(normalizePremiumTabs({ id: 'bad', label: 'object' }, new Set())), '[]')
assert.equal(JSON.stringify(normalizePremiumTabs([
  { id: 'valid.ext', label: ' Valid extension ' },
  { id: 'phone:multi', label: 'collision' },
  { id: 'bad/id', label: 'unsafe id' },
  { id: 'missing-label' },
  { id: 'valid.ext', label: 'duplicate' },
  { id: 'second', label: 'Second', component: 'Panel' },
], new Set(['phone:multi']))), JSON.stringify([
  { id: 'valid.ext', label: 'Valid extension' },
  { id: 'second', label: 'Second', component: 'Panel' },
]))

console.log('PRODUCTION_UI_SAFETY=PASS (stream attribution, strict metrics, RTC state, transactional overlays, validated extension navigation)')
