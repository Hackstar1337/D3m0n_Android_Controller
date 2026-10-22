<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import DeviceWindowView from '@/views/DeviceWindowView.vue'
import { deviceWindowUrl, openDeviceWindow } from '@/composables/useDeviceWindows'
import { cleanLayout, tileLayout, type DevicePanel } from '@/composables/deviceLayout'
const props = defineProps<{ devices: any[]; requests: string[]; visible: boolean }>()
const selected = ref(''), search = ref(''), layoutMessage = ref('')
const area = ref<HTMLElement | null>(null)
const panels = ref<DevicePanel[]>([])
const STORAGE_KEY = 'd3m0n-workspace-layout-v2'
let depth = 1, handledRequests = 0, restored = false
let resizeObserver: ResizeObserver | null = null
let saveTimer: ReturnType<typeof setTimeout> | null = null
let gesture: { device: string; x: number; y: number; left: number; top: number; width: number; height: number; mode: 'move' | 'resize' } | null = null
const available = computed(() => props.devices.filter(d => !String(d.serial).startsWith('ios:')))
const matches = computed(() => available.value.filter(d => `${d.nickname || ''} ${d.model || ''} ${d.serial}`.toLowerCase().includes(search.value.toLowerCase().trim())))
const minHeight = computed(() => Math.max(760, ...panels.value.map(p => p.top + p.height + 30)))
function width() { return area.value?.clientWidth || 1100 }
function label(serial: string) { const d = props.devices.find(d => d.serial === serial); return d?.nickname ? `${d.nickname} · ${serial}` : serial }
function save() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ version: 2, savedAt: Date.now(), panels: panels.value })); layoutMessage.value = `${panels.value.length} window layout saved in this browser` }
  catch { layoutMessage.value = 'Browser storage is unavailable; keep this tab open to retain the layout.' }
}
function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    const clean = cleanLayout(saved.panels, new Set(available.value.map(d => d.serial)), width())
    if (saved.version !== 2) { layoutMessage.value = 'No saved workspace yet'; return }
    panels.value = clean; depth = clean.length + 1
    layoutMessage.value = `${clean.length} saved window(s) restored`
  } catch { layoutMessage.value = 'Saved layout could not be read. You can open windows normally.' }
}
function open(serial: string) {
  if (!serial || !available.value.some(d => d.serial === serial)) return
  const existing = panels.value.find(p => p.device === serial)
  if (existing) { existing.z = ++depth; return }
  const index = panels.value.length, w = Math.min(340, Math.max(260, width() - 24))
  panels.value.push({ device: serial, left: Math.min(12 + (index % 3) * 28, Math.max(0, width() - w - 12)), top: 12 + (index % 6) * 28, width: w, height: 690, z: ++depth })
}
function close(serial: string) { panels.value = panels.value.filter(p => p.device !== serial) }
function tile() { panels.value = tileLayout(panels.value, width()); depth = panels.value.length + 1 }
async function openMatches() {
  for (const d of matches.value) { open(d.serial); await new Promise(resolve => setTimeout(resolve, 70)) }
  await nextTick(); tile()
}
function startGesture(serial: string, event: PointerEvent, mode: 'move' | 'resize') {
  if (event.button !== 0) return
  const panel = panels.value.find(p => p.device === serial)
  if (!panel) return
  panel.z = ++depth
  gesture = { device: serial, x: event.clientX, y: event.clientY, left: panel.left, top: panel.top, width: panel.width, height: panel.height, mode }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  event.preventDefault()
}
function move(event: PointerEvent) {
  if (!gesture) return
  const panel = panels.value.find(p => p.device === gesture!.device)
  if (!panel) return
  const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y
  if (gesture.mode === 'move') {
    panel.left = Math.max(0, Math.min(Math.max(0, width() - panel.width - 10), gesture.left + dx))
    panel.top = Math.max(0, gesture.top + dy)
  } else {
    panel.width = Math.max(260, Math.min(1000, width() - panel.left - 10, gesture.width + dx))
    panel.height = Math.max(480, Math.min(1400, gesture.height + dy))
  }
}
watch(available, async devices => {
  if (!selected.value && devices.length) selected.value = devices[0].serial
  if (!restored && devices.length) { restored = true; await nextTick(); restore() }
}, { immediate: true })
watch(matches, devices => { if (!devices.some(d => d.serial === selected.value)) selected.value = devices[0]?.serial || '' })
watch(() => props.requests, async requests => {
  await nextTick()
  for (const serial of requests.slice(handledRequests)) open(serial)
  handledRequests = requests.length
}, { deep: true, immediate: true })
watch(panels, () => { if (saveTimer) clearTimeout(saveTimer); saveTimer = setTimeout(save, 600) }, { deep: true })
onMounted(() => {
  if (!area.value) return
  resizeObserver = new ResizeObserver(() => {
    for (const p of panels.value) { p.width = Math.min(p.width, Math.max(260, width() - 20)); p.left = Math.min(p.left, Math.max(0, width() - p.width - 10)) }
  })
  resizeObserver.observe(area.value)
})
onBeforeUnmount(() => { resizeObserver?.disconnect(); if (saveTimer) { clearTimeout(saveTimer); save() } })
</script>
<template>
  <section class="workspace">
    <div class="workspace-toolbar">
      <div class="workspace-heading"><h2>Device windows</h2><span>{{ panels.length }} open · shared H.264 encoders</span></div>
      <div class="ws-group">
        <input v-model="search" class="ws-input ws-search" aria-label="Search workspace devices" placeholder="Find device or IP" />
        <select v-model="selected" class="ws-input ws-select" aria-label="Choose a device for a window"><option v-for="d in matches" :key="d.serial" :value="d.serial">{{ d.nickname || d.serial }}</option></select>
        <button class="ws-btn ws-btn--primary" :disabled="!selected" @click="open(selected)">Open window</button>
        <button class="ws-btn" :disabled="!matches.length" @click="openMatches">{{ search ? 'Open matching devices' : 'Open all devices' }}</button>
      </div>
      <span class="ws-divider" aria-hidden="true"></span>
      <div class="ws-group">
        <button class="ws-btn" :disabled="!panels.length" @click="tile">Tile windows</button>
        <button class="ws-btn" @click="save">Save layout</button><button class="ws-btn" @click="restore">Restore layout</button>
      </div>
      <span class="ws-spacer" aria-hidden="true"></span>
      <button class="ws-btn ws-btn--danger" :disabled="!panels.length" @click="panels = []">Close all windows</button>
    </div>
    <p class="workspace-help" role="status">{{ layoutMessage || 'Layouts save automatically. Drag title bars; use bottom-right resize grips. Controls only affect their own device.' }}</p>
    <div v-if="!panels.length" class="workspace-empty">Choose a phone and open a window, or restore your saved layout. Closing a window does not stop other viewers.</div>
    <div ref="area" class="workspace-area" :style="{ minHeight: minHeight + 'px' }">
      <article v-for="p in panels" :key="p.device" role="region" :aria-label="'Device window ' + p.device" class="floating-device" :style="{ left: p.left + 'px', top: p.top + 'px', width: p.width + 'px', height: p.height + 'px', zIndex: p.z }" @pointerdown="p.z = ++depth">
        <div class="floating-titlebar" @pointerdown="startGesture(p.device, $event, 'move')" @pointermove="move" @pointerup="gesture = null" @pointercancel="gesture = null">
          <span :title="label(p.device)">{{ label(p.device) }}</span>
          <a :href="deviceWindowUrl(p.device)" target="_blank" rel="noopener" :aria-label="'Pop out device ' + p.device" @pointerdown.stop @click="openDeviceWindow(p.device, $event)">Pop out</a>
          <button :aria-label="'Close device window ' + p.device" @pointerdown.stop @click="close(p.device)">Close</button>
        </div>
        <DeviceWindowView :device-serial="p.device" embedded :visible="visible" />
        <button class="resize-grip" :aria-label="'Resize device window ' + p.device" title="Drag to resize" @pointerdown.stop="startGesture(p.device, $event, 'resize')" @pointermove="move" @pointerup="gesture = null" @pointercancel="gesture = null" />
      </article>
    </div>
  </section>
