<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { dashboardAuth, lockDashboard } from '@/composables/dashboardAuth'
import {
  proxySession,
  type AppliedMode, type ClientState, type ClientStatus, type Device, type EditorState, type Job,
  type JobAction, type JobResult, type JobStatus, type ProxyType, type PublicProfile,
} from '@/composables/proxySession'
import { looksLikeEndpoint, parseProxyEndpoint } from '@/composables/proxyEndpoint'

type Capabilities = {
  device_wide: boolean
  authentication: string
  supported_types: ProxyType[]
  detection_note: string
}
type ApiPayload = { job?: unknown; jobs?: unknown; profile?: unknown; devices?: unknown; clients?: unknown; total?: unknown }
type DeviceFilter = 'all' | 'online' | 'offline' | 'profile' | 'none' | 'routed' | 'client'
type BulkRow = { line: number; serial: string; label: string; endpoint: string; auth: boolean; error: string; state: 'ready' | 'saved' | 'failed' | 'invalid'; message: string }
type BulkPayload = { proxy_type: ProxyType; host: string; port: number; username: string | null; password: string | null }

// Shared, memory-only session (survives switching dashboard sections; cleared when the dashboard locks or reloads).
const { devices, jobs, clients, clientsCheckedAt, selected, editors, activeSerial } = proxySession
// Access follows the dashboard unlock (server session cookie); there is no separate proxy key.
const unlocked = computed(() => dashboardAuth.phase === 'unlocked')

const capabilities = ref<Capabilities | null>(null)
const capabilitiesUnavailable = ref(false)
const loadingDevices = ref(false)
const loadingClients = ref(false)
const clientsUnavailable = ref(false)
const search = ref('')
const filter = ref<DeviceFilter>('all')
const confirmRoutes = ref(false)
const pageError = ref('')
const pageMessage = ref('')
const alive = ref(true)
const quickPaste = ref('')
const quickPasteError = ref('')
const bulkOpen = ref(false)
const bulkText = ref('')
const bulkMasked = ref(true)
const bulkTarget = ref<'selected' | 'visible'>('selected')
const bulkDefaultType = ref<ProxyType>('http')
const bulkRows = ref<BulkRow[]>([])
const bulkSaving = ref(false)
const bulkSummary = ref('')
// Parsed bulk credentials stay out of reactive/rendered state and are cleared after saving or closing.
let bulkPayloads = new Map<number, BulkPayload>()

let deviceRefreshTimer: ReturnType<typeof setTimeout> | null = null
let deviceController: AbortController | null = null
const pollers = new Map<string, { timer: ReturnType<typeof setTimeout> | null; controller: AbortController | null; attempts: number }>()
const maxJobPolls = 900

const FILTERS: { id: DeviceFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'online', label: 'Online' },
  { id: 'offline', label: 'Offline' },
  { id: 'profile', label: 'With profile' },
  { id: 'none', label: 'No profile' },
  { id: 'routed', label: 'Proxy routed' },
  { id: 'client', label: 'Needs client' },
]

function clientOf(serial: string): ClientState | null {
  return clients.value[serial] ?? null
}
function matchesFilterWith(device: Device, mode: DeviceFilter): boolean {
  switch (mode) {
    case 'online': return device.online
    case 'offline': return !device.online
    case 'profile': return !!device.profile
    case 'none': return !device.profile
    case 'routed': return device.profile?.applied_mode === 'proxy'
    case 'client': { const c = clientOf(device.serial); return !!c && (c.status === 'client_missing' || c.status === 'other_vpn') }
    default: return true
  }
}
function matchesFilter(device: Device): boolean {
  return matchesFilterWith(device, filter.value)
}
const filteredDevices = computed(() => {
  const query = search.value.trim().toLowerCase()
  return devices.value.filter(device => matchesFilter(device) && (!query || `${device.label} ${device.serial} ${device.profile?.host ?? ''}`.toLowerCase().includes(query)))
})
const filterCounts = computed(() => {
  const counts: Record<DeviceFilter, number> = { all: 0, online: 0, offline: 0, profile: 0, none: 0, routed: 0, client: 0 }
  for (const f of FILTERS) counts[f.id] = devices.value.filter(device => matchesFilterWith(device, f.id)).length
  return counts
})
const selectedDevices = computed(() => devices.value.filter(device => selected.value.has(device.serial)))
const selectedCount = computed(() => selected.value.size)
const selectedNames = computed(() => selectedDevices.value.map(device => device.label === device.serial ? device.serial : `${device.label} (${device.serial})`))
const hasVisibleSelection = computed(() => filteredDevices.value.some(device => selected.value.has(device.serial)))
const allVisibleSelected = computed(() => filteredDevices.value.length > 0 && filteredDevices.value.every(device => selected.value.has(device.serial)))
const runningJobs = computed(() => jobs.value.filter(job => job.status === 'queued' || job.status === 'running'))
const activeDevice = computed(() => devices.value.find(device => device.serial === activeSerial.value) ?? null)
const activeEditor = computed<EditorState | null>(() => (activeSerial.value ? editors[activeSerial.value] ?? null : null))
const stats = computed(() => {
  const list = devices.value
  const known = list.filter(device => device.online && clientOf(device.serial) && clientOf(device.serial)!.status !== 'unknown')
  return {
    total: list.length,
    online: list.filter(device => device.online).length,
    profiles: list.filter(device => device.profile).length,
    routed: list.filter(device => device.profile?.applied_mode === 'proxy').length,
    ready: known.filter(device => clientOf(device.serial)!.status === 'ready').length,
    checked: known.length,
  }
})
const prepareTargets = computed(() => devices.value.filter(needsPrepare).map(device => device.serial))
const capabilityText = computed(() => {
  if (capabilitiesUnavailable.value) return 'Setup/status unavailable'
  if (!unlocked.value || !stats.value.checked) return capabilities.value?.device_wide ? 'Clash Meta device-wide mode' : 'Setup required for device-wide mode'
  return `${stats.value.ready}/${stats.value.checked} phones have the client · Apply installs it when missing`
})

function safeFailure(status: number, operation: string): Error {
  if (status === 401) return new Error('Your dashboard session ended. Unlock the dashboard again to continue.')
  if (status === 403) return new Error('Proxy administration is only allowed from the dashboard domain.')
  if (status === 404) return new Error(`${operation} is not available on this server.`)
  if (status === 409) return new Error(`${operation} cannot run while another proxy operation is active.`)
  if (status >= 500) return new Error(`The proxy service could not ${operation.toLowerCase()}.`)
  return new Error(`The proxy service rejected this ${operation.toLowerCase()} request.`)
}

// Only these fixed server messages are ever shown; any other response text is replaced by a generic message.
const SAFE_SERVER_DETAILS = new Map<string, string>([
  ['Invalid device serial', 'This device serial is not valid for proxy management.'],
  ['Invalid label', 'Enter a device label up to 120 characters.'],
  ['Invalid host', 'Enter a valid proxy host.'],
  ['Invalid username', 'The username contains unsupported characters.'],
  ['Invalid password', 'The password contains unsupported characters.'],
  ['Host must not contain a scheme, path, userinfo, or whitespace', 'Enter only the proxy host (no http://, path, user:pass@ or spaces). Paste a full endpoint into Quick paste to split it.'],
  ['Invalid proxy host', 'The proxy host is not a valid hostname or IP address.'],
  ['Profile fields are invalid', 'The profile request was incomplete. Reload the dashboard and try again.'],
  ['Invalid profile request', 'The profile request was incomplete. Reload the dashboard and try again.'],
  ['Unsupported proxy type', 'Choose HTTP, HTTPS or SOCKS5.'],
  ['Invalid proxy port', 'Enter a proxy port from 1 to 65535.'],
  ['Cross-origin proxy administration denied', 'Proxy administration is only allowed from the dashboard domain.'],
  ['Dashboard unlock required', 'Your dashboard session ended. Unlock the dashboard again to continue.'],
  ['Profile not found', 'No saved profile exists for this device.'],
  ['Disable routing and wait for active jobs before deleting this profile', 'Disable routing and wait for active jobs before deleting this profile.'],
  ['Job fields are invalid', 'The job request was incomplete. Reload the dashboard and try again.'],
  ['Invalid job request', 'The job request was incomplete. Reload the dashboard and try again.'],
  ['Unsupported job action', 'This job action is not supported.'],
  ['Select at least one device', 'Select at least one device.'],
  ['Explicit confirmation is required for route changes', 'Confirm the route-changing action before continuing.'],
  ['Explicit confirmation is required for device changes', 'Confirm the action before continuing.'],
  ['One or more selected devices are not in the registry', 'One or more selected devices are no longer known. Refresh devices and try again.'],
  ['A proxy job is already active for a selected device', 'A proxy job is already running for a selected device. Wait for it to finish.'],
  ['Wait for active proxy jobs to finish', 'Too many proxy jobs are running. Wait for them to finish.'],
  ['Job not found', 'This job is no longer available on the server (the service may have restarted).'],
])

async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  // Authenticated by the HttpOnly dashboard session cookie sent with same-origin requests.
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  let response: Response
  try {
    response = await fetch(path, { ...init, headers, cache: 'no-store', credentials: 'same-origin' })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('The proxy service could not be reached.')
  }
  if (!response.ok) {
    let known: string | undefined
    try {
      const failure = asRecord(await response.json() as unknown)
      known = SAFE_SERVER_DETAILS.get(asString(failure.detail))
    } catch { known = undefined }
    if (known) throw new Error(known)
    throw safeFailure(response.status, path.includes('/jobs') ? 'job request' : 'proxy request')
  }
  if (response.status === 204) return null
  try {
    return await response.json() as unknown
  } catch {
    throw new Error('The proxy service returned an invalid response.')
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : {}
}
function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}
function asBoolean(value: unknown): boolean {
  return value === true
}
function asProxyType(value: unknown): ProxyType {
  return value === 'https' || value === 'socks5' ? value : 'http'
}
function asDevice(value: unknown): Device | null {
  const item = asRecord(value)
  const serial = asString(item.serial).trim()
  if (!serial) return null
  const rawProfile = item.profile
  const profileRecord = rawProfile && typeof rawProfile === 'object' ? asRecord(rawProfile) : null
  const profile: PublicProfile | null = profileRecord ? {
    serial,
    label: asString(profileRecord.label, asString(item.label, serial)),
    proxy_type: asProxyType(profileRecord.proxy_type),
    host: asString(profileRecord.host),
    port: typeof profileRecord.port === 'number' ? profileRecord.port : Number(profileRecord.port) || 0,
    username_set: asBoolean(profileRecord.username_set),
    password_set: asBoolean(profileRecord.password_set),
    desired_mode: profileRecord.desired_mode === 'proxy' ? 'proxy' : 'normal',
    applied_mode: profileRecord.applied_mode === 'proxy' || profileRecord.applied_mode === 'normal' ? profileRecord.applied_mode : 'unknown',
    last_result: profileRecord.last_result && typeof profileRecord.last_result === 'object' ? profileRecord.last_result as Record<string, unknown> : null,
    updated_at: asString(profileRecord.updated_at),
  } : null
  return { serial, label: asString(item.label, serial), online: item.online === true, profile }
}
function asJob(value: unknown): Job | null {
  const item = asRecord(value)
  const action = item.action === 'apply' || item.action === 'disable' || item.action === 'prepare' ? item.action : 'test'
  const rawStatus = item.status
  const status: JobStatus = rawStatus === 'running' || rawStatus === 'completed' || rawStatus === 'failed' ? rawStatus : 'queued'
  const rawDevices = Array.isArray(item.devices) ? item.devices : []
  const rawResults = Array.isArray(item.results) ? item.results : []
  const results: JobResult[] = rawResults.map(raw => {
    const result = asRecord(raw)
    const output: JobResult = { serial: asString(result.serial), ok: result.ok === true, status: asString(result.status), message: asString(result.message, 'No result message was provided.') }
    if (typeof result.exit_ip === 'string' && result.exit_ip) output.exit_ip = result.exit_ip
    if (typeof result.verified_device === 'boolean') output.verified_device = result.verified_device
    if (typeof result.installed_now === 'boolean') output.installed_now = result.installed_now
    if (typeof result.version === 'string' && result.version) output.version = result.version.slice(0, 40)
    return output
  })
  const id = asString(item.id)
  if (!id) return null
  return { id, action, status, devices: rawDevices.map(value => String(value)), results, created_at: asString(item.created_at) }
}
function asClient(value: unknown): ClientState | null {
  const item = asRecord(value)
  const serial = asString(item.serial)
  if (!serial) return null
  const known: ClientStatus[] = ['ready', 'client_missing', 'other_vpn', 'unknown', 'offline']
  const status = known.find(entry => entry === item.status) ?? 'unknown'
  const consent = typeof item.vpn_consent === 'boolean' ? item.vpn_consent : null
  return { serial, status, message: asString(item.message).slice(0, 200), vpn_consent: consent }
}

function replaceJob(job: Job) {
  const index = jobs.value.findIndex(item => item.id === job.id)
  if (index < 0) jobs.value.unshift(job)
  else jobs.value[index] = job
}

async function loadCapabilities() {
  try {
    const payload = asRecord(await request('/api/proxy/capabilities'))
    const types: ProxyType[] = Array.isArray(payload.supported_types) ? payload.supported_types.filter((value): value is ProxyType => value === 'http' || value === 'https' || value === 'socks5') : ['http', 'https', 'socks5']
    capabilities.value = {
      device_wide: payload.device_wide === true,
      authentication: asString(payload.authentication, 'dashboard-session'),
      supported_types: types,
      detection_note: asString(payload.detection_note, 'Detection is limited to the configured proxy and cannot prove anonymity or a mobile/carrier IP.'),
    }
    capabilitiesUnavailable.value = false
  } catch {
    capabilities.value = null
    capabilitiesUnavailable.value = true
  }
}

function cancelDeviceRefresh() {
  if (deviceRefreshTimer) clearTimeout(deviceRefreshTimer)
  deviceRefreshTimer = null
  deviceController?.abort()
  deviceController = null
}

function scheduleDeviceRefresh() {
  if (!alive.value || !unlocked.value) return
  if (deviceRefreshTimer) clearTimeout(deviceRefreshTimer)
  deviceRefreshTimer = setTimeout(() => {
    deviceRefreshTimer = null
    void loadDevices()
  }, 15000)
}

function applyDeviceList(rawDevices: unknown[]) {
  const next = rawDevices.map(asDevice).filter((device): device is Device => device !== null)
  devices.value = next
  const known = new Set(next.map(device => device.serial))
  selected.value = new Set(Array.from(selected.value).filter(serial => known.has(serial)))
  if (activeSerial.value && !known.has(activeSerial.value)) activeSerial.value = ''
}

async function loadDevices() {
  if (!unlocked.value || !alive.value) return
  deviceController?.abort()
  const controller = new AbortController()
  deviceController = controller
  loadingDevices.value = true
  try {
    const payload = asRecord(await request('/api/proxy/devices', { signal: controller.signal })) as ApiPayload
    applyDeviceList(Array.isArray(payload.devices) ? payload.devices : [])
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError') && unlocked.value) pageError.value = error instanceof Error ? error.message : 'Could not refresh proxy devices.'
  } finally {
    if (deviceController === controller) {
      deviceController = null
      loadingDevices.value = false
      // The refresh loop always continues (previously an aborted refresh silently ended it).
      scheduleDeviceRefresh()
    }
  }
}

async function loadClients(refresh = false) {
  if (!unlocked.value || !alive.value || loadingClients.value) return
  loadingClients.value = true
  try {
    const payload = asRecord(await request(refresh ? '/api/proxy/clients?refresh=1' : '/api/proxy/clients')) as ApiPayload
    const next: Record<string, ClientState> = {}
    for (const raw of Array.isArray(payload.clients) ? payload.clients : []) {
      const client = asClient(raw)
      if (client) next[client.serial] = client
    }
    clients.value = next
    clientsCheckedAt.value = asString(asRecord(payload).checked_at)
    clientsUnavailable.value = false
  } catch {
    // Older servers have no readiness endpoint; readiness then stays "Not checked".
    clientsUnavailable.value = true
  } finally {
    loadingClients.value = false
  }
}

async function loadRecentJobs() {
  if (!unlocked.value || !alive.value) return
  try {
    const payload = asRecord(await request('/api/proxy/jobs')) as ApiPayload
    const recent = (Array.isArray(payload.jobs) ? payload.jobs : []).map(asJob).filter((job): job is Job => job !== null)
    for (const job of recent.slice().reverse()) replaceJob(job)
    jobs.value = jobs.value.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 50)
    for (const job of jobs.value) if (job.status === 'queued' || job.status === 'running') scheduleJobPoll(job.id)
  } catch {
    // Listing is a convenience; per-job polling still works without it.
  }
}

function makeEditor(device: Device): EditorState {
  return {
    label: device.profile?.label || device.label,
    proxy_type: device.profile?.proxy_type || 'http',
    host: device.profile?.host || '',
    port: device.profile?.port ? String(device.profile.port) : '',
    username: '',
    password: '',
    clearUsername: false,
    clearPassword: false,
    busy: false,
    error: '',
    savedMessage: '',
    hint: '',
  }
}

function editorFor(device: Device): EditorState {
  const existing = editors[device.serial]
  if (existing) return existing
  const created = makeEditor(device)
  editors[device.serial] = created
  return created
}

function openEditor(serial: string) {
  const device = devices.value.find(item => item.serial === serial)
  if (!device) return
  const existing = editors[serial]
  // Re-seed from the server unless the user has unsaved typed credentials.
  if (!existing || (!existing.busy && !existing.username && !existing.password)) editors[serial] = makeEditor(device)
  activeSerial.value = serial
  quickPaste.value = ''
  quickPasteError.value = ''
}

function closeEditor() {
  const serial = activeSerial.value
  if (serial && editors[serial] && !editors[serial]!.busy) delete editors[serial]
  activeSerial.value = ''
  quickPaste.value = ''
  quickPasteError.value = ''
}

function toggleSelection(serial: string) {
  const next = new Set(selected.value)
  if (next.has(serial)) next.delete(serial)
  else next.add(serial)
  selected.value = next
}
function selectVisible() {
  const next = new Set(selected.value)
  for (const device of filteredDevices.value) next.add(device.serial)
  selected.value = next
}
function clearSelection() {
  selected.value = new Set()
}
function selectAllVisibleToggle() {
  if (hasVisibleSelection.value) {
    const next = new Set(selected.value)
    for (const device of filteredDevices.value) next.delete(device.serial)
    selected.value = next
  } else selectVisible()
}

