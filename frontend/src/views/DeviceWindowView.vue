<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { api } from '@/composables/useApi'
import { createPhoneInteractions } from '@/composables/phoneInteractions'
import type { LivePort } from '@/composables/phoneInteractions'
import { liveInputChannel, type LiveAck } from '@/composables/liveInput'
import { startH264Stream, type H264Metrics } from '@/composables/useH264Stream'

const props = defineProps<{ deviceSerial: string; embedded?: boolean; visible?: boolean; syncDevices?: string[] }>()
const canvas = ref<HTMLCanvasElement | null>(null)
const handle = shallowRef<H264Metrics | null>(null)
const label = ref(props.deviceSerial)
const text = ref('')
const message = ref('')
const paused = ref(false)
const moreControls = ref(false)
const viewOnly = ref(false), editingName = ref(false), nicknameDraft = ref('')
const idle = computed(() => handle.value?.idle.value || false)
const frameAge = computed(() => handle.value?.frameAgeSeconds.value || 0)
const inputBusy = ref(false)
const busy = ref(false)
const stage = ref<HTMLElement | null>(null)
const pressDot = ref<{ x: number; y: number; fading: boolean } | null>(null)
const queuedActionCount = ref(0)
const lastInputRttMs = ref<number | null>(null)
let alive = true
let drag: any = null
let activePointerId: number | null = null
let pressDotTimer: any = null
let inputInFlight = false
const actionQueue: any[] = []
const MAX_INPUT_QUEUE = 8
let interactions: ReturnType<typeof createPhoneInteractions> | null = null
const status = computed(() => paused.value ? 'Paused in this window' : handle.value?.status.value || 'Connecting')
const stats = computed(() => ({ fps: handle.value?.renderFps.value || 0, queue: handle.value?.queue.value || 0, latency: handle.value?.latencyMs.value || 0, kbps: handle.value?.kbps.value || 0, dropped: handle.value?.dropped.value || 0 }))
const keys = [
  { name: 'Back', code: 'BACK' }, { name: 'Home', code: 'HOME' }, { name: 'Recent apps', code: 'APP_SWITCH' },
  { name: 'Wake', code: 'WAKEUP' }, { name: 'Volume up', code: 'VOLUME_UP' }, { name: 'Volume down', code: 'VOLUME_DOWN' },
  { name: 'Enter', code: 'ENTER' }, { name: 'Backspace', code: 'DEL' },
]
function connect() {
  handle.value?.stop()
  if (!alive || !canvas.value) return
  paused.value = false
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  handle.value = startH264Stream(`${proto}://${location.host}/api/phone/android-h264/${encodeURIComponent(props.deviceSerial)}`, canvas.value, { visible: () => props.visible !== false, kind: props.embedded ? 'workspace' : 'device-window' })
}
function clearQueuedActions() {
  while (actionQueue.length) actionQueue.shift().resolve(false)
  queuedActionCount.value = inputInFlight ? 1 : 0
}
function cancelInteractions() {
  interactions?.cancel()
}
function togglePause() {
  if (paused.value) connect()
  else {
    cancelInteractions()
    clearQueuedActions()
    cancelPointer()
    handle.value?.stop(); handle.value = null; paused.value = true
  }
}
function toggleViewOnly() {
  viewOnly.value = !viewOnly.value
  cancelInteractions()
  if (viewOnly.value) {
    clearQueuedActions()
    cancelPointer()
    message.value = 'View-only mode: input disabled'
  } else if (!inputInFlight) message.value = ''
}
async function pumpInputQueue() {
  if (inputInFlight || !alive || paused.value || viewOnly.value || props.visible === false) return
  const item = actionQueue.shift()
  if (!item) { inputBusy.value = false; return }
  inputInFlight = true
  inputBusy.value = true
  const startedAt = Date.now()
  let success = false
  // SYNC ALL: the dashboard passes the phones to mirror this input to.
  const followers = syncFollowers()
  if (followers.length && SYNC_PATHS.has(item.path)) item.body = { ...item.body, sync: { devices: followers } }
  try {
    const result = await api(item.path, { method: 'POST', body: JSON.stringify({ device: props.deviceSerial, ...item.body }) })
    if (result.ok === false) throw new Error(result.error || 'Device action failed')
    success = true
    if (alive) message.value = 'Sent to ' + label.value + syncSummary(result)
  } catch (error) {
    if (alive) message.value = error instanceof Error ? error.message : 'Device action failed'
  } finally {
    lastInputRttMs.value = Math.max(0, Date.now() - startedAt)
    queuedActionCount.value = Math.max(0, queuedActionCount.value - 1)
    inputInFlight = false
    inputBusy.value = false
    item.resolve(success)
    if (actionQueue.length && alive && !paused.value && !viewOnly.value) void pumpInputQueue()
  }
}
const SYNC_PATHS = new Set(['/api/phone/tap', '/api/phone/swipe', '/api/phone/key', '/api/phone/input'])
function syncFollowers(): string[] {
  const list = Array.isArray(props.syncDevices) ? props.syncDevices : []
  if (!list.includes(props.deviceSerial)) return []
  return list.filter(serial => typeof serial === 'string' && serial && serial !== props.deviceSerial)
}
function syncSummary(result: any): string {
  const sync = result && typeof result === 'object' ? result.sync : null
  if (!sync || typeof sync !== 'object') return ''
  const failed = Array.isArray(sync.failed) ? sync.failed.length : 0
  const pending = Number(sync.pending) || 0
  return ` · mirrored to ${Number(sync.ok) || 0}/${Number(sync.targets) || 0}` + (pending ? ` (${pending} pending)` : '') + (failed ? ` · ${failed} failed` : '')
}
function send(path: string, body: Record<string, unknown>) {
  if (!alive || viewOnly.value || paused.value || props.visible === false) {
    message.value = viewOnly.value ? 'View-only mode: input rejected' : 'Paused: input rejected'
    return Promise.resolve(false)
  }
  if (queuedActionCount.value >= MAX_INPUT_QUEUE) {
    message.value = 'Input queue full (8 actions); try again.'
    return Promise.resolve(false)
  }
  message.value = ''
  const accepted = new Promise((resolve) => actionQueue.push({ path, body, resolve }))
  queuedActionCount.value += 1
  void pumpInputQueue()
  return accepted
}
async function saveNickname() {
  try {
    const result = await api('/api/phone/nickname', { method: 'POST', body: JSON.stringify({ serial: props.deviceSerial, nickname: nicknameDraft.value.trim() }) })
    if (result.ok === false) throw new Error(result.error || 'Could not save label')
    label.value = nicknameDraft.value.trim() || props.deviceSerial; editingName.value = false
    message.value = 'Device label saved'
    if (!props.embedded) document.title = `${label.value} — D3m0n in the droid`
  } catch (error) { message.value = error instanceof Error ? error.message : 'Could not save label' }
}
function point(event: PointerEvent) {
  const el = canvas.value
  if (!el || !el.width || !el.height) return null
  const rect = el.getBoundingClientRect()
  const scale = Math.min(rect.width / el.width, rect.height / el.height)
  if (!scale) return null
  const rawX = (event.clientX - rect.left - (rect.width - el.width * scale) / 2) / scale
  const rawY = (event.clientY - rect.top - (rect.height - el.height * scale) / 2) / scale
  if (rawX < 0 || rawY < 0 || rawX > el.width || rawY > el.height) return null
  return { x: Math.max(0, Math.min(el.width - 1, Math.round(rawX))), y: Math.max(0, Math.min(el.height - 1, Math.round(rawY))), width: el.width, height: el.height }
}
function showPressDot(event: PointerEvent) {
  if (pressDotTimer) clearTimeout(pressDotTimer)
  const target = stage.value || canvas.value
  const rect = target?.getBoundingClientRect()
  if (rect) pressDot.value = { x: event.clientX - rect.left, y: event.clientY - rect.top, fading: false }
}
function fadePressDot() {
  if (!pressDot.value) return
  pressDot.value = { ...pressDot.value, fading: true }
  pressDotTimer = setTimeout(() => { pressDot.value = null; pressDotTimer = null }, 250)
}
function releasePointer(event: PointerEvent) {
  const el = canvas.value
  try {
    if (el && typeof el.hasPointerCapture === 'function' && el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId)
    else if (el && typeof el.releasePointerCapture === 'function') el.releasePointerCapture(event.pointerId)
  } catch {}
}
function cancelPointer(event?: PointerEvent) {
  interactions?.pointerCancel(event)
  if (event && activePointerId !== null && event.pointerId !== activePointerId) return
  if (event) releasePointer(event)
  drag = null
  activePointerId = null
  fadePressDot()
}
function focusScreen() {
  try { canvas.value?.focus({ preventScroll: true }) } catch { canvas.value?.focus() }
}
function pointerDown(event: PointerEvent) {
  focusScreen()
  if (paused.value || viewOnly.value || props.visible === false || status.value !== 'streaming' || event.button !== 0 || activePointerId !== null) return
  const p = point(event)
  if (!p) return
  if (interactions) { interactions.pointerDown(event); showPressDot(event); return }
  event.preventDefault()
  activePointerId = event.pointerId
  canvas.value?.setPointerCapture(event.pointerId)
  showPressDot(event)
  drag = { ...p, time: Date.now(), pointerId: event.pointerId }
}
function pointerUp(event: PointerEvent) {
  if (interactions) { interactions.pointerUp(event); fadePressDot(); return }
  if (activePointerId === null || event.pointerId !== activePointerId) return
  const start = drag
  releasePointer(event)
  drag = null
  activePointerId = null
  fadePressDot()
  if (!start) return
  const end = point(event)
  if (!end) return
  event.preventDefault()
  const common = { stream_w: end.width, stream_h: end.height }
  const elapsed = Date.now() - start.time
  if (Math.hypot(end.x - start.x, end.y - start.y) < 15 && elapsed < 500) {
    void send('/api/phone/tap', { ...common, x: end.x, y: end.y })
  } else {
    void send('/api/phone/swipe', { ...common, x1: start.x, y1: start.y, x2: end.x, y2: end.y, duration: Math.max(100, Math.min(1500, elapsed)) })
  }
}
function scroll(direction: 'up' | 'down') {
  const el = canvas.value
  if (!el || !el.width || !el.height) return
  const x = Math.round(el.width / 2)
  const top = Math.round(el.height * 0.25)
  const bottom = Math.round(el.height * 0.75)
  void send('/api/phone/swipe', {
    x1: x, y1: direction === 'down' ? bottom : top,
    x2: x, y2: direction === 'down' ? top : bottom,
    duration: 300, stream_w: el.width, stream_h: el.height,
  })
}
async function sendText() {
  if (!text.value.trim() || busy.value) return false
  const value = text.value
  busy.value = true
  try {
    const succeeded = await send('/api/phone/input', { action: 'text', text: value })
    if (succeeded && text.value === value) text.value = ''
    return succeeded
  } finally { busy.value = false }
}
function textKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.defaultPrevented) return
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault()
    void sendText()
  }
}
function onWheel(event: WheelEvent) { interactions?.wheel(event) }
function pointerMove(event: PointerEvent) { interactions?.pointerMove(event) }
function onKeydown(event: KeyboardEvent) { interactions?.keydown(event) }
function onPaste(event: ClipboardEvent) { interactions?.paste(event) }
function reportInteraction(statusMessage: string) {
  if (alive) message.value = statusMessage
}
function interactionEnabled() {
  return alive && !paused.value && !viewOnly.value && props.visible !== false && status.value === 'streaming' && Boolean(canvas.value)
}
/* Live input: real-time touch to this phone and its Sync followers; HTTP stays the fallback. */
let releaseLiveInput: (() => void) | null = null
function liveAck(ack: LiveAck) {
  if (!alive || (ack.phase === 'down' && ack.ok)) return
  const rtt = liveInputChannel.rttMs()
  if (rtt !== null) lastInputRttMs.value = rtt
  if (ack.cancelled) {
    if (ack.reason && ack.reason !== 'superseded') message.value = 'Touch released by the safety timer'
    return
  }
  message.value = ack.ok ? 'Sent to ' + label.value + ' · live' + syncSummary(ack) : (ack.error || 'Device action failed')
}
function livePort(): LivePort | null {
  if (!liveInputChannel.isReady()) return null
  return {
    ready: () => liveInputChannel.isReady(),
    down: (x, y) => liveInputChannel.down(props.deviceSerial, syncFollowers(), x, y, liveAck),
    move: (id, x, y) => liveInputChannel.move(id, x, y),
    up: (id, x, y) => liveInputChannel.up(id, x, y),
    cancel: id => liveInputChannel.cancel(id),
    key: key => liveInputChannel.key(props.deviceSerial, syncFollowers(), key, liveAck),
  }
}
function setupInteractions() {
  interactions?.dispose()
  interactions = createPhoneInteractions({ element: () => canvas.value, enabled: interactionEnabled, send, focus: focusScreen, report: reportInteraction, live: livePort })
}
watch(() => props.visible, value => {
  if (value === false) { cancelInteractions(); clearQueuedActions(); cancelPointer() }
})
async function fullscreen() {
  try { await stage.value?.requestFullscreen() } catch { message.value = 'Fullscreen is unavailable in this browser; resize the window instead.' }
}
onMounted(async () => {
  releaseLiveInput = liveInputChannel.acquire()
  await nextTick()
  setupInteractions()
  connect()
  try {
    const response = await api('/api/phone/devices')
    const devices = Array.isArray(response) ? response : response.devices || []
    const device = devices.find((d: any) => d.serial === props.deviceSerial)
    if (device) label.value = device.nickname || `${device.model || 'Android'} · ${props.deviceSerial}`
  } catch {}
  if (alive && !props.embedded) document.title = `${label.value} — D3m0n in the droid`
})
onUnmounted(() => {
  alive = false
  releaseLiveInput?.(); releaseLiveInput = null
  interactions?.dispose(); interactions = null
  clearQueuedActions()
  cancelPointer()
  if (pressDotTimer) clearTimeout(pressDotTimer)
  handle.value?.stop(); handle.value = null
})
</script>

