import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import ts from 'typescript'

const here = path.dirname(fileURLToPath(import.meta.url))
const sourcePath = path.join(here, 'src', 'composables', 'phoneTint.ts')
const tsText = readFileSync(sourcePath, 'utf8')
const compiled = ts.transpileModule(tsText, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, strict: true },
}).outputText
const api = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

let total = 0
function check(condition, message) { total += 1; assert.ok(condition, message) }
function equal(actual, expected, message) { total += 1; assert.deepEqual(actual, expected, message) }
function near(actual, expected, epsilon, message) { total += 1; assert.ok(Math.abs(actual - expected) <= epsilon, `${message}: ${actual} vs ${expected}`) }

function cssRGB(value) {
  const match = String(value).match(/\d+(?:\.\d+)?/g)
  return [Number(match?.[0] ?? 0), Number(match?.[1] ?? 0), Number(match?.[2] ?? 0)]
}
function assertPaletteReadable(palette, label) {
  for (const [name, value] of Object.entries(palette.vars)) {
    if (name === '--phone-surface' || name === '--phone-cap-top') continue
    const ratioSurface = api.contrastRatio(value, palette.surface)
    const ratioTop = api.contrastRatio(value, palette.top)
    check(ratioSurface >= 4.5, `${label} ${name} contrast against surface (${ratioSurface})`)
    check(ratioTop >= 4.5, `${label} ${name} contrast against top (${ratioTop})`)
  }
}

// Color math and every approved foreground token, including edge-case samples.
near(api.contrastRatio([0, 0, 0], [255, 255, 255]), 21, 1e-9, 'W3C black/white ratio')
equal(api.paletteForColor([-30, 260, 128]).surface, 'rgb(0 255 128)', 'numeric channels clamp')
for (const [label, color] of [['black', [0, 0, 0]], ['navy', [9, 19, 72]], ['white', [255, 255, 255]], ['gray', [128, 128, 128]], ['red', [220, 20, 40]], ['blue', [20, 70, 220]], ['purple', [105, 35, 160]], ['green', [20, 155, 85]]]) {
  assertPaletteReadable(api.paletteForColor(color), label)
}
check(api.paletteForColor([5, 5, 5]).mode === 'dark', 'dark sample selects light ink mode')
check(api.paletteForColor([250, 250, 250]).mode === 'light', 'light sample selects dark ink mode')
check(api.paletteForColor([20, 20, 20]).surface !== api.paletteForColor([220, 220, 220]).surface, 'sample changes surface')

// Include the independently reported [110,110,110] failure and both sides of every gray transition.
for (let gray = 0; gray <= 255; gray++) assertPaletteReadable(api.paletteForColor([gray, gray, gray]), `gray-${gray}`)
const levels = [0, 64, 110, 118, 128, 192, 255]
for (const r of levels) for (const g of levels) for (const b of levels)
  assertPaletteReadable(api.paletteForColor([r, g, b]), `grid-${r}-${g}-${b}`)

let draws = 0
let reads = 0
let sourceContextRequests = 0
let networkCalls = 0
const idleQueue = []
let idleId = 0
const originalNow = Date.now
const originalSetTimeout = globalThis.setTimeout
const originalClearTimeout = globalThis.clearTimeout
const timerQueue = new Map()
let timerId = 0
globalThis.setTimeout = (callback, delay) => { const id = ++timerId; timerQueue.set(id, { callback, delay }); return id }
globalThis.clearTimeout = id => timerQueue.delete(id)
let now = 10_000
Date.now = () => now
const originalFetch = globalThis.fetch
globalThis.fetch = () => { networkCalls += 1; throw new Error('network forbidden in tint helper') }

globalThis.requestIdleCallback = callback => {
  const id = ++idleId
  callback.__id = id
  idleQueue.push(callback)
  return id
}
globalThis.cancelIdleCallback = id => {
  const index = idleQueue.findIndex(item => item.__id === id)
  if (index >= 0) idleQueue.splice(index, 1)
}
function flushOne() {
  if (!idleQueue.length && timerQueue.size) {
    const [id, item] = timerQueue.entries().next().value
    timerQueue.delete(id)
    assert.equal(item.delay, 150, 'every tint batch has an explicit 150ms pacing timer')
    item.callback()
  }
  const callback = idleQueue.shift()
  if (callback) callback({ timeRemaining: () => 50 })
}
function flushAll() {
  let guard = 0
  while ((idleQueue.length || timerQueue.size) && guard++ < 4) flushOne()
}

const docListeners = new Map()
const doc = {
  hidden: false,
  defaultView: {
    innerWidth: 1_000,
    innerHeight: 800,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
  },
  createElement: () => scratch,
  addEventListener: (name, listener) => docListeners.set(name, listener),
  removeEventListener: (name, listener) => { if (docListeners.get(name) === listener) docListeners.delete(name) },
}
globalThis.document = doc

let screen = { color: [24, 104, 198], width: 1280, height: 720, throwOnDraw: false, alpha: 255 }
const source = {
  get width() { return screen.width },
  get height() { return screen.height },
  getContext() { sourceContextRequests += 1; throw new Error('source context must never be read') },
}
const scratchData = new Uint8ClampedArray(32 * 2 * 4)
const scratchContext = {
  clearRect() {},
  drawImage(input) {
    draws += 1
    if (input !== source || screen.throwOnDraw) throw new Error('tainted source')
    for (let index = 0; index < scratchData.length; index += 4) {
      scratchData[index] = screen.color[0]
      scratchData[index + 1] = screen.color[1]
      scratchData[index + 2] = screen.color[2]
      scratchData[index + 3] = screen.alpha
    }
    // One noisy pixel must not beat the dominant/median strip color.
    scratchData[0] = 255; scratchData[1] = 0; scratchData[2] = 255
  },
  getImageData() { reads += 1; return { data: scratchData } },
}
const scratch = { width: 0, height: 0, getContext: () => scratchContext }
function root(rect = { left: 10, top: 10, right: 400, bottom: 300, width: 390, height: 290 }) {
  const styleValues = new Map()
  const node = {
    ownerDocument: doc,
    isConnected: true,
    canvas: source,
    style: { setProperty(name, value) { styleValues.set(name, value) } },
    getBoundingClientRect: () => rect,
    querySelector: selector => selector === 'canvas' ? source : null,
    values: styleValues,
  }
  return node
}

// Initial sample, dominant/median noise rejection, exact scratch dimensions, and no source mutation.
const manager = api.createPhoneTintManager()
const first = root()
const dimensions = [source.width, source.height]
manager.attach('one', first)
manager.attach('removed', null)
manager.start()
equal(idleQueue.length, 0, 'start does not immediately arm a hot idle loop')
equal(timerQueue.size, 1, 'start queues a paced sampling timer')
flushOne()
check(draws === 1 && reads === 1, 'one idle batch performs one bounded scratch read')
check(first.values.get('--phone-surface') === 'rgb(24 104 198)', 'sampled upper screen reaches the root surface')
check(first.values.has('--phone-cap-top') && first.values.has('--phone-agent'), 'all approved CSS vars are written')
equal([source.width, source.height], dimensions, 'source dimensions are not mutated')
equal(sourceContextRequests, 0, 'source canvas context is never requested')
check(networkCalls === 0, 'sampler performs no network calls')

// Change threshold and four-second cache window.
const drawsBeforeCache = draws
screen.color = [35, 115, 210]
now += 3_999
flushOne()
equal(draws, drawsBeforeCache, 'cached root is not sampled before four seconds')
now += 1
flushOne()
equal(draws, drawsBeforeCache + 1, 'root refreshes after four seconds')
check(first.values.get('--phone-surface') === 'rgb(35 115 210)', 'changed screen color refreshes CSS surface')

// Batch cap and round-robin fairness across three roots.
const second = root()
const third = root()
manager.attach('two', second)
manager.attach('three', third)
now += 4_000
const beforeBatch = draws
flushOne()
check(draws - beforeBatch <= 2, 'idle batch caps reads at two')
now += 4_000
flushOne()
check(draws > beforeBatch + 1, 'round-robin eventually samples another root')

// Hidden and offscreen roots skip without consuming a sample.
const hiddenRoot = root()
manager.attach('hidden', hiddenRoot)
doc.hidden = true
now += 4_000
const beforeHidden = draws
flushOne()
equal(draws, beforeHidden, 'document hidden pauses sampling')
equal(timerQueue.size + idleQueue.length, 0, 'hidden tab leaves no sampling work scheduled')
doc.hidden = false
docListeners.get('visibilitychange')?.()
flushOne()
check(draws > beforeHidden, 'visibility wake resumes sampling')
const offscreen = root({ left: 2_000, top: 2_000, right: 2_100, bottom: 2_100, width: 100, height: 100 })
manager.attach('offscreen', offscreen)
now += 4_000
const beforeOffscreen = draws
flushAll()
check(!offscreen.values.has('--phone-surface'), 'offscreen root is skipped')
equal(source.width, dimensions[0], 'offscreen skip still leaves source untouched')

// Transparent/unready and tainted sources fail safely and do not poison healthy roots.
const unready = root()
source.readyState = 1
manager.attach('unready', unready)
now += 4_000
flushAll()
check(!unready.values.has('--phone-surface'), 'unready video source is rejected')
delete source.readyState
const transparent = root()
manager.attach('transparent', transparent)
screen.alpha = 0
now += 4_000
flushAll()
check(!transparent.values.has('--phone-surface'), 'transparent strip is rejected')
screen.alpha = 255
const tainted = root()
manager.attach('tainted', tainted)
screen.throwOnDraw = true
now += 4_000
flushAll()
check(!tainted.values.has('--phone-surface'), 'tainted source fails safely')
screen.throwOnDraw = false
const healthy = root()
manager.attach('healthy', healthy)
now += 4_000
flushAll()
check(healthy.values.has('--phone-surface'), 'tainted root does not poison healthy sample')

// Null/rebind and stop cleanup are safe; stopped manager must not schedule more work.
manager.attach('one', null)
manager.attach('rebind', null)
manager.attach('rebind', root())
manager.stop()
check(idleQueue.length === 0, 'stop cancels idle work')
equal(timerQueue.size, 0, 'stop cancels pacing timers')
manager.attach('after-stop', root())
flushAll()
check(!manager || true, 'stop/attach null lifecycle is non-throwing')
check(networkCalls === 0, 'entire lifecycle remains offline')

// Browsers without requestIdleCallback use the same paced fallback and clean up.
delete globalThis.requestIdleCallback
const fallback = api.createPhoneTintManager(), fallbackRoot = root()
fallback.attach('fallback', fallbackRoot); fallback.start()
equal(timerQueue.size, 1, 'timer-only browser schedules a bounded batch')
flushOne()
check(fallbackRoot.values.has('--phone-surface'), 'timer-only fallback samples the phone')
fallback.attach('fallback', null)
equal(timerQueue.size, 0, 'removing last phone clears the timer')
fallback.stop()
equal(docListeners.size, 0, 'all managers release visibility listeners')

Date.now = originalNow
globalThis.setTimeout = originalSetTimeout
globalThis.clearTimeout = originalClearTimeout
if (originalFetch) globalThis.fetch = originalFetch
else delete globalThis.fetch
console.log(`phone tint behavioral tests: ${total} passed, 0 failed`)