function parsePort(value: string): number | null {
  if (!/^\d{1,5}$/.test(value.trim())) return null
  const port = Number(value)
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null
}
function validHost(host: string): boolean {
  return host.length > 0 && host.length <= 253 && !/[\s/@\\]/.test(host) && !host.includes('://') && !/[<>"']/.test(host)
}

function fillFromEndpoint(editor: EditorState, text: string): boolean {
  const parsed = parseProxyEndpoint(text, editor.proxy_type)
  if (!parsed.ok) { quickPasteError.value = parsed.error; return false }
  editor.proxy_type = parsed.value.proxy_type
  editor.host = parsed.value.host
  editor.port = String(parsed.value.port)
  if (parsed.value.username !== null) { editor.username = parsed.value.username; editor.clearUsername = false }
  if (parsed.value.password !== null) { editor.password = parsed.value.password; editor.clearPassword = false }
  quickPasteError.value = ''
  editor.hint = `Endpoint split into ${parsed.value.proxy_type.toUpperCase()} ${parsed.value.host}:${parsed.value.port}${parsed.value.username !== null ? ' with credentials' : ''}. Review, then Save profile only.`
  return true
}

function applyQuickPaste() {
  const editor = activeEditor.value
  if (!editor) return
  if (fillFromEndpoint(editor, quickPaste.value)) quickPaste.value = ''
}

function normalizeHostField(editor: EditorState) {
  if (looksLikeEndpoint(editor.host)) fillFromEndpoint(editor, editor.host)
}

function profileBody(device: Device, editor: EditorState): Record<string, unknown> | null {
  editor.error = ''
  editor.savedMessage = ''
  if (looksLikeEndpoint(editor.host) && !fillFromEndpoint(editor, editor.host)) { editor.error = quickPasteError.value; return null }
  const host = editor.host.trim()
  const port = parsePort(editor.port)
  if (!editor.label.trim() || editor.label.trim().length > 120) { editor.error = 'Enter a device label up to 120 characters.'; return null }
  if (!validHost(host)) { editor.error = 'Enter a proxy host without a scheme, path, userinfo, or whitespace.'; return null }
  if (port === null) { editor.error = 'Enter a proxy port from 1 to 65535.'; return null }
  if (/[^\u0020-\u007e]/.test(editor.username) || /[^\u0020-\u007e]/.test(editor.password)) { editor.error = 'Proxy credentials contain unsupported characters.'; return null }
  return {
    serial: device.serial,
    label: editor.label.trim(),
    proxy_type: editor.proxy_type,
    host,
    port,
    username: editor.clearUsername ? '' : (editor.username || null),
    password: editor.clearPassword ? '' : (editor.password || null),
  }
}

function storeProfile(serial: string, rawProfile: unknown) {
  const index = devices.value.findIndex(item => item.serial === serial)
  if (index < 0) return
  const current = devices.value[index]!
  const profile = asRecord(rawProfile)
  const nextDevice = asDevice({ serial, label: asString(profile.label, current.label), online: current.online, profile })
  if (nextDevice) devices.value[index] = nextDevice
}

async function saveProfile(device: Device) {
  const editor = editorFor(device)
  const body = profileBody(device, editor)
  if (!body) return false
  editor.busy = true
  try {
    const payload = asRecord(await request('/api/proxy/profiles', { method: 'POST', body: JSON.stringify(body) }))
    storeProfile(device.serial, payload.profile)
    editor.savedMessage = 'Saved. Routing was not changed; use Apply to route this device.'
    editor.hint = ''
    editor.clearUsername = false
    editor.clearPassword = false
    editor.username = ''
    editor.password = ''
    pageMessage.value = `Saved proxy profile for ${device.label}. No route was applied.`
    return true
  } catch (error) {
    editor.error = error instanceof Error ? error.message : 'Could not save the proxy profile.'
    return false
  } finally {
    editor.busy = false
  }
}

async function deleteProfile(device: Device) {
  const editor = editorFor(device)
  if (!device.profile) return
  if (device.profile.applied_mode === 'proxy' || device.profile.desired_mode === 'proxy') { editor.error = 'Disable routing and wait for active jobs before deleting this profile.'; return }
  if (!window.confirm(`Delete the saved proxy profile for ${device.label} (${device.serial})? Routing is not changed.`)) return
  editor.busy = true
  editor.error = ''
  try {
    await request(`/api/proxy/profiles/${encodeURIComponent(device.serial)}`, { method: 'DELETE' })
    const index = devices.value.findIndex(item => item.serial === device.serial)
    if (index >= 0) devices.value[index] = { ...devices.value[index]!, profile: null }
    editors[device.serial] = makeEditor(devices.value[index] ?? device)
    pageMessage.value = `Deleted the saved proxy profile for ${device.label}. Routing was not changed.`
  } catch (error) {
    editor.error = error instanceof Error ? error.message : 'Could not delete the proxy profile.'
  } finally {
    const current = editors[device.serial]
    if (current) current.busy = false
  }
}

function clearPoller(id: string) {
  const state = pollers.get(id)
  if (!state) return
  if (state.timer) clearTimeout(state.timer)
  state.controller?.abort()
  pollers.delete(id)
}

function stopAllPolling() {
  cancelDeviceRefresh()
  for (const id of Array.from(pollers.keys())) clearPoller(id)
}

function scheduleJobPoll(id: string) {
  if (!alive.value || !unlocked.value || pollers.has(id)) return
  const state = { timer: null as ReturnType<typeof setTimeout> | null, controller: null as AbortController | null, attempts: 0 }
  pollers.set(id, state)
  state.timer = setTimeout(() => void pollJob(id), 500)
}

async function pollJob(id: string) {
  const state = pollers.get(id)
  if (!state || !alive.value || !unlocked.value) { clearPoller(id); return }
  state.timer = null
  state.attempts += 1
  state.controller = new AbortController()
  try {
    const payload = asRecord(await request(`/api/proxy/jobs/${encodeURIComponent(id)}`, { signal: state.controller.signal }))
    const job = asJob(payload.job)
    if (!job) throw new Error('The proxy service returned an invalid job.')
    replaceJob(job)
    if (job.status === 'completed' || job.status === 'failed') {
      clearPoller(id)
      void loadDevices()
      if (job.action !== 'test') void loadClients(false)
      return
    }
    if (state.attempts >= maxJobPolls) {
      replaceJob({ ...job, status: 'failed', results: job.results.length ? job.results : [{ serial: '', ok: false, status: 'timeout', message: 'Job polling timed out before completion.' }] })
      clearPoller(id)
      return
    }
    state.controller = null
    state.timer = setTimeout(() => void pollJob(id), 1000)
  } catch (error) {
    if (!(error instanceof DOMException && error.name === 'AbortError') && alive.value && unlocked.value) {
      const current = jobs.value.find(item => item.id === id)
      if (current) replaceJob({ ...current, status: 'failed', results: [{ serial: '', ok: false, status: 'error', message: error instanceof Error ? error.message : 'Could not poll this job.' }] })
    }
    clearPoller(id)
  }
}

async function startJob(action: JobAction, targetSerials: string[], confirmed: boolean) {
  pageError.value = ''
  pageMessage.value = ''
  const devicesForJob = Array.from(new Set(targetSerials.filter(serial => devices.value.some(device => device.serial === serial))))
  if (!devicesForJob.length) { pageError.value = 'Select at least one device. Actions never default to all phones.'; return }
  if ((action === 'apply' || action === 'disable') && !confirmed) { pageError.value = `Confirm the route-changing ${action} action before continuing.`; return }
  const missingProfiles = devicesForJob.filter(serial => !devices.value.find(device => device.serial === serial)?.profile)
  if (action !== 'disable' && action !== 'prepare' && missingProfiles.length) { pageError.value = `${missingProfiles.length} selected device${missingProfiles.length === 1 ? ' has' : 's have'} no saved profile. Save a profile first.`; return }
  try {
    const payload = asRecord(await request('/api/proxy/jobs', { method: 'POST', body: JSON.stringify({ action, devices: devicesForJob, confirmed }) }))
    const job = asJob(payload.job)
    if (!job) throw new Error('The proxy service returned an invalid job.')
    replaceJob(job)
    scheduleJobPoll(job.id)
    pageMessage.value = `${jobLabel(job)} started for ${job.devices.length} device${job.devices.length === 1 ? '' : 's'}.`
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : 'Could not start the proxy job.'
  }
}

async function saveAndTest(device: Device) {
  if (await saveProfile(device)) await startJob('test', [device.serial], false)
}

function startSelected(action: JobAction) {
  void startJob(action, Array.from(selected.value), action === 'test' || confirmRoutes.value)
}

function routeActionFor(device: Device, action: 'apply' | 'disable') {
  void startJob(action, [device.serial], confirmRoutes.value)
}

// ---- One-click client setup ----
// Installs the checksum-verified Clash Meta build when missing and pre-approves its
// VPN permission. It never imports a profile, starts the VPN or changes routing,
// and never touches a phone whose VPN belongs to another app.
function needsPrepare(device: Device): boolean {
  if (!device.online) return false
  const client = clientOf(device.serial)
  if (!client) return true
  if (client.status === 'other_vpn' || client.status === 'offline') return false
  return client.status !== 'ready' || client.vpn_consent === false
}
function jobActiveFor(serial: string): boolean {
  return runningJobs.value.some(job => job.devices.includes(serial))
}
function prepareClients(serials: string[]) {
  void startJob('prepare', serials, true)
}

// ---- Bulk import (save-only) ----
function openBulk() {
  bulkOpen.value = true
  bulkTarget.value = selectedCount.value ? 'selected' : 'visible'
  bulkRows.value = []
  bulkSummary.value = ''
}
function closeBulk() {
  bulkOpen.value = false
  bulkText.value = ''
  bulkRows.value = []
  bulkSummary.value = ''
  bulkPayloads = new Map()
}
function buildBulkPreview() {
  bulkSummary.value = ''
  bulkPayloads = new Map()
  const lines = bulkText.value.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'))
  const bySerial = new Map(devices.value.map(device => [device.serial.toLowerCase(), device]))
  const ordered = bulkTarget.value === 'selected' ? selectedDevices.value : filteredDevices.value
  const used = new Set<string>()
  const rows: BulkRow[] = []
  let cursor = 0
  lines.forEach((line, index) => {
    const keyedMatch = /^([^,;\t= ]+)\s*[,;\t= ]\s*(\S+)$/.exec(line)
    const keyedDevice = keyedMatch ? bySerial.get(keyedMatch[1]!.toLowerCase()) : undefined
    let device: Device | undefined
    let endpointText = line
    if (keyedDevice && keyedMatch) { device = keyedDevice; endpointText = keyedMatch[2]! }
    else {
      while (cursor < ordered.length && used.has(ordered[cursor]!.serial)) cursor += 1
      device = ordered[cursor]
      cursor += 1
    }
    if (!device) { rows.push({ line: index + 1, serial: '', label: '', endpoint: '', auth: false, error: 'No device left to assign this line to.', state: 'invalid', message: '' }); return }
    if (used.has(device.serial)) { rows.push({ line: index + 1, serial: device.serial, label: device.label, endpoint: '', auth: false, error: 'This device already has a line in this import.', state: 'invalid', message: '' }); return }
    used.add(device.serial)
    const parsed = parseProxyEndpoint(endpointText, bulkDefaultType.value)
    if (!parsed.ok) { rows.push({ line: index + 1, serial: device.serial, label: device.label, endpoint: '', auth: false, error: parsed.error, state: 'invalid', message: '' }); return }
    bulkPayloads.set(index + 1, parsed.value)
    rows.push({ line: index + 1, serial: device.serial, label: device.label, endpoint: `${parsed.value.proxy_type.toUpperCase()} ${parsed.value.host}:${parsed.value.port}`, auth: parsed.value.username !== null, error: '', state: 'ready', message: '' })
  })
  bulkRows.value = rows
  if (!rows.length) bulkSummary.value = 'Paste one endpoint per line first.'
}
const bulkReadyCount = computed(() => bulkRows.value.filter(row => row.state === 'ready').length)
async function saveBulk() {
  if (bulkSaving.value) return
  bulkSaving.value = true
  let saved = 0
  let failed = 0
  try {
    for (const row of bulkRows.value) {
      if (row.state !== 'ready') continue
      const parsed = bulkPayloads.get(row.line)
      const device = devices.value.find(item => item.serial === row.serial)
      if (!parsed || !device) { row.state = 'failed'; row.message = 'Device is no longer available.'; failed += 1; continue }
      try {
        const body = { serial: device.serial, label: device.profile?.label || device.label, proxy_type: parsed.proxy_type, host: parsed.host, port: parsed.port, username: parsed.username, password: parsed.password }
        const payload = asRecord(await request('/api/proxy/profiles', { method: 'POST', body: JSON.stringify(body) }))
        storeProfile(device.serial, payload.profile)
        if (editors[device.serial] && activeSerial.value !== device.serial) delete editors[device.serial]
        row.state = 'saved'
        row.message = 'Saved'
        saved += 1
      } catch (error) {
        row.state = 'failed'
        row.message = error instanceof Error ? error.message : 'Could not save.'
        failed += 1
      }
    }
  } finally {
    bulkPayloads = new Map()
    bulkText.value = ''
    bulkSaving.value = false
  }
  bulkSummary.value = `Saved ${saved} profile${saved === 1 ? '' : 's'}${failed ? `, ${failed} failed` : ''}. Routing was not changed.`
  if (activeSerial.value) openEditor(activeSerial.value)
}

// ---- Presentation helpers ----
function modeLabel(mode: AppliedMode): string {
  return mode === 'proxy' ? 'Proxy routing' : mode === 'normal' ? 'Normal internet' : 'Unknown / setup required'
}
function jobLabel(job: Job): string {
  if (job.action === 'prepare') return 'Client setup'
  return job.action === 'test' ? 'Server proxy test' : job.action === 'apply' ? 'Device-wide apply' : 'Disable managed routing'
}
function clientLabel(serial: string, online: boolean): { text: string; tone: string; title: string } {
  if (!online) return { text: 'Offline', tone: 'muted', title: 'Device is offline' }
  const client = clientOf(serial)
  if (!client) return { text: clientsUnavailable.value ? 'Unavailable' : 'Not checked', tone: 'muted', title: 'Use Check clients to inspect the managed client (read-only)' }
  if (client.status === 'ready' && client.vpn_consent === false) return { text: 'Needs consent', tone: 'warn', title: 'Prepare client pre-approves the VPN permission (Apply does this too)' }
  if (client.status === 'ready') return { text: 'Client ready', tone: 'ok', title: client.message }
  if (client.status === 'client_missing') return { text: 'Not installed', tone: 'warn', title: 'Prepare client or Apply installs the verified Clash Meta build automatically' }
  if (client.status === 'other_vpn') return { text: 'Other VPN', tone: 'danger', title: client.message }
  return { text: 'Unknown', tone: 'muted', title: client.message || 'Readiness could not be checked' }
}
function lastResult(profile: PublicProfile | null): { ok: boolean; text: string; title: string } | null {
  const result = profile?.last_result
  if (!result) return null
  const ok = result.ok === true
  const message = asString(result.message).slice(0, 160)
  const exitIp = asString(result.exit_ip)
  return { ok, text: ok ? (exitIp ? `OK · exit ${exitIp}` : 'OK') : 'Failed', title: message }
}
function jobProgress(job: Job): number {
  if (!job.devices.length) return 0
  if (job.status === 'completed' || job.status === 'failed') return 100
  return Math.max(6, Math.round((job.results.length / job.devices.length) * 100))
}
function shortTime(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString()
}

watch(unlocked, value => { if (!value) stopAllPolling() })

onMounted(() => {
  // The dashboard is already unlocked when this section renders: load straight away.
  // Returning to the section resumes refresh and re-attaches job progress.
  void loadCapabilities()
  if (unlocked.value) {
    void loadDevices()
    void loadClients(false)
    void loadRecentJobs()
  }
})
onUnmounted(() => {
  alive.value = false
  stopAllPolling()
  quickPaste.value = ''
  closeBulk()
})
</script>

<template>
  <main class="proxy-panel">
    <header class="pm-header">
      <div class="pm-header__copy">
        <p class="eyebrow">D3m0n proxy controls</p>
        <h1>Per-device proxy manager</h1>
        <p class="intro">Profiles are saved per device. Saving never changes routing; Apply and Disable are separate, explicit jobs.</p>
      </div>
      <div class="pm-header__side">
        <span class="capability-badge" :title="capabilities?.detection_note || ''"><UiIcon name="shield" :size="13" />{{ capabilityText }}</span>
        <span class="pm-lock-state is-unlocked" :title="dashboardAuth.local ? 'Direct local access on the phone host' : 'Unlocked by the dashboard password for this browser'"><UiIcon name="unlock" :size="13" />{{ dashboardAuth.local ? 'Local access' : 'Unlocked · dashboard session' }}</span>
        <button v-if="!dashboardAuth.local" type="button" class="quiet-button" title="Lock the whole dashboard and clear proxy drafts from memory" @click="lockDashboard"><UiIcon name="lock" :size="13" />Lock dashboard</button>
      </div>
    </header>

    <section class="public-warning" role="note">
      <UiIcon name="alert" :size="14" />
      <p><strong>Protected by the dashboard password.</strong> Proxy credentials are encrypted on the server and never rendered back or kept in browser storage. Locking the dashboard clears unsaved drafts from memory. Routing changes only through explicit Apply / Disable jobs.</p>
    </section>

    <div v-if="pageMessage || pageError" class="pm-status-line">
      <p v-if="pageMessage" class="success-text" aria-live="polite"><UiIcon name="check" :size="13" />{{ pageMessage }}</p>
      <p v-if="pageError" class="error-text" role="alert"><UiIcon name="alert" :size="13" />{{ pageError }}</p>
    </div>

    <!-- Proxy workspace (the dashboard is unlocked whenever this renders) -->
    <template v-if="unlocked">
      <section class="pm-kpis" aria-label="Proxy fleet summary">
        <div class="pm-kpi"><span>Devices</span><b>{{ stats.total }}</b><small>live + saved registry</small></div>
        <div class="pm-kpi"><span>Online</span><b>{{ stats.online }}</b><small>connected now</small></div>
        <div class="pm-kpi"><span>Profiles saved</span><b>{{ stats.profiles }}</b><small>{{ stats.total - stats.profiles }} without profile</small></div>
        <div class="pm-kpi"><span>Proxy routed</span><b>{{ stats.routed }}</b><small>applied on device</small></div>
        <div class="pm-kpi" :class="{ 'is-warn': stats.checked && stats.ready < stats.checked }"><span>Client ready</span><b>{{ stats.checked ? `${stats.ready}/${stats.checked}` : '—' }}</b><small>{{ clientsCheckedAt ? `checked ${shortTime(clientsCheckedAt)}` : 'not checked yet' }}</small></div>
        <div class="pm-kpi"><span>Active jobs</span><b>{{ runningJobs.length }}</b><small>{{ jobs.length }} in this session</small></div>
      </section>

      <section class="pm-workspace" :class="{ 'has-inspector': !!activeDevice }">
        <div class="pm-main">
          <div class="panel-card pm-devices selection-card" aria-labelledby="devices-heading">
            <div class="pm-card-head">
              <div>
                <h2 id="devices-heading">Devices</h2>
                <p class="muted">{{ devices.length }} saved/live device{{ devices.length === 1 ? '' : 's' }} · {{ selectedCount }} selected · new phones appear automatically</p>
              </div>
              <div class="pm-card-head__actions">
                <button type="button" class="quiet-button" :disabled="loadingDevices" @click="loadDevices()"><UiIcon name="refresh" :size="13" />{{ loadingDevices ? 'Refreshing…' : 'Refresh devices' }}</button>
                <button type="button" class="quiet-button" :disabled="loadingClients" title="Read-only check of the managed client on each online phone" @click="loadClients(true)"><UiIcon name="shield" :size="13" />{{ loadingClients ? 'Checking…' : 'Check clients' }}</button>
                <button type="button" class="quiet-button" @click="openBulk"><UiIcon name="layers" :size="13" />Bulk import</button>
                <button type="button" class="primary-button pm-prepare-all" :disabled="!prepareTargets.length || runningJobs.length > 0" :title="prepareTargets.length ? 'One click: install the verified Clash Meta client where missing and pre-approve its VPN permission. Routing is not changed.' : 'Every online phone already has the client ready'" @click="prepareClients(prepareTargets)"><UiIcon name="download" :size="13" />Prepare all clients<span class="pm-count">{{ prepareTargets.length }}</span></button>
              </div>
            </div>

            <div class="pm-toolbar">
              <div class="pm-search">
                <UiIcon name="search" :size="14" />
                <input id="proxy-device-search" v-model="search" class="text-input" type="search" autocomplete="off" aria-label="Search label, serial, or proxy host" placeholder="Search label, serial or proxy host" />
              </div>
              <div class="pm-filters" role="group" aria-label="Filter devices">
                <button v-for="f in FILTERS" :key="f.id" type="button" class="pm-chip" :class="{ 'is-active': filter === f.id }" :aria-pressed="filter === f.id" @click="filter = f.id">{{ f.label }}<span>{{ filterCounts[f.id] }}</span></button>
              </div>
            </div>

            <div class="pm-bulkbar actions-card" :class="{ 'is-active': selectedCount > 0 }" aria-labelledby="actions-heading">
              <div class="pm-bulkbar__info">
                <strong id="actions-heading">{{ selectedCount ? `${selectedCount} selected` : 'Selected-device jobs' }}</strong>
                <small v-if="selectedNames.length" class="selected-targets" :title="selectedNames.join(', ')">Targets: {{ selectedNames.join(', ') }}</small>
                <small v-else class="muted">Select devices before starting a job. Actions never default to all phones.</small>
              </div>
              <div class="pm-bulkbar__actions job-actions">
                <button type="button" class="quiet-button" :disabled="!selectedCount || runningJobs.length > 0" @click="startSelected('test')"><UiIcon name="flask" :size="13" />Test selected (server preflight)</button>
                <button type="button" class="quiet-button" :disabled="!selectedCount || runningJobs.length > 0" title="Install the client where missing and pre-approve VPN permission (routing unchanged)" @click="prepareClients(Array.from(selected))"><UiIcon name="download" :size="13" />Prepare selected</button>
                <span class="ui-vdivider" aria-hidden="true"></span>
                <label class="confirm-box" title="I understand Apply changes device routing and Disable returns this managed routing to normal internet. These are not save-only actions."><input v-model="confirmRoutes" type="checkbox" /><span>Allow route changes</span></label>
                <button type="button" class="primary-button" :disabled="!selectedCount || !confirmRoutes || runningJobs.length > 0" @click="startSelected('apply')"><UiIcon name="bolt" :size="13" />Apply selected routing</button>
                <button type="button" class="danger-button" :disabled="!selectedCount || !confirmRoutes || runningJobs.length > 0" @click="startSelected('disable')"><UiIcon name="stop" :size="12" />Disable selected routing</button>
                <button type="button" class="quiet-button pm-icon-btn" :disabled="!selectedCount" title="Clear selection" aria-label="Clear selection" @click="clearSelection"><UiIcon name="close" :size="13" /></button>
              </div>
              <p v-if="confirmRoutes" class="pm-bulkbar__note">I understand Apply changes device routing and Disable returns this managed routing to normal internet. These are not save-only actions.</p>
            </div>

            <p v-if="!devices.length" class="pm-empty">No live or saved devices are available yet. Newly connected devices appear on refresh.</p>
            <div v-else class="pm-table-wrap device-list" role="group" aria-label="Proxy device selection">
              <table class="pm-table">
                <thead>
                  <tr>
                    <th class="col-check"><input type="checkbox" :checked="allVisibleSelected" :indeterminate="hasVisibleSelection && !allVisibleSelected" :disabled="!filteredDevices.length" aria-label="Select visible devices" @change="selectAllVisibleToggle" /></th>
                    <th>Device</th>
                    <th>Status</th>
                    <th>Client</th>
                    <th>Proxy endpoint</th>
                    <th>Routing</th>
                    <th>Last result</th>
                    <th class="col-actions"><span class="visually-hidden">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="device in filteredDevices" :key="device.serial" class="device-card" :class="{ offline: !device.online, 'is-active': activeSerial === device.serial, 'is-selected': selected.has(device.serial) }" @click="openEditor(device.serial)">
                    <td class="col-check" @click.stop><input type="checkbox" :checked="selected.has(device.serial)" :aria-label="`Select ${device.serial}`" @change="toggleSelection(device.serial)" /></td>
                    <td class="col-device"><strong>{{ device.label }}</strong><small>{{ device.serial }}</small></td>
                    <td><span class="pm-pill" :class="device.online ? 'pm-pill--ok' : 'pm-pill--muted'"><i></i>{{ device.online ? 'Online' : 'Offline' }}</span></td>
                    <td><span class="pm-pill" :class="`pm-pill--${clientLabel(device.serial, device.online).tone}`" :title="clientLabel(device.serial, device.online).title">{{ clientLabel(device.serial, device.online).text }}</span></td>
                    <td class="col-endpoint">
                      <template v-if="device.profile"><span class="pm-type">{{ device.profile.proxy_type.toUpperCase() }}</span><code>{{ device.profile.host }}:{{ device.profile.port }}</code><UiIcon v-if="device.profile.username_set || device.profile.password_set" name="key" :size="12" class="pm-cred" title="Credentials saved (never shown)" /></template>
                      <span v-else class="muted">No profile</span>
                    </td>
                    <td><span v-if="device.profile" class="pm-pill mode-state" :class="`mode-${device.profile.applied_mode}`">{{ modeLabel(device.profile.applied_mode) }}</span><span v-else class="muted">—</span></td>
                    <td class="col-result"><span v-if="lastResult(device.profile)" :class="lastResult(device.profile)!.ok ? 'pm-ok' : 'pm-fail'" :title="lastResult(device.profile)!.title">{{ lastResult(device.profile)!.text }}</span><span v-else class="muted">—</span></td>
                    <td class="col-actions"><button type="button" class="quiet-button pm-edit" :aria-label="`Edit profile ${device.serial}`" @click.stop="openEditor(device.serial)"><UiIcon name="edit" :size="12" />{{ device.profile ? 'Edit' : 'Set up' }}</button></td>
                  </tr>
                </tbody>
              </table>
              <p v-if="!filteredDevices.length" class="pm-empty">No devices match this search or filter.</p>
            </div>
          </div>

          <section v-if="jobs.length" class="panel-card jobs-card" aria-labelledby="jobs-heading">
            <div class="pm-card-head">
              <div><h2 id="jobs-heading">Job progress</h2><p class="muted">Polling stops after a bounded window, on completion, when the dashboard locks, or when you leave this section (it resumes on return).</p></div>
            </div>
            <div class="job-list">
              <details v-for="job in jobs" :key="job.id" class="job-item" :open="job.status === 'queued' || job.status === 'running' || job.status === 'failed'">
                <summary class="job-head">
                  <strong>{{ jobLabel(job) }}</strong>
                  <span class="job-status" :class="`status-${job.status}`">{{ job.status }}</span>
                  <small>{{ job.devices.length }} device{{ job.devices.length === 1 ? '' : 's' }} · {{ shortTime(job.created_at) }}</small>
                  <span class="progress-track" :class="{ 'is-live': job.status === 'queued' || job.status === 'running' }" aria-label="Job progress"><span :style="{ width: `${jobProgress(job)}%` }"></span></span>
                </summary>
                <div v-if="job.results.length" class="result-list">
                  <div v-for="(result, index) in job.results" :key="`${job.id}-${result.serial}-${index}`" class="result-row" :class="{ failed: !result.ok }">
                    <code>{{ result.serial || 'Job' }}</code>
                    <span class="result-state">{{ result.ok ? 'Succeeded' : 'Failed' }}</span>
                    <small>{{ result.message }}</small>
                    <small v-if="result.exit_ip">Proxy exit IP: {{ result.exit_ip }}</small>
                    <small v-if="result.ok && job.action === 'test'">Server-side test only · phone routing not verified</small>
                    <small v-if="result.ok && job.action === 'prepare'">{{ result.installed_now ? `Installed ${result.version || 'Clash Meta'}` : 'Client already installed' }} · VPN permission pre-approved · routing unchanged</small>
                    <small v-if="result.ok && job.action === 'apply'">{{ result.verified_device === true ? 'Device-side routing verified' : 'Apply returned without device verification · setup required' }}</small>
                  </div>
                </div>
                <p v-else class="muted job-waiting">Waiting for per-device results…</p>
              </details>
            </div>
          </section>
        </div>

        <!-- Inspector -->
        <aside class="pm-inspector" aria-label="Device proxy profile">
          <template v-if="activeDevice && activeEditor">
            <div class="pm-inspector__head">
              <div>
                <p class="eyebrow">Proxy profile</p>
                <h2>{{ activeDevice.label }}</h2>
                <code>{{ activeDevice.serial }}</code>
              </div>
              <button type="button" class="quiet-button pm-icon-btn" aria-label="Close editor" title="Close editor" @click="closeEditor"><UiIcon name="close" :size="14" /></button>
            </div>
            <div class="pm-inspector__pills">
              <span class="pm-pill" :class="activeDevice.online ? 'pm-pill--ok' : 'pm-pill--muted'"><i></i>{{ activeDevice.online ? 'Online' : 'Offline · saved registry' }}</span>
              <span class="pm-pill" :class="`pm-pill--${clientLabel(activeDevice.serial, activeDevice.online).tone}`">{{ clientLabel(activeDevice.serial, activeDevice.online).text }}</span>
              <span v-if="activeDevice.profile" class="pm-pill mode-state" :class="`mode-${activeDevice.profile.applied_mode}`">{{ modeLabel(activeDevice.profile.applied_mode) }}</span>
            </div>
            <div v-if="activeDevice.online && (clientOf(activeDevice.serial)?.status === 'client_missing' || clientOf(activeDevice.serial)?.vpn_consent === false)" class="pm-callout pm-callout--warn pm-callout--action">
              <span>{{ clientOf(activeDevice.serial)?.status === 'client_missing' ? 'Clash Meta is not installed on this phone yet.' : 'Clash Meta still needs its VPN permission.' }} One click installs the checksum-verified build and pre-approves the VPN permission; Apply routing does this automatically too.</span>
              <button type="button" class="primary-button" :disabled="jobActiveFor(activeDevice.serial)" @click="prepareClients([activeDevice.serial])"><UiIcon name="download" :size="13" />{{ jobActiveFor(activeDevice.serial) ? 'Working…' : 'Prepare client' }}</button>
            </div>
            <p v-else-if="clientOf(activeDevice.serial)?.status === 'other_vpn'" class="pm-callout pm-callout--danger">Another app owns this phone's always-on VPN. Apply will not take it over.</p>

            <div class="pm-quick">
              <label class="field-label" for="proxy-quick-paste">Quick paste endpoint</label>
              <div class="pm-quick__row">
                <input id="proxy-quick-paste" v-model="quickPaste" class="text-input" type="password" autocomplete="new-password" spellcheck="false" placeholder="http://user:pass@host:port · host:port:user:pass" @keydown.enter.prevent="applyQuickPaste" />
                <button type="button" class="quiet-button" :disabled="!quickPaste" @click="applyQuickPaste">Fill</button>
              </div>
              <small v-if="quickPasteError" class="error-text">{{ quickPasteError }}</small>
              <small v-else class="muted">Parsed locally into the fields below; credentials stay hidden.</small>
            </div>

            <form class="editor" @submit.prevent="saveProfile(activeDevice)">
              <div class="editor-grid">
                <div class="wide-field"><label class="field-label" :for="`label-${activeDevice.serial}`">Label</label><input :id="`label-${activeDevice.serial}`" v-model="activeEditor.label" class="text-input" maxlength="120" autocomplete="off" /></div>
                <div><label class="field-label" :for="`type-${activeDevice.serial}`">Proxy type</label><select :id="`type-${activeDevice.serial}`" v-model="activeEditor.proxy_type" class="text-input"><option v-for="type in (capabilities?.supported_types || ['http', 'https', 'socks5'])" :key="type" :value="type">{{ type.toUpperCase() }}</option></select></div>
                <div><label class="field-label" :for="`port-${activeDevice.serial}`">Port</label><input :id="`port-${activeDevice.serial}`" v-model="activeEditor.port" class="text-input" inputmode="numeric" autocomplete="off" placeholder="8080" /></div>
                <div class="wide-field"><label class="field-label" :for="`host-${activeDevice.serial}`">Host</label><input :id="`host-${activeDevice.serial}`" v-model="activeEditor.host" class="text-input mono" autocomplete="off" spellcheck="false" placeholder="proxy.example.net" @blur="normalizeHostField(activeEditor)" /></div>
                <div class="wide-field"><label class="field-label" :for="`username-${activeDevice.serial}`">Username <small v-if="activeDevice.profile?.username_set" class="credential-hint">saved · blank preserves</small></label><input :id="`username-${activeDevice.serial}`" v-model="activeEditor.username" class="text-input" type="text" autocomplete="new-password" spellcheck="false" placeholder="Leave blank to preserve" /></div>
                <div class="wide-field"><label class="field-label" :for="`password-${activeDevice.serial}`">Password <small v-if="activeDevice.profile?.password_set" class="credential-hint">saved · blank preserves</small></label><input :id="`password-${activeDevice.serial}`" v-model="activeEditor.password" class="text-input" type="password" autocomplete="new-password" placeholder="Leave blank to preserve" /></div>
              </div>
              <div class="clear-options"><label><input v-model="activeEditor.clearUsername" type="checkbox" /> Clear saved username</label><label><input v-model="activeEditor.clearPassword" type="checkbox" /> Clear saved password</label></div>
              <p class="editor-note">Credentials are sent only over this admin request. Blank fields preserve an existing credential; use the clear boxes to explicitly remove one.</p>
              <p v-if="activeEditor.hint" class="pm-callout">{{ activeEditor.hint }}</p>
              <div class="button-row">
                <button type="submit" class="primary-button" :disabled="activeEditor.busy">{{ activeEditor.busy ? 'Saving…' : 'Save profile only' }}</button>
                <button type="button" class="quiet-button" :disabled="activeEditor.busy" @click="saveAndTest(activeDevice)"><UiIcon name="flask" :size="13" />Save and Test</button>
              </div>
              <p v-if="activeEditor.savedMessage" class="success-text">{{ activeEditor.savedMessage }}</p>
              <p v-if="activeEditor.error" class="error-text" role="alert">{{ activeEditor.error }}</p>
            </form>

            <div class="pm-route">
              <h3>Routing on this phone</h3>
              <p class="muted pm-route__hint">Apply runs the server test, installs the client if needed, then imports and starts the profile and verifies the phone's exit IP.</p>
              <label class="confirm-box"><input v-model="confirmRoutes" type="checkbox" /> <span>I understand Apply changes device routing and Disable returns this managed routing to normal internet. These are not save-only actions.</span></label>
              <div class="button-row">
                <button type="button" class="quiet-button" :disabled="!confirmRoutes || activeEditor.busy || !activeDevice.profile" @click="routeActionFor(activeDevice, 'apply')"><UiIcon name="bolt" :size="13" />Apply routing</button>
                <button type="button" class="danger-button" :disabled="!confirmRoutes || activeEditor.busy" @click="routeActionFor(activeDevice, 'disable')">Disable routing</button>
              </div>
            </div>

            <div v-if="activeDevice.profile" class="pm-inspector__foot">
              <div v-if="lastResult(activeDevice.profile)" class="pm-last">
                <span :class="lastResult(activeDevice.profile)!.ok ? 'pm-ok' : 'pm-fail'">{{ lastResult(activeDevice.profile)!.text }}</span>
                <small>{{ lastResult(activeDevice.profile)!.title }}</small>
              </div>
              <button type="button" class="quiet-button pm-delete" :disabled="activeEditor.busy || activeDevice.profile.applied_mode === 'proxy'" :title="activeDevice.profile.applied_mode === 'proxy' ? 'Disable routing before deleting' : 'Delete the saved profile (routing is not changed)'" @click="deleteProfile(activeDevice)"><UiIcon name="trash" :size="13" />Delete profile</button>
            </div>
          </template>
          <template v-else>
            <div class="pm-inspector__empty">
              <div class="pm-lock-icon"><UiIcon name="sliders" :size="20" /></div>
              <h2>Select a device</h2>
              <p class="muted">Click any row to edit its proxy profile, or use Bulk import to assign one endpoint per line to many phones at once. Saving is always save-only.</p>
            </div>
            <div class="pm-inspector__section">
              <h3>Truthful detection boundary</h3>
              <p class="muted">{{ capabilities?.detection_note || 'Detection can report the configured proxy exit only; it cannot prove anonymity, a genuine mobile/carrier IP, hidden VPN state, or spoofed fingerprints.' }}</p>
              <ul class="pm-facts">
                <li><strong>Test</strong> checks proxy authentication/HTTPS reachability on the server (server preflight).</li>
                <li><strong>Apply</strong> needs Clash Meta on the phone; only a result marked device-verified represents phone-side routing. Unsupported devices report setup required.</li>
                <li><strong>Endpoints</strong> must be public; LAN/private proxy addresses are rejected by design.</li>
                <li><strong>HTTP global proxy</strong> settings alone are not all-app routing; device-wide mode uses the Clash Meta VPN client.</li>
              </ul>
            </div>
          </template>
        </aside>
      </section>
    </template>

    <!-- Bulk import -->
    <div v-if="bulkOpen" class="pm-dialog-backdrop" @click.self="closeBulk" @keydown.esc="closeBulk">
      <section class="pm-dialog" role="dialog" aria-modal="true" aria-labelledby="bulk-heading">
        <header class="pm-dialog__head">
          <div><p class="eyebrow">Save-only</p><h2 id="bulk-heading">Bulk import proxy profiles</h2></div>
          <button type="button" class="quiet-button pm-icon-btn" aria-label="Close bulk import" @click="closeBulk"><UiIcon name="close" :size="14" /></button>
        </header>
        <div class="pm-dialog__body">
          <div class="pm-dialog__form">
            <p class="muted">One endpoint per line. Lines are assigned in order to the target devices, or start a line with a device serial and a comma to target it exactly (e.g. <code>192.0.2.40:5555,host:port:user:pass</code>). Lines starting with # are ignored.</p>
            <div class="pm-dialog__options">
              <label class="field-label" for="bulk-target">Assign lines to</label>
              <select id="bulk-target" v-model="bulkTarget" class="text-input"><option value="selected" :disabled="!selectedCount">Selected devices ({{ selectedCount }})</option><option value="visible">Visible devices ({{ filteredDevices.length }})</option></select>
              <label class="field-label" for="bulk-type">Type when no scheme is given</label>
              <select id="bulk-type" v-model="bulkDefaultType" class="text-input"><option value="http">HTTP</option><option value="https">HTTPS</option><option value="socks5">SOCKS5</option></select>
            </div>
            <label class="field-label" for="bulk-text">Endpoints</label>
            <textarea id="bulk-text" v-model="bulkText" class="text-input pm-bulk-text" :class="{ 'is-masked': bulkMasked }" rows="9" spellcheck="false" autocomplete="off" placeholder="http://user:pass@proxy1.example:8080&#10;proxy2.example:3128:user:pass&#10;socks5://proxy3.example:1080"></textarea>
            <label class="pm-inline-check"><input v-model="bulkMasked" type="checkbox" /> Mask endpoints while typing</label>
          </div>
          <div class="pm-dialog__preview">
            <div class="pm-dialog__preview-head"><h3>Preview</h3><small class="muted">{{ bulkRows.length ? `${bulkReadyCount} ready · ${bulkRows.length - bulkReadyCount} skipped` : 'Nothing parsed yet' }}</small></div>
            <div class="pm-preview-table">
              <div v-for="row in bulkRows" :key="row.line" class="pm-preview-row" :class="`is-${row.state}`">
                <span class="pm-preview-line">#{{ row.line }}</span>
                <span class="pm-preview-device"><strong>{{ row.label || '—' }}</strong><small>{{ row.serial }}</small></span>
                <span class="pm-preview-endpoint">{{ row.endpoint || row.error }}<UiIcon v-if="row.auth" name="key" :size="11" /></span>
                <span class="pm-preview-state">{{ row.state === 'ready' ? 'Ready' : row.state === 'saved' ? 'Saved' : row.state === 'failed' ? row.message : 'Skipped' }}</span>
              </div>
              <p v-if="!bulkRows.length" class="pm-empty">Preview shows which phone each line will be saved to before anything is sent.</p>
            </div>
            <p v-if="bulkSummary" class="success-text">{{ bulkSummary }}</p>
          </div>
        </div>
        <footer class="pm-dialog__foot">
          <small class="muted">Saves profiles only. Routing is not changed until you Apply.</small>
          <span class="ui-toolbar__spacer"></span>
          <button type="button" class="quiet-button" :disabled="!bulkText.trim() || bulkSaving" @click="buildBulkPreview">Preview assignment</button>
          <button type="button" class="primary-button" :disabled="!bulkReadyCount || bulkSaving" @click="saveBulk">{{ bulkSaving ? 'Saving…' : `Save ${bulkReadyCount} profile${bulkReadyCount === 1 ? '' : 's'} only` }}</button>
        </footer>
      </section>
    </div>
  </main>
</template>

<style scoped>
.proxy-panel { box-sizing: border-box; display: flex; flex-direction: column; gap: 14px; padding: 0 0 28px; color: var(--ui-text-2); font-size: var(--ui-fs-sm); min-width: 0; }
.visually-hidden { position: absolute !important; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.eyebrow { margin: 0; color: var(--ui-accent); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.1em; text-transform: uppercase; }
.muted { color: var(--ui-text-3); font-size: var(--ui-fs-xs); line-height: 1.5; margin: 0; }
code, .mono { font-family: var(--ui-mono); }
h2 { margin: 0; color: var(--ui-text-1); font-size: var(--ui-fs-md); font-weight: 650; }
h3 { margin: 0 0 8px; color: var(--ui-text-1); font-size: var(--ui-fs-sm); font-weight: 650; }

/* Header */
.pm-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; padding: 4px 2px 0; }
.pm-header h1 { margin: 4px 0 2px; color: var(--ui-text-1); font-size: 20px; font-weight: 650; letter-spacing: -0.01em; }
.intro { margin: 0; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.pm-header__side { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.capability-badge, .pm-lock-state { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 11px; border-radius: 999px; font-size: var(--ui-fs-2xs); font-weight: 600; white-space: nowrap; }
.capability-badge { border: 1px solid var(--ui-accent-line); background: var(--ui-accent-soft); color: #93ebd2; }
.pm-lock-state { border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-2); }
.pm-lock-state.is-unlocked { border-color: rgba(240, 180, 76, 0.45); background: var(--ui-warn-soft); color: #f8d08a; }

.public-warning { display: flex; align-items: flex-start; gap: 10px; padding: 10px 14px; border: 1px solid rgba(240, 180, 76, 0.28); border-left: 3px solid var(--ui-warn); border-radius: var(--ui-radius-sm); background: rgba(240, 180, 76, 0.06); color: #f3d7a3; }
.public-warning p { margin: 0; font-size: var(--ui-fs-xs); line-height: 1.5; }
.public-warning strong { color: #ffe2ad; }
.public-warning :deep(svg) { flex: none; margin-top: 2px; color: var(--ui-warn); }

.pm-status-line { display: grid; gap: 6px; }
.success-text, .error-text { display: flex; align-items: flex-start; gap: 7px; margin: 0; font-size: var(--ui-fs-xs); line-height: 1.45; }
.success-text { color: #93e8bd; }
.error-text { color: #ffb0b0; }
.pm-status-line .success-text, .pm-status-line .error-text { padding: 8px 12px; border-radius: var(--ui-radius-sm); }
.pm-status-line .success-text { background: var(--ui-success-soft); border: 1px solid rgba(70, 207, 139, 0.3); }
.pm-status-line .error-text { background: var(--ui-danger-soft); border: 1px solid rgba(242, 109, 109, 0.35); }

/* Cards, fields, buttons */
.panel-card { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); padding: 16px; background: var(--ui-surface-1); min-width: 0; }
.field-label { display: block; margin: 0 0 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase; }
.credential-hint { margin-left: 6px; color: var(--ui-text-4); font-weight: 500; letter-spacing: 0; text-transform: none; }
.text-input { box-sizing: border-box; width: 100%; min-width: 0; height: var(--ui-control-h); padding: 0 11px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); background: var(--ui-surface-sunken); color: var(--ui-text-1); font: inherit; font-size: var(--ui-fs-sm); transition: border-color 0.15s, box-shadow 0.15s; }
select.text-input { padding-right: 30px; }
textarea.text-input { height: auto; padding: 10px 11px; resize: vertical; font-family: var(--ui-mono); font-size: var(--ui-fs-xs); line-height: 1.6; }
.text-input:hover { border-color: var(--ui-border-hover); }
.text-input:focus { outline: none; border-color: var(--ui-accent); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
.button-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
button { font: inherit; }
.quiet-button, .primary-button, .danger-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: var(--ui-control-h); padding: 0 12px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); background: var(--ui-surface-3); color: var(--ui-text-1); font-size: var(--ui-fs-xs); font-weight: 600; white-space: nowrap; cursor: pointer; transition: background-color 0.15s, border-color 0.15s, color 0.15s; }
.quiet-button:hover:not(:disabled) { background: var(--ui-surface-hover); border-color: var(--ui-border-hover); }
.primary-button { border-color: var(--ui-accent); background: var(--ui-accent); color: var(--ui-on-accent); }
.primary-button:hover:not(:disabled) { background: var(--ui-accent-hover); border-color: var(--ui-accent-hover); }
.danger-button { border-color: rgba(242, 109, 109, 0.45); background: var(--ui-danger-soft); color: #ffb8b8; }
.danger-button:hover:not(:disabled) { background: rgba(242, 109, 109, 0.2); }
.quiet-button:disabled, .primary-button:disabled, .danger-button:disabled { opacity: 0.4; cursor: default; }
.quiet-button:focus-visible, .primary-button:focus-visible, .danger-button:focus-visible, .pm-chip:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.pm-icon-btn { width: var(--ui-control-h); padding: 0; }
input[type="checkbox"] { accent-color: var(--ui-accent); width: 15px; height: 15px; margin: 0; cursor: pointer; }

/* Locked */
.pm-locked { display: grid; grid-template-columns: minmax(360px, 520px) minmax(0, 1fr); gap: 14px; align-items: start; }
.security-card { padding: 22px; }
.security-card h2 { margin: 12px 0 4px; font-size: var(--ui-fs-lg); }
.security-card .muted { margin-bottom: 18px; }
.unlock-form { display: grid; gap: 0; }
.unlock-form .button-row { margin-top: 12px; }
.pm-lock-icon { display: grid; place-items: center; width: 42px; height: 42px; border-radius: var(--ui-radius); border: 1px solid var(--ui-accent-line); background: var(--ui-accent-soft); color: var(--ui-accent); }
.boundary-card h2 { margin-bottom: 6px; }
.boundary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 14px 0 0; }
.boundary-grid div { padding: 11px 12px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); background: var(--ui-surface-2); }
.boundary-grid dt { color: var(--ui-text-1); font-size: var(--ui-fs-xs); font-weight: 650; }
.boundary-grid dd { margin: 3px 0 0; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); line-height: 1.5; }

/* KPIs */
.pm-kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); overflow: hidden; }
.pm-kpi { display: grid; gap: 2px; padding: 12px 16px; border-left: 1px solid var(--ui-border); min-width: 0; }
.pm-kpi:first-child { border-left: 0; }
.pm-kpi span { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
.pm-kpi b { color: var(--ui-text-1); font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.25; }
.pm-kpi small { color: var(--ui-text-4); font-size: var(--ui-fs-2xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pm-kpi.is-warn b { color: var(--ui-warn); }

/* Workspace */
.pm-workspace { display: grid; grid-template-columns: minmax(0, 1fr) 380px; gap: 14px; align-items: start; }
.pm-main { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.pm-devices { padding: 0; overflow: hidden; }
.pm-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.pm-devices > .pm-card-head { padding: 14px 16px 12px; }
.pm-card-head__actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.pm-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 0 16px 12px; }
.pm-search { position: relative; flex: 0 1 300px; min-width: 220px; }
.pm-search :deep(svg) { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--ui-text-4); pointer-events: none; }
.pm-search .text-input { padding-left: 32px; height: var(--ui-control-h-sm); font-size: var(--ui-fs-xs); }
.pm-filters { display: flex; gap: 2px; padding: 3px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); background: var(--ui-surface-sunken); flex-wrap: wrap; }
.pm-chip { display: inline-flex; align-items: center; gap: 6px; height: 24px; padding: 0 9px; border: 0; border-radius: var(--ui-radius-xs); background: transparent; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-weight: 600; cursor: pointer; }
.pm-chip span { min-width: 16px; padding: 0 5px; border-radius: 999px; background: var(--ui-surface-3); color: var(--ui-text-2); font-variant-numeric: tabular-nums; text-align: center; line-height: 16px; }
.pm-chip:hover { color: var(--ui-text-1); background: var(--ui-surface-2); }
.pm-chip.is-active { background: var(--ui-surface-3); color: var(--ui-text-1); box-shadow: inset 0 0 0 1px var(--ui-border-strong); }
.pm-chip.is-active span { background: var(--ui-accent-soft); color: var(--ui-accent); }

.pm-bulkbar { display: grid; grid-template-columns: minmax(180px, 1fr) auto; align-items: center; gap: 8px 14px; padding: 10px 16px; border-top: 1px solid var(--ui-border); border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-2); }
.pm-bulkbar.is-active { background: linear-gradient(90deg, rgba(45, 212, 168, 0.07), transparent 60%), var(--ui-surface-2); }
.pm-bulkbar__info { display: grid; gap: 2px; min-width: 0; }
.pm-bulkbar__info strong { color: var(--ui-text-1); font-size: var(--ui-fs-xs); }
.selected-targets { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pm-bulkbar__actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.pm-bulkbar__actions .quiet-button, .pm-bulkbar__actions .primary-button, .pm-bulkbar__actions .danger-button { height: var(--ui-control-h-sm); font-size: var(--ui-fs-2xs); }
.pm-bulkbar__actions .pm-icon-btn { width: var(--ui-control-h-sm); }
.pm-bulkbar__note { grid-column: 1 / -1; margin: 0; color: #f8d08a; font-size: var(--ui-fs-2xs); }
.confirm-box { display: inline-flex; align-items: center; gap: 7px; color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; cursor: pointer; }
.pm-route .confirm-box { align-items: flex-start; font-weight: 500; line-height: 1.5; margin-bottom: 10px; }
.pm-route .confirm-box input { margin-top: 2px; flex: none; }

/* Table */
.pm-table-wrap { max-height: calc(100vh - 360px); min-height: 240px; overflow: auto; }
.pm-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: var(--ui-fs-xs); }
.pm-table thead th { position: sticky; top: 0; z-index: 1; padding: 8px 12px; border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-1); color: var(--ui-text-4); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.05em; text-align: left; text-transform: uppercase; white-space: nowrap; }
.pm-table tbody td { padding: 8px 12px; border-bottom: 1px solid var(--ui-border); vertical-align: middle; white-space: nowrap; }
.pm-table tbody tr { cursor: pointer; transition: background-color 0.1s; }
.pm-table tbody tr:hover { background: var(--ui-surface-2); }
.pm-table tbody tr.is-selected { background: rgba(45, 212, 168, 0.05); }
.pm-table tbody tr.is-active { background: var(--ui-accent-soft); box-shadow: inset 3px 0 0 var(--ui-accent); }
.pm-table tbody tr.offline .col-device strong { color: var(--ui-text-3); }
.col-check { width: 36px; padding-right: 0 !important; }
.col-device strong { display: block; color: var(--ui-text-1); font-size: var(--ui-fs-xs); font-weight: 600; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
.col-device small { display: block; color: var(--ui-text-4); font-family: var(--ui-mono); font-size: 10.5px; }
.col-endpoint { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.col-endpoint code { color: var(--ui-text-1); font-size: var(--ui-fs-2xs); }
.pm-type { display: inline-block; margin-right: 6px; padding: 1px 5px; border-radius: 4px; background: var(--ui-surface-3); color: var(--ui-text-2); font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em; }
.pm-cred { margin-left: 6px; color: var(--ui-text-3); vertical-align: -1px; }
.col-result { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
.col-actions { width: 1%; text-align: right; }
.pm-edit { height: 24px; padding: 0 9px; font-size: var(--ui-fs-2xs); }
.pm-ok { color: #93e8bd; font-weight: 600; }
.pm-fail { color: #ffb0b0; font-weight: 600; }
.pm-empty { margin: 0; padding: 28px 16px; color: var(--ui-text-3); font-size: var(--ui-fs-xs); text-align: center; }

.pm-pill { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 9px; border-radius: 999px; border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; white-space: nowrap; }
.pm-pill i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pm-pill--ok { color: #93e8bd; border-color: rgba(70, 207, 139, 0.35); background: var(--ui-success-soft); }
.pm-pill--warn { color: #f8d08a; border-color: rgba(240, 180, 76, 0.4); background: var(--ui-warn-soft); }
.pm-pill--danger { color: #ffb8b8; border-color: rgba(242, 109, 109, 0.4); background: var(--ui-danger-soft); }
.pm-pill--muted { color: var(--ui-text-3); }
.mode-proxy { color: #93ebd2; border-color: var(--ui-accent-line); background: var(--ui-accent-soft); }
.mode-normal { color: var(--ui-text-2); }
.mode-unknown { color: #f8d08a; border-color: rgba(240, 180, 76, 0.4); background: var(--ui-warn-soft); }

/* Jobs */
.jobs-card .pm-card-head { margin-bottom: 12px; }
.job-list { display: flex; flex-direction: column; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); overflow: hidden; }
.job-item { border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-2); }
.job-item:last-child { border-bottom: 0; }
.job-head { display: grid; grid-template-columns: minmax(150px, auto) auto 1fr minmax(120px, 200px); align-items: center; gap: 12px; padding: 10px 14px; cursor: pointer; list-style: none; }
.job-head::-webkit-details-marker { display: none; }
.job-head strong { color: var(--ui-text-1); font-size: var(--ui-fs-xs); }
.job-head small { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.job-status { display: inline-flex; align-items: center; height: 20px; padding: 0 8px; border-radius: 999px; font-size: var(--ui-fs-2xs); font-weight: 650; text-transform: capitalize; background: var(--ui-surface-3); color: var(--ui-text-2); }
.status-running, .status-queued { background: var(--ui-warn-soft); color: #f8d08a; }
.status-completed { background: var(--ui-success-soft); color: #93e8bd; }
.status-failed { background: var(--ui-danger-soft); color: #ffb8b8; }
.progress-track { position: relative; height: 4px; border-radius: 999px; background: var(--ui-surface-3); overflow: hidden; }
.progress-track > span { position: absolute; inset: 0 auto 0 0; border-radius: inherit; background: var(--ui-accent); transition: width 0.3s; }
.progress-track.is-live > span { animation: pm-pulse 1.4s ease-in-out infinite; }
@keyframes pm-pulse { 50% { opacity: 0.45; } }
.result-list { display: flex; flex-direction: column; border-top: 1px solid var(--ui-border); }
.result-row { display: grid; grid-template-columns: minmax(150px, 200px) 90px minmax(0, 1fr); gap: 2px 12px; padding: 7px 14px; border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-sunken); font-size: var(--ui-fs-2xs); }
.result-row:last-child { border-bottom: 0; }
.result-row code { color: var(--ui-text-1); }
.result-row .result-state { color: #93e8bd; font-weight: 650; }
.result-row.failed .result-state { color: #ffb0b0; }
.result-row small { grid-column: 3; color: var(--ui-text-3); }
.result-row small:first-of-type { grid-row: 1; }
.job-waiting { padding: 8px 14px 12px; }

/* Inspector */
.pm-inspector { position: sticky; top: calc(var(--ui-topbar-h) + 14px); display: flex; flex-direction: column; gap: 14px; max-height: calc(100vh - var(--ui-topbar-h) - 28px); overflow: auto; padding: 16px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); }
.pm-inspector__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.pm-inspector__head h2 { margin: 3px 0 2px; font-size: var(--ui-fs-lg); }
.pm-inspector__head code { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.pm-inspector__pills { display: flex; gap: 6px; flex-wrap: wrap; }
.pm-callout { margin: 0; padding: 9px 11px; border-radius: var(--ui-radius-sm); border: 1px solid var(--ui-border); background: var(--ui-surface-2); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); line-height: 1.5; }
.pm-callout--warn { border-color: rgba(240, 180, 76, 0.35); background: var(--ui-warn-soft); color: #f8d08a; }
.pm-callout--action { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pm-callout--action > span { min-width: 0; line-height: 1.45; }
.pm-callout--action .primary-button { flex: none; }
.pm-route__hint { margin: 0 0 8px; font-size: 12px; line-height: 1.45; }
.pm-prepare-all .pm-count { margin-left: 6px; min-width: 18px; padding: 0 5px; border-radius: 999px; background: rgba(0, 0, 0, 0.22); font-size: 11px; font-variant-numeric: tabular-nums; text-align: center; }
.pm-callout--danger { border-color: rgba(242, 109, 109, 0.35); background: var(--ui-danger-soft); color: #ffb8b8; }
.pm-quick { display: grid; gap: 6px; padding: 12px; border: 1px dashed var(--ui-border-strong); border-radius: var(--ui-radius); background: var(--ui-surface-sunken); }
.pm-quick .field-label { margin: 0; }
.pm-quick__row { display: flex; gap: 6px; }
.pm-quick small { font-size: var(--ui-fs-2xs); }
.editor { display: grid; gap: 12px; }
.editor-grid { display: grid; grid-template-columns: minmax(0, 1fr) 110px; gap: 10px; }
.editor-grid .wide-field { grid-column: 1 / -1; }
.clear-options { display: flex; gap: 16px; flex-wrap: wrap; color: var(--ui-text-2); font-size: var(--ui-fs-2xs); }
.clear-options label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.editor-note { margin: 0; color: var(--ui-text-4); font-size: var(--ui-fs-2xs); line-height: 1.5; }
.pm-route { padding-top: 14px; border-top: 1px solid var(--ui-border); }
.pm-inspector__foot { display: flex; align-items: flex-end; justify-content: space-between; gap: 10px; padding-top: 14px; border-top: 1px solid var(--ui-border); }
.pm-last { display: grid; gap: 2px; min-width: 0; font-size: var(--ui-fs-2xs); }
.pm-last small { color: var(--ui-text-4); overflow: hidden; text-overflow: ellipsis; }
.pm-delete { color: #ffb8b8; height: var(--ui-control-h-sm); font-size: var(--ui-fs-2xs); }
.pm-inspector__empty { display: grid; justify-items: start; gap: 8px; padding-bottom: 14px; border-bottom: 1px solid var(--ui-border); }
.pm-inspector__empty h2 { margin-top: 4px; }
.pm-facts { margin: 10px 0 0; padding: 0; list-style: none; display: grid; gap: 8px; }
.pm-facts li { padding: 9px 11px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); background: var(--ui-surface-2); color: var(--ui-text-3); font-size: var(--ui-fs-2xs); line-height: 1.5; }
.pm-facts strong { color: var(--ui-text-1); }

/* Bulk dialog */
.pm-dialog-backdrop { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 32px; background: rgba(5, 7, 10, 0.72); backdrop-filter: blur(3px); }
.pm-dialog { display: flex; flex-direction: column; width: min(1080px, 100%); max-height: calc(100vh - 64px); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); box-shadow: var(--ui-shadow-pop); overflow: hidden; }
.pm-dialog__head, .pm-dialog__foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 18px; }
.pm-dialog__head { border-bottom: 1px solid var(--ui-border); }
.pm-dialog__head h2 { margin-top: 3px; font-size: var(--ui-fs-lg); }
.pm-dialog__foot { border-top: 1px solid var(--ui-border); background: var(--ui-surface-2); }
.pm-dialog__body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr); gap: 18px; padding: 18px; overflow: auto; }
.pm-dialog__form { display: grid; gap: 10px; align-content: start; }
.pm-dialog__options { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; align-items: end; }
.pm-dialog__options .field-label:nth-of-type(1) { grid-column: 1; grid-row: 1; }
.pm-dialog__options .field-label:nth-of-type(2) { grid-column: 2; grid-row: 1; }
.pm-bulk-text.is-masked { -webkit-text-security: disc; }
.pm-inline-check { display: inline-flex; align-items: center; gap: 7px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.pm-dialog__preview { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.pm-dialog__preview-head { display: flex; align-items: baseline; justify-content: space-between; }
.pm-dialog__preview-head h3 { margin: 0; }
.pm-preview-table { flex: 1; min-height: 240px; max-height: 420px; overflow: auto; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); background: var(--ui-surface-sunken); }
.pm-preview-row { display: grid; grid-template-columns: 40px minmax(120px, 0.9fr) minmax(0, 1.4fr) 90px; gap: 10px; align-items: center; padding: 7px 12px; border-bottom: 1px solid var(--ui-border); font-size: var(--ui-fs-2xs); }
.pm-preview-line { color: var(--ui-text-4); font-family: var(--ui-mono); }
.pm-preview-device strong { display: block; color: var(--ui-text-1); }
.pm-preview-device small { color: var(--ui-text-4); font-family: var(--ui-mono); }
.pm-preview-endpoint { display: inline-flex; align-items: center; gap: 6px; font-family: var(--ui-mono); color: var(--ui-text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pm-preview-state { text-align: right; font-weight: 650; color: var(--ui-text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pm-preview-row.is-ready .pm-preview-state { color: var(--ui-accent); }
.pm-preview-row.is-saved .pm-preview-state { color: #93e8bd; }
.pm-preview-row.is-invalid .pm-preview-endpoint, .pm-preview-row.is-failed .pm-preview-state { color: #ffb0b0; font-family: inherit; }

@media (max-width: 1440px) {
  .pm-workspace { grid-template-columns: minmax(0, 1fr) 340px; }
  .pm-kpi { padding: 10px 12px; }
  .col-result { max-width: 140px; }
  .pm-bulkbar { grid-template-columns: 1fr; }
  .pm-bulkbar__actions { justify-content: flex-start; }
}
@media (max-width: 1280px) {
  .pm-kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .pm-kpi:nth-child(4) { border-left: 0; }
  .pm-kpi:nth-child(n + 4) { border-top: 1px solid var(--ui-border); }
  .pm-bulkbar { grid-template-columns: 1fr; }
  .pm-bulkbar__actions { justify-content: flex-start; }
  .pm-locked { grid-template-columns: 1fr; }
  .pm-dialog__body { grid-template-columns: 1fr; }
}
</style>