</template>
<style scoped>
.workspace { color: var(--ui-text-2); }
.workspace-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; margin-bottom: 8px; padding: 12px 14px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); }
.workspace-heading { display: flex; gap: 10px; align-items: baseline; width: 100%; margin-bottom: 2px; }
.workspace-heading h2 { margin: 0; color: var(--ui-text-1); font-size: var(--ui-fs-md); font-weight: 650; }
.workspace-heading span { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.ws-group { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ws-divider { width: 1px; height: 22px; background: var(--ui-border-strong); }
.ws-spacer { flex: 1; }
.ws-input, .ws-btn { box-sizing: border-box; height: var(--ui-control-h-sm); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); background: var(--ui-surface-sunken); color: var(--ui-text-1); font: inherit; font-size: var(--ui-fs-xs); }
.ws-input { padding: 0 10px; }
.ws-input:focus { outline: none; border-color: var(--ui-accent); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
.ws-search { width: 180px; }
.ws-select { min-width: 170px; max-width: 240px; padding-right: 26px; }
.ws-btn { padding: 0 11px; background: var(--ui-surface-3); font-weight: 600; white-space: nowrap; cursor: pointer; transition: background-color 0.15s, border-color 0.15s; }
.ws-btn:hover:not(:disabled) { background: var(--ui-surface-hover); border-color: var(--ui-border-hover); }
.ws-btn--primary { border-color: var(--ui-accent); background: var(--ui-accent); color: var(--ui-on-accent); }
.ws-btn--primary:hover:not(:disabled) { background: var(--ui-accent-hover); border-color: var(--ui-accent-hover); }
.ws-btn--danger { border-color: rgba(242, 109, 109, 0.4); background: var(--ui-danger-soft); color: #ffb8b8; }
.ws-btn--danger:hover:not(:disabled) { background: rgba(242, 109, 109, 0.2); }
.ws-btn:disabled { opacity: 0.4; cursor: default; }
.ws-btn:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.workspace-help { margin: 0 0 12px; padding: 0 2px; color: var(--ui-text-4); font-size: var(--ui-fs-2xs); }
.workspace-area { position: relative; background: radial-gradient(circle at 1px 1px, rgba(148, 163, 184, 0.14) 1px, transparent 0) 0 0 / 22px 22px, var(--ui-surface-sunken); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); overflow: auto; }
.workspace-empty { display: flex; align-items: center; justify-content: center; min-height: 64px; margin-bottom: 12px; padding: 18px; border: 1px dashed var(--ui-border-strong); border-radius: var(--ui-radius-lg); color: var(--ui-text-3); font-size: var(--ui-fs-xs); text-align: center; }
.floating-device { position: absolute; min-width: 260px; min-height: 480px; overflow: hidden; display: flex; flex-direction: column; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); box-shadow: var(--ui-shadow-pop); }
.floating-titlebar { display: flex; align-items: center; gap: 6px; height: 34px; padding: 0 8px 0 12px; border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-2); color: var(--ui-text-1); cursor: move; touch-action: none; user-select: none; font-size: var(--ui-fs-xs); font-weight: 600; }
.floating-titlebar span { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.floating-titlebar a, .floating-titlebar button { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-xs); background: var(--ui-surface-3); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; text-decoration: none; cursor: pointer; }
.floating-titlebar a:hover { color: var(--ui-text-1); border-color: var(--ui-border-hover); }
.floating-titlebar button:hover { color: #ffb8b8; border-color: rgba(242, 109, 109, 0.45); }
.resize-grip { position: absolute; bottom: 1px; right: 1px; width: 20px; height: 20px; cursor: nwse-resize; touch-action: none; border: 0; background: linear-gradient(135deg, transparent 55%, var(--ui-text-4) 56%, var(--ui-text-4) 62%, transparent 63%, transparent 72%, var(--ui-text-4) 73%, var(--ui-text-4) 80%, transparent 81%); }
.floating-device :deep(.device-window) { flex: 1; min-height: 0; height: auto; }
.floating-device :deep(.window-title h1) { font-size: 12px; }
.floating-device :deep(.window-title strong) { font-size: 10px; }
.floating-device :deep(.window-header > a) { display: none; }
</style>