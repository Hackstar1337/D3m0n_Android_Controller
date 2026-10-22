<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { startH264Stream, type H264Metrics } from '@/composables/useH264Stream'
import FleetFilesPanel from '@/components/FleetFilesPanel.vue'

type FleetDevice = {
  serial: string
  label: string
  model: string
  ip: string
  online: boolean
}

type FleetGroup = {
  id: string
  name: string
  devices: string[]
  created_at?: string
  updated_at?: string
}

type DeviceResult = {
  device: string
  ok: boolean
  error?: string
  started_at?: number
  duration_ms?: number
  dry_run?: boolean
}

type ActionResponse = {
  ok: boolean
  action: string
  results: DeviceResult[]
  duration_ms?: number
  dispatch_spread_ms?: number
  dry_run: boolean
}

type Capabilities = { unicode?: boolean; files?: boolean; batch_install?: boolean; sync?: string }

const props = defineProps<{ devices: any[]; visible: boolean }>()

const groups = ref<FleetGroup[]>([])
const groupsLoading = ref(false)
const groupsError = ref('')
const currentGroupId = ref('')
const groupName = ref('')
const deleteArmed = ref(false)
const selectedSet = ref<Set<string>>(new Set())
const search = ref('')
const capabilities = ref<Capabilities>({ unicode: false })
const actionError = ref('')
const textError = ref('')
const textDraft = ref('')
const sending = ref(false)
const sendingLabel = ref('')
const lastResult = ref<ActionResponse | null>(null)
const previewResult = ref<ActionResponse | null>(null)
const leaderSerial = ref('')
const streamRequested = ref(false)
const armed = ref(false)
const canvas = ref<HTMLCanvasElement | null>(null)
const streamHandle = shallowRef<H264Metrics | null>(null)
const dragStart = ref<{ x: number; y: number; time: number } | null>(null)
let alive = true

function normalizeDevice(input: any): FleetDevice | null {
  const serial = String(typeof input === 'string' ? input : (input?.serial ?? input?.device ?? input?.id ?? '')).trim()
  if (!serial) return null
  if (typeof input === 'string') return { serial, label: serial, model: 'Android', ip: '', online: true }
  const status = String(input?.status ?? input?.state ?? '').toLowerCase()
  const online = input?.online === true || input?.connected === true || status === 'online' || status === 'device' || (input?.online !== false && input?.offline !== true && status !== 'offline')
  return {
    serial,
    label: String(input?.nickname ?? input?.name ?? input?.label ?? input?.model ?? serial),
    model: String(input?.model ?? input?.product ?? 'Android'),
    ip: String(input?.ip ?? input?.ip_address ?? input?.address ?? input?.host ?? ''),
    online,
  }
}

const knownDevices = computed<FleetDevice[]>(() => {
  const bySerial = new Map<string, FleetDevice>()
  for (const raw of props.devices || []) {
    const device = normalizeDevice(raw)
    if (device && !device.serial.startsWith('ios:')) bySerial.set(device.serial, device)
  }
  return Array.from(bySerial.values()).sort((a, b) => a.label.localeCompare(b.label) || a.serial.localeCompare(b.serial))
})

const currentGroup = computed(() => groups.value.find(group => group.id === currentGroupId.value) || null)
const displayDevices = computed<FleetDevice[]>(() => {
  const bySerial = new Map(knownDevices.value.map(device => [device.serial, device]))
  for (const serial of currentGroup.value?.devices || []) {
    if (!bySerial.has(serial)) bySerial.set(serial, { serial, label: serial, model: 'Saved member', ip: '', online: false })
  }
  return Array.from(bySerial.values()).sort((a, b) => Number(b.online) - Number(a.online) || a.label.localeCompare(b.label) || a.serial.localeCompare(b.serial))
})
const filteredDevices = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return displayDevices.value
  return displayDevices.value.filter(device => `${device.label} ${device.model} ${device.ip} ${device.serial}`.toLowerCase().includes(query))
})
const selectedDevices = computed(() => displayDevices.value.filter(device => selectedSet.value.has(device.serial)))
const selectedOnlineDevices = computed(() => selectedDevices.value.filter(device => device.online))
const selectedOfflineDevices = computed(() => selectedDevices.value.filter(device => !device.online))
const selectedCount = computed(() => selectedSet.value.size)
const selectedNames = computed(() => selectedDevices.value.map(device => device.label === device.serial ? device.serial : `${device.label} (${device.serial})`))
const leaderOptions = computed(() => selectedOnlineDevices.value)
const streamStatus = computed(() => streamHandle.value?.status.value || (streamRequested.value ? 'Starting leader stream' : 'Leader stream stopped'))
const streamIsLive = computed(() => streamStatus.value === 'streaming')
const capabilitiesLabel = computed(() => capabilities.value.unicode ? 'Unicode text enabled' : 'Stage 1 · ASCII text only')

function errorText(payload: any, fallback: string): string {
  const detail = payload?.detail ?? payload?.error ?? payload?.message
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) return detail.map(item => typeof item === 'string' ? item : item?.msg || JSON.stringify(item)).join('; ')
  return fallback
}

async function fetchJson(path: string, init?: RequestInit): Promise<any> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch (error) {
    throw new Error(error instanceof Error ? `Network error: ${error.message}` : 'Network error while contacting the fleet service')
  }
  const raw = await response.text()
  let payload: any = null
  if (raw) {
    try { payload = JSON.parse(raw) } catch { payload = null }
  }
  if (!response.ok) throw new Error(errorText(payload, `Fleet service returned HTTP ${response.status}`))
  return payload
}

async function loadGroups() {
  groupsLoading.value = true
  groupsError.value = ''
  try {
    const payload = await fetchJson('/api/fleet/groups')
    if (payload?.ok === false) throw new Error(errorText(payload, 'Could not load device groups'))
    groups.value = Array.isArray(payload?.groups) ? payload.groups.map((group: any) => ({
      id: String(group.id), name: String(group.name), devices: Array.isArray(group.devices) ? Array.from(new Set(group.devices.map(String))) : [], created_at: group.created_at, updated_at: group.updated_at,
    })) : []
  } catch (error) {
    groupsError.value = error instanceof Error ? error.message : 'Could not load device groups'
  } finally {
    groupsLoading.value = false
  }
}

async function loadCapabilities() {
  try {
    const payload = await fetchJson('/api/fleet/capabilities')
    if (payload?.ok !== false) capabilities.value = payload || { unicode: false }
  } catch {
    // The stage1 badge remains safe and informative when capabilities are unavailable.
  }
}

function setSelection(serials: Iterable<string>) {
  selectedSet.value = new Set(serials)
}

function toggleDevice(serial: string) {
  const next = new Set(selectedSet.value)
  if (next.has(serial)) next.delete(serial)
  else next.add(serial)
  setSelection(next)
}

function selectVisible() {
  const next = new Set(selectedSet.value)
  for (const device of filteredDevices.value) next.add(device.serial)
  setSelection(next)
}

function clearSelection() {
  setSelection([])
}

function loadGroup() {
  deleteArmed.value = false
  const group = currentGroup.value
  groupName.value = group?.name || ''
  setSelection(group ? group.devices : [])
}

function replaceGroup(group: FleetGroup) {
  const index = groups.value.findIndex(item => item.id === group.id)
  if (index < 0) groups.value.push(group)
  else groups.value[index] = group
}

async function createGroup() {
  const name = groupName.value.trim()
  if (!name) { groupsError.value = 'Enter a group name first.'; return }
  groupsError.value = ''
  try {
    const payload = await fetchJson('/api/fleet/groups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, devices: Array.from(selectedSet.value) }),
    })
    if (payload?.ok === false || !payload?.group) throw new Error(errorText(payload, 'Could not create this group'))
    const group = payload.group as FleetGroup
    replaceGroup({ ...group, devices: Array.from(new Set(group.devices || [])) })
    currentGroupId.value = group.id
    groupName.value = group.name
    deleteArmed.value = false
  } catch (error) {
    groupsError.value = error instanceof Error ? `Could not create group: ${error.message}` : 'Could not create group. The name may already be in use.'
  }
}

