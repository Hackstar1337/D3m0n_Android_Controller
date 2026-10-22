#!/usr/bin/env node
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'

const componentPath = process.argv[2] || path.resolve(process.cwd(), 'src/views/DeviceWindowView.vue')
const source = fs.readFileSync(componentPath, 'utf8')

function transpileScript(sfc) {
  const match = sfc.match(/<script\s+setup(?:\s+lang="ts")?>([\s\S]*?)<\/script>/)
  assert.ok(match, `No <script setup> found in ${componentPath}`)
  let code = match[1]
    .replace(/^\s*import[^\n]*\n/gm, '')
    .replace(/const props = defineProps<[^>]+>\(\)/, "const props = { deviceSerial: 'offline-test-device', embedded: false, visible: true }")
  code += `\n;globalThis.__actions = {\n  send, sendText, pointerDown, pointerUp, cancelPointer, toggleViewOnly, scroll, textKeydown,
  setVisible: (value) => { props.visible = value },\n  setCanvas: (value) => { canvas.value = value },\n  setStage: (value) => { stage.value = value },\n  setHandle: (value) => { handle.value = value },\n  setPaused: (value) => { paused.value = value },\n  state: () => ({ inputBusy: inputBusy.value, busy: busy.value, queued: queuedActionCount.value, message: message.value, text: text.value, viewOnly: viewOnly.value }),\n  setText: (value) => { text.value = value },\n  setNow: (value) => { Date.now = () => value }\n};`
  return ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
}

function makeVm(apiImpl) {
  const ref = (value) => ({ value })
  const shallowRef = ref
  const computed = (getter) => ({ get value() { return getter() } })
  const context = {
    console,
    setTimeout,
    clearTimeout,
    Date,
    Math,
    Promise,
    performance,
    location: { protocol: 'http:', host: 'offline.test' },
    document: { title: '' },
    api: apiImpl,
    startH264Stream: () => ({ stop() {}, status: { value: 'streaming' } }),
    ref,
    shallowRef,
    computed,
    nextTick: async () => {},
    watch: () => {},
    onMounted: () => {},
    onUnmounted: () => {},
    defineProps: () => ({ deviceSerial: 'offline-test-device', embedded: false, visible: true }),
  }
  vm.createContext(context)
  vm.runInContext(transpileScript(source), context, { filename: componentPath })
  return context.__actions
}

const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }
function canvasMock() {
  return {
    width: 100, height: 80,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 80 }),
    focus() {}, setPointerCapture() {}, hasPointerCapture: () => false, releasePointerCapture() {},
  }
}
function pointer(pointerId, clientX, clientY, button = 0) {
  return { pointerId, clientX, clientY, button, preventDefault() {} }
}

async function testQueuedOrderingAndBusy() {
  const requests = []
  const actions = makeVm((pathName) => new Promise((resolve) => requests.push({ path: pathName, resolve })))
  actions.setCanvas(canvasMock())
  actions.setHandle({ status: { value: 'streaming' } })
  const first = actions.send('/api/phone/tap', { x: 1 })
  const second = actions.send('/api/phone/key', { key: 'HOME' })
  assert.deepEqual(requests.map((r) => r.path), ['/api/phone/tap'], 'only one request may be in flight')
  assert.equal(actions.state().queued, 2, 'queued count includes the in-flight request')
  assert.equal(actions.state().inputBusy, true)
  requests[0].resolve({ ok: true })
  await flush()
  assert.deepEqual(requests.map((r) => r.path), ['/api/phone/tap', '/api/phone/key'], 'queued actions preserve order')
  assert.equal(actions.state().queued, 1)
  requests[1].resolve({ ok: true })
  assert.equal(await first, true)
  assert.equal(await second, true)
  await flush()
  assert.equal(actions.state().queued, 0)
  assert.equal(actions.state().inputBusy, false, 'busy clears after the final request completes')
}

async function testOverflow() {
  const requests = []
  const actions = makeVm((pathName) => new Promise((resolve) => requests.push({ path: pathName, resolve })))
  const accepted = []
  for (let i = 0; i < 8; i += 1) accepted.push(actions.send('/api/phone/tap', { x: i }))
  const rejected = await actions.send('/api/phone/tap', { x: 99 })
  assert.equal(rejected, false, 'the ninth action is rejected')
  assert.match(actions.state().message, /queue full/i)
  assert.equal(actions.state().queued, 8)
  for (let i = 0; i < 8; i += 1) {
    assert.ok(requests[i], 'the next queued request must start after the prior completion')
    requests[i].resolve({ ok: true })
    await flush()
  }
  await Promise.all(accepted)
}

async function testLongPressSwipe() {
  const requests = []
  const actions = makeVm((pathName, options) => new Promise((resolve) => requests.push({ path: pathName, options, resolve })))
  actions.setCanvas(canvasMock())
  actions.setHandle({ status: { value: 'streaming' } })
  actions.setNow(1000)
  actions.pointerDown(pointer(7, 40, 30))
  actions.setNow(1700)
  actions.pointerUp(pointer(7, 40, 30))
  assert.equal(requests.length, 1)
  assert.equal(requests[0].path, '/api/phone/swipe', 'stationary press >=500ms routes to swipe')
  const body = JSON.parse(requests[0].options.body)
  assert.equal(body.x1, body.x2)
  assert.equal(body.y1, body.y2)
  assert.equal(body.duration, 700)
  requests[0].resolve({ ok: true })
  await flush()
}

async function testCanceledPointerNeverSends() {
  const requests = []
  const actions = makeVm((pathName) => new Promise((resolve) => requests.push({ path: pathName, resolve })))
  actions.setCanvas(canvasMock())
  actions.setHandle({ status: { value: 'streaming' } })
  actions.setNow(2000)
  actions.pointerDown(pointer(11, 20, 20))
  actions.cancelPointer(pointer(11, 20, 20))
  actions.pointerUp(pointer(11, 20, 20))
  await flush()
  assert.equal(requests.length, 0, 'canceled pointer sequences do not send input')
}

async function testViewOnlyRejection() {
  let calls = 0
  const actions = makeVm(async () => { calls += 1; return { ok: true } })
  actions.toggleViewOnly()
  const result = await actions.send('/api/phone/tap', { x: 1 })
  assert.equal(result, false)
  assert.equal(calls, 0, 'view-only mode rejects without an API request')
  assert.equal(actions.state().viewOnly, true)
}

await testQueuedOrderingAndBusy()
await testOverflow()
await testLongPressSwipe()
await testCanceledPointerNeverSends()
await testViewOnlyRejection()
console.log('PASS device window input queue/pointer tests')

async function testScrollDirection() {
 const calls=[]; const actions=makeVm(async (path, options) => {calls.push({path,body:JSON.parse(options.body)});return {ok:true}})
 actions.setCanvas(canvasMock());actions.setHandle({status:{value:'streaming'}})
 actions.scroll('down');await flush()
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/phone/swipe');assert(calls[0].body.y1>calls[0].body.y2,'Scroll down is a bottom-to-top finger swipe')
 actions.scroll('up');await flush();assert(calls[1].body.y1<calls[1].body.y2)
}
async function testMultilineAndHiddenInput() {
 const calls=[];const actions=makeVm(async (path,options)=>{calls.push({path,body:JSON.parse(options.body)});return {ok:true}})
 actions.setText('Hindi: नमस्ते\nSecond line');let prevented=false
 actions.textKeydown({key:'Enter',ctrlKey:false,metaKey:false,preventDefault(){prevented=true}});await flush();assert.equal(calls.length,0);assert.equal(prevented,false,'Plain Enter stays in the multiline text field')
 actions.textKeydown({key:'Enter',ctrlKey:true,metaKey:false,preventDefault(){prevented=true}});await flush();assert.equal(calls.length,1);assert.equal(calls[0].body.text,'Hindi: नमस्ते\nSecond line');assert.equal(prevented,true)
 actions.setVisible(false);const result=await actions.send('/api/phone/key',{key:'HOME'});assert.equal(result,false);assert.equal(calls.length,1,'Hidden viewers must not send new commands')
}
await testScrollDirection();await testMultilineAndHiddenInput()
console.log('PASS actual controller scroll direction, multiline shortcut and hidden-view input tests')