<template>
  <main class="device-window" :class="{ embedded, 'expanded-controls': moreControls }">
    <header class="window-header">
      <div class="window-title"><strong>D3m0n in the droid</strong><h1 :title="label">{{ label }}</h1><code>{{ deviceSerial }}</code><button class="name-edit" type="button" @click="nicknameDraft = label; editingName = !editingName" :aria-label="'Edit label for ' + deviceSerial">Edit label</button></div>
      <a v-if="!embedded" href="/?multi=1" target="_blank" rel="noopener" class="window-button">Dashboard</a>
    </header>
    <form v-if="editingName" class="text-row" @submit.prevent="saveNickname"><input v-model="nicknameDraft" maxlength="60" :aria-label="'Device label ' + deviceSerial" placeholder="Friendly device name" /><button class="window-button">Save label</button><button type="button" class="window-button" @click="editingName = false">Cancel</button></form>
    <div class="window-toolbar">
      <span class="connection-status" :class="{ live: status === 'streaming' }">{{ status === 'streaming' ? (idle ? 'H.264 · screen unchanged' : 'H.264 live') : status }}</span>
      <button class="window-button" type="button" @click="togglePause">{{ paused ? 'Resume' : 'Pause' }}</button>
      <button class="window-button" type="button" @click="connect">Reconnect</button>
      <button class="window-button" type="button" @click="fullscreen">Fullscreen</button>
      <button class="window-button" type="button" :class="{ 'view-only': viewOnly }" :aria-pressed="viewOnly" @click="toggleViewOnly">{{ viewOnly ? 'View only' : 'Controls on' }}</button>
    </div>
    <section ref="stage" class="window-stage">
      <canvas ref="canvas" tabindex="0" aria-label="Live device screen: tap or drag to control this phone" :class="{ dragging: Boolean(drag), 'view-only-cursor': viewOnly, 'paused-cursor': paused }" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="cancelPointer" @lostpointercapture="cancelPointer" @wheel="onWheel" @keydown="onKeydown" @paste="onPaste" @contextmenu.prevent />
      <span v-if="pressDot" class="press-dot" :class="{ fading: pressDot.fading }" :style="{ left: `${pressDot.x}px`, top: `${pressDot.y}px` }" aria-hidden="true" />
      <div v-if="paused" class="paused-cover">Paused in this window only</div>
    </section>
    <div class="keyboard-tools">
      <span class="keyboard-badge">Keyboard target: screen</span>
      <span class="keyboard-hint">Wheel to scroll · drag to swipe · focus screen to type · Esc = Back</span>
      <button class="window-button" type="button" :disabled="viewOnly || inputBusy" @click="scroll('up')">Scroll up</button>
      <button class="window-button" type="button" :disabled="viewOnly || inputBusy" @click="scroll('down')">Scroll down</button>
    </div>
    <div class="window-keys"><button v-for="key in keys" :key="key.code" type="button" class="window-button" :disabled="viewOnly || inputBusy" @click="send('/api/phone/key', { key: key.code })">{{ key.name }}</button><button v-if="embedded" type="button" class="window-button more-controls-toggle" :aria-expanded="moreControls" aria-label="Show extra phone controls" @click="moreControls = !moreControls">{{ moreControls ? 'Less' : 'More' }}</button></div>
    <form class="text-row" @submit.prevent="sendText">
      <textarea v-model="text" aria-label="Text to send to this device" placeholder="Type text for this device" :disabled="viewOnly" autocomplete="off" rows="1" @keydown="textKeydown" />
      <button class="window-button" :disabled="busy || viewOnly || inputBusy || !text.trim()">Send text</button>
    </form>
    <footer class="window-footer"><span>H.264 · {{ stats.fps }} fps · {{ stats.latency }} ms decode · q{{ stats.queue }} · {{ stats.kbps }} kbps · input q{{ queuedActionCount }} · request RTT {{ lastInputRttMs == null ? '—' : `${lastInputRttMs} ms` }}</span><span>{{ message || (viewOnly ? 'View-only mode: taps, keys and text disabled' : idle ? `No screen change for ${frameAge}s · reconnect if the phone is moving` : 'Independent viewer · shared encoder · no MJPEG') }}</span></footer>
  </main>
</template>

<style scoped>
.device-window { min-height:100dvh; height:100dvh; display:flex; flex-direction:column; gap:10px; padding:14px; box-sizing:border-box; background:var(--ui-bg); color:var(--ui-text-2); font-family:inherit; font-size:var(--ui-fs-sm); }
.device-window.embedded { min-height:0; height:100%; background:var(--ui-surface-1); }
.window-header { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding-bottom:10px; border-bottom:1px solid var(--ui-border); }
.window-title { min-width:0; }
.window-title strong { font-size:var(--ui-fs-2xs); color:var(--ui-accent); font-weight:650; letter-spacing:.1em; text-transform:uppercase; }
.window-title h1 { font-size:16px; font-weight:650; color:var(--ui-text-1); margin:4px 0 2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.window-title code { font-family:var(--ui-mono); font-size:var(--ui-fs-2xs); color:var(--ui-text-3); }
.window-toolbar,.window-keys,.keyboard-tools { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.window-toolbar .connection-status { margin-right:auto; display:inline-flex; align-items:center; gap:6px; font-size:var(--ui-fs-2xs); font-weight:600; color:var(--ui-text-3); }
.window-toolbar .connection-status::before { content:''; width:7px; height:7px; border-radius:50%; background:var(--ui-text-4); flex:none; }
.connection-status.live { color:#93e8bd; }
.connection-status.live::before { background:var(--ui-success); box-shadow:0 0 0 3px var(--ui-success-soft); }
.window-button { display:inline-flex; align-items:center; justify-content:center; box-sizing:border-box; height:var(--ui-control-h-sm); border:1px solid var(--ui-border-strong); background:var(--ui-surface-3); color:var(--ui-text-1); border-radius:var(--ui-radius-sm); padding:0 10px; font-size:var(--ui-fs-xs); font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; transition:background-color .15s, border-color .15s; }
.window-button:hover:not(:disabled) { border-color:var(--ui-border-hover); background:var(--ui-surface-hover); }
.window-button:focus-visible { outline:2px solid var(--ui-accent); outline-offset:2px; }
.window-button:disabled { opacity:.4; cursor:default; }
.window-stage { flex:1; min-height:160px; overflow:hidden; position:relative; display:flex; justify-content:center; align-items:center; border:1px solid var(--ui-border); border-radius:var(--ui-radius-lg); background:#000; }
.window-stage:fullscreen { padding:0; background:#000; }
.window-stage canvas { width:100%; height:100%; object-fit:contain; touch-action:none; cursor:pointer; outline:none; }
.window-stage canvas:focus-visible { box-shadow:inset 0 0 0 2px var(--ui-accent); }
.window-stage canvas.dragging { cursor:grabbing; }
.window-stage canvas.view-only-cursor { cursor:not-allowed; }
.window-stage canvas.paused-cursor { cursor:default; }
.paused-cover { position:absolute; inset:0; display:grid; place-items:center; background:rgba(8,10,14,.82); color:var(--ui-text-2); font-size:var(--ui-fs-sm); }
.press-dot { position:absolute; width:12px; height:12px; border:2px solid #fff; border-radius:50%; background:rgba(45,212,168,.6); box-shadow:0 0 0 2px rgba(0,0,0,.5); transform:translate(-50%,-50%); pointer-events:none; opacity:1; transition:opacity 250ms ease-out; }
.press-dot.fading { opacity:0; }
.window-keys { justify-content:center; padding:8px; border:1px solid var(--ui-border); border-radius:var(--ui-radius); background:var(--ui-surface-2); }
.keyboard-tools { justify-content:flex-start; color:var(--ui-text-3); font-size:var(--ui-fs-2xs); }
.keyboard-badge { display:inline-flex; align-items:center; height:22px; border:1px solid var(--ui-accent-line); border-radius:999px; padding:0 9px; background:var(--ui-accent-soft); color:#93ebd2; font-weight:600; }
.keyboard-hint { margin-right:auto; color:var(--ui-text-4); }
.text-row { display:flex; gap:8px; align-items:stretch; }
.text-row input,.text-row textarea { flex:1; min-width:0; resize:vertical; box-sizing:border-box; border:1px solid var(--ui-border-strong); border-radius:var(--ui-radius-sm); background:var(--ui-surface-sunken); color:var(--ui-text-1); padding:8px 10px; font-size:var(--ui-fs-sm); font-family:inherit; }
.text-row input:focus,.text-row textarea:focus { outline:none; border-color:var(--ui-accent); box-shadow:0 0 0 3px var(--ui-accent-soft); }
.text-row textarea { min-height:54px; }
.window-footer { display:flex; flex-direction:column; gap:3px; padding-top:8px; border-top:1px solid var(--ui-border); font-family:var(--ui-mono); font-size:10.5px; color:var(--ui-text-4); line-height:1.45; }
.name-edit { margin-left:8px; border:0; background:transparent; color:var(--ui-text-3); font-size:var(--ui-fs-2xs); font-weight:600; cursor:pointer; min-height:32px; }
.name-edit:hover { color:var(--ui-accent); }
.window-button.view-only { color:#f8d08a; border-color:rgba(240,180,76,.45); background:var(--ui-warn-soft); }
@media(max-width:620px) { .device-window { padding:10px; gap:8px; }.window-toolbar .connection-status { flex-basis:100%; margin-right:0; }.window-button { padding:0 8px; }.window-title h1 { font-size:14px; }.keyboard-hint { flex-basis:100%; order:3; }.text-row { flex-direction:column; }.text-row .window-button { align-self:flex-end; } }
@media(pointer:coarse) { .window-button { min-height:44px; }.name-edit { min-height:44px; }.text-row input,.text-row textarea { min-height:44px;font-size:16px;box-sizing:border-box; } }
.embedded { gap:6px;padding:8px; }
.embedded .window-header { padding-bottom:6px; }
.embedded .window-title { display:flex;align-items:center;gap:5px;width:100%; }.embedded .window-title strong,.embedded .window-title code { display:none; }.embedded .window-title h1 { flex:1;min-width:0;font-size:12px;margin:0; }.embedded .name-edit { margin-left:auto; }
.embedded .window-toolbar .connection-status { flex-basis:auto;flex:1;margin-right:0;font-size:10px; }.embedded .window-button { height:26px;padding:0 8px;font-size:var(--ui-fs-2xs); }
.embedded .window-keys { padding:5px; }
.embedded .text-row { flex-direction:row;align-items:center; }.embedded .text-row textarea { min-height:40px;max-height:100px;box-sizing:border-box; }.embedded .text-row .window-button { align-self:center; }
.embedded .window-stage { min-height:220px; border-radius:var(--ui-radius); }.embedded:not(.expanded-controls) .keyboard-hint { display:none; }
.embedded:not(.expanded-controls) .window-keys button:nth-child(n+5):not(.more-controls-toggle) { display:none; }.embedded:not(.expanded-controls) .window-footer>span:first-child { display:none; }
</style>