async function updateCurrentGroup(mode: 'rename' | 'members') {
  const group = currentGroup.value
  if (!group) { groupsError.value = 'Choose a device group first.'; return }
  const name = groupName.value.trim()
  if (mode === 'rename' && !name) { groupsError.value = 'Enter a group name first.'; return }
  groupsError.value = ''
  try {
    const body = mode === 'rename' ? { name } : { devices: Array.from(selectedSet.value) }
    const payload = await fetchJson(`/api/fleet/groups/${encodeURIComponent(group.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    if (payload?.ok === false || !payload?.group) throw new Error(errorText(payload, 'Could not update this group'))
    const saved = payload.group as FleetGroup
    replaceGroup({ ...saved, devices: Array.from(new Set(saved.devices || [])) })
    groupName.value = saved.name
    deleteArmed.value = false
  } catch (error) {
    groupsError.value = error instanceof Error ? `Could not update group: ${error.message}` : 'Could not update group. Check for a duplicate name.'
  }
}

async function deleteCurrentGroup() {
  const group = currentGroup.value
  if (!group) return
  groupsError.value = ''
  try {
    const payload = await fetchJson(`/api/fleet/groups/${encodeURIComponent(group.id)}`, { method: 'DELETE' })
    if (payload?.ok === false) throw new Error(errorText(payload, 'Could not delete this group'))
    groups.value = groups.value.filter(item => item.id !== group.id)
    currentGroupId.value = ''
    groupName.value = ''
    deleteArmed.value = false
    setSelection([])
  } catch (error) {
    groupsError.value = error instanceof Error ? error.message : 'Could not delete this group.'
  }
}

function targetSnapshot() {
  const devices = Array.from(selectedSet.value)
  if (!devices.length) {
    actionError.value = 'Select at least one device. Actions never default to all phones.'
    return null
  }
  return devices
}

async function dispatch(action: 'tap' | 'swipe' | 'key' | 'text', params: Record<string, unknown>, dryRun = false): Promise<boolean> {
  if (sending.value) return false
  const devices = targetSnapshot()
  if (!devices) return false
  actionError.value = ''
  if (dryRun) previewResult.value = null
  else lastResult.value = null
  sending.value = true
  sendingLabel.value = dryRun ? 'Preparing preview' : `Sending ${action}`
  try {
    const payload = await fetchJson('/api/fleet/actions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets: { devices, group_ids: [], all: false }, action, params, dry_run: dryRun }),
    })
    const result: ActionResponse = {
      ok: payload?.ok !== false,
      action: String(payload?.action || action),
      results: Array.isArray(payload?.results) ? payload.results : [],
      duration_ms: payload?.duration_ms,
      dispatch_spread_ms: payload?.dispatch_spread_ms,
      dry_run: Boolean(payload?.dry_run ?? dryRun),
    }
    if (dryRun) previewResult.value = result
    else lastResult.value = result
    const failed = result.results.filter(item => !item.ok)
    if (result.ok === false || failed.length) actionError.value = `${failed.length || 'Some'} of ${devices.length} selected device${devices.length === 1 ? '' : 's'} reported a failure.`
    return result.ok && failed.length === 0
  } catch (error) {
    const message = error instanceof Error ? error.message : 'The fleet service could not complete this action.'
    if (dryRun) previewResult.value = null
    actionError.value = dryRun ? `Preview failed: ${message}` : `Action failed: ${message}`
    return false
  } finally {
    sending.value = false
    sendingLabel.value = ''
  }
}

function sendKey(key: string) {
  void dispatch('key', { key })
}

async function sendText() {
  textError.value = ''
  if (!textDraft.value) { textError.value = 'Enter text first.'; return }
  if (textDraft.value.length > 4096) { textError.value = 'Text is limited to 4096 characters.'; return }
  if (!capabilities.value.unicode && !/^[\x00-\x7F]*$/.test(textDraft.value)) { textError.value = 'Unicode transport is not available on the server.'; return }
  if (await dispatch('text', { text: textDraft.value })) textDraft.value = ''
}

function clamp(value: number, minimum = 0, maximum = 1) {
  return Math.max(minimum, Math.min(maximum, value))
}

function normalizedPoint(event: PointerEvent) {
  const element = canvas.value
  if (!element || !element.width || !element.height) return null
  const rect = element.getBoundingClientRect()
  const scale = Math.min(rect.width / element.width, rect.height / element.height)
  if (!scale || !rect.width || !rect.height) return null
  const contentWidth = element.width * scale
  const contentHeight = element.height * scale
  const offsetX = (rect.width - contentWidth) / 2
  const offsetY = (rect.height - contentHeight) / 2
  const x = (event.clientX - rect.left - offsetX) / contentWidth
  const y = (event.clientY - rect.top - offsetY) / contentHeight
  if (x < 0 || x > 1 || y < 0 || y > 1) return null
  return { x: clamp(x), y: clamp(y) }
}

function pointerDown(event: PointerEvent) {
  if (!armed.value || !leaderSerial.value || !streamIsLive.value || sending.value || event.button !== 0) return
  const point = normalizedPoint(event)
  if (!point) return
  event.preventDefault()
  canvas.value?.setPointerCapture(event.pointerId)
  dragStart.value = { ...point, time: Date.now() }
}

function pointerUp(event: PointerEvent) {
  const start = dragStart.value
  dragStart.value = null
  if (!start || !armed.value || sending.value) return
  const end = normalizedPoint(event)
  if (!end) return
  event.preventDefault()
  const distance = Math.hypot(end.x - start.x, end.y - start.y)
  if (distance < 15 / Math.max(canvas.value?.width || 1, canvas.value?.height || 1)) {
    void dispatch('tap', { x: clamp(end.x), y: clamp(end.y) })
  } else {
    void dispatch('swipe', {
      x1: clamp(start.x), y1: clamp(start.y), x2: clamp(end.x), y2: clamp(end.y),
      duration: Math.max(100, Math.min(1500, Date.now() - start.time)),
    })
  }
}

function stopStream() {
  streamRequested.value = false
  armed.value = false
  dragStart.value = null
}

watch([leaderSerial, streamRequested], ([serial, requested], _previous, onCleanup) => {
  const previous = streamHandle.value
  streamHandle.value = null
  previous?.stop()
  armed.value = false
  dragStart.value = null
  let createdHandle: H264Metrics | null = null
  onCleanup(() => {
    if (streamHandle.value === createdHandle) streamHandle.value = null
    createdHandle?.stop()
    previous?.stop()
  })
  if (!serial || !requested || !canvas.value || !alive) {
    return
  }
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  createdHandle = startH264Stream(`${protocol}://${location.host}/api/phone/android-h264/${encodeURIComponent(serial)}`, canvas.value, { visible: () => props.visible !== false, kind: 'fleet-leader' })
  streamHandle.value = createdHandle
}, { flush: 'post' })

watch(() => Array.from(selectedSet.value).sort().join('|'), () => {
  if (leaderSerial.value && !selectedOnlineDevices.value.some(device => device.serial === leaderSerial.value)) leaderSerial.value = ''
})

watch(() => props.visible, visible => { if (visible) void loadGroups() })

onMounted(() => {
  void loadGroups()
  void loadCapabilities()
})

onUnmounted(() => {
  alive = false
  streamHandle.value?.stop()
  streamHandle.value = null
})
</script>

<template>
  <main v-if="visible" class="fleet-panel">
    <header class="panel-header">
      <div>
        <p class="eyebrow">D3m0n fleet controls</p>
        <h1>Control selected phones</h1>
        <p class="intro">Choose devices explicitly. Touch, keys and text affect selected phones only.</p>
      </div>
      <span class="capability-badge">{{ capabilitiesLabel }}</span>
    </header>
    <nav aria-label="Fleet sections" class="button-row"><a class="quiet-button" href="#selection-heading">Devices</a><a class="quiet-button" href="#groups-heading">Groups</a><a class="quiet-button" href="#leader-heading">Leader screen</a><a class="quiet-button" href="#actions-heading">Actions</a><a class="quiet-button" href="#fleet-files">Files and APKs</a></nav>

    <div class="fleet-columns">
    <div class="fleet-col fleet-col--side">
    <section class="panel-card selection-card" aria-labelledby="selection-heading">
      <div class="section-heading">
        <div><h2 id="selection-heading">Devices</h2><p>{{ selectedCount }} selected<span v-if="selectedOfflineDevices.length"> · {{ selectedOfflineDevices.length }} offline</span></p></div>
        <div class="selection-actions"><button type="button" class="quiet-button" :disabled="!filteredDevices.length" @click="selectVisible">Select visible</button><button type="button" class="quiet-button" :disabled="!selectedCount" @click="clearSelection">Clear selection</button></div>
      </div>
      <label class="field-label" for="device-search">Search label, model or IP</label>
      <input id="device-search" v-model="search" class="text-input" type="search" placeholder="Find a phone or serial" autocomplete="off" />
      <p v-if="!displayDevices.length" class="muted">No devices are available yet. Saved offline members appear when their group is selected.</p>
      <div v-else class="device-list" role="group" aria-label="Fleet device selection">
        <label v-for="device in filteredDevices" :key="device.serial" class="device-row" :class="{ offline: !device.online }">
          <input type="checkbox" :checked="selectedSet.has(device.serial)" :aria-label="`Select device ${device.serial}`" @change="toggleDevice(device.serial)" />
          <span class="device-copy"><strong>{{ device.label }}</strong><small>{{ device.model }}<span v-if="device.ip"> · {{ device.ip }}</span> · {{ device.serial }}</small></span>
          <span class="device-state">{{ device.online ? 'Online' : 'Offline · saved member' }}</span>
        </label>
        <p v-if="search && !filteredDevices.length" class="muted">No devices match this search.</p>
      </div>
    </section>

    <section class="panel-card groups-card" aria-labelledby="groups-heading">
      <div class="section-heading"><div><h2 id="groups-heading">Device groups</h2><p>Keep offline members saved for later.</p></div></div>
      <label class="field-label" for="group-select">Device group</label>
      <select id="group-select" v-model="currentGroupId" class="text-input" :disabled="groupsLoading" @change="loadGroup">
        <option value="">No group selected</option>
        <option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }} · {{ group.devices.length }} saved</option>
      </select>
      <label class="field-label" for="group-name">Group name</label>
      <input id="group-name" v-model="groupName" class="text-input" maxlength="60" placeholder="For example, QA phones" />
      <div class="button-row">
        <button type="button" class="primary-button" :disabled="!groupName.trim()" @click="createGroup">Create group</button>
        <button type="button" class="quiet-button" :disabled="!currentGroup" @click="updateCurrentGroup('rename')">Rename group</button>
        <button type="button" class="quiet-button" :disabled="!currentGroup" @click="updateCurrentGroup('members')">Save selected members</button>
      </div>
      <div v-if="currentGroup" class="delete-row">
        <button v-if="!deleteArmed" type="button" class="danger-button" @click="deleteArmed = true">Delete group</button>
        <template v-else><span>Delete “{{ currentGroup.name }}”?</span><button type="button" class="danger-button" @click="deleteCurrentGroup">Delete group</button><button type="button" class="quiet-button" @click="deleteArmed = false">Keep group</button></template>
      </div>
      <p v-if="groupsError" class="error-text" role="alert">{{ groupsError }}</p>
      <p v-if="currentGroup && selectedOfflineDevices.length" class="offline-note">Offline saved members stay in this group and can be removed by unchecking them, then saving selected members.</p>
    </section>
    </div>

    <div class="fleet-col fleet-col--main">
    <section class="panel-card leader-card" aria-labelledby="leader-heading">
      <div class="section-heading"><div><h2 id="leader-heading">Leader screen</h2><p>Use one selected online Android phone as the visual reference.</p></div></div>
      <label class="field-label" for="leader-select">Choose leader</label>
      <select id="leader-select" v-model="leaderSerial" class="text-input" :disabled="sending || !leaderOptions.length">
        <option value="">Choose a selected online device</option>
        <option v-for="device in leaderOptions" :key="device.serial" :value="device.serial">{{ device.label }} · {{ device.serial }}</option>
      </select>
      <div class="leader-toolbar">
        <span class="stream-status" :class="{ live: streamIsLive }">{{ streamStatus }}</span>
        <button type="button" class="quiet-button" :disabled="!leaderSerial || sending || streamRequested" @click="streamRequested = true">Start leader stream</button>
        <button type="button" class="quiet-button" :disabled="!streamRequested" @click="stopStream">Pause leader stream</button>
        <button type="button" class="arm-button" :class="{ armed: armed }" :disabled="!leaderSerial || !streamIsLive || sending" :aria-pressed="armed" @click="armed = !armed">{{ armed ? 'Disable synchronized touch' : 'Enable synchronized touch' }}</button>
      </div>
      <section class="leader-stage" aria-label="Leader H.264 screen">
        <canvas ref="canvas" @pointerdown="pointerDown" @pointerup="pointerUp" @pointercancel="dragStart = null" @contextmenu.prevent />
        <div v-if="!leaderSerial" class="stage-message">Choose a selected online device to preview its screen.</div>
        <div v-else-if="!streamRequested" class="stage-message">Start the leader stream when you are ready. Synchronized touch is off by default.</div>
        <div v-else-if="!streamIsLive" class="stage-message">{{ streamStatus }}</div>
      </section>
      <p class="touch-note">{{ armed ? 'Synchronized touch is armed. Taps and drags will be sent to the current selection.' : 'Synchronized touch is off. Arm it only when you want leader taps and drags broadcast.' }}</p>
    </section>

    <section class="panel-card actions-card" aria-labelledby="actions-heading">
      <div class="section-heading"><div><h2 id="actions-heading">Fleet actions</h2><p>{{ selectedCount ? `Ready for ${selectedCount} selected device${selectedCount === 1 ? '' : 's'}.` : 'Select devices before sending an action.' }}</p><small v-if="selectedNames.length" class="selected-targets">Targets: {{ selectedNames.join(', ') }}</small></div></div>
      <div class="key-grid">
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('BACK')">Send Back</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('HOME')">Send Home</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('APP_SWITCH')">Send Recent apps</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('WAKEUP')">Send Wake</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('VOLUME_UP')">Send Volume up</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('VOLUME_DOWN')">Send Volume down</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('ENTER')">Send Enter</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('DEL')">Send Delete</button>
        <button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="sendKey('SPACE')">Send Space</button>
      </div>
      <div class="text-action">
        <label class="field-label" for="fleet-text">Text for selected devices</label>
        <div class="text-row"><input id="fleet-text" v-model="textDraft" class="text-input" maxlength="4096" :disabled="sending" :placeholder="capabilities.unicode ? 'Text, including Chinese, Hindi and emoji' : 'ASCII text'" autocomplete="off" @input="textError = ''" /><button type="button" class="primary-button" :disabled="!selectedCount || sending || !textDraft" @click="sendText">Send text</button></div>
        <p v-if="textError" class="error-text" role="alert">{{ textError }}</p>
      </div>
      <div class="preview-row"><button type="button" class="quiet-button" :disabled="!selectedCount || sending" @click="dispatch('key', { key: 'HOME' }, true)">Preview Home action (dry run)</button><span>No device action is sent by a preview.</span></div>
      <p v-if="sending" class="sending-text" aria-live="polite">{{ sendingLabel }} for the current selection…</p>
      <p v-if="actionError" class="error-text" role="alert">{{ actionError }}</p>
    </section>

    <section v-if="previewResult" class="panel-card result-card preview-card" aria-labelledby="preview-heading">
      <div class="section-heading"><div><h2 id="preview-heading">Preview result</h2><p>Dry run only · no device action was sent.</p></div></div>
      <div class="result-meta"><span>{{ previewResult.action }} · {{ previewResult.results.length }} device results</span><span v-if="previewResult.duration_ms != null">Total {{ previewResult.duration_ms }} ms</span></div>
      <div class="result-list"><div v-for="item in previewResult.results" :key="`preview-${item.device}`" class="result-row"><strong>{{ item.device }}</strong><span>{{ item.ok ? 'Target validated · not executed' : 'Invalid target' }}</span><small v-if="item.error">{{ item.error }}</small><small v-if="item.duration_ms != null">{{ item.duration_ms }} ms</small></div></div>
    </section>

    <section v-if="lastResult" class="panel-card result-card" aria-labelledby="results-heading">
      <div class="section-heading"><div><h2 id="results-heading">Latest action results</h2><p>Best-effort dispatch; not frame-accurate synchronization.</p></div></div>
      <div class="result-meta"><span>{{ lastResult.action }} · {{ lastResult.results.length }} device results</span><span v-if="lastResult.dispatch_spread_ms != null">Dispatch spread {{ lastResult.dispatch_spread_ms }} ms</span><span v-if="lastResult.duration_ms != null">Total {{ lastResult.duration_ms }} ms</span></div>
      <div class="result-list"><div v-for="item in lastResult.results" :key="item.device" class="result-row" :class="{ failed: !item.ok }"><strong>{{ item.device }}</strong><span>{{ item.ok ? 'Succeeded' : 'Failed' }}</span><small v-if="item.error">{{ item.error }}</small><small v-if="item.duration_ms != null">{{ item.duration_ms }} ms</small></div><p v-if="!lastResult.results.length" class="muted">The service returned no per-device results.</p></div>
    </section>
    </div>
    </div>
    <section id="fleet-files"><FleetFilesPanel :selected-devices="Array.from(selectedSet)" :visible="visible" /></section>
  </main>
</template>

<style scoped>
/* Fleet controls — desktop redesign (class names unchanged). */
.fleet-panel { box-sizing: border-box; min-height: 100%; padding: 0 0 24px; display: flex; flex-direction: column; gap: 14px; color: var(--ui-text-2); font-family: inherit; }
.panel-header, .section-heading, .leader-toolbar, .button-row, .selection-actions, .text-row, .preview-row, .result-meta, .delete-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.panel-header { align-items: flex-end; justify-content: space-between; gap: 16px; padding: 4px 2px 0; }
.eyebrow { margin: 0; color: var(--ui-accent); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.1em; text-transform: uppercase; }
.panel-header h1 { margin: 4px 0 2px; color: var(--ui-text-1); font-size: 20px; font-weight: 650; letter-spacing: -0.01em; line-height: 1.25; }
.intro, .section-heading p { margin: 3px 0 0; color: var(--ui-text-3); font-size: var(--ui-fs-xs); line-height: 1.5; }
.capability-badge { flex: none; display: inline-flex; align-items: center; height: 26px; padding: 0 11px; border-radius: 999px; border: 1px solid var(--ui-accent-line); background: var(--ui-accent-soft); color: #93ebd2; font-size: var(--ui-fs-2xs); font-weight: 600; }
nav.button-row { gap: 2px; padding: 3px; width: max-content; max-width: 100%; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); background: var(--ui-surface-1); }
nav.button-row .quiet-button { border-color: transparent; background: transparent; color: var(--ui-text-3); height: 28px; padding: 0 12px; text-decoration: none; display: inline-flex; align-items: center; }
nav.button-row .quiet-button:hover { background: var(--ui-surface-3); color: var(--ui-text-1); border-color: transparent; }

.fleet-columns { display: grid; grid-template-columns: minmax(340px, 420px) minmax(0, 1fr); gap: 14px; align-items: start; }
.fleet-col { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.fleet-col--side { position: sticky; top: calc(var(--ui-topbar-h) + 14px); }

.panel-card { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); padding: 16px; background: var(--ui-surface-1); box-shadow: none; min-width: 0; }
.section-heading { justify-content: space-between; align-items: flex-start; margin: 0 0 12px; }
.section-heading h2 { margin: 0; color: var(--ui-text-1); font-size: var(--ui-fs-md); font-weight: 650; }
.selected-targets { display: block; max-width: 760px; margin-top: 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); line-height: 1.5; overflow-wrap: anywhere; }
.field-label { display: block; margin: 12px 0 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase; }
.section-heading + .field-label { margin-top: 0; }
.text-input { box-sizing: border-box; width: 100%; min-width: 0; height: var(--ui-control-h); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); padding: 0 11px; color: var(--ui-text-1); background: var(--ui-surface-sunken); font: inherit; font-size: var(--ui-fs-sm); transition: border-color 0.15s, box-shadow 0.15s; }
select.text-input { padding-right: 30px; }
.text-input:hover:not(:disabled) { border-color: var(--ui-border-hover); }
.text-input:focus { outline: none; border-color: var(--ui-accent); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
.quiet-button:focus-visible, .primary-button:focus-visible, .danger-button:focus-visible, .arm-button:focus-visible, .device-row:has(input:focus-visible) { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.text-input:disabled { opacity: 0.5; }
button { font: inherit; }
.quiet-button, .primary-button, .danger-button, .arm-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: var(--ui-control-h); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); padding: 0 12px; color: var(--ui-text-1); background: var(--ui-surface-3); font-size: var(--ui-fs-xs); font-weight: 600; cursor: pointer; white-space: nowrap; transition: background-color 0.15s, border-color 0.15s, color 0.15s; }
.quiet-button:hover:not(:disabled), .arm-button:hover:not(:disabled) { border-color: var(--ui-border-hover); background: var(--ui-surface-hover); }
.primary-button { border-color: var(--ui-accent); color: var(--ui-on-accent); background: var(--ui-accent); }
.primary-button:hover:not(:disabled) { background: var(--ui-accent-hover); border-color: var(--ui-accent-hover); }
.danger-button { border-color: rgba(242, 109, 109, 0.45); color: #ffb8b8; background: var(--ui-danger-soft); }
.danger-button:hover:not(:disabled) { background: rgba(242, 109, 109, 0.2); }
.arm-button { color: #f8d08a; border-color: rgba(240, 180, 76, 0.45); background: var(--ui-warn-soft); }
.arm-button.armed { color: #1b1305; border-color: var(--ui-warn); background: var(--ui-warn); }
.quiet-button:disabled, .primary-button:disabled, .danger-button:disabled, .arm-button:disabled { opacity: 0.4; cursor: default; }

.selection-actions { gap: 6px; }
.device-list { display: flex; flex-direction: column; gap: 0; margin-top: 10px; max-height: max(280px, calc(100vh - 560px)); overflow: auto; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); background: var(--ui-surface-sunken); }
.device-row { display: flex; align-items: center; gap: 10px; min-width: 0; padding: 8px 12px; border: 0; border-bottom: 1px solid var(--ui-border); border-radius: 0; background: transparent; cursor: pointer; transition: background-color 0.12s; }
.device-row:last-child { border-bottom: 0; }
.device-row:hover { background: var(--ui-surface-2); }
.device-row:has(input:checked) { background: var(--ui-accent-soft); }
.device-row.offline { opacity: 0.75; }
.device-row input { flex: none; accent-color: var(--ui-accent); }
.device-copy { display: flex; flex-direction: column; min-width: 0; flex: 1; gap: 2px; }
.device-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-1); font-size: var(--ui-fs-sm); font-weight: 600; }
.device-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-4); font-family: var(--ui-mono); font-size: 10.5px; }
.device-state { flex: none; display: inline-flex; align-items: center; gap: 6px; color: #93e8bd; font-size: var(--ui-fs-2xs); font-weight: 600; text-align: right; }
.device-state::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--ui-success); }
.offline .device-state { color: #f8d08a; }
.offline .device-state::before { background: var(--ui-warn); }
.muted, .offline-note, .touch-note, .preview-row span { color: var(--ui-text-3); font-size: var(--ui-fs-xs); line-height: 1.5; }
.offline-note { margin: 10px 0 0; color: #f8d08a; }
.delete-row { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--ui-border); color: #ffb8b8; font-size: var(--ui-fs-xs); }
.groups-card .button-row { margin-top: 12px; }

.key-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.key-grid button { width: 100%; }
.text-action { margin-top: 14px; padding-top: 2px; }
.text-row { align-items: stretch; flex-wrap: nowrap; }
.text-row .text-input { flex: 1; }
.text-row .primary-button { flex: none; }
.preview-row { justify-content: flex-start; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--ui-border); }
.preview-row span { margin-left: 2px; }
.error-text { margin: 10px 0 0; color: #ffb0b0; font-size: var(--ui-fs-xs); line-height: 1.45; }
.sending-text { color: var(--ui-accent); font-size: var(--ui-fs-xs); }

.leader-toolbar { margin: 12px 0 10px; }
.stream-status { margin-right: auto; display: inline-flex; align-items: center; gap: 7px; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.stream-status::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--ui-text-4); }
.stream-status.live { color: #93e8bd; }
.stream-status.live::before { background: var(--ui-success); box-shadow: 0 0 0 3px rgba(70, 207, 139, 0.15); }
.leader-stage { position: relative; display: flex; align-items: center; justify-content: center; height: clamp(260px, 44vh, 560px); min-height: 220px; overflow: hidden; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); background: #07090c; }
.leader-stage canvas { display: block; width: 100%; height: 100%; min-height: 220px; object-fit: contain; touch-action: none; cursor: pointer; }
.stage-message { position: absolute; inset: 0; display: grid; place-items: center; padding: 20px; color: var(--ui-text-3); font-size: var(--ui-fs-sm); text-align: center; pointer-events: none; }
.touch-note { margin: 10px 0 0; }

.result-meta { justify-content: space-between; color: var(--ui-text-3); font-size: var(--ui-fs-xs); font-family: var(--ui-mono); }
.result-list { display: flex; flex-direction: column; gap: 0; margin-top: 10px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); overflow: hidden; }
.result-row { display: grid; grid-template-columns: minmax(120px, 1fr) auto auto; gap: 10px; align-items: center; padding: 8px 12px; border: 0; border-bottom: 1px solid var(--ui-border); border-radius: 0; background: var(--ui-surface-sunken); font-size: var(--ui-fs-xs); }
.result-row:last-child { border-bottom: 0; }
.result-row strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-1); font-family: var(--ui-mono); font-weight: 500; }
.result-row span { color: #93e8bd; font-weight: 600; }
.result-row.failed { background: rgba(242, 109, 109, 0.06); }
.result-row.failed span { color: #ffb0b0; }
.result-row small { color: var(--ui-text-4); font-family: var(--ui-mono); }
.preview-card { border-color: rgba(240, 180, 76, 0.35); }
.preview-card .result-row span { color: #f8d08a; }

@media (max-width: 1280px) {
  .fleet-columns { grid-template-columns: minmax(300px, 360px) minmax(0, 1fr); }
  .key-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
</style>
