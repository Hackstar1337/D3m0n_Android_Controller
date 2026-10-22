<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { api } from '@/composables/useApi'
import { createPhoneTintManager } from '@/composables/phoneTint'
import { useFleetReboot, rebootChipText } from '@/composables/fleetReboot'
import RebootDialog from '@/components/RebootDialog.vue'
import { deviceWindowUrl, openDeviceWindow } from '@/composables/useDeviceWindows'
import DeviceWorkspaceView from '@/views/DeviceWorkspaceView.vue'
import FleetControlPanel from '@/components/FleetControlPanel.vue'
import FleetCommandCentre from '@/components/ai/FleetCommandCentre.vue'
import FleetAiBadge from '@/components/ai/FleetAiBadge.vue'
import DeviceDiscoveryPanel from '@/components/DeviceDiscoveryPanel.vue'
import GridGroupsPanel from '@/components/GridGroupsPanel.vue'
import ProxyManagerPanel from '@/components/ProxyManagerPanel.vue'
import StreamOverview from '@/components/StreamOverview.vue'
import DeviceWindowView from '@/views/DeviceWindowView.vue'
import { createPhoneInteractions } from '@/composables/phoneInteractions'
import type { LivePort } from '@/composables/phoneInteractions'
import { liveInputChannel, type LiveAck } from '@/composables/liveInput'
import { parseStreamIntent, startAllIntent, stopAllIntent, setDeviceIntent, wantsStream, type StreamIntent } from '@/composables/streamIntent'
import { phoneSubTab, phoneSubTabSeeded, type PhoneSubTab } from '@/composables/shellNavigation'
import { gridFocus } from '@/composables/gridFocus'
import UiIcon from '@/components/ui/UiIcon.vue'
import AIModelSettingsPanel from '@/components/AIModelSettingsPanel.vue'
import { clampAgentOptions, createAgentSseParser, formatPullProgress, pollAgentPull, resolveAgentSelection, DEFAULT_AGENT_MODEL, DEFAULT_AGENT_PROVIDER, type AgentOllamaModel, type AgentPullStatus, type AgentProviderOption } from '@/composables/agentConsole'
const workspaceRequests = ref<string[]>([])
const workspaceActivated = ref(new URLSearchParams(location.search).has('windows'))
function openWorkspaceDevice(serial: string) {
  workspaceRequests.value.push(serial)
  workspaceActivated.value = true
  subTab.value = 'windows'
}
import { startH264Stream as startAndroidDecoder, type H264Metrics, type H264Profile } from '@/composables/useH264Stream'
const androidHandles = new Map<string, H264Metrics>()
const androidCanvases = new Map<string, HTMLCanvasElement>()
const androidWatchers = new Map<string, (() => void)[]>()
const androidRenderFps = ref<Record<string, number>>({})

const devices = ref<any[]>([])
const deviceSearch = ref('')
function readDashboardPreferences(): Record<string, unknown> {
  try { const value = JSON.parse(localStorage.getItem('d3m0n.dashboard-interaction.v1') || '{}'); return value && typeof value === 'object' && !Array.isArray(value) ? value : {} } catch { return {} }
}
const dashboardPreferences = readDashboardPreferences()
const GRID_COLUMN_CHOICES = ['auto','1','2','3','4','6','8','10','12']
const gridColumns = ref(GRID_COLUMN_CHOICES.includes(String(dashboardPreferences.columns)) ? String(dashboardPreferences.columns) : 'auto')
// The Glass HUD tag keeps a minimum width; large fleets scroll rather than shrink labels.
const GRID_DENSITIES = [
  { value: '200', label: 'Wall', density: 'wall', minColumn: 168 },
  { value: '280', label: 'Compact', density: 'compact', minColumn: 168 },
  { value: '320', label: 'Comfort', density: 'comfort', minColumn: 220 },
  { value: '480', label: 'Large', density: 'large', minColumn: 300 },
  // Fit screen: columns and tile height are computed so every shown phone fits without scrolling.
  { value: 'fit', label: 'Fit screen', density: 'wall', minColumn: 168 },
] as const
// Layout v2: Fit screen is the default so the phones use the whole screen. Older saved layouts
// are migrated to Fit once; any layout chosen after that is remembered as before.
const GRID_LAYOUT_VERSION = 2
const previewSize = ref(dashboardPreferences.layoutVersion === GRID_LAYOUT_VERSION && GRID_DENSITIES.some(item => item.value === String(dashboardPreferences.preview)) ? String(dashboardPreferences.preview) : 'fit')
const gridDensityInfo = computed(() => GRID_DENSITIES.find(item => item.value === previewSize.value) || GRID_DENSITIES[2])
const gridDensity = computed(() => gridDensityInfo.value.density)
/* ── FIT SCREEN: size the wall so N phones (e.g. 50) fit on one desktop screen ── */
const FIT_ASPECT = 720 / 404          // portrait phone frame height / width
const FIT_MIN_WIDTH = 140            // unboxed bezel: model, full IPv4 and fixed activity glyphs
const FIT_MIN_HEIGHT = 88             // smallest readable preview; below this the wall scrolls
// Integrated bezel: fixed-height identity region, zero inset. Keep in sync with CSS.
const HUB_FULL_HEIGHT = 60           // identity row + reserved signals/actions row
const HUB_COMPACT_HEIGHT = 54        // compact identity row + reserved signals/actions row
const HUB_COMPACT_BELOW = 140        // use the compact hub when the full one would leave less preview
const hubCompact = ref(false)
const isFitLayout = computed(() => previewSize.value === 'fit')
const fitLayout = ref({ columns: 0, height: 0 })
const gridAvailableWidth = ref(1024)
let fitGridEl: HTMLElement | null = null
let fitObserver: ResizeObserver | null = null
let fitFrame = 0
function fitScrollParent(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY
    if ((overflow === 'auto' || overflow === 'scroll') && node.clientHeight > 0) return node
  }
  return null
}
// Pure sizing: pick the column count that gives the tallest phone preview while all tiles fit.
function solveFitLayout(count: number, width: number, height: number, colGap: number, rowGap: number, chrome: number) {
  const minCardWidth = Math.min(FIT_MIN_WIDTH, width)
  let best = { columns: Math.max(1, Math.min(count, Math.floor((width + colGap) / (minCardWidth + colGap)) || 1)), height: FIT_MIN_HEIGHT }
  let bestHeight = 0
  for (let columns = 1; columns <= Math.max(1, count); columns++) {
    const tileWidth = (width - (columns - 1) * colGap) / columns
    if (tileWidth < minCardWidth) break
    const rows = Math.ceil(count / columns)
    const byHeight = (height - (rows - 1) * rowGap) / rows - chrome
    const preview = Math.floor(Math.min(byHeight, tileWidth * FIT_ASPECT))
    if (preview > bestHeight) { bestHeight = preview; best = { columns, height: preview } }
  }
  return { columns: best.columns, height: Math.max(FIT_MIN_HEIGHT, best.height) }
}
function computeFitLayout() {
  fitFrame = 0
  const grid = fitGridEl
  if (!grid || subTab.value !== 'multi' || !grid.clientWidth) return
  gridAvailableWidth.value = grid.clientWidth
  if (!isFitLayout.value) { if (hubCompact.value) hubCompact.value = false; return }
  const style = getComputedStyle(grid)
  const colGap = parseFloat(style.columnGap) || 8, rowGap = parseFloat(style.rowGap) || 8
  const parent = fitScrollParent(grid)
  const gridTop = grid.getBoundingClientRect().top
  const available = parent
    ? parent.clientHeight - (gridTop - parent.getBoundingClientRect().top + parent.scrollTop) - 12
    : window.innerHeight - (gridTop + window.scrollY) - 12
  // The Glass Hub sits above the stream at a known height (+2px tile border); tight walls switch to the compact hub.
  const count = Math.max(1, visibleDeviceCount.value)
  const solve = (hub: number) => solveFitLayout(count, grid.clientWidth, Math.max(available, FIT_MIN_HEIGHT + hub + 2), colGap, rowGap, hub + 2)
  let next = solve(HUB_FULL_HEIGHT), compact = false
  if (next.height < HUB_COMPACT_BELOW) { next = solve(HUB_COMPACT_HEIGHT); compact = true }
  if (compact !== hubCompact.value) hubCompact.value = compact
  if (next.columns !== fitLayout.value.columns || next.height !== fitLayout.value.height) fitLayout.value = next
}
function scheduleFit() {
  if (fitFrame || typeof requestAnimationFrame === 'undefined') return
  fitFrame = requestAnimationFrame(computeFitLayout)
}
function bindFitGrid(element: unknown) {
  const el = element instanceof HTMLElement ? element : null
  if (el === fitGridEl) return
  if (fitGridEl) fitObserver?.unobserve(fitGridEl)
  fitGridEl = el
  if (el && typeof ResizeObserver !== 'undefined') { fitObserver ??= new ResizeObserver(scheduleFit); fitObserver.observe(el) }
  scheduleFit()
}
if (typeof window !== 'undefined') window.addEventListener('resize', scheduleFit)
const previewHeightCss = computed(() => isFitLayout.value ? `${fitLayout.value.height || 160}px` : previewSize.value + 'px')
// Tile identity: "N." number, the full device name and the complete IP in every layout.
// A phone whose traffic is routed through its proxy shows the proxy exit IP instead of its LAN IP.
function lanIp(d: any): string {
  const serial = String(d?.serial || '')
  const match = /^((?:\d{1,3}\.){3}\d{1,3})(?::\d+)?$/.exec(serial)
  if (match) return match[1] as string
  return typeof d?.ip === 'string' && d.ip ? d.ip : serial
}
function tileLabel(d: any): string { return String(d?.nickname || d?.model || d?.serial || '') }
type ProxyRoute = { mode: string; exitIp: string; verified: boolean }
const proxyRouting = ref<Record<string, ProxyRoute>>({})
let proxyRoutingTimer: number | null = null
const proxyRoutingFresh = ref(false)
const proxyRoutingUpdatedAt = ref(0)
function tileRoute(serial: string): ProxyRoute | null {
  const route = proxyRouting.value[serial]
  return route && route.mode === 'proxy' ? route : null
}
function tileIp(d: any): string { const route = tileRoute(d?.serial); return route && route.exitIp ? route.exitIp : lanIp(d) }
function tileIpState(serial: string): 'lan' | 'proxy' | 'proxy-unverified' {
  const route = tileRoute(serial)
  return !route ? 'lan' : route.exitIp && route.verified ? 'proxy' : 'proxy-unverified'
}
function tileIdentityTitle(d: any): string {
  const number = deviceNumbers.value[d.serial]
  const parts = [(number ? number + '. ' : '') + tileLabel(d)]
  const route = tileRoute(d.serial)
  if (route) parts.push(route.exitIp
    ? `Proxy exit IP ${route.exitIp}${route.verified ? ' (verified on the phone)' : ' (tested from the server; not yet verified on the phone)'}`
    : 'Proxy routing applied · exit IP not verified yet')
  parts.push(`${d.connection === 'wifi' ? 'Wireless ADB' : 'USB ADB'} ${lanIp(d)}`)
  if (lanIp(d) !== String(d.serial)) parts.push(String(d.serial))
  return parts.join(' · ')
}
async function loadProxyRouting() {
  try {
    const result = await api('/api/proxy/routing')
    const raw = result && typeof result === 'object' && result.devices && typeof result.devices === 'object' ? result.devices as Record<string, any> : {}
    const next: Record<string, ProxyRoute> = {}
    for (const [serial, value] of Object.entries(raw)) {
      if (!value || typeof value !== 'object') continue
      next[serial] = { mode: String(value.mode || 'normal'), exitIp: typeof value.exit_ip === 'string' ? value.exit_ip.slice(0, 64) : '', verified: value.verified === true }
    }
    proxyRouting.value = next
    proxyRoutingFresh.value = true; proxyRoutingUpdatedAt.value = Date.now()
  } catch { proxyRoutingFresh.value = false /* retained identity is advisory, not active proof */ }
}
// Stream quality for grid tiles. Auto follows the layout; device windows and the control desk stay HD.
type GridQuality = 'auto' | H264Profile
const gridQuality = ref<GridQuality>((['auto','hd','grid','wall'] as const).includes(dashboardPreferences.quality as GridQuality) ? dashboardPreferences.quality as GridQuality : 'auto')
const QUALITY_SPECS: Record<H264Profile, string> = { hd: '720p · 24 fps · 2 Mbps', grid: '480p · 15 fps · 1 Mbps', wall: '360p · 12 fps · 0.6 Mbps' }
function tileProfile(): H264Profile {
  if (gridQuality.value !== 'auto') return gridQuality.value
  if (isFitLayout.value) { const h = fitLayout.value.height; return h > 440 ? 'hd' : h > 300 ? 'grid' : 'wall' }
  return gridDensity.value === 'large' ? 'hd' : gridDensity.value === 'comfort' || gridDensity.value === 'compact' ? 'grid' : 'wall'
}
const gridQualitySpec = computed(() => QUALITY_SPECS[tileProfile()])
const statusFilter = ref<'all' | 'live' | 'stopped' | 'attention'>('all')
const showCardLogs = ref(dashboardPreferences.logs === true)
const focusedGridDevice = ref('')

function tileState(serial: string): 'live' | 'stopped' | 'attention' {
  if (!multiStreaming.value[serial]) return 'stopped'
  if (streamFrozen.value[serial] || blackWarning.value[serial]) return 'attention'
  const status = rtcStatus.value[serial] || ''
  if (multiStreamMode.value[serial] === 'mjpeg' || status === 'Streaming') return 'live'
  return 'attention'
}
function matchesDevice(d: any) {
  const query = deviceSearch.value.toLowerCase().trim()
  if (query) {
    const n = deviceNumbers.value[d.serial]
    const range = /^#?(\d{1,4})\s*-\s*#?(\d{1,4})$/.exec(query)
    const exact = /^#(\d{1,4})$/.exec(query)
    if (range) { const a = Number(range[1]), b = Number(range[2]); if (n == null || n < Math.min(a, b) || n > Math.max(a, b)) return false }
    else if (exact) { if (n !== Number(exact[1])) return false }
    // Plain digits: phone number or the last part of its IP ("37" finds phone 37 and 192.0.2.37).
    else if (/^\d{1,4}$/.test(query)) { if (n !== Number(query) && !lanIp(d).endsWith('.' + query)) return false }
    else if (!`${d.nickname || ''} ${d.model || ''} ${d.serial}`.toLowerCase().includes(query)) return false
  }
  return statusFilter.value === 'all' || tileState(d.serial) === statusFilter.value
}
/* ── PHONE NUMBERS: persistent 1, 2, 3 … stored on the server so every browser agrees ── */
const deviceNumbers = ref<Record<string, number>>({})
let numbersBusy = false
function phoneTag(serial: string): string { const n = deviceNumbers.value[serial]; return n ? n + '. ' : '' }
function sortByNumber(list: any[]): any[] {
  const last = Number.MAX_SAFE_INTEGER
  return [...list].sort((a, b) => ((deviceNumbers.value[a.serial] ?? last) - (deviceNumbers.value[b.serial] ?? last)) || 0)
}
function applyNumbers(numbers: unknown) {
  if (!numbers || typeof numbers !== 'object') return
  const clean: Record<string, number> = {}
  for (const [serial, value] of Object.entries(numbers as Record<string, unknown>)) if (Number.isInteger(value) && (value as number) > 0) clean[serial] = value as number
  deviceNumbers.value = clean
  const sorted = sortByNumber(devices.value)
  if (sorted.some((d, i) => d !== devices.value[i])) devices.value = sorted
}
async function syncDeviceNumbers() {
  if (numbersBusy || !devices.value.length) return
  numbersBusy = true
  try {
    const listed = await api('/api/fleet/numbers')
    const known = listed && typeof listed.numbers === 'object' ? listed.numbers : {}
    const serials = devices.value.map(d => String(d.serial))
    if (serials.some(serial => !(serial in known))) {
      const assigned = await api('/api/fleet/numbers/assign', { method: 'POST', body: JSON.stringify({ devices: serials.slice(0, 500) }) })
      applyNumbers(assigned?.numbers)
    } else applyNumbers(known)
  } catch { /* Numbers are labels only; the grid keeps working without them. */ }
  finally { numbersBusy = false }
}
async function renumberPhone(serial: string) {
  const current = deviceNumbers.value[serial]
  const answer = window.prompt(`Number for ${serial} (1-9999). If another phone already has this number, the two swap.`, current ? String(current) : '')
  if (answer == null) return
  const number = Number(answer.trim())
  if (!Number.isInteger(number) || number < 1 || number > 9999) { statusText.value = 'Phone number must be a whole number from 1 to 9999'; return }
  try {
    const result = await api(`/api/fleet/numbers/${encodeURIComponent(serial)}`, { method: 'PUT', body: JSON.stringify({ number }) })
    applyNumbers(result?.numbers)
    statusText.value = `${serial} is now phone ${number}` + (result?.swapped ? ` · ${result.swapped.device} moved to ${result.swapped.number}` : '')
  } catch (error) { statusText.value = error instanceof Error ? error.message : 'Could not change the phone number' }
}
async function renumberAllByIp() {
  const count = devices.value.length
  if (!count || !window.confirm(`Renumber the ${count} connected phones 1-${count} in IP order? Saved numbers of phones that are not connected now are forgotten.`)) return
  try {
    const result = await api('/api/fleet/numbers/reset', { method: 'POST', body: JSON.stringify({ devices: devices.value.map(d => String(d.serial)) }) })
    applyNumbers(result?.numbers)
    statusText.value = `Phones renumbered 1-${count} in IP order`
  } catch (error) { statusText.value = error instanceof Error ? error.message : 'Could not renumber phones' }
}
const visibleDeviceCount = computed(() => devices.value.filter(matchesDevice).length)
const liveDeviceCount = computed(() => devices.value.filter(d => tileState(d.serial) === 'live').length)
const attentionDeviceCount = computed(() => devices.value.filter(d => tileState(d.serial) === 'attention').length)
const fleetGridStyle = computed(() => (isFitLayout.value && fitLayout.value.columns
  ? { gridTemplateColumns: `repeat(${fitLayout.value.columns}, minmax(0, 1fr))` }
  : { gridTemplateColumns: gridColumns.value === 'auto' ? `repeat(auto-fill, minmax(min(100%, ${gridDensityInfo.value.minColumn}px), 1fr))` : `repeat(${Math.max(1, Math.min(Number(gridColumns.value), Math.floor((gridAvailableWidth.value + 8) / (FIT_MIN_WIDTH + 8))))}, minmax(0, 1fr))` }))
function selectVisibleTiles() { selectedTiles.value = devices.value.filter(matchesDevice).map(d => d.serial) }
// Tiles scrolled out of view receive one refresh frame every few seconds instead of a full stream.
const onScreenTiles = ref<Record<string, boolean>>({})
let tileObserver: IntersectionObserver | null = null
const observedTiles = new Map<string, Element>()
const phoneTint = createPhoneTintManager()
// Stable Vue refs avoid null/rebind churn and needless tint/visibility work on every status refresh.
const tintTileRefs = new Map<string, (element: unknown) => void>()
function tileRef(serial: string) {
  let callback = tintTileRefs.get(serial)
  if (!callback) { callback = (element: unknown) => bindTile(serial, element); tintTileRefs.set(serial, callback) }
  return callback
}
function tileOnScreen(serial: string) { return onScreenTiles.value[serial] !== false }
function bindTile(serial: string, element: unknown) {
  phoneTint.attach(serial, element instanceof HTMLElement ? element : null)
  const previous = observedTiles.get(serial)
  if (previous && previous !== element) { tileObserver?.unobserve(previous); observedTiles.delete(serial) }
  if (!(element instanceof Element) || previous === element || typeof IntersectionObserver === 'undefined') return
  tileObserver ??= new IntersectionObserver(entries => {
    for (const entry of entries) {
      const key = (entry.target as HTMLElement).dataset.serial
      if (!key) continue
      onScreenTiles.value[key] = entry.isIntersecting
      androidHandles.get('multi:' + key)?.update()
    }
  }, { root: null, rootMargin: '240px 0px' })
  ;(element as HTMLElement).dataset.serial = serial
  observedTiles.set(serial, element)
  tileObserver.observe(element)
}
function refreshStreamHints() { for (const [key, handle] of androidHandles) if (key.startsWith('multi:')) handle.update() }
/* ── SYNC ALL: mirror input on one phone to every phone in scope ─────────── */
const syncEnabled = ref(false), syncArming = ref(false)
const syncScope = ref<'all' | 'selected' | 'visible'>((['all','selected','visible'] as const).includes(dashboardPreferences.syncScope as any) ? dashboardPreferences.syncScope as 'all' | 'selected' | 'visible' : 'all')
const MAX_SYNC_DEVICES = 100
const syncPool = computed(() => devices.value
  .filter(d => !isIosDevice(d.serial))
  .filter(d => syncScope.value === 'all' || (syncScope.value === 'selected' ? selectedTiles.value.includes(d.serial) : matchesDevice(d)))
  .map(d => d.serial as string)
  .slice(0, MAX_SYNC_DEVICES + 1))
const syncScopeLabel = computed(() => syncScope.value === 'all' ? 'all phones' : syncScope.value === 'selected' ? 'selected phones' : 'filtered phones')
const syncActionLabel = computed(() => syncScope.value === 'all' ? 'Sync All' : syncScope.value === 'selected' ? 'Sync selected' : 'Sync filtered')
const syncMemberSummary = computed(() => syncPool.value.map(s => deviceNumbers.value[s] ? '#' + deviceNumbers.value[s] : lanIp({ serial: s })).join(', '))
const syncReport = ref<{ leader: string; ok: number; targets: number; failed: { device: string; error: string }[]; pending: number; ms: number; at: number } | null>(null)
function requestSyncAll() {
  if (syncEnabled.value) { syncEnabled.value = false; syncArming.value = false; return }
  syncArming.value = true
}
function confirmSyncAll() { syncArming.value = false; syncEnabled.value = syncPool.value.length > 1; syncReport.value = null }
function syncFollowersFor(serial: string): string[] {
  if (!syncEnabled.value || isIosDevice(serial) || !syncPool.value.includes(serial)) return []
  return syncPool.value.filter(item => item !== serial).slice(0, MAX_SYNC_DEVICES)
}
function withSync(serial: string, body: Record<string, unknown>): Record<string, unknown> {
  const followers = syncFollowersFor(serial)
  return followers.length ? { ...body, sync: { devices: followers } } : body
}
function noteSyncResult(serial: string, result: any) {
  const sync = result && typeof result === 'object' ? result.sync : null
  if (!sync || typeof sync !== 'object') return ''
  syncReport.value = { leader: serial, ok: Number(sync.ok) || 0, targets: Number(sync.targets) || 0, failed: Array.isArray(sync.failed) ? sync.failed.slice(0, 20) : [], pending: Number(sync.pending) || 0, ms: Number(sync.ms) || 0, at: Date.now() }
  return ` · mirrored to ${syncReport.value.ok}/${syncReport.value.targets}`
}
/* Sync All v2: phones without a live stream get a leased instant-control link; per-phone results are tracked. */
type SyncPhoneStatus = { instant: boolean; path: string; linkError: string; lastOk: boolean | null; lastAge: number; lastError: string }
const syncStatus = ref<Record<string, SyncPhoneStatus>>({}), syncStatusLoaded = ref(false)
const SYNC_LEASE_MS = 20000, SYNC_STATUS_MS = 3000
let syncLeaseTimer: number | null = null, syncStatusTimer: number | null = null, syncPoolTimer: number | null = null
let syncLeaseBusy = false, syncGeneration = 0
const syncInstantCount = computed(() => syncPool.value.filter(serial => syncStatus.value[serial]?.instant).length)
const syncFailedTiles = computed(() => new Set(syncPool.value.filter(serial => {
  const status = syncStatus.value[serial]
  return !!status && status.lastOk === false && status.lastAge < 60
})))
type TileSyncState = 'inactive' | 'pending' | 'ready' | 'fallback' | 'failed' | 'stale-fail'
function tileSyncState(serial: string): TileSyncState {
  if (!syncEnabled.value || !syncPool.value.includes(serial)) return 'inactive'
  const status = syncStatus.value[serial]
  if (!syncStatusLoaded.value || !status) return 'pending'
  if (status.lastOk === false) return status.lastAge < 60 ? 'failed' : 'stale-fail'
  if (status.instant || status.path === 'stream') return 'ready'
  return 'fallback'
}
function tileSyncTitle(serial: string): string {
  const state = tileSyncState(serial)
  if (state === 'inactive') return ''
  if (state === 'pending') return 'Sync member · readiness is pending'
  const status = syncStatus.value[serial]
  if (state === 'failed') return `Sync member · last mirrored input failed${status?.lastError ? ': ' + status.lastError : ''}`
  if (state === 'stale-fail') return `Sync member · previous input failure is stale${status?.lastError ? ': ' + status.lastError : ''}`
  if (state === 'fallback') {
    const path = status?.path === 'adb' ? 'ADB fallback' : status?.path === 'link' ? 'sync link fallback' : 'fallback path'
    return `Sync member · ${path}${status?.linkError ? ': ' + status.linkError : ''}`
  }
  return 'Sync member · ready for mirrored input'
}

const syncInstantTitle = computed(() => {
  const pool = syncPool.value
  if (!syncStatusLoaded.value) return 'Connecting instant control to every synced phone…'
  const slow = pool.filter(serial => !syncStatus.value[serial]?.instant)
  const viaLink = pool.filter(serial => syncStatus.value[serial]?.path === 'link').length
  const fast = pool.length - slow.length
  const head = `${fast}/${pool.length} phones take mirrored input instantly (${fast - viaLink} through their live stream, ${viaLink} through a sync link).`
  if (!slow.length) return head
  const names = slow.slice(0, 12).map(serial => {
    const number = deviceNumbers.value[serial], error = syncStatus.value[serial]?.linkError
    return `${number ? number + '. ' : ''}${lanIp({ serial })}${error ? ' (' + error + ')' : ''}`
  })
  return `${head}\nStill using the slower ADB path: ${names.join(', ')}${slow.length > 12 ? ' …' : ''}`
})
async function refreshSyncStatus() {
  try {
    const result = await api('/api/phone/sync/status')
    const raw = result && typeof result === 'object' && result.devices && typeof result.devices === 'object' ? result.devices as Record<string, any> : {}
    const next: Record<string, SyncPhoneStatus> = {}
    for (const [serial, value] of Object.entries(raw)) {
      if (!value || typeof value !== 'object') continue
      const last = value.last && typeof value.last === 'object' ? value.last : null
      next[serial] = {
        instant: value.instant === true, path: typeof value.path === 'string' ? value.path : 'adb',
        linkError: typeof value.link_error === 'string' ? value.link_error.slice(0, 160) : '',
        lastOk: last ? last.ok === true : null, lastAge: last ? Number(last.age) || 0 : Number.POSITIVE_INFINITY,
        lastError: last && typeof last.error === 'string' ? last.error.slice(0, 160) : '',
      }
    }
    if (syncEnabled.value) { syncStatus.value = next; syncStatusLoaded.value = true }
  } catch { /* advisory only: mirrored input still works through the ADB fallback */ }
}
// Phones whose live stream already carries control need no extra link.
// The activity dock is server truth, never inferred from a model, session or stream.
type AgentRunState = { running: boolean; stopping: boolean; updatedAt: number }
const agentRunsBySerial = ref<Record<string, AgentRunState>>({})
const agentRunsFresh = ref(false)
const agentRunsUpdatedAt = ref(0)
const dockClock = ref(Date.now())
const AGENT_RUN_POLL_MS = 2500
const AGENT_RUN_STALE_MS = 7000
let agentRunPollTimer: number | null = null
let agentRunPollController: AbortController | null = null
let agentRunPollBusy = false
let agentRunPollStopped = false
function clearAgentRunState() { agentRunsBySerial.value = {}; agentRunsFresh.value = false }
function aggregateAgentRuns(payload: unknown): Record<string, AgentRunState> {
  if (!Array.isArray(payload)) throw new Error('Automation status unavailable')
  const now = Date.now(), next: Record<string, AgentRunState> = {}
  for (const row of payload) {
    if (!row || typeof row !== 'object' || typeof row.device !== 'string' || typeof row.running !== 'boolean' || typeof row.stopping !== 'boolean') throw new Error('Automation status unavailable')
    const serial = row.device
    if (!serial) continue
    const prior = next[serial]
    next[serial] = { running: Boolean(prior?.running || (row.running && !row.stopping)), stopping: Boolean(prior?.stopping || row.stopping), updatedAt: now }
  }
  return next
}
async function pollAgentRuns() {
  dockClock.value = Date.now()
  if (agentRunPollStopped || agentRunPollBusy || document.hidden || subTab.value !== 'multi') return
  agentRunPollBusy = true
  const controller = new AbortController(); agentRunPollController = controller
  const timeout = window.setTimeout(() => controller.abort(), 1800)
  try {
    const response = await fetch('/api/agent-chat/sessions', { cache: 'no-store', credentials: 'same-origin', signal: controller.signal })
    if (!response.ok) throw new Error('Automation status unavailable')
    const next = aggregateAgentRuns(await response.json())
    if (!agentRunPollStopped && !controller.signal.aborted) {
      agentRunsBySerial.value = next; agentRunsFresh.value = true; agentRunsUpdatedAt.value = Date.now()
    }
  } catch { if (!agentRunPollStopped) clearAgentRunState() }
  finally { window.clearTimeout(timeout); agentRunPollBusy = false; if (agentRunPollController === controller) agentRunPollController = null }
}
function scheduleAgentRunPoll(delay = AGENT_RUN_POLL_MS) {
  if (agentRunPollStopped || document.hidden) return
  if (agentRunPollTimer) window.clearTimeout(agentRunPollTimer)
  agentRunPollTimer = window.setTimeout(async () => { agentRunPollTimer = null; await pollAgentRuns(); scheduleAgentRunPoll() }, delay)
}
function agentRunState(serial: string): AgentRunState | null {
  const state = agentRunsBySerial.value[serial]
  return state && dockClock.value - state.updatedAt <= AGENT_RUN_STALE_MS ? state : null
}
type DockState = 'active' | 'off' | 'unknown'
function tileDockState(serial: string, slot: 'proxy' | 'agent'): DockState {
  if (slot === 'proxy') {
    if (!proxyRoutingFresh.value || dockClock.value - proxyRoutingUpdatedAt.value > 45000) return 'unknown'
    const route = tileRoute(serial)
    return !route ? 'off' : route.exitIp && route.verified ? 'active' : 'unknown'
  }
  if (!agentRunsFresh.value || dockClock.value - agentRunsUpdatedAt.value > AGENT_RUN_STALE_MS) return 'unknown'
  const run = agentRunState(serial)
  return run?.running ? 'active' : run?.stopping ? 'unknown' : 'off'
}
function tileDockTitle(serial: string, slot: 'proxy' | 'agent'): string {
  const state = tileDockState(serial, slot)
  if (slot === 'proxy') return state === 'active' ? 'Proxy on · applied route with phone-verified exit IP' : state === 'off' ? 'Proxy off · LAN route' : 'Proxy route pending, stale or unavailable · not verified active'
  if (state === 'active') return 'Automation running · active Agent Console run on this phone'
  if (state === 'off') return 'Automation off · no executing Agent Console run'
  return agentRunState(serial)?.stopping ? 'Automation stopping · shutdown pending' : 'Automation status unavailable · waiting for fresh run metadata'
}
// Glass Hub signals: one truthful level per slot, shared by the tile hub and the Control desk bar.
const HUB_SLOTS = [
  { key: 'sync', label: 'Sync', icon: 'refresh' },
  { key: 'proxy', label: 'Proxy', icon: 'shieldLock' },
  { key: 'agent', label: 'Automation', icon: 'bot' },
] as const
type HubSlot = typeof HUB_SLOTS[number]['key']
type HubLevel = 'on' | 'off' | 'warn' | 'bad'
function hubSignal(serial: string, slot: HubSlot): HubLevel {
  if (slot === 'sync') {
    const state = tileSyncState(serial)
    return state === 'ready' ? 'on' : state === 'inactive' ? 'off' : state === 'failed' ? 'bad' : 'warn'
  }
  const state = tileDockState(serial, slot)
  return state === 'active' ? 'on' : state === 'off' ? 'off' : 'warn'
}
function hubSignalTitle(serial: string, slot: HubSlot): string {
  if (slot === 'sync') return tileSyncState(serial) === 'inactive' ? 'Sync off · not in the Sync set' : tileSyncTitle(serial)
  return tileDockTitle(serial, slot)
}
function hubSignalValue(serial: string, slot: HubSlot): string {
  if (slot === 'sync') {
    const state = tileSyncState(serial)
    return state === 'ready' ? 'Connected' : state === 'inactive' ? 'Off' : state === 'pending' ? 'Connecting' : state === 'fallback' ? 'ADB fallback' : state === 'failed' ? 'Failed' : 'Check'
  }
  const state = tileDockState(serial, slot)
  if (slot === 'proxy') return state === 'active' ? (tileRoute(serial)?.exitIp || 'Active') : state === 'off' ? 'Off · LAN' : tileRoute(serial) ? 'Unverified' : 'Checking'
  if (state === 'active') return 'Running'
  if (state === 'off') return 'Idle'
  return agentRunState(serial)?.stopping ? 'Stopping' : 'Unavailable'
}
function onAgentRunVisibility() {
  if (document.hidden) {
    if (agentRunPollTimer) window.clearTimeout(agentRunPollTimer)
    agentRunPollTimer = null; agentRunPollController?.abort(); clearAgentRunState()
  } else { void pollAgentRuns(); scheduleAgentRunPoll() }
}

function syncLeaseTargets(): string[] { return syncPool.value.filter(serial => syncStatus.value[serial]?.path !== 'stream').slice(0, MAX_SYNC_DEVICES) }
async function leaseSyncLinks() {
  if (syncLeaseBusy || !syncEnabled.value) return
  syncLeaseBusy = true
  try { await api('/api/phone/sync/links', { method: 'POST', body: JSON.stringify({ devices: syncLeaseTargets() }) }) } catch { /* retried on the next heartbeat */ }
  finally { syncLeaseBusy = false }
}
function releaseSyncLinks(keepalive = false) {
  try {
    void fetch('/api/phone/sync/links', { method: 'POST', keepalive, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ devices: [] }) }).catch(() => {})
  } catch { /* leases expire on their own within a minute */ }
}
function stopSyncTimers() {
  if (syncLeaseTimer) clearInterval(syncLeaseTimer)
  if (syncStatusTimer) clearInterval(syncStatusTimer)
  if (syncPoolTimer) clearTimeout(syncPoolTimer)
  syncLeaseTimer = null; syncStatusTimer = null; syncPoolTimer = null
}
function releaseSyncOnPageHide() { if (syncEnabled.value) releaseSyncLinks(true) }
watch(syncEnabled, async on => {
  const generation = ++syncGeneration
  stopSyncTimers()
  syncStatus.value = {}; syncStatusLoaded.value = false
  if (!on) { releaseSyncLinks(); return }
  await refreshSyncStatus()
  if (generation !== syncGeneration || !syncEnabled.value) return
  await leaseSyncLinks()
  if (generation !== syncGeneration || !syncEnabled.value) return
  syncLeaseTimer = window.setInterval(() => { void leaseSyncLinks() }, SYNC_LEASE_MS)
  syncStatusTimer = window.setInterval(() => { if (!document.hidden) void refreshSyncStatus() }, SYNC_STATUS_MS)
})
watch(() => syncPool.value.join(','), () => {
  if (!syncEnabled.value) return
  if (syncPoolTimer) clearTimeout(syncPoolTimer)
  syncPoolTimer = window.setTimeout(() => { syncPoolTimer = null; void leaseSyncLinks() }, 400)
})
const SYNC_KEYS = [
  { label: 'Back', key: 'BACK', icon: 'back' }, { label: 'Home', key: 'HOME', icon: 'home' },
  { label: 'Recents', key: 'APP_SWITCH', icon: 'grid' }, { label: 'Wake', key: 'WAKEUP', icon: 'power' },
  { label: 'Vol −', key: 'VOLUME_DOWN', icon: '' }, { label: 'Vol +', key: 'VOLUME_UP', icon: '' },
] as const
async function sendSyncKey(key: string) {
  const [leader] = syncPool.value
  if (!leader) return
  // Live channel: the key reaches the leader and every instant follower together.
  if (!isIosDevice(leader) && liveInputChannel.key(leader, syncFollowersFor(leader), key, ack => {
    tileActionMessage.value[leader] = ack.ok ? `${key} sent` + noteSyncResult(leader, ack) : (ack.error || 'Key failed')
  })) return
  try {
    const result = await api('/api/phone/key', { method: 'POST', body: JSON.stringify({ device: leader, key, ...withSync(leader, {}) }) })
    tileActionMessage.value[leader] = `${key} sent` + noteSyncResult(leader, result)
  } catch (error) { tileActionMessage.value[leader] = error instanceof Error ? error.message : 'Key failed' }
}
const selectedDevice = ref('')
const selectedIsIos = computed(() => isIosDevice(selectedDevice.value))
const hasIosDevices = computed(() => devices.value.some(d => isIosDevice(d.serial)))
const hasAndroidDevices = computed(() => devices.value.some(d => !isIosDevice(d.serial)))
const streaming = ref(false)
const INTENT_KEY = 'd3m0n.stream-intent.v1'
function readStreamIntent(): StreamIntent {
  try { return parseStreamIntent(JSON.parse(localStorage.getItem(INTENT_KEY) || 'null')) } catch { return stopAllIntent() }
}
const streamIntent = ref<StreamIntent>(readStreamIntent())
const initialSubTab: PhoneSubTab = new URLSearchParams(location.search).has('discover') ? 'discover' : new URLSearchParams(location.search).has('proxies') ? 'proxies' : new URLSearchParams(location.search).has('fleet') ? 'fleet' : new URLSearchParams(location.search).has('ai') || new URLSearchParams(location.search).has('fleet-ai') ? 'ai' : new URLSearchParams(location.search).has('windows') ? 'windows' : new URLSearchParams(location.search).has('multi') || streamIntent.value.all || Object.values(streamIntent.value.devices).some(Boolean) ? 'multi' : 'single'
// The desktop sidebar and this workspace share one sub-view ref. Seed it once per page load
// unless the user already picked a workspace view from the sidebar.
if (!phoneSubTabSeeded.value) { phoneSubTab.value = initialSubTab; phoneSubTabSeeded.value = true }
const subTab = phoneSubTab
if (subTab.value === 'windows') workspaceActivated.value = true
const multiMounted = ref(subTab.value === 'multi')
const controlDevice = ref(''), gridTouch = ref(dashboardPreferences.direct !== false)
const selectedTiles = ref<string[]>([]), tileActionMessage = ref<Record<string, string>>({})
const groupsOpen = ref(false)
function resetSyncScope() { syncEnabled.value = false; syncArming.value = false; syncReport.value = null }
// Reconfirm membership after changing the scope or selected set. Never silently broaden sync.
watch(syncScope, resetSyncScope, { flush: 'sync' })
watch(() => selectedTiles.value.slice().sort().join(','), () => { if (syncScope.value === 'selected') resetSyncScope() }, { flush: 'sync' })
// Reboot: preflight dialog, confirmed request and recovery status. Never mirrored through Sync.
const reboot = useFleetReboot(api, { onBack: () => { void loadDevices() } })
const rebootDialog = reboot.dialog
const rebootEntries = reboot.entries
const rebootActiveCount = reboot.activeCount
function rebootLabel(serial: string): string { const n = deviceNumbers.value[serial]; return n ? String(n) : (serial.split(':')[0] || serial) }
function rebootActiveFor(serial: string): string { const r = reboot.status.value[serial]; return r && (r.state === 'rebooting' || r.state === 'recovering' || r.state === 'needs_usb') ? r.state : '' }
function rebootTitle(serial: string): string { const r = reboot.status.value[serial]; return r ? 'Reboot: ' + (r.message || rebootChipText(r)) : '' }
function openReboot(serials: string[]) { void reboot.open(serials.filter(s => !isIosDevice(s))) }
void reboot.refresh()
function requestSyncSelected() {
  syncScope.value = 'selected'
  groupsOpen.value = false
  if (syncPool.value.length < 2) return
  syncEnabled.value = false
  requestSyncAll()
}
const controlDevices = computed(() => devices.value.filter(d => !isIosDevice(d.serial)))
const controlDeviceInfo = computed(() => devices.value.find(d => d.serial === controlDevice.value) || null)
const controlPosition = computed(() => controlDevices.value.findIndex(d => d.serial === controlDevice.value))
function switchControlDevice(direction: number) {
  const list = controlDevices.value
  if (!list.length) return
  const index = (Math.max(0, controlPosition.value) + direction + list.length) % list.length
  const next = list[index]
  if (next) openDirectControl(next.serial)
}
watch([gridTouch, previewSize, gridColumns, showCardLogs, gridQuality, syncScope], () => {
  try { localStorage.setItem('d3m0n.dashboard-interaction.v1', JSON.stringify({ direct: gridTouch.value, preview: previewSize.value, columns: gridColumns.value, logs: showCardLogs.value, quality: gridQuality.value, syncScope: syncScope.value, layoutVersion: GRID_LAYOUT_VERSION })) } catch {}
})
// Quality and visibility hints reach the stream server immediately, not on the next 1 s tick.
watch([previewSize, gridQuality, controlDevice, focusedGridDevice, subTab, deviceSearch, statusFilter], () => { void nextTick(refreshStreamHints) })
// Fit screen recomputes when anything changes the space above or beside the wall, or the phone count.
watch([isFitLayout, visibleDeviceCount, syncEnabled, syncArming, controlDevice, subTab, gridTouch, showCardLogs, gridFocus, groupsOpen], () => { void nextTick(scheduleFit) })
// Focus mode (sidebar and top bar hidden). Esc exits unless a phone, a field or the control desk has the keyboard.
const gridFocusOn = computed(() => gridFocus.value && subTab.value === 'multi')
// The former full-width help line, now a tooltip so the phones get that row.
const gridHelpText = computed(() => gridTouch.value
  ? 'Direct controls enabled: tap or drag a phone · wheel over its screen to scroll · click its screen before typing or pasting. Scroll the gaps to move the dashboard. Click a phone name to open the control desk. Input is never broadcast unless Sync All is on.'
  : 'Preview-only mode: phone previews do not send input. Use Control for the larger controller, or enable Interact in previews.')
function toggleGridFocus() { gridFocus.value = !gridFocus.value }
function exitGridFocusOnEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !gridFocusOn.value || event.defaultPrevented) return
  const target = event.target as HTMLElement | null
  if (target && typeof target.closest === 'function' && target.closest('input, textarea, select, [contenteditable="true"], .multi-card, .direct-control-overlay, [role="dialog"]')) return
  gridFocus.value = false
}
watch(() => fitLayout.value.height, () => { void nextTick(refreshStreamHints) })
watch(syncPool, pool => { if (syncEnabled.value && pool.length < 2) syncEnabled.value = false })

let alive = true, devicePollTimer: number | null = null, devicePollBusy = false
function persistStreamIntent(value: StreamIntent) {
  streamIntent.value = value
  try { localStorage.setItem(INTENT_KEY, JSON.stringify(value)) } catch { statusText.value = 'Streaming active; this browser could not save the setting' }
}
function openDirectControl(serial: string) {
  if (isIosDevice(serial)) { openWorkspaceDevice(serial); return }
  controlDevice.value = serial
  focusedGridDevice.value = serial
  subTab.value = 'multi'
}
function stopSelectedStreams() {
  for (const serial of selectedTiles.value) stopMultiStream(serial)
  selectedTiles.value = []
}
async function reconcileStreams() {
  if (!alive) return
  const batch = ++startBatch
  for (const d of devices.value) {
    if (!alive || batch !== startBatch) return
    if (wantsStream(streamIntent.value, d.serial)) {
      multiMounted.value = true
      if (!multiStreaming.value[d.serial] || (!isIosDevice(d.serial) && (!androidHandles.has('multi:' + d.serial) || !androidCanvases.get('multi:' + d.serial)?.isConnected))) {
        await startMultiStream(d.serial, 'rtc', false)
        await new Promise(resolve => setTimeout(resolve, 80))
      }
    } else if (multiStreaming.value[d.serial]) stopMultiStream(d.serial, false)
  }
}
function syncStreamIntent(event: StorageEvent) {
  if (event.key !== INTENT_KEY) return
  try { streamIntent.value = parseStreamIntent(JSON.parse(event.newValue || 'null')) } catch { streamIntent.value = stopAllIntent() }
  void reconcileStreams()
}
async function pollDevicesAndStreams() {
  if (!alive || devicePollBusy) return
  devicePollBusy = true
  try { await loadDevices(); await reconcileStreams() } finally { devicePollBusy = false }
}
async function sendTileInput(serial: string, path: string, body: Record<string, unknown>) {
  try {
    const r = await api(path, { method: 'POST', body: JSON.stringify({ device: serial, ...withSync(serial, body) }) })
    if (r.ok === false) throw new Error(r.error || 'Device action failed')
    tileActionMessage.value[serial] = 'Input sent to ' + serial + noteSyncResult(serial, r)
    return r
  } catch (error) {
    tileActionMessage.value[serial] = error instanceof Error ? error.message : 'Device action failed'
    throw error
  }
}
const gridInteractions = new Map<string, ReturnType<typeof createPhoneInteractions>>()
let releaseLiveInput: (() => void) | null = null
/* Live input: touches stream in real time to the phone and its Sync followers. */
function gridLiveAck(serial: string, ack: LiveAck) {
  if (ack.phase === 'down' && ack.ok) return
  if (ack.cancelled) {
    if (ack.reason && ack.reason !== 'superseded') tileActionMessage.value[serial] = 'Touch released by the safety timer'
    return
  }
  if (!ack.ok) {
    noteSyncResult(serial, ack)
    tileActionMessage.value[serial] = ack.error || 'Device action failed'
    return
  }
  tileActionMessage.value[serial] = 'Input sent to ' + serial + noteSyncResult(serial, ack)
}
function gridLivePort(serial: string): LivePort | null {
  if (isIosDevice(serial) || !liveInputChannel.isReady()) return null
  return {
    ready: () => liveInputChannel.isReady(),
    down: (x, y) => liveInputChannel.down(serial, syncFollowersFor(serial), x, y, ack => gridLiveAck(serial, ack)),
    move: (id, x, y) => liveInputChannel.move(id, x, y),
    up: (id, x, y) => liveInputChannel.up(id, x, y),
    cancel: id => liveInputChannel.cancel(id),
    key: key => liveInputChannel.key(serial, syncFollowersFor(serial), key, ack => gridLiveAck(serial, ack)),
  }
}
function gridInteraction(serial: string) {
  const existing = gridInteractions.get(serial)
  if (existing) return existing
  const controls = createPhoneInteractions({
    element: () => androidCanvases.get('multi:' + serial) || null,
    enabled: () => alive && gridTouch.value && subTab.value === 'multi' && !!multiStreaming.value[serial] && rtcStatus.value[serial] === 'Streaming',
    send: (path, body) => sendTileInput(serial, path, body),
    focus: () => { focusedGridDevice.value = serial; androidCanvases.get('multi:' + serial)?.focus({ preventScroll: true }) },
    report: message => { tileActionMessage.value[serial] = message },
    live: () => gridLivePort(serial),
  })
  gridInteractions.set(serial, controls)
  return controls
}
function gridPointerDown(serial: string, event: PointerEvent) { gridInteraction(serial).pointerDown(event) }
function gridPointerMove(serial: string, event: PointerEvent) { gridInteractions.get(serial)?.pointerMove(event) }
function gridPointerUp(serial: string, event: PointerEvent) { gridInteraction(serial).pointerUp(event) }
function gridPointerCancel(serial: string, event?: PointerEvent) { gridInteractions.get(serial)?.pointerCancel(event) }
function gridKeyDown(serial: string, event: KeyboardEvent) {
  if (!gridTouch.value && event.key === 'Enter') { event.preventDefault(); openDirectControl(serial); return }
  gridInteraction(serial).keydown(event)
}
watch([gridTouch, subTab], () => {
  if (!gridTouch.value || subTab.value !== 'multi') for (const controls of gridInteractions.values()) controls.cancel()
})
const nickname = ref('')
const editingNickname = ref(false)
let logTimer: number | null = null
const srvLogs = ref<string[]>([])
const botLogs = ref<string[]>([])
let srvSeq = 0
const statusText = ref('')

/* ── log level filter ─────────────────────────────────────────────────── */
const logFilter = ref<string>('All')
const LOG_FILTERS = ['All', 'Error', 'Warn', 'App', 'Flask'] as const
const logsOpen = ref(false)

const filteredSrvLogs = computed(() => {
  const logs = srvLogs.value.slice(-50)
  const f = logFilter.value
  if (f === 'All') return logs
  if (f === 'Error') return logs.filter(l => l.includes('[ERROR]') || l.includes('[CRITICAL]'))
  if (f === 'Warn') return logs.filter(l => l.includes('[WARN') || l.includes('[WARNING]'))
  if (f === 'App') return logs.filter(l => !l.includes('[Flask]') && !l.includes('werkzeug'))
  if (f === 'Flask') return logs.filter(l => l.includes('[Flask]') || l.includes('werkzeug') || l.includes('HTTP'))
  return logs
})

/* ── bot log type selector ────────────────────────────────────────────── */
const botLogType = ref('bot')
const BOT_LOG_TYPES = [
  { value: 'bot', label: 'Bot' },
  { value: 'post', label: 'Post Bot' },
]

/* ══════════════════════════════════════════════════════════════════════════
   WebRTC streaming — shared logic for single + multi device
   ══════════════════════════════════════════════════════════════════════════ */
interface RtcSession { pc: RTCPeerConnection; sessionId: string; pollTimer: number; sse: EventSource | null; status: string }
const rtcSessions = ref<Record<string, RtcSession>>({})
const rtcStatus = ref<Record<string, string>>({})

interface RecordingResponse {
  ok?: boolean
  running?: boolean
  filename?: string
  mode?: string
  url?: string
  error?: string
}

type NewsResult = {
  ok?: boolean
  error?: string
  current_url?: string
  headlines?: any[]
  articles?: any[]
  errors?: any[]
  extraction?: any
  completion?: any
  screenshots?: Record<string, string>
}

type StreamInfo = {
  ok?: boolean
  platform?: string
  requested_mode?: string
  effective_mode?: string
  recommended_mode?: string
  fallback_mode?: string
  stream_url?: string
  mjpeg_url?: string
  mjpeg_settings?: Record<string, unknown>
  unsupported_actions?: string[]
  recovery?: Record<string, unknown>
  error?: string
}

function isIosSerial(serial: string | null | undefined): boolean {
  return !!serial && serial.startsWith('ios:')
}

function devicePlatform(serial: string | null | undefined): 'ios' | 'android' {
  const row = devices.value.find((d: any) => d.serial === serial)
  const platform = String(row?.platform || row?.device_platform || '').toLowerCase()
  if (platform === 'ios' || platform === 'android') return platform
  return isIosSerial(serial) ? 'ios' : 'android'
}

function isIosDevice(serial: string | null | undefined): boolean {
  return devicePlatform(serial) === 'ios'
}

function mjpegStreamUrl(serial: string): string {
  const modeParam = isIosDevice(serial) ? '&mode=wda-mjpeg' : ''
  return `/api/phone/stream?device=${encodeURIComponent(serial)}&fps=5${modeParam}`
}

function effectiveStreamMode(serial: string, mode: 'rtc' | 'mjpeg'): 'rtc' | 'mjpeg' {
  return isIosDevice(serial) ? 'mjpeg' : mode
}

function streamModeText(serial: string, mode: 'rtc' | 'mjpeg' | 'h264'): string {
  if (mode === 'rtc' || mode === 'h264') return 'H.264 WebSocket'
  return isIosDevice(serial) ? 'WDA MJPEG' : 'MJPEG'
}

function streamModeTitle(serial: string, mode: 'rtc' | 'mjpeg' | 'h264'): string {
  if (isIosDevice(serial)) return 'iOS streams through WebDriverAgent MJPEG'
  return mode === 'rtc' || mode === 'h264' ? 'Android H.264 WebSocket stream over ADB and SSH' : 'Android MJPEG fallback stream'
}

function multiStreamLabel(serial: string): string {
  const mode = multiStreamMode.value[serial] || 'mjpeg'
  if (mode === 'rtc') return androidRenderFps.value[serial] ? `H.264 ${androidRenderFps.value[serial]}fps` : (rtcStatus.value[serial] || 'H.264')
  return streamModeText(serial, mode)
}

function streamFallbackUrl(serial: string, fallback?: any): string {
  const url = String(fallback?.url || '').trim()
  return url || mjpegStreamUrl(serial)
}

const streamInfo = ref<Record<string, StreamInfo>>({})
const streamInfoStatus = ref<Record<string, string>>({})

function streamInfoModeParam(serial: string, mode: 'rtc' | 'mjpeg'): string {
  if (isIosDevice(serial)) return 'mjpeg'
  return mode === 'rtc' ? 'portal' : 'screencap'
}

function streamInfoTitle(serial: string): string {
  const info = streamInfo.value[serial]
  if (!info) return streamModeTitle(serial, multiStreamMode.value[serial] || singleStreamMode.value)
  const parts = [
    info.effective_mode ? `mode ${info.effective_mode}` : '',
    info.fallback_mode ? `fallback ${info.fallback_mode}` : '',
    info.unsupported_actions?.length ? `unsupported ${info.unsupported_actions.join(', ')}` : '',
  ].filter(Boolean)
  return parts.join(' | ') || streamModeTitle(serial, multiStreamMode.value[serial] || singleStreamMode.value)
}

async function resolveMjpegStreamUrl(serial: string, mode: 'rtc' | 'mjpeg'): Promise<string> {
  const fallback = mjpegStreamUrl(serial)
  try {
    const info = await api<StreamInfo>(
      `/api/phone/stream-info?device=${encodeURIComponent(serial)}&fps=5&mode=${encodeURIComponent(streamInfoModeParam(serial, mode))}`
    )
    streamInfo.value[serial] = info
    streamInfoStatus.value[serial] = ''
    return String(info.stream_url || fallback)
  } catch (error) {
    streamInfoStatus.value[serial] = error instanceof Error ? error.message.replace(/^API \d+:\s*/, '') : 'Stream metadata unavailable'
    return fallback
  }
}

function applyIosStreamFallback(serial: string, fallback?: any) {
  rtcStatus.value[serial] = 'WDA MJPEG'
  if (serial === selectedDevice.value) {
    singleStreamMode.value = 'mjpeg'
    singleMjpegUrl.value = streamFallbackUrl(serial, fallback)
  }
  if (multiStreaming.value[serial]) {
    multiStreamMode.value[serial] = 'mjpeg'
    mjpegUrls.value[serial] = streamFallbackUrl(serial, fallback)
  }
}

function blackWarningText(serial: string): string {
  return isIosDevice(serial) ? 'WDA stream stalled' : 'Screen capture protected or stream stalled — reconnect H.264'
}

function streamPlaceholderText(serial: string): string {
  return isIosDevice(serial) ? 'Press play for WDA MJPEG' : 'Press H.264 to start live video'
}

function uuid(): string {
  return ([1e7] as any + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, (c: any) =>
    (c ^ ((crypto.getRandomValues(new Uint8Array(1))[0] ?? 0) & 15) >> c / 4).toString(16))
}

async function rtcFixPortal(serial: string) {
  rtcStatus.value[serial] = 'Fixing Portal...'
  try {
    const r = await api(`/api/phone/portal-fix/${serial}`, { method: 'POST' })
    rtcStatus.value[serial] = r.ok ? 'Portal fixed — retry stream' : ('Fix failed: ' + (r.error || ''))
  } catch { rtcStatus.value[serial] = 'Fix failed' }
}

async function rtcStart(serial: string, scope: 'multi' | 'single' = 'multi') {
  if (isIosDevice(serial)) { applyIosStreamFallback(serial); return }
  rtcStop(serial, scope)
  rtcStatus.value[serial] = 'H.264 connecting...'
  await nextTick()
  const canvas = document.getElementById(`rtc-video-${scope}-${serial}`) as HTMLCanvasElement | null
  if (!alive || !canvas || (scope === 'multi' ? !multiStreaming.value[serial] : !streaming.value)) return
  const handleKey = scope + ':' + serial
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const handle = startAndroidDecoder(`${proto}://${location.host}/api/phone/android-h264/${encodeURIComponent(serial)}`, canvas, { visible: () => scope === 'single' ? subTab.value === 'single' : subTab.value === 'multi' && (!controlDevice.value || focusedGridDevice.value === serial) && matchesDevice(devices.value.find(d => d.serial === serial) || { serial }) && tileOnScreen(serial), kind: scope === 'multi' ? 'dashboard' : 'single', profile: () => scope === 'single' ? 'hd' : tileProfile() })
  androidHandles.set(handleKey, handle)
  androidCanvases.set(handleKey, canvas)
  androidWatchers.set(handleKey, [
    watch(handle.status, value => { rtcStatus.value[serial] = value === 'streaming' ? 'Streaming' : `H.264 ${value}` }, { immediate: true }),
    watch(handle.renderFps, value => { androidRenderFps.value[serial] = value }),
  ])
}
function rtcStop(serial: string, scope: 'multi' | 'single' = 'multi') {
  const key = scope + ':' + serial
  androidWatchers.get(key)?.forEach(stop => stop())
  androidWatchers.delete(key)
  androidHandles.get(key)?.stop()
  androidHandles.delete(key)
  androidCanvases.delete(key)
  androidRenderFps.value[serial] = 0
  if (!isIosDevice(serial)) { rtcStatus.value[serial] = ''; return }
  clearBlackCheck(serial)
  const sess = rtcSessions.value[serial]
  if (!sess) return
  if (sess.pollTimer) clearInterval(sess.pollTimer)
  if (sess.sse) { sess.sse.close(); sess.sse = null }
  api('/api/phone/webrtc-signal', {
    method: 'POST',
    body: JSON.stringify({ device: serial, method: 'stream/stop', params: { sessionId: sess.sessionId } })
  }).catch(() => {})
  sess.pc.close()
  delete rtcSessions.value[serial]
  rtcStatus.value[serial] = ''
  const videoEl = document.getElementById(`rtc-video-${serial}`) as HTMLVideoElement
  if (videoEl) videoEl.srcObject = null
}

/* ── DataChannel input (low-latency) with HTTP fallback ─────────────── */
function sendInput(_serial: string, _msg: object): boolean {
  // DataChannel input disabled — portal can't exec shell "input" commands
  // without root. Will implement via AccessibilityService dispatchGesture() later.
  // For now, always fall through to HTTP → backend platform control.
  return false
}

function setupDataChannel(serial: string, pc: RTCPeerConnection) {
  try {
    const dc = pc.createDataChannel('control', { ordered: true, negotiated: true, id: 1 })
    dc.onopen = () => { console.log(`[RTC ${serial}] DataChannel open`); (rtcSessions.value[serial] as any)._dc = dc }
    dc.onclose = () => { (rtcSessions.value[serial] as any)._dc = undefined }
  } catch (e) { console.warn('[RTC] DataChannel setup failed:', e) }
}

/* ── tap/swipe on video element ──────────────────────────────────────── */
interface DragState { x: number; y: number; t: number }
const dragState = ref<Record<string, DragState | null>>({})
const dragMoved = ref<Record<string, boolean>>({})

function videoMouseDown(serial: string, e: MouseEvent) {
  e.preventDefault()
  const video = e.target as HTMLVideoElement
  const rect = video.getBoundingClientRect()
  const sx = video.videoWidth / rect.width, sy = video.videoHeight / rect.height
  dragState.value[serial] = { x: Math.round((e.clientX - rect.left) * sx), y: Math.round((e.clientY - rect.top) * sy), t: Date.now() }
  dragMoved.value[serial] = false
}

function videoMouseMove(serial: string) {
  if (dragState.value[serial]) dragMoved.value[serial] = true
}

function videoMouseUp(serial: string, e: MouseEvent) {
  const ds = dragState.value[serial]
  if (!ds) return
  e.preventDefault()
  const video = e.target as HTMLVideoElement
  const rect = video.getBoundingClientRect()
  const sx = video.videoWidth / rect.width, sy = video.videoHeight / rect.height
  const ex = Math.round((e.clientX - rect.left) * sx), ey = Math.round((e.clientY - rect.top) * sy)
  const sw = video.videoWidth, sh = video.videoHeight
  const dx = ex - ds.x, dy = ey - ds.y, dist = Math.sqrt(dx * dx + dy * dy)
  if (!dragMoved.value[serial] || dist < 20) {
    if (!sendInput(serial, { action: 'tap', x: ex, y: ey }))
      api('/api/phone/tap', { method: 'POST', body: JSON.stringify({ device: serial, x: ex, y: ey, stream_w: sw, stream_h: sh }) })
  } else {
    if (!sendInput(serial, { action: 'swipe', x1: ds.x, y1: ds.y, x2: ex, y2: ey, duration: 300 }))
      api('/api/phone/swipe', { method: 'POST', body: JSON.stringify({ device: serial, x1: ds.x, y1: ds.y, x2: ex, y2: ey, stream_w: sw, stream_h: sh }) })
  }
  dragState.value[serial] = null
}

function videoMouseLeave(serial: string) { dragState.value[serial] = null }

function videoTouchStart(serial: string, e: TouchEvent) {
  if (!e.touches.length) return; e.preventDefault()
  const touch = e.touches[0], video = e.target as HTMLVideoElement, rect = video.getBoundingClientRect()
  if (!touch) return
  const sx = video.videoWidth / rect.width, sy = video.videoHeight / rect.height
  dragState.value[serial] = { x: Math.round((touch.clientX - rect.left) * sx), y: Math.round((touch.clientY - rect.top) * sy), t: Date.now() }
  dragMoved.value[serial] = false
}
function videoTouchMove(serial: string) { if (dragState.value[serial]) dragMoved.value[serial] = true }
function videoTouchEnd(serial: string, e: TouchEvent) {
  const ds = dragState.value[serial]; if (!ds) return; e.preventDefault()
  const touch = e.changedTouches[0], video = e.target as HTMLVideoElement, rect = video.getBoundingClientRect()
  if (!touch) return
  const sx = video.videoWidth / rect.width, sy = video.videoHeight / rect.height
  const ex = Math.round((touch.clientX - rect.left) * sx), ey = Math.round((touch.clientY - rect.top) * sy)
  const sw = video.videoWidth, sh = video.videoHeight
  const dx = ex - ds.x, dy = ey - ds.y, dist = Math.sqrt(dx * dx + dy * dy)
  if (!dragMoved.value[serial] || dist < 20) {
    if (!sendInput(serial, { action: 'tap', x: ex, y: ey }))
      api('/api/phone/tap', { method: 'POST', body: JSON.stringify({ device: serial, x: ex, y: ey, stream_w: sw, stream_h: sh }) })
  } else {
    if (!sendInput(serial, { action: 'swipe', x1: ds.x, y1: ds.y, x2: ex, y2: ey, duration: 300 }))
      api('/api/phone/swipe', { method: 'POST', body: JSON.stringify({ device: serial, x1: ds.x, y1: ds.y, x2: ex, y2: ey, stream_w: sw, stream_h: sh }) })
  }
  dragState.value[serial] = null
}

function sendKeyTo(serial: string, key: number | string) {
  // Use /api/phone/key for string keys (BACK, HOME, etc.) and /api/phone/input for numeric keycodes
  // With SYNC ALL on, the same key is mirrored to every phone in scope.
  const done = (result: any) => { const note = noteSyncResult(serial, result); if (note) tileActionMessage.value[serial] = 'Key sent' + note }
  if (typeof key === 'string') {
    api('/api/phone/key', { method: 'POST', body: JSON.stringify({ device: serial, key, ...withSync(serial, {}) }) }).then(done).catch(() => {})
  } else {
    api('/api/phone/input', { method: 'POST', body: JSON.stringify({ device: serial, action: 'keyevent', keycode: key, ...withSync(serial, {}) }) }).then(done).catch(() => {})
  }
}

function hardwareKeys(serial: string): { label: string; key: number | string; title: string }[] {
  if (isIosDevice(serial)) {
    return [
      { label: 'Back', key: 'BACK', title: 'Back' },
      { label: 'Home', key: 'HOME', title: 'Home' },
      { label: 'Enter', key: 'ENTER', title: 'Enter' },
    ]
  }
  return [
    { label: '\u25C0', key: 4, title: 'Back' },
    { label: '\u2302', key: 3, title: 'Home' },
    { label: '\u25A6', key: 187, title: 'Recents' },
    { label: '\u23FB', key: 26, title: 'Power' },
    { label: 'Vol+', key: 24, title: 'Volume up' },
    { label: 'Vol-', key: 25, title: 'Volume down' },
  ]
}

/* ── screen recording ───────────────────────────────────────────────── */
const recordingState = ref<Record<string, boolean>>({})
const recordingBusy = ref<Record<string, boolean>>({})
const recordingStatus = ref<Record<string, string>>({})
const recordingUrls = ref<Record<string, string>>({})
const recordingFilenames = ref<Record<string, string>>({})

function applyRecordingStatus(serial: string, result: RecordingResponse) {
  recordingState.value[serial] = !!result.running
  if (result.filename) recordingFilenames.value[serial] = result.filename
  if (result.url) recordingUrls.value[serial] = result.url
  if (result.error) {
    recordingStatus.value[serial] = result.error
  } else if (result.running) {
    recordingStatus.value[serial] = result.mode ? `Recording ${result.mode}` : 'Recording'
  } else if (result.url) {
    recordingStatus.value[serial] = 'Saved recording'
  } else {
    recordingStatus.value[serial] = ''
  }
}

function recordingErrorText(error: unknown): string {
  if (error instanceof Error) return error.message.replace(/^API \d+:\s*/, '')
  return 'Recording request failed'
}

async function refreshRecordingStatus(serial: string) {
  if (!serial) return
  try {
    const result = await api<RecordingResponse>(`/api/phone/recording/status/${encodeURIComponent(serial)}`)
    applyRecordingStatus(serial, result)
  } catch (error) {
    recordingStatus.value[serial] = recordingErrorText(error)
  }
}

function refreshAllRecordingStatus() {
  for (const d of devices.value) refreshRecordingStatus(d.serial)
}

function recordingButtonText(serial: string): string {
  if (recordingBusy.value[serial]) return '...'
  return recordingState.value[serial] ? 'Stop Rec' : 'Record'
}

function recordingTinyText(serial: string): string {
  if (recordingBusy.value[serial]) return '...'
  return recordingState.value[serial] ? 'Stop' : 'Rec'
}

function recordingTitle(serial: string): string {
  if (recordingState.value[serial]) return 'Stop screen recording and save MP4'
  return isIosDevice(serial) ? 'Record WDA MJPEG stream to MP4' : 'Record Android screen to MP4'
}

async function toggleRecording(serial: string) {
  if (!serial || recordingBusy.value[serial]) return
  recordingBusy.value[serial] = true
  recordingStatus.value[serial] = ''
  try {
    const endpoint = recordingState.value[serial] ? '/api/phone/recording/stop' : '/api/phone/recording/start'
    const result = await api<RecordingResponse>(endpoint, {
      method: 'POST',
      body: JSON.stringify({ device: serial })
    })
    applyRecordingStatus(serial, result)
  } catch (error) {
    recordingStatus.value[serial] = recordingErrorText(error)
  } finally {
    recordingBusy.value[serial] = false
  }
}

/* ── iOS browser/news smoke workflow ────────────────────────────────── */
const newsUrl = ref('https://text.npr.org/')
const newsBundleId = ref('com.google.chrome.ios')
const newsMaxHeadlines = ref(5)
const newsMaxArticles = ref(3)
const newsWaitSeconds = ref(2)
const newsSaveScreenshots = ref(true)
const newsOutDir = ref('data/ios_chrome_news_smoke')
const newsRunning = ref(false)
const newsResult = ref<NewsResult | null>(null)
const newsError = ref('')

const newsHeadlines = computed(() => Array.isArray(newsResult.value?.headlines) ? newsResult.value!.headlines! : [])
const newsArticles = computed(() => Array.isArray(newsResult.value?.articles) ? newsResult.value!.articles! : [])
const newsErrors = computed(() => Array.isArray(newsResult.value?.errors) ? newsResult.value!.errors! : [])
const newsExtraction = computed(() => newsResult.value?.extraction || {})
const newsScreenshotEntries = computed(() =>
  Object.entries(newsResult.value?.screenshots || {}).filter(([, path]) => !!path)
)

function compactEvidence(evidence: any): string {
  if (!evidence) return ''
  const source = evidence.source || 'unknown'
  const attempts = evidence.attempts !== undefined ? `${evidence.attempts}x` : ''
  if (evidence.returned !== undefined) return `${source} ${evidence.returned}/${evidence.target} ${attempts}`.trim()
  if (evidence.returned_lines !== undefined) {
    return `${source} ${evidence.returned_lines}/${evidence.target_lines} lines ${attempts}`.trim()
  }
  return source
}

function articleEvidence(index: number): any {
  const items = newsExtraction.value?.articles
  return Array.isArray(items) ? items[index] || {} : {}
}

function firstNewsError(): string {
  if (newsResult.value?.error) return String(newsResult.value.error)
  const first = newsErrors.value[0]
  if (!first) return ''
  return String(first.error || first.back_error || first.stage || first.article || JSON.stringify(first))
}

async function runNewsSmoke() {
  if (!selectedDevice.value || newsRunning.value) return
  newsRunning.value = true
  newsError.value = ''
  newsResult.value = null
  try {
    const result = await api<NewsResult>('/api/phone/browser/read-news', {
      method: 'POST',
      body: JSON.stringify({
        device: selectedDevice.value,
        url: newsUrl.value || 'https://text.npr.org/',
        bundle_id: newsBundleId.value || undefined,
        max_headlines: Math.max(1, Number(newsMaxHeadlines.value) || 5),
        max_articles: Math.max(0, Number(newsMaxArticles.value) || 0),
        wait_s: Math.max(0.5, Number(newsWaitSeconds.value) || 2),
        save_screenshots: newsSaveScreenshots.value,
        out_dir: newsOutDir.value || undefined,
      })
    })
    newsResult.value = result
    if (!result.ok) newsError.value = firstNewsError() || 'News workflow failed'
  } catch (error) {
    newsError.value = error instanceof Error ? error.message.replace(/^API \d+:\s*/, '') : 'News workflow failed'
  } finally {
    newsRunning.value = false
  }
}

/* ── Frozen stream detection ──────────────────────────────────────────── */
const streamFrozen = ref<Record<string, boolean>>({})
const frozenTimers = ref<Record<string, number>>({})
const lastFrameHash = ref<Record<string, string>>({})
const frozenCount = ref<Record<string, number>>({})
// Also keep blackWarning for FLAG_SECURE
const blackWarning = ref<Record<string, boolean>>({})

function startBlackCheck(serial: string) {
  clearBlackCheck(serial)
  lastFrameHash.value[serial] = ''
  frozenCount.value[serial] = 0
  streamFrozen.value[serial] = false

  // Start periodic check: every 3s, sample video frame, compare hash
  frozenTimers.value[serial] = window.setInterval(() => {
    const video = document.getElementById(`rtc-video-${serial}`) as HTMLVideoElement
    if (!video || !video.videoWidth) return
    try {
      const c = document.createElement('canvas')
      c.width = 8; c.height = 8
      c.getContext('2d')!.drawImage(video, 0, 0, 8, 8)
      const data = c.getContext('2d')!.getImageData(0, 0, 8, 8).data
      // Simple hash: sum of all pixel values
      let hash = 0
      for (let i = 0; i < data.length; i += 4) hash += (data[i] ?? 0) + (data[i+1] ?? 0) + (data[i+2] ?? 0)
      const hashStr = hash.toString()

      // Black screen check (first time only)
      if (!lastFrameHash.value[serial] && hash / (8 * 8 * 3) < 3) {
        blackWarning.value[serial] = true
        setTimeout(() => { blackWarning.value[serial] = false }, 6000)
      }

      // Only check ICE state — if ICE is dead, stream is dead. Period.
      const sess = rtcSessions.value[serial]
      const iceState = sess?.pc?.iceConnectionState
      if (iceState === 'disconnected' || iceState === 'failed' || iceState === 'closed') {
        streamFrozen.value[serial] = true
      } else {
        streamFrozen.value[serial] = false
      }
    } catch {}
  }, 3000)
}

function clearBlackCheck(serial: string) {
  if (frozenTimers.value[serial]) { clearInterval(frozenTimers.value[serial]); delete frozenTimers.value[serial] }
  streamFrozen.value[serial] = false
  blackWarning.value[serial] = false
  frozenCount.value[serial] = 0
  lastFrameHash.value[serial] = ''
}

function reconnectStream(serial: string) {
  streamFrozen.value[serial] = false
  if (isIosDevice(serial)) {
    if (streaming.value && serial === selectedDevice.value) {
      stopStream()
      singleStreamMode.value = 'mjpeg'
      setTimeout(() => startStream(), 500)
    } else {
      stopMultiStream(serial)
      setTimeout(() => startMultiStream(serial, 'mjpeg'), 500)
    }
    return
  }
  const scope = subTab.value === 'single' ? 'single' : 'multi'
  rtcStop(serial, scope)
  setTimeout(() => rtcStart(serial, scope), 500)
}

/* ── toggle stream mode (switch while streaming) ────────────────────── */
function toggleMultiMode(serial: string) {
  if (!multiStreaming.value[serial] || isIosDevice(serial)) return
  stopMultiStream(serial)
  void startMultiStream(serial, 'rtc')
}
function toggleSingleMode() {
  if (!streaming.value || !selectedDevice.value || selectedIsIos.value) return
  stopStream()
  singleStreamMode.value = 'rtc'
  void startStream()
}
/* ── UI grid overlay toggle ──────────────────────────────────────────── */
const overlayOn = ref<Record<string, boolean>>({})
const overlayBusy = ref<Record<string, boolean>>({})

// Overlay writes are per-device transactions. A click while that device is busy is intentionally ignored;
// the existing button is disabled so a second stale toggle cannot be computed or sent.
async function toggleOverlay(serial: string) {
  if (isIosDevice(serial) || !alive || overlayBusy.value[serial]) return
  const nextValue = !Boolean(overlayOn.value[serial])
  overlayBusy.value[serial] = true
  tileActionMessage.value[serial] = 'Updating UI grid…'
  try {
    const result = await api<{ ok?: unknown }>(`/api/phone/overlay/${encodeURIComponent(serial)}`, {
      method: 'POST', body: JSON.stringify({ visible: nextValue })
    })
    if (!result || typeof result !== 'object' || result.ok !== true) throw new Error('Overlay update was not confirmed')
    if (!alive) return
    overlayOn.value[serial] = nextValue
    tileActionMessage.value[serial] = nextValue ? 'UI grid on' : 'UI grid off'
  } catch {
    if (alive) tileActionMessage.value[serial] = 'UI grid update failed'
  } finally {
    if (alive) delete overlayBusy.value[serial]
  }
}

/* ── multi-device ────────────────────────────────────────────────────── */
const multiStreaming = ref<Record<string, boolean>>({})
const multiStreamMode = ref<Record<string, 'rtc' | 'mjpeg'>>({})
const mjpegUrls = ref<Record<string, string>>({})

async function startMultiStream(serial: string, mode: 'rtc' | 'mjpeg' = 'rtc', remember = true) {
  if (!alive) return
  if (remember) persistStreamIntent(setDeviceIntent(streamIntent.value, serial, true))
  multiMounted.value = true
  // Start All is idempotent: a healthy viewer is never restarted.
  if (multiStreaming.value[serial] && (isIosDevice(serial) || (androidHandles.has('multi:' + serial) && androidCanvases.get('multi:' + serial)?.isConnected))) return
  if (multiStreaming.value[serial]) stopMultiStream(serial, false)
  const actualMode = effectiveStreamMode(serial, mode)
  multiStreaming.value[serial] = true
  multiStreamMode.value[serial] = actualMode
  if (actualMode === 'rtc') {
    await rtcStart(serial)
  } else {
    streamFrozen.value[serial] = false
    mjpegReconnects.value[serial] = 0
    streamInfoStatus.value[serial] = isIosDevice(serial) ? 'Loading WDA MJPEG...' : ''
    mjpegUrls.value[serial] = mjpegStreamUrl(serial)
    mjpegUrls.value[serial] = await resolveMjpegStreamUrl(serial, actualMode)
    startMjpegWatchdog(serial)
  }
}
function stopMultiStream(serial: string, remember = true) {
  gridInteractions.get(serial)?.cancel()
  if (remember) persistStreamIntent(setDeviceIntent(streamIntent.value, serial, false))
  if (controlDevice.value === serial) controlDevice.value = ''
  multiStreaming.value[serial] = false
  if (multiStreamMode.value[serial] === 'rtc') rtcStop(serial)
  stopMjpegWatchdog(serial)
  mjpegUrls.value[serial] = ''
  streamInfoStatus.value[serial] = ''
  multiStreamMode.value[serial] = 'rtc'
}
let startBatch = 0
// Start All shows what is actually happening instead of a fixed label.
const startingAll = ref(false)
let startAllToken = 0
const androidSerials = computed(() => devices.value.filter(d => !isIosDevice(d.serial)).map(d => String(d.serial)))
const requestedStreamCount = computed(() => androidSerials.value.filter(serial => multiStreaming.value[serial]).length)
const androidLiveCount = computed(() => androidSerials.value.filter(serial => tileState(serial) === 'live').length)
const startAllState = computed<'idle' | 'partial' | 'all' | 'starting'>(() => {
  const total = androidSerials.value.length
  if (startingAll.value) return 'starting'
  if (total && requestedStreamCount.value >= total) return 'all'
  return requestedStreamCount.value > 0 ? 'partial' : 'idle'
})
const startAllText = computed(() => {
  const total = androidSerials.value.length
  switch (startAllState.value) {
    case 'starting': return `Starting ${requestedStreamCount.value}/${total}…`
    case 'all': return `Streaming ${androidLiveCount.value}/${total}`
    case 'partial': return `Start ${total - requestedStreamCount.value} more`
    default: return 'Start All (H.264)'
  }
})
const startAllTitle = computed(() => startAllState.value === 'all'
  ? 'Every phone is streaming over H.264. Click to reconnect any stream that stalled.'
  : startAllState.value === 'partial' ? `Start H.264 on the ${androidSerials.value.length - requestedStreamCount.value} phones that are not streaming` : 'Native Android H.264 over WebSocket; no Portal, ICE, or MJPEG')
async function startAllStreams(mode: 'rtc' | 'mjpeg' = 'rtc') {
  persistStreamIntent(startAllIntent(streamIntent.value))
  multiMounted.value = true
  const batch = ++startBatch
  const token = ++startAllToken
  startingAll.value = devices.value.some(d => !multiStreaming.value[d.serial])
  try {
    for (const d of devices.value) {
      if (batch !== startBatch) return
      if (alive && wantsStream(streamIntent.value, d.serial) && !multiStreaming.value[d.serial]) {
        await startMultiStream(d.serial, mode, false)
        await new Promise(resolve => setTimeout(resolve, 80))
      }
    }
    // Already-running phones are re-checked: a stalled viewer reconnects, a healthy one is left alone.
    if (batch === startBatch) void reconcileStreams()
  } finally { if (token === startAllToken) startingAll.value = false }
}
function stopAllStreams() { persistStreamIntent(stopAllIntent()); startBatch++; startAllToken++; startingAll.value = false; controlDevice.value = ''; for (const serial of Object.keys(multiStreaming.value)) stopMultiStream(serial, false) }

/* ── single device ───────────────────────────────────────────────────── */
const singleStreamMode = ref<'rtc' | 'mjpeg' | 'h264'>('rtc')
const singleMjpegUrl = ref('')

/* ── H.264 (GhostAgent WebSocket stream, iOS) ─────────────────────────── */
const h264Status = ref('')
const h264ShowMetrics = ref(true)
const h264M = ref({ recvFps: 0, renderFps: 0, queue: 0, latencyMs: 0, kbps: 0, dropped: 0 })
let h264Handle: { status: any; recvFps: any; renderFps: any; queue: any; latencyMs: any; kbps: any; dropped: any; stop: () => void } | null = null
function h264StreamWsUrl(serial: string): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/api/phone/h264/${encodeURIComponent(serial)}`
}
async function startH264(serial: string) {
  stopH264()
  await nextTick()
  const canvas = document.getElementById(`h264-canvas-${serial}`) as HTMLCanvasElement | null
  if (!canvas) return
  const { startH264Stream, h264Supported } = await import('@/composables/useH264Stream')
  if (!h264Supported()) { h264Status.value = 'WebCodecs unavailable — use MJPEG'; return }
  h264Handle = startH264Stream(h264StreamWsUrl(serial), canvas)
  const h = h264Handle
  watch(h.status, (v: string) => { h264Status.value = v })
  const sync = () => { h264M.value = { recvFps: h.recvFps.value, renderFps: h.renderFps.value, queue: h.queue.value, latencyMs: h.latencyMs.value, kbps: h.kbps.value, dropped: h.dropped.value } }
  watch([h.recvFps, h.renderFps, h.queue, h.latencyMs, h.kbps, h.dropped], sync)
}
function stopH264() {
  if (h264Handle) { h264Handle.stop(); h264Handle = null }
  h264Status.value = ''
  h264M.value = { recvFps: 0, renderFps: 0, queue: 0, latencyMs: 0, kbps: 0, dropped: 0 }
}

async function startStream() {
  if (!selectedDevice.value) return
  const serial = selectedDevice.value
  if (singleStreamMode.value !== 'h264') singleStreamMode.value = effectiveStreamMode(serial, singleStreamMode.value)
  streaming.value = true
  if (singleStreamMode.value === 'h264') {
    await startH264(serial)
  } else if (singleStreamMode.value === 'rtc') {
    rtcStart(serial, 'single')
    // H.264 reconnects on its own; never switch Android to MJPEG.
  } else {
    streamFrozen.value[serial] = false
    mjpegReconnects.value[serial] = 0
    streamInfoStatus.value[serial] = isIosDevice(serial) ? 'Loading WDA MJPEG...' : ''
    singleMjpegUrl.value = mjpegStreamUrl(serial)
    singleMjpegUrl.value = await resolveMjpegStreamUrl(serial, singleStreamMode.value)
    startMjpegWatchdog(serial)
  }
}
function stopStream() {
  if (selectedDevice.value) { rtcStop(selectedDevice.value, 'single'); stopMjpegWatchdog(selectedDevice.value) }
  stopH264()
  singleMjpegUrl.value = ''
  if (selectedDevice.value) streamInfoStatus.value[selectedDevice.value] = ''
  streaming.value = false
}

/* ── MJPEG tap/swipe (uses naturalWidth/Height for coordinate scaling) ─ */
function mjpegImageLoaded(serial: string) {
  streamFrozen.value[serial] = false
  blackWarning.value[serial] = false
  if (streamInfoStatus.value[serial]?.startsWith('Loading ')) streamInfoStatus.value[serial] = ''
  if (isIosDevice(serial)) rtcStatus.value[serial] = 'WDA MJPEG'
}

function mjpegImageError(serial: string) {
  streamFrozen.value[serial] = true
  streamInfoStatus.value[serial] = isIosDevice(serial)
    ? 'WDA MJPEG failed — reconnecting…'
    : 'MJPEG stream failed — reconnecting…'
  // Hard error → reconnect right away (backoff-limited by the watchdog).
  reconnectMjpeg(serial)
}

/* ── MJPEG stall watchdog ─────────────────────────────────────────────────
 * An <img> streaming multipart-JPEG holds its LAST frame forever if the
 * connection silently stalls — no 'error' event fires, so health stays green
 * while the picture is frozen. Sample the frame to a tiny canvas every few
 * seconds; if it hasn't changed for a while, force a reconnect (cache-busted
 * src reload). Reconnecting a genuinely-static screen is harmless. */
const mjpegWatchTimers = ref<Record<string, number>>({})
const mjpegLastHash = ref<Record<string, string>>({})
const mjpegStaleCount = ref<Record<string, number>>({})
const mjpegReconnects = ref<Record<string, number>>({})
const MJPEG_STALL_CHECKS = 3          // ~3 * 2.5s = ~7.5s frozen → reconnect
const MJPEG_MAX_RECONNECTS = 20       // safety cap; reset on any fresh frame

function mjpegFrameHash(serial: string): string | null {
  const img = document.getElementById(`mjpeg-img-${serial}`) as HTMLImageElement | null
  if (!img || !img.naturalWidth) return null
  try {
    const c = document.createElement('canvas')
    c.width = 16; c.height = 16
    const ctx = c.getContext('2d')!
    ctx.drawImage(img, 0, 0, 16, 16)
    const d = ctx.getImageData(0, 0, 16, 16).data
    let h = 0
    for (let i = 0; i < d.length; i += 4) h = (h * 31 + (d[i]! + d[i+1]! * 3 + d[i+2]! * 7)) | 0
    return String(h)
  } catch { return null }  // tainted canvas etc. → skip (don't false-reconnect)
}

function reconnectMjpeg(serial: string) {
  if ((mjpegReconnects.value[serial] || 0) >= MJPEG_MAX_RECONNECTS) return
  mjpegReconnects.value[serial] = (mjpegReconnects.value[serial] || 0) + 1
  mjpegStaleCount.value[serial] = 0
  const base = mjpegStreamUrl(serial)
  const sep = base.includes('?') ? '&' : '?'
  // New URL forces the <img> to drop the dead connection and open a fresh one.
  const url = `${base}${sep}_r=${mjpegReconnects.value[serial]}`
  // Route to whichever view is showing this device (multi grid vs single panel).
  if (multiStreaming.value[serial]) mjpegUrls.value[serial] = url
  else singleMjpegUrl.value = url
}

function startMjpegWatchdog(serial: string) {
  stopMjpegWatchdog(serial)
  mjpegLastHash.value[serial] = ''
  mjpegStaleCount.value[serial] = 0
  mjpegWatchTimers.value[serial] = window.setInterval(() => {
    // Active if this device is streaming MJPEG in either the single panel or the grid.
    const singleActive = streaming.value && selectedDevice.value === serial && singleStreamMode.value === 'mjpeg'
    const multiActive = multiStreaming.value[serial] && multiStreamMode.value[serial] === 'mjpeg'
    if (!singleActive && !multiActive) return
    const hash = mjpegFrameHash(serial)
    if (hash == null) return
    if (hash === mjpegLastHash.value[serial]) {
      mjpegStaleCount.value[serial] = (mjpegStaleCount.value[serial] || 0) + 1
      if (mjpegStaleCount.value[serial] >= MJPEG_STALL_CHECKS) {
        streamInfoStatus.value[serial] = 'Stream stalled — reconnecting…'
        reconnectMjpeg(serial)
      }
    } else {
      mjpegLastHash.value[serial] = hash
      mjpegStaleCount.value[serial] = 0
      mjpegReconnects.value[serial] = 0   // healthy frames → reset backoff
      streamFrozen.value[serial] = false
    }
  }, 2500)
}

function stopMjpegWatchdog(serial: string) {
  if (mjpegWatchTimers.value[serial]) { clearInterval(mjpegWatchTimers.value[serial]); delete mjpegWatchTimers.value[serial] }
}

function mjpegImagePoint(img: HTMLImageElement | HTMLCanvasElement, clientX: number, clientY: number): { x: number; y: number; width: number; height: number } | null {
  const rect = img.getBoundingClientRect()
  const sw = (img as HTMLImageElement).naturalWidth || (img as HTMLCanvasElement).width
  const sh = (img as HTMLImageElement).naturalHeight || (img as HTMLCanvasElement).height
  if (!sw || !sh || !rect.width || !rect.height) return null
  const scale = Math.min(rect.width / sw, rect.height / sh)
  const x = (clientX - rect.left - (rect.width - sw * scale) / 2) / scale
  const y = (clientY - rect.top - (rect.height - sh * scale) / 2) / scale
  if (x < 0 || y < 0 || x > sw || y > sh) return null
  return { x: Math.round(x), y: Math.round(y), width: sw, height: sh }
}
function mjpegMouseDown(serial: string, e: MouseEvent) {
  e.preventDefault()
  const img = e.target as HTMLImageElement
  const point = mjpegImagePoint(img, e.clientX, e.clientY)
  if (!point) return
  dragState.value[serial] = { x: point.x, y: point.y, t: Date.now() }
  dragMoved.value[serial] = false
}

function mjpegMove(serial: string) {
  if (dragState.value[serial]) dragMoved.value[serial] = true
}

function mjpegMouseUp(serial: string, e: MouseEvent) {
  const ds = dragState.value[serial]; if (!ds) return; e.preventDefault()
  const img = e.target as HTMLImageElement
  const point = mjpegImagePoint(img, e.clientX, e.clientY)
  if (!point) { dragState.value[serial] = null; return }
  const dx = point.x - ds.x, dy = point.y - ds.y, dist = Math.sqrt(dx * dx + dy * dy)
  if (!dragMoved.value[serial] || dist < 20) {
    api('/api/phone/tap', { method: 'POST', body: JSON.stringify({ device: serial, x: point.x, y: point.y, stream_w: point.width, stream_h: point.height }) })
  } else {
    api('/api/phone/swipe', { method: 'POST', body: JSON.stringify({ device: serial, x1: ds.x, y1: ds.y, x2: point.x, y2: point.y, stream_w: point.width, stream_h: point.height }) })
  }
  dragState.value[serial] = null
}

function mjpegTouchStart(serial: string, e: TouchEvent) {
  if (!e.touches.length) return
  e.preventDefault()
  const touch = e.touches[0]
  if (!touch) return
  const point = mjpegImagePoint(e.target as HTMLImageElement, touch.clientX, touch.clientY)
  if (!point) return
  dragState.value[serial] = { x: point.x, y: point.y, t: Date.now() }
  dragMoved.value[serial] = false
}

function mjpegTouchMove(serial: string, e: TouchEvent) {
  if (!dragState.value[serial]) return
  e.preventDefault()
  dragMoved.value[serial] = true
}

function mjpegTouchEnd(serial: string, e: TouchEvent) {
  const ds = dragState.value[serial]
  if (!ds) return
  e.preventDefault()
  const touch = e.changedTouches[0]
  if (!touch) { dragState.value[serial] = null; return }
  const point = mjpegImagePoint(e.target as HTMLImageElement, touch.clientX, touch.clientY)
  if (!point) { dragState.value[serial] = null; return }
  const dx = point.x - ds.x, dy = point.y - ds.y, dist = Math.sqrt(dx * dx + dy * dy)
  if (!dragMoved.value[serial] || dist < 20) {
    api('/api/phone/tap', { method: 'POST', body: JSON.stringify({ device: serial, x: point.x, y: point.y, stream_w: point.width, stream_h: point.height }) })
  } else {
    api('/api/phone/swipe', { method: 'POST', body: JSON.stringify({ device: serial, x1: ds.x, y1: ds.y, x2: point.x, y2: point.y, stream_w: point.width, stream_h: point.height }) })
  }
  dragState.value[serial] = null
}

function mjpegTouchCancel(serial: string) { dragState.value[serial] = null }

/* ── per-device logs ─────────────────────────────────────────────────── */
const multiLogs = ref<Record<string, string[]>>({})
let multiLogsBusy = false
async function pollMultiLogs() {
  if (!showCardLogs.value || multiLogsBusy || document.hidden || controlDevice.value || subTab.value !== 'multi') return
  multiLogsBusy = true
  try {
    for (let offset = 0; offset < devices.value.length; offset += 4) {
      await Promise.all(devices.value.slice(offset, offset + 4).map(async d => {
        try {
          const resp = await api(`/api/logs?device=${encodeURIComponent(d.serial)}&limit=12`)
          multiLogs.value[d.serial] = (resp.lines || []).map((l: any) => `[${l.level}] ${l.msg}`)
        } catch { if (!multiLogs.value[d.serial]) multiLogs.value[d.serial] = [] }
      }))
    }
  } finally { multiLogsBusy = false }
}

/* ── device health ──────────────────────────────────────────────────── */
const healthData = ref<Record<string, any>>({})
const healthLoading = ref<Record<string, boolean>>({})
const healthFixStatus = ref<Record<string, string>>({})
const showRecoveryDetails = ref<Record<string, boolean>>({})
function toggleRecoveryDetails(serial: string) {
  showRecoveryDetails.value[serial] = !showRecoveryDetails.value[serial]
}
const showWirelessModal = ref(false)
const wirelessIp = ref('')
const wirelessPort = ref('5555')
const wirelessCode = ref('')
const wirelessResult = ref('')
const IOS_AUTO_FIXES = new Set(['start_appium', 'reset_session', 'appium_session', 'wda_session', 'restart_remote_xpc_tunnel'])

// Auto-reset a stale WDA session without the user clicking. Only for session
// resets (not the heavier tunnel restart), one attempt per distinct dead
// session, capped so a persistently broken device can't loop (Reset WDA stays
// available manually). The guard clears once the device is healthy again.
const AUTO_RESET_ACTIONS = new Set(['reset_session', 'appium_session', 'wda_session'])
const autoResetGuard = ref<Record<string, { key: string; attempts: number }>>({})

function maybeAutoReset(serial: string) {
  const h = healthData.value[serial]
  if (!h) return
  const state = String(h?.connection?.status || h?.appium?.state || '')
  if (['available', 'ready', 'connected', 'online'].includes(state)) {
    delete autoResetGuard.value[serial]
    return
  }
  const action = iosRecoveryAction(serial)
  if (!AUTO_RESET_ACTIONS.has(action) || !iosRecoveryCanApplyFix(serial) || iosRecoveryFixBusy(serial)) return
  const key = String(h?.wda?.session || h?.appium?.session_id || state)
  const g = autoResetGuard.value[serial]
  if (g && g.key === key) return
  const attempts = (g?.attempts || 0) + 1
  if (attempts > 2) return
  autoResetGuard.value[serial] = { key, attempts }
  healthFixStatus.value[serial] = 'Auto-resetting WDA…'
  fixIssue(serial, action)
}

async function loadHealth(serial: string) {
  healthLoading.value[serial] = true
  try {
    healthData.value[serial] = await api(`/api/phone/health/${serial}`)
  } catch { healthData.value[serial] = null }
  healthLoading.value[serial] = false
  maybeAutoReset(serial)
}

async function loadAllHealth() {
  const serials = devices.value.map(d => d.serial)
  let index = 0
  await Promise.all(Array.from({ length: Math.min(3, serials.length) }, async () => {
    while (alive && index < serials.length) { const serial = serials[index++]; if (serial) await loadHealth(serial) }
  }))
}

function isIosHealthPayload(h: any): boolean {
  return h?.platform === 'ios' || h?.connection?.type === 'appium-wda'
}

function iosActiveAppLabel(h: any): string {
  const active = h?.wda?.active_app || h?.device_info?.active_app || {}
  return String(active.name || active.bundleId || active.bundle_id || '').trim()
}

function healthDotColor(c: string): string {
  return c === 'green' ? '#22c55e' : c === 'yellow' ? '#f59e0b' : c === 'red' ? '#ef4444' : '#475569'
}

function healthDots(serial: string): string[] {
  const h = healthData.value[serial]
  if (!h) return []
  if (isIosHealthPayload(h)) {
    const state = h.connection?.status || h.appium?.state || 'session_error'
    const appiumReachable = h.appium?.reachable === true
    const sessionOk = state === 'available' || !!h.wda?.session || !!h.appium?.session_id
    const screenshotOk = h.wda?.screenshot_ok === true || (h.wda?.checks?.screenshot_bytes || 0) > 0
    const sourceOk = h.wda?.source_ok === true || (h.wda?.checks?.source_bytes || 0) > 0
    const activeOk = !!iosActiveAppLabel(h)
    const reachableMissing = appiumReachable ? 'yellow' : 'red'
    return [
      appiumReachable ? 'green' : 'red',
      sessionOk ? 'green' : reachableMissing,
      screenshotOk ? 'green' : reachableMissing,
      sourceOk ? 'green' : reachableMissing,
      activeOk ? 'green' : 'gray',
    ]
  }
  const dots: string[] = []
  dots.push(h.portal?.http_responding ? 'green' : h.portal?.installed ? 'yellow' : 'red')
  dots.push(h.wifi?.connected ? 'green' : 'gray')
  dots.push(h.battery?.level > 20 ? 'green' : h.battery?.level > 5 ? 'yellow' : 'red')
  dots.push(h.storage?.free_mb > 1000 ? 'green' : h.storage?.free_mb > 500 ? 'yellow' : 'red')
  dots.push(h.screen_on ? 'green' : 'yellow')
  return dots
}

function healthTitle(serial: string): string {
  const h = healthData.value[serial]
  if (!h) return ''
  if (!isIosHealthPayload(h)) return 'Portal / WiFi / Battery / Storage / Screen'
  const state = h.connection?.status || h.appium?.state || 'unknown'
  const message = String(h.appium?.message || h.error || '').trim()
  const activeApp = iosActiveAppLabel(h)
  const recovery = iosRecovery(serial)
  const parts = [`Appium / WDA / Screenshot / Source / Active app: ${state}`]
  if (activeApp) parts.push(`active ${activeApp}`)
  if (h.recommended_fix) parts.push(`fix ${h.recommended_fix}`)
  if (recovery?.summary) parts.push(recovery.summary)
  if (message) parts.push(message)
  return parts.join(' | ')
}

function iosRecovery(serial: string): any | null {
  const h = healthData.value[serial]
  if (!isIosHealthPayload(h)) return null
  return h?.recovery || null
}

function iosRecoveryVisible(serial: string): boolean {
  // The tunnel/WDA health probe is synchronous and slow (often >10s), so the
  // dashboard poll times out and the card flashes "remote_xpc_tunnel_unavailable"
  // even while a live stream is flowing over that exact tunnel — pure noise. The
  // stream itself is proof the tunnel works. Suppress this recovery card on the
  // Phone Agent tab entirely; full, honest diagnostics live in the 🩺 Device
  // Health tab where a stale reading isn't sitting on top of a working stream.
  void serial
  return false
}

function iosRecoveryState(serial: string): string {
  const h = healthData.value[serial]
  return String(h?.connection?.status || h?.appium?.state || iosRecovery(serial)?.state || 'unknown')
}

function iosRecoveryCode(serial: string): string {
  return String(iosRecovery(serial)?.code || healthData.value[serial]?.recommended_fix || '')
}

function iosRecoveryAction(serial: string): string {
  return iosRecoveryCode(serial) || String(healthData.value[serial]?.recommended_fix || '')
}

function iosRecoveryCanApplyFix(serial: string): boolean {
  if (!serial || !isIosDevice(serial)) return false
  const recovery = iosRecovery(serial)
  if (recovery?.auto_fixable === false || recovery?.manual_action_required === true) return false
  return IOS_AUTO_FIXES.has(iosRecoveryAction(serial))
}

function iosRecoveryFixBusy(serial: string): boolean {
  return healthFixStatus.value[serial]?.startsWith('Applying ') || false
}

function iosRecoveryActionLabel(serial: string): string {
  const action = iosRecoveryAction(serial)
  if (action === 'start_appium') return 'Start Appium'
  if (action === 'restart_remote_xpc_tunnel') return 'Restart Tunnel'
  if (action === 'reset_session' || action === 'appium_session' || action === 'wda_session') return 'Reset WDA'
  return 'Apply Fix'
}

function iosRecoveryActionTitle(serial: string): string {
  const action = iosRecoveryAction(serial)
  const recovery = iosRecovery(serial)
  if (recovery?.auto_fixable === false || recovery?.manual_action_required === true) return 'Manual recovery is required; copy the recovery commands below'
  if (action === 'start_appium') return 'Start local Appium when IOS_APPIUM_URL points to localhost'
  if (action === 'restart_remote_xpc_tunnel') return 'Restart the XCUITest RemoteXPC tunnel when the process is owned by this user'
  if (action) return `Apply iOS health fix: ${action}`
  return 'No automatic iOS health fix is available'
}

function iosRecoverySummary(serial: string): string {
  return String(iosRecovery(serial)?.summary || healthData.value[serial]?.appium?.message || '')
}

function iosRecoverySteps(serial: string): string[] {
  const steps = iosRecovery(serial)?.steps
  return Array.isArray(steps) ? steps.slice(0, 3).map(step => String(step)) : []
}

function iosRecoveryCommands(serial: string): string[] {
  const commands = iosRecovery(serial)?.commands
  return Array.isArray(commands) ? commands.map(cmd => String(cmd)).filter(Boolean) : []
}

function iosHealthDetails(serial: string): { label: string; value: string; ok?: boolean }[] {
  const h = healthData.value[serial]
  if (!isIosHealthPayload(h)) return []
  const wda = h?.wda || {}
  const appium = h?.appium || {}
  const rows = [
    { label: 'Appium', value: appium.reachable ? 'reachable' : 'down', ok: appium.reachable === true },
    { label: 'WDA', value: wda.ready ? 'ready' : String(appium.state || h?.connection?.status || 'not ready'), ok: wda.ready === true },
    { label: 'Session', value: String(wda.session || appium.session_id || 'none'), ok: !!(wda.session || appium.session_id) },
    { label: 'Stream', value: String(wda.mjpeg_url || 'WDA MJPEG'), ok: wda.ready === true },
  ]
  return rows.filter(row => row.value)
}

async function copyIosRecoveryCommand(serial: string, command: string) {
  try {
    await navigator.clipboard.writeText(command)
    healthFixStatus.value[serial] = 'Copied recovery command'
  } catch {
    healthFixStatus.value[serial] = 'Copy failed'
  }
}

async function fixIssue(serial: string, issue: string) {
  if (!serial || !issue) return
  healthFixStatus.value[serial] = `Applying ${issue}...`
  try {
    const result = await api(`/api/phone/health/${serial}/fix`, { method: 'POST', body: JSON.stringify({ issue }) })
    if (result.ok) {
      healthFixStatus.value[serial] = result.message || 'Fix applied'
    } else if (result.manual_action_required) {
      healthFixStatus.value[serial] = result.message || 'Manual action required'
    } else {
      healthFixStatus.value[serial] = result.error || 'Fix failed'
    }
  } catch (error) {
    healthFixStatus.value[serial] = error instanceof Error ? error.message.replace(/^API \d+:\s*/, '') : 'Fix failed'
  } finally {
    await loadHealth(serial)
  }
}

async function goWireless(serial: string) {
  const res = await api('/api/phone/wireless/enable', { method: 'POST', body: JSON.stringify({ device: serial }) })
  if (res.ok) statusText.value = `WiFi enabled: ${res.wifi_ip}:${res.wifi_port}`
  else statusText.value = res.error || 'WiFi enable failed'
  await loadDevices()
}

async function wirelessConnect() {
  wirelessResult.value = 'Connecting...'
  const res = wirelessCode.value
    ? await api('/api/phone/wireless/pair', { method: 'POST', body: JSON.stringify({ ip: wirelessIp.value, port: parseInt(wirelessPort.value), code: wirelessCode.value }) })
    : await api('/api/phone/wireless/connect', { method: 'POST', body: JSON.stringify({ ip: wirelessIp.value, port: parseInt(wirelessPort.value) }) })
  wirelessResult.value = res.ok ? `Connected: ${res.device_serial || wirelessIp.value}` : (res.error || 'Failed')
  if (res.ok) { showWirelessModal.value = false; await loadDevices() }
}

/* ── agent chat ─────────────────────────────────────────────────────── */
const chatMessages = ref<{role: string; content: string; tool_name?: string; tool_args?: any; image?: string}[]>([])
const chatInput = ref('')
const chatSending = ref(false)
const chatSessionId = ref('')
let chatAbortController: AbortController | null = null
let chatRunCounter = 0
let chatActiveRunId = ''
let chatStopRequested = false
const chatTokens = ref(0)
const chatActivity = ref('')  // brief status during thinking
const chatVerbose = ref(false) // show tool calls inline in chat

/* ── Save conversation as skill ──────────────────────────────────────── */
const saveSkillModal = ref(false)
const saveSkillKind = ref<'hard' | 'soft'>('hard')
const saveSkillName = ref('')
const saveSkillPackage = ref('')
const saveSkillDesc = ref('')
const saveSkillSteps = ref('')      // editable pretty-printed JSON of recorded steps
const saveSkillGuidance = ref('')   // markdown for soft skills
const saveSkillSummary = ref('')    // draft summary from backend
const saveSkillLoading = ref(false) // draft fetch in flight
const saveSkillSaving = ref(false)  // save in flight
const saveSkillError = ref('')

function openSaveSkillModal() {
  if (!chatSessionId.value) return
  saveSkillKind.value = 'hard'
  saveSkillName.value = ''
  saveSkillPackage.value = ''
  saveSkillDesc.value = ''
  saveSkillSteps.value = ''
  saveSkillGuidance.value = ''
  saveSkillSummary.value = ''
  saveSkillError.value = ''
  saveSkillModal.value = true
  loadDraftSteps()
}

function setSaveSkillKind(k: 'hard' | 'soft') {
  saveSkillKind.value = k
  if (k === 'hard' && !saveSkillSteps.value && chatSessionId.value) loadDraftSteps()
}

async function loadDraftSteps() {
  if (!chatSessionId.value) return
  saveSkillLoading.value = true
  saveSkillError.value = ''
  try {
    const resp = await fetch('/api/skills/draft-from-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: chatSessionId.value }),
    })
    if (!resp.ok) throw new Error(await resp.text())
    const data = await resp.json()
    saveSkillSummary.value = data.summary || ''
    saveSkillSteps.value = JSON.stringify(data.steps || [], null, 2)
    if (data.app_package && !saveSkillPackage.value) saveSkillPackage.value = data.app_package
  } catch (e: any) {
    saveSkillError.value = 'Failed to load steps: ' + e.message
  } finally {
    saveSkillLoading.value = false
  }
}

async function saveSkillFromChat() {
  saveSkillError.value = ''
  if (!saveSkillName.value.trim()) { saveSkillError.value = 'Name is required.'; return }
  const body: any = {
    conversation_id: chatSessionId.value || undefined,
    kind: saveSkillKind.value,
    name: saveSkillName.value.trim(),
    app_package: saveSkillPackage.value.trim() || undefined,
    description: saveSkillDesc.value.trim() || undefined,
  }
  if (saveSkillKind.value === 'hard') {
    try { body.steps = JSON.parse(saveSkillSteps.value || '[]') }
    catch (e: any) { saveSkillError.value = 'Steps must be valid JSON: ' + e.message; return }
  } else {
    body.guidance = saveSkillGuidance.value
  }
  saveSkillSaving.value = true
  try {
    const resp = await fetch('/api/skills/save-from-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok) throw new Error(await resp.text())
    const data = await resp.json()
    statusText.value = data.message || 'Skill saved.'
    saveSkillModal.value = false
  } catch (e: any) {
    saveSkillError.value = 'Save failed: ' + e.message
  } finally {
    saveSkillSaving.value = false
  }
}

// Markdown rendering
function renderMd(text: string): string {
  const w = window as any
  if (w.marked) return w.marked.parse(text)
  return text.replace(/\n/g, '<br>')
}
// Load marked.js
if (!(window as any).marked) {
  const s = document.createElement('script')
  s.src = 'https://cdn.jsdelivr.net/npm/marked/marked.min.js'
  document.head.appendChild(s)
}
const chatProvider = ref(localStorage.getItem('agent_provider') || DEFAULT_AGENT_PROVIDER)
const chatModel = ref(localStorage.getItem('agent_model') || DEFAULT_AGENT_MODEL)
const CHAT_PROVIDERS = ref<AgentProviderOption[]>([
  { id: 'claude-code', label: 'Claude Code', models: ['sonnet', 'opus', 'haiku'] },
  { id: 'anthropic', label: 'Claude API', models: ['claude-sonnet-4-20250514', 'claude-opus-4-20250514'] },
  { id: 'openrouter', label: 'OpenRouter', models: ['anthropic/claude-sonnet-4', 'google/gemini-2.5-pro'] },
  { id: DEFAULT_AGENT_PROVIDER, label: 'Ollama (local)', models: [DEFAULT_AGENT_MODEL] },
])
const aiSettingsOpen = ref(false)
function openAiSettings() { if (!chatSending.value) aiSettingsOpen.value = true }
function closeAiSettings() { if (!chatSending.value) aiSettingsOpen.value = false }
async function handleAiSettingsSaved() {
  const activeProvider = chatProvider.value
  const activeModel = chatModel.value
  await fetchProviders()
  // A saved provider list refreshes new-chat choices, but never rewrites a live conversation.
  if (chatSessionId.value) { chatProvider.value = activeProvider; chatModel.value = activeModel }
  else await fetchSavedAiDefaults()
}

function applyProviderList(raw: unknown) {
  const providers = Array.isArray(raw) ? raw.filter((item): item is AgentProviderOption => !!item && typeof item === 'object' && typeof (item as any).id === 'string').map((item: any) => ({
    id: item.id, label: String(item.label || item.id), models: Array.isArray(item.models) ? item.models.map(String) : [],
  })) : []
  if (!providers.some((item) => item.id === DEFAULT_AGENT_PROVIDER)) providers.push({ id: DEFAULT_AGENT_PROVIDER, label: 'Ollama (local)', models: [DEFAULT_AGENT_MODEL] })
  CHAT_PROVIDERS.value = providers
  const selection = resolveAgentSelection(providers, chatProvider.value, chatModel.value)
  chatProvider.value = selection.provider
  chatModel.value = selection.model
}

async function fetchProviders() {
  try {
    const resp = await fetch('/api/agent-chat/providers')
    if (resp.ok) applyProviderList(await resp.json())
  } catch {}
}

async function fetchSavedAiDefaults() {
  if (chatSessionId.value || chatSending.value) return
  try {
    const response = await fetch('/api/ai-settings', { credentials: 'same-origin', cache: 'no-store' })
    if (!response.ok) return
    const data = await response.json()
    if (chatSessionId.value || chatSending.value) return
    const choice = resolveAgentSelection(CHAT_PROVIDERS.value, data.default_provider, data.default_model)
    chatProvider.value = choice.provider; chatModel.value = choice.model
    if (data.defaults) {
      const options = clampAgentOptions({ contextLength: data.defaults.context_length, maxOutputTokens: data.defaults.max_output_tokens, maxTurns: data.defaults.max_turns })
      chatContextLength.value = options.contextLength; chatMaxOutputTokens.value = options.maxOutputTokens; chatMaxTurns.value = options.maxTurns
    }
    persistAgentPreferences()
    if (chatProvider.value === 'ollama') void fetchOllamaStatus()
  } catch { /* A failed configuration read never switches to a cloud provider. */ }
}

// Ollama model status
const ollamaModels = ref<AgentOllamaModel[]>([])
const ollamaStatusOk = ref(false)
const ollamaStatusError = ref('')
const ollamaLoading = ref('')  // model name currently loading
const ollamaPullStatus = ref<AgentPullStatus | null>(null)
const chatAutoScreenshot = ref(localStorage.getItem('agent_auto_screenshot') !== 'false')
const chatAdvancedOpen = ref(false)
const chatContextLength = ref(8192)
const chatMaxOutputTokens = ref(1024)
const chatMaxTurns = ref(12)

function persistAgentPreferences() {
  localStorage.setItem('agent_provider', chatProvider.value)
  localStorage.setItem('agent_model', chatModel.value)
  localStorage.setItem('agent_auto_screenshot', String(chatAutoScreenshot.value))
}

async function fetchOllamaStatus() {
  if (chatProvider.value !== 'ollama') return
  try {
    const resp = await fetch('/api/agent-chat/ollama/status')
    if (resp.ok) {
      const data = await resp.json()
      ollamaStatusOk.value = data.ok === true
      ollamaStatusError.value = ollamaStatusOk.value ? '' : String(data.error || 'Ollama is unavailable')
      ollamaModels.value = ollamaStatusOk.value && Array.isArray(data.models) ? data.models : []
    } else {
      ollamaStatusOk.value = false; ollamaModels.value = []; ollamaStatusError.value = `Status request failed (${resp.status})`
    }
  } catch (error: any) { ollamaStatusOk.value = false; ollamaModels.value = []; ollamaStatusError.value = error?.message || 'Ollama status unavailable' }
}

function ollamaModelStatus(name: string): string {
  if (ollamaLoading.value === name) return 'loading'
  if (!ollamaStatusOk.value) return 'unavailable'
  const m = ollamaModels.value.find(m => m.name === name)
  return m?.status || 'not-installed'
}

function ollamaModelVram(name: string): number {
  const m = ollamaModels.value.find(m => m.name === name)
  return m?.vram_gb || 0
}

function ollamaModelCapabilities(name: string): string[] {
  return ollamaModels.value.find(m => m.name === name)?.capabilities || []
}

function hasOllamaCapability(name: string, capability: string): boolean {
  return ollamaModelCapabilities(name).some(item => item.toLowerCase() === capability.toLowerCase())
}

async function ollamaLoad(model: string) {
  ollamaLoading.value = model
  try {
    const resp = await fetch('/api/agent-chat/ollama/load', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok || !data.ok) chatMessages.value.push({ role: 'system', content: `Load failed: ${data.error || `HTTP ${resp.status}`}` })
    else {
      await fetchOllamaStatus()
      if (ollamaModelStatus(model) !== 'loaded') chatMessages.value.push({ role: 'system', content: `Load did not complete: ${model} is not loaded yet.` })
      else chatMessages.value.push({ role: 'system', content: `Loaded ${model}.` })
    }
  } catch (e: any) {
    chatMessages.value.push({ role: 'system', content: `Load failed: ${e.message}` })
  }
  ollamaLoading.value = ''
  await fetchOllamaStatus()
}

async function ollamaUnload(model?: string) {
  const m = model || chatModel.value
  try {
    const resp = await fetch('/api/agent-chat/ollama/unload', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: m }),
    })
    const data = await resp.json().catch(() => ({}))
    if (resp.ok && data.ok) chatMessages.value.push({ role: 'system', content: `Unloaded ${m} from VRAM.` })
    else chatMessages.value.push({ role: 'system', content: `Unload failed: ${data.error || `HTTP ${resp.status}`}` })
  } catch (e: any) {
    chatMessages.value.push({ role: 'system', content: `Failed to unload: ${e.message}` })
  }
  await fetchOllamaStatus()
}

const ollamaPullName = ref('')
const ollamaPulling = ref(false)
let ollamaPullController: AbortController | null = null

async function ollamaPull() {
  const model = ollamaPullName.value.trim()
  if (!model || ollamaPulling.value) return
  const controller = new AbortController()
  ollamaPullController = controller
  ollamaPulling.value = true
  ollamaPullStatus.value = { status: 'pulling', model, percent: 0, message: 'Starting download…' }
  chatMessages.value.push({ role: 'system', content: `Pulling ${model}...` })
  try {
    const resp = await fetch('/api/agent-chat/ollama/pull', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }), signal: controller.signal,
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok || !data.ok || data.status === 'error') throw new Error(data.error || `HTTP ${resp.status}`)
    if (data.status !== 'done') {
      const progress = await pollAgentPull((url) => fetch(url, { signal: controller.signal }), model, {
        signal: controller.signal, onProgress: (state) => { ollamaPullStatus.value = state },
      })
      ollamaPullStatus.value = progress
      if (progress.status === 'error') throw new Error(progress.error || 'Download failed')
    }
    ollamaPullStatus.value = { status: 'done', model }
    chatMessages.value.push({ role: 'system', content: `${model} downloaded successfully.` })
    ollamaPullName.value = ''
    await fetchProviders(); await fetchOllamaStatus()
  } catch (e: any) {
    if (e?.name !== 'AbortError' && alive) {
      ollamaPullStatus.value = { status: 'error', model, error: e.message }
      chatMessages.value.push({ role: 'system', content: `Pull failed: ${e.message}` })
    }
  }
  ollamaPulling.value = false
  if (ollamaPullController === controller) ollamaPullController = null
}

const chatConversations = ref<{id: string; title: string; provider: string; model: string; message_count: number; updated_at: string}[]>([])

async function loadConversations() {
  if (!selectedDevice.value) return
  try {
    const resp = await fetch(`/api/agent-chat/conversations?device=${selectedDevice.value}`)
    chatConversations.value = await resp.json()
  } catch { chatConversations.value = [] }
}

async function resumeConversation(cid: string) {
  try {
    const resp = await fetch(`/api/agent-chat/conversation/${cid}/resume`, { method: 'POST' })
    const data = await resp.json()
    if (data.ok) {
      chatSessionId.value = data.session_id
      chatProvider.value = data.provider || chatProvider.value
      chatModel.value = data.model || chatModel.value
      chatMessages.value = (data.messages || []).map((m: any) => ({
        role: m.role, content: m.content, tool_name: m.tool_name, tool_args: m.tool_args
      }))
      await nextTick()
      const el = document.getElementById('agent-chat-scroll')
      if (el) el.scrollTop = el.scrollHeight
    }
  } catch (e: any) { console.error('resume failed', e) }
}

async function deleteConversation(cid: string) {
  try { await fetch(`/api/agent-chat/conversation/${cid}`, { method: 'DELETE' }) } catch {}
  await loadConversations()
  if (chatSessionId.value === cid) chatClear()
}

function onProviderChange() {
  const selection = resolveAgentSelection(CHAT_PROVIDERS.value, chatProvider.value, '')
  chatProvider.value = selection.provider
  chatModel.value = selection.model
  persistAgentPreferences()
  chatSessionId.value = '' // reset session on provider change
  if (chatProvider.value === 'ollama') fetchOllamaStatus()
}
function onModelChange() { persistAgentPreferences(); chatSessionId.value = '' }
function toggleAutoScreenshot() { chatAutoScreenshot.value = !chatAutoScreenshot.value; persistAgentPreferences() }
function normalizeChatOptions() {
  const options = clampAgentOptions({ contextLength: chatContextLength.value, maxOutputTokens: chatMaxOutputTokens.value, maxTurns: chatMaxTurns.value })
  chatContextLength.value = options.contextLength; chatMaxOutputTokens.value = options.maxOutputTokens; chatMaxTurns.value = options.maxTurns
  return options
}

async function chatSend() {
  if (!chatInput.value.trim() || chatSending.value || !selectedDevice.value) return
  const msg = chatInput.value.trim()
  const target = { device: selectedDevice.value, provider: chatProvider.value, model: chatModel.value }
  chatInput.value = ''
  chatMessages.value.push({ role: 'user', content: msg })
  chatSending.value = true
  chatStopRequested = false
  const runId = ++chatRunCounter
  chatActiveRunId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${runId}`
  const requestRunId = chatActiveRunId
  chatActivity.value = 'Reading screen...'
  chatTokens.value += msg.length / 4  // rough estimate
  // Auto-start stream
  if (!streaming.value) { singleStreamMode.value = 'mjpeg'; startStream() }

  try {
    const controller = new AbortController()
    chatAbortController = controller
    if (!chatSessionId.value) {
      const created = await fetch('/api/agent-chat/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(target), signal: controller.signal,
      })
      if (!created.ok) throw new Error(`Cannot create agent session (${created.status})`)
      const session = await created.json()
      if (chatStopRequested || controller.signal.aborted || runId !== chatRunCounter) return
      if (!session.session_id) throw new Error('Agent session did not return an id')
      chatSessionId.value = session.session_id
    }
    if (chatStopRequested || controller.signal.aborted || runId !== chatRunCounter) return
    const options = normalizeChatOptions()
    const body: any = {
      content: msg, ...target, run_id: requestRunId,
      auto_screenshot: chatAutoScreenshot.value, context_length: options.contextLength,
      max_output_tokens: options.maxOutputTokens, max_turns: options.maxTurns,
    }
    if (chatSessionId.value) body.session_id = chatSessionId.value
    const resp = await fetch('/api/agent-chat/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    if (!resp.ok) {
      const error = await resp.json().catch(() => ({}))
      throw new Error(error.detail || `Agent request failed (${resp.status})`)
    }
    const reader = resp.body?.getReader()
    const decoder = new TextDecoder()
    const parser = createAgentSseParser()

    while (reader) {
      const { done, value } = await reader.read()
      const finalText = done ? decoder.decode() : decoder.decode(value, { stream: true })
      const events = done ? (finalText ? (parser.push(finalText), parser.flush()) : parser.flush()) : parser.push(finalText)
      for (const rawEvent of events) {
        try {
          const event: any = rawEvent
          if (runId !== chatRunCounter) continue
          if (event.type === 'session' && event.session_id) {
            chatSessionId.value = event.session_id
          } else if (event.type === 'text') {
            chatActivity.value = ''
            // Check if last message is assistant — append to it. Otherwise start new.
            const last = chatMessages.value[chatMessages.value.length - 1]
            if (last?.role === 'assistant') {
              last.content += event.content
            } else {
              chatMessages.value.push({ role: 'assistant', content: event.content })
            }
          } else if (event.type === 'tool_call') {
            const TOOL_EMOJI: Record<string, string> = {
              screenshot: '📸', get_screen_tree: '🌳', get_elements: '🔍', tap: '👆', tap_element: '👆',
              swipe: '👋', type_text: '⌨️', press_key: '🔘', launch_app: '🚀', force_stop: '⛔',
              ocr_screen: '🔤', find_on_screen: '🔎', classify_screen: '📋', get_phone_state: '📱',
              shell: '💻', list_packages: '📦', clipboard_get: '📋', clipboard_set: '📋', list_skills: '🧩', run_skill: '⚡',
              long_press: '✊', open_notifications: '🔔', get_notifications: '🔔', wait: '⏳',
            }
            const SHORT_NAME: Record<string, string> = {
              get_screen_tree: 'tree', get_elements: 'elements', get_phone_state: 'state',
              screenshot_annotated: 'annotated ss', screenshot_cropped: 'crop ss',
              find_on_screen: 'find', classify_screen: 'classify', ocr_screen: 'OCR',
              ocr_region: 'OCR region', list_packages: 'packages', list_skills: 'skills',
              launch_app: 'launch', force_stop: 'stop', type_text: 'type', press_key: 'key',
              tap_element: 'tap #', long_press: 'hold', open_notifications: 'notifs',
              get_notifications: 'notifs', clipboard_get: 'clip', clipboard_set: 'clip set',
              run_skill: 'skill', screenshot: 'screenshot',
            }
            const emoji = TOOL_EMOJI[event.name] || '🔧'
            const short = SHORT_NAME[event.name] || event.name
            chatActivity.value = `${emoji} ${short}...`
            if (chatVerbose.value) {
              chatMessages.value.push({ role: 'tool_call', content: `${emoji} ${event.name}(${JSON.stringify(event.args).slice(0, 120)})`, tool_name: event.name, tool_args: event.args })
            } else {
              // Brief: just emoji, or emoji + key info
              let brief = `${emoji} ${short}`
              const a = event.args || {}
              if (event.name === 'tap' && a.x) brief = `${emoji} (${a.x},${a.y})`
              else if (event.name === 'type_text' && a.text) brief = `${emoji} "${a.text.slice(0, 20)}"`
              else if (event.name === 'launch_app' && a.package) brief = `${emoji} ${a.package.split('.').pop()}`
              else if (event.name === 'press_key' && a.key) brief = `${emoji} ${a.key.replace('KEYCODE_', '')}`
              else if (event.name === 'swipe') brief = `${emoji} swipe`
              else if (event.name === 'shell' && a.command) brief = `${emoji} $ ${a.command.slice(0, 25)}`
              chatMessages.value.push({ role: 'tool_call', content: brief, tool_name: event.name })
            }
          } else if (event.type === 'tool_result') {
            chatActivity.value = '🤔 Thinking...'
            if (chatVerbose.value) {
              chatMessages.value.push({ role: 'tool_result', content: event.result?.slice(0, 300) || '', tool_name: event.name })
            }
          } else if (event.type === 'activity') {
            chatActivity.value = event.content || 'Working...'
          } else if (event.type === 'tokens') {
            // Real token count from Claude Code
            chatTokens.value = (event.input || 0) + (event.output || 0)
            if (event.cost) chatActivity.value = `💰 $${event.cost.toFixed(4)}`
          } else if (event.type === 'screenshot') {
            chatMessages.value.push({ role: 'screenshot', content: '', image: event.image })
          } else if (event.type === 'stopped') {
            chatMessages.value.push({ role: 'stopped', content: event.content || 'Stopped by user' })
            chatActivity.value = ''
          } else if (event.type === 'error') {
            chatMessages.value.push({ role: 'error', content: event.content })
            chatActivity.value = ''
          } else if (event.type === 'done') {
            chatActivity.value = ''
          }
          // Auto-scroll
          const el = document.getElementById('agent-chat-scroll')
          if (el) el.scrollTop = el.scrollHeight
        } catch {}
      }
      if (done) break
    }
  } catch (e: any) {
    if (!chatStopRequested && e?.name !== 'AbortError') chatMessages.value.push({ role: 'error', content: e.message })
  }
  if (runId === chatRunCounter) {
    chatSending.value = false
    chatAbortController = null
    chatActivity.value = ''
    loadConversations()  // refresh conversation list after each turn
  }
}

function chatStop() {
  if (!chatSending.value) return
  chatStopRequested = true
  const session = chatSessionId.value
  if (session) {
    fetch(`/api/agent-chat/stop/${encodeURIComponent(session)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: chatActiveRunId }),
    }).catch(() => {})
  }
  if (chatAbortController) { chatAbortController.abort(); chatAbortController = null }
  chatSending.value = false
  chatActivity.value = ''
  chatMessages.value.push({ role: 'stopped', content: 'Stopped by user' })
}

function onAgentDeviceChange() {
  if (chatSending.value) chatStop()
  chatClear()
  updateNicknameRow()
}

function chatClear() {
  chatMessages.value = []
  chatSessionId.value = ''
  chatTokens.value = 0
  chatActivity.value = ''
  void fetchSavedAiDefaults()
}

/* ── general ─────────────────────────────────────────────────────────── */
async function loadDevices() {
  try {
    const resp = await api('/api/phone/devices')
    devices.value = sortByNumber(resp.devices || resp || [])
    if (devices.value.length && !selectedDevice.value) selectedDevice.value = devices.value[0].serial
    statusText.value = devices.value.length ? `${devices.value.length} device(s)` : 'No devices'
    refreshAllRecordingStatus()
    void syncDeviceNumbers()
  } catch (e: any) { statusText.value = e.message || 'Device load error' }
}

function updateNicknameRow() {
  const dev = devices.value.find(d => d.serial === selectedDevice.value)
  nickname.value = dev?.nickname || ''
}

async function saveNickname() {
  await api('/api/phone/nickname', { method: 'POST', body: JSON.stringify({ serial: selectedDevice.value, nickname: nickname.value }) })
  statusText.value = 'Nickname saved'
  await loadDevices()
}

async function pollLogs() {
  if (document.hidden || !logsOpen.value) return
  try {
    const resp = await api(`/api/logs?from=${srvSeq}&limit=50`)
    if (resp.lines?.length) {
      srvLogs.value.push(...resp.lines.map((l: any) => `[${l.level}] ${l.msg}`))
      srvSeq += resp.lines.length
      if (srvLogs.value.length > 200) srvLogs.value = srvLogs.value.slice(-100)
    }
  } catch {}
}

let multiLogTimer: number | null = null

watch(subTab, value => { if (value === 'multi') { void pollAgentRuns(); scheduleAgentRunPoll() } })

onMounted(async () => {
  phoneTint.start()
  releaseLiveInput = liveInputChannel.acquire()
  agentRunPollStopped = false
  document.addEventListener('visibilitychange', onAgentRunVisibility)
  void pollAgentRuns(); scheduleAgentRunPoll(AGENT_RUN_POLL_MS)
  await loadDevices()
  if (!alive) return
  window.addEventListener('storage', syncStreamIntent)
  window.addEventListener('keydown', exitGridFocusOnEscape)
  if (new URLSearchParams(location.search).has('autostart')) persistStreamIntent(startAllIntent(streamIntent.value))
  await reconcileStreams()
  if (!alive) return
  devicePollTimer = window.setInterval(pollDevicesAndStreams, 10000)
  void loadProxyRouting()
  proxyRoutingTimer = window.setInterval(() => { if (!document.hidden) void loadProxyRouting() }, 30000)
  window.addEventListener('pagehide', releaseSyncOnPageHide)
  loadAllHealth()
  loadConversations()
  await fetchProviders()
  await fetchSavedAiDefaults()
  fetchOllamaStatus()
  logTimer = window.setInterval(pollLogs, 3000)
  multiLogTimer = window.setInterval(pollMultiLogs, 8000)
  pollMultiLogs()
})
watch(subTab, (value, previous) => {
  if (value === 'multi') { multiMounted.value = true; void reconcileStreams() }
  // A tab change is not an explicit Stop: keep the grid viewers alive.
  if (previous === 'single' && value !== 'single') stopStream()
})
// Sidebar navigation can open the Windows workspace directly; mount it on first use.
watch(subTab, value => { if (value === 'windows') workspaceActivated.value = true })
// Leaving the Proxy Manager refreshes the tiles' proxy exit IPs straight away.
watch(subTab, (value, previous) => { if (previous === 'proxies' && value !== 'proxies') void loadProxyRouting() })
watch(selectedDevice, () => {
  loadConversations()
  chatClear()
  refreshRecordingStatus(selectedDevice.value)
})
onUnmounted(() => {
  phoneTint.stop(); tintTileRefs.clear()
  releaseLiveInput?.(); releaseLiveInput = null
  reboot.dispose()
  agentRunPollStopped = true
  document.removeEventListener('visibilitychange', onAgentRunVisibility)
  if (agentRunPollTimer) window.clearTimeout(agentRunPollTimer)
  agentRunPollTimer = null
  agentRunPollController?.abort(); agentRunPollController = null
  clearAgentRunState()
  if (chatSending.value) chatStop()
  ollamaPullController?.abort(); ollamaPullController = null
  alive = false; startBatch++
  overlayBusy.value = {}; overlayOn.value = {}
  for (const controls of gridInteractions.values()) controls.dispose()
  gridInteractions.clear()
  window.removeEventListener('storage', syncStreamIntent)
  window.removeEventListener('keydown', exitGridFocusOnEscape)
  tileObserver?.disconnect(); tileObserver = null; observedTiles.clear()
  fitObserver?.disconnect(); fitObserver = null; fitGridEl = null
  if (fitFrame && typeof cancelAnimationFrame !== 'undefined') cancelAnimationFrame(fitFrame)
  fitFrame = 0
  if (typeof window !== 'undefined') window.removeEventListener('resize', scheduleFit)
  if (devicePollTimer) clearInterval(devicePollTimer)
  if (proxyRoutingTimer) clearInterval(proxyRoutingTimer)
  proxyRoutingTimer = null
  window.removeEventListener('pagehide', releaseSyncOnPageHide)
  syncGeneration++; stopSyncTimers()
  if (syncEnabled.value) releaseSyncLinks(true)
  for (const key of [...androidHandles.keys()]) { const i = key.indexOf(':'); rtcStop(key.slice(i + 1), key.startsWith('single:') ? 'single' : 'multi') }
  for (const serial of Object.keys(mjpegWatchTimers.value)) stopMjpegWatchdog(serial)
  // Clean up all RTC sessions
  for (const serial of Object.keys(rtcSessions.value)) rtcStop(serial)
  if (logTimer) clearInterval(logTimer)
  if (multiLogTimer) clearInterval(multiLogTimer)
})
</script>

<template>
  <div class="phone-admin" :class="{ 'with-control-dock': controlDevice && subTab === 'multi', 'is-document-view': subTab === 'fleet' || subTab === 'proxies' || subTab === 'ai' || subTab === 'discover', 'is-live-grid': subTab === 'multi', 'is-grid-focus': gridFocusOn }" :style="{ '--phone-preview-height': previewHeightCss }">
    <AIModelSettingsPanel v-if="aiSettingsOpen" :locked="chatSending" @close="closeAiSettings" @saved="handleAiSettingsSaved" />
    <Teleport to="body">
      <div v-if="controlDevice" v-show="subTab === 'multi'" class="direct-control-overlay">
        <section class="direct-control-dialog" role="complementary" :aria-label="'Phone control dock ' + controlDevice">
          <div class="direct-control-bar">
            <div class="dock-title"><UiIcon name="phone" :size="15" /><div><strong>Control desk<template v-if="deviceNumbers[controlDevice]"> · Phone {{ deviceNumbers[controlDevice] }}</template></strong><span v-if="syncEnabled && syncPool.includes(controlDevice)" class="dock-sync-note">{{ syncActionLabel }} · mirroring to {{ syncFollowersFor(controlDevice).length }} phones</span><span v-else>Other phones stay available</span></div></div>
            <button class="ctrl-btn" title="Change this phone's number" @click="renumberPhone(controlDevice)"><UiIcon name="hash" :size="12" />Number</button>
            <button class="ctrl-btn ctrl-btn--stop" @click="stopMultiStream(controlDevice)"><UiIcon name="stop" :size="12" />Stop this stream</button>
            <button class="ctrl-btn ctrl-btn--icon" aria-label="Close direct phone controls" title="Close control desk" @click="controlDevice = ''"><UiIcon name="close" :size="14" /></button>
          </div>
          <!-- Control desk Glass Hub: the full status bar for the phone being controlled. -->
          <div v-if="controlDeviceInfo" class="control-hub" role="group" :aria-label="'Phone ' + (deviceNumbers[controlDevice] || controlDevice) + ' status'">
            <div class="control-hub__id" :title="tileIdentityTitle(controlDeviceInfo)">
              <span class="hub-chip" :class="'is-' + tileState(controlDevice)"><span class="tile-number">{{ deviceNumbers[controlDevice] ? String(deviceNumbers[controlDevice]).padStart(2, '0') : '—' }}</span><span class="hub-dot" aria-hidden="true"></span></span>
              <span class="control-hub__name"><strong>{{ tileLabel(controlDeviceInfo) }}</strong><span><UiIcon name="phone" :size="11" />{{ lanIp(controlDeviceInfo) }}</span></span>
            </div>
            <div v-for="slot in HUB_SLOTS" :key="slot.key" class="control-hub__cell" :class="['control-hub__cell--' + slot.key, 'is-' + hubSignal(controlDevice, slot.key)]" role="img" :aria-label="slot.label + ': ' + hubSignalValue(controlDevice, slot.key) + ' · ' + hubSignalTitle(controlDevice, slot.key)" :title="slot.label + ' · ' + hubSignalTitle(controlDevice, slot.key)">
              <span class="control-hub__icon"><UiIcon :name="slot.icon" :size="15" /></span>
              <span class="control-hub__text"><b>{{ slot.label }}</b><em>{{ hubSignalValue(controlDevice, slot.key) }}</em></span>
            </div>
            <button v-if="!isIosDevice(controlDevice)" type="button" class="control-hub__power" :class="rebootActiveFor(controlDevice) ? 'is-' + rebootActiveFor(controlDevice) : ''" :disabled="!!rebootActiveFor(controlDevice)" :aria-label="rebootActiveFor(controlDevice) ? rebootTitle(controlDevice) : 'Reboot phone ' + (deviceNumbers[controlDevice] || controlDevice)" :title="rebootActiveFor(controlDevice) ? rebootTitle(controlDevice) : 'Reboot this phone (asks for confirmation)'" @click="openReboot([controlDevice])"><UiIcon name="power" :size="17" /></button>
          </div>
          <div class="control-device-switcher">
            <button class="ctrl-btn ctrl-btn--icon" aria-label="Previous phone in control dock" title="Previous phone" :disabled="controlDevices.length < 2" @click="switchControlDevice(-1)"><UiIcon name="chevronLeft" :size="15" /></button>
            <select :value="controlDevice" aria-label="Phone in control dock" @change="openDirectControl(($event.target as HTMLSelectElement).value)"><option v-for="phone in controlDevices" :key="phone.serial" :value="phone.serial">{{ phoneTag(phone.serial) }}{{ phone.nickname || phone.model || 'Android' }} · {{ phone.serial }}</option></select>
            <button class="ctrl-btn ctrl-btn--icon" aria-label="Next phone in control dock" title="Next phone" :disabled="controlDevices.length < 2" @click="switchControlDevice(1)"><UiIcon name="chevronRight" :size="15" /></button>
            <small>{{ controlPosition + 1 }} / {{ controlDevices.length }}</small>
          </div>
        <DeviceWindowView :key="controlDevice" :device-serial="controlDevice" :embedded="true" :visible="subTab === 'multi'" :sync-devices="syncEnabled ? syncPool : []" />
        </section>
      </div>
    </Teleport>
    <!-- Workspace navigation lives in the desktop sidebar (App.vue); status messages go to the top bar. -->
    <Teleport defer to="#d3m0n-topbar-status"><span class="status-text" :title="statusText">{{ statusText }}</span></Teleport>

    <StreamOverview v-if="subTab !== 'single' && subTab !== 'proxies' && subTab !== 'multi'" :visible="true" />
    <FleetControlPanel v-if="subTab === 'fleet'" :devices="devices" :visible="true" />
    <FleetCommandCentre v-if="subTab === 'ai'" :devices="devices" />
    <DeviceDiscoveryPanel v-if="subTab === 'discover'" @enrolled="loadDevices" />
    <ProxyManagerPanel v-if="subTab === 'proxies'" />
    <DeviceWorkspaceView v-if="workspaceActivated" v-show="subTab === 'windows'" :devices="devices" :requests="workspaceRequests" :visible="subTab === 'windows'" />
    <!-- ═══════════════ Single Device ═══════════════ -->
    <div v-if="subTab === 'single'" class="single-layout">
      <!-- LEFT: Agent Chat -->
      <div class="agent-panel">
        <!-- Chat header -->
        <div class="agent-header">
          <span class="agent-state-dot"
            :style="{ background: chatSending ? '#f59e0b' : chatMessages.length ? '#22c55e' : '#475569' }"></span>
          <span class="agent-header__title">Agent</span>
          <!-- Conversation picker -->
          <select @change="(e: any) => { if (e.target.value === '__new__') chatClear(); else resumeConversation(e.target.value); e.target.value = chatSessionId || '__new__'; }"
            class="agent-select agent-select--conversation" :disabled="chatSending" aria-label="Conversation">
            <option value="__new__">+ New chat</option>
            <option v-for="c in chatConversations" :key="c.id" :value="c.id"
              :selected="c.id === chatSessionId">
              {{ c.title || 'Untitled' }} ({{ c.message_count }})
            </option>
          </select>
          <select v-model="chatProvider" @change="onProviderChange"
            class="agent-select" :disabled="chatSending" aria-label="Model provider">
            <option v-for="p in CHAT_PROVIDERS" :key="p.id" :value="p.id">{{ p.label }}</option>
          </select>
          <select v-model="chatModel" @change="onModelChange"
            class="agent-select agent-select--model" :disabled="chatSending" aria-label="Model">
            <option v-for="m in (CHAT_PROVIDERS.find(p => p.id === chatProvider)?.models || [])" :key="m" :value="m">{{ m }}</option>
          </select>
          <!-- Ollama model status + load/unload. Status is authoritative: an error never looks ready. -->
          <template v-if="chatProvider === 'ollama'">
            <span v-if="!ollamaStatusOk"
              class="ui-badge ui-badge--muted"
              :title="ollamaStatusError || 'Ollama status unavailable'">
              ○ unavailable
            </span>
            <span v-else-if="ollamaModelStatus(chatModel) === 'loaded'"
              class="ui-badge ui-badge--success"
              :title="`${ollamaModelVram(chatModel)} GB VRAM`">
              ● loaded · {{ ollamaModelVram(chatModel) }}GB
            </span>
            <span v-else-if="ollamaModelStatus(chatModel) === 'loading'"
              class="ui-badge ui-badge--warn agent-pulse">
              ◌ loading...
            </span>
            <span v-else-if="ollamaModelStatus(chatModel) === 'downloaded'"
              class="ui-badge ui-badge--info" title="Installed locally; load before sending">
              ◌ downloaded
            </span>
            <span v-else
              class="ui-badge ui-badge--muted">
              ○ not installed
            </span>
            <span v-if="ollamaStatusOk" class="agent-capabilities" :title="`Capabilities reported by Ollama for ${chatModel}`">
              <span class="agent-capability" :class="{ 'is-supported': hasOllamaCapability(chatModel, 'vision') }">{{ hasOllamaCapability(chatModel, 'vision') ? '✓' : '—' }} Vision</span>
              <span class="agent-capability" :class="{ 'is-supported': hasOllamaCapability(chatModel, 'tools') }">{{ hasOllamaCapability(chatModel, 'tools') ? '✓' : '—' }} Tools</span>
            </span>
            <span v-if="ollamaPullStatus && ollamaPullStatus.model === chatModel && ollamaPulling" class="agent-download" :title="ollamaPullStatus.message || ''">
              ↓ {{ formatPullProgress(ollamaPullStatus) }}
            </span>
            <button v-if="ollamaModelStatus(chatModel) === 'loaded'" @click="ollamaUnload()"
              class="agent-chip-btn agent-chip-btn--danger"
              title="Free VRAM">unload</button>
            <button v-else-if="ollamaStatusOk && ollamaModelStatus(chatModel) === 'downloaded'" @click="ollamaLoad(chatModel)"
              class="agent-chip-btn agent-chip-btn--accent"
              title="Load into VRAM">load</button>
            <span class="agent-pull">
              <input v-model="ollamaPullName" placeholder="pull model..."
                @keyup.enter="ollamaPull" :disabled="ollamaPulling"
                class="agent-pull__input" aria-label="Ollama model to pull" />
              <button @click="ollamaPull" :disabled="ollamaPulling || !ollamaPullName.trim()"
                class="agent-chip-btn" aria-label="Pull Ollama model">{{ ollamaPulling ? formatPullProgress(ollamaPullStatus) : '↓' }}</button>
            </span>
          </template>
          <span class="agent-header__actions">
            <button @click="toggleAutoScreenshot" class="agent-chip-btn" :class="{ 'is-on': chatAutoScreenshot }"
              :aria-pressed="chatAutoScreenshot" title="Attach a fresh screenshot when the model supports vision">
              {{ chatAutoScreenshot ? 'Screenshot on' : 'Screenshot off' }}
            </button>
            <button @click="chatAdvancedOpen = !chatAdvancedOpen" class="agent-chip-btn" :class="{ 'is-on': chatAdvancedOpen }"
              :aria-expanded="chatAdvancedOpen" title="Bounded context and response options">Options</button>
            <button @click="openAiSettings" :disabled="chatSending" class="agent-chip-btn" title="Configure AI providers and new-chat defaults"><UiIcon name="sliders" :size="13" />AI settings</button>
            <button @click="chatVerbose = !chatVerbose"
              class="agent-chip-btn" :class="{ 'is-on': chatVerbose }" :aria-pressed="chatVerbose"
              :title="chatVerbose ? 'Verbose: showing all tool calls + results' : 'Brief: showing tool names only'">
              {{ chatVerbose ? 'Verbose' : 'Brief' }}
            </button>
            <span v-if="chatTokens > 0" class="agent-tokens" :title="`~${Math.round(chatTokens)} tokens used`">
              ~{{ chatTokens > 1000 ? (chatTokens / 1000).toFixed(1) + 'k' : Math.round(chatTokens) }} tok
            </span>
            <button v-if="chatSessionId" @click="openSaveSkillModal"
              class="agent-chip-btn agent-chip-btn--accent"
              title="Save this conversation as a reusable skill">Save skill</button>
            <button v-if="chatMessages.length" @click="chatClear" :disabled="chatSending" class="agent-chip-btn">Clear</button>
            <button v-if="chatSessionId && chatConversations.some(c => c.id === chatSessionId)" @click="deleteConversation(chatSessionId)"
              class="agent-chip-btn agent-chip-btn--danger" title="Delete conversation" aria-label="Delete conversation">Delete</button>
          </span>
        </div>
        <div v-if="chatAdvancedOpen" class="agent-options" role="group" aria-label="Agent options">
          <label>Context <input v-model.number="chatContextLength" type="number" min="1024" max="32768" step="512" /></label>
          <label>Output <input v-model.number="chatMaxOutputTokens" type="number" min="128" max="8192" step="128" /></label>
          <label>Turns <input v-model.number="chatMaxTurns" type="number" min="1" max="24" step="1" /></label>
          <span class="agent-options__hint">{{ chatAutoScreenshot ? 'Vision attachment enabled when available.' : 'No automatic screenshots.' }}</span>
        </div>
        <!-- Messages -->
        <div id="agent-chat-scroll" class="agent-messages">
          <div v-if="!chatMessages.length" class="agent-empty">
            Chat with the agent to control this phone.<br/>
            Try: "What app is open?" or "Open TikTok and search for cats"
          </div>
          <div v-for="(m, i) in chatMessages" :key="i"
            :style="{
              alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: m.role === 'screenshot' ? '200px' : '85%',
            }">
            <!-- User message -->
            <div v-if="m.role === 'user'" class="msg msg--user">
              {{ m.content }}
            </div>
            <!-- Assistant text (markdown) -->
            <div v-else-if="m.role === 'assistant'" class="agent-md msg msg--assistant"
              v-html="renderMd(m.content)"></div>
            <!-- Tool call (compact inline) -->
            <div v-else-if="m.role === 'tool_call'"
              class="msg-tool">
              {{ m.content }}
            </div>
            <!-- Tool result (verbose only, compact) -->
            <div v-else-if="m.role === 'tool_result'"
              class="msg-tool-result">
              {{ m.content }}
            </div>
            <div v-else-if="m.role === 'system'" class="msg-system"
              :class="{ 'is-error': /failed|unavailable/i.test(m.content) }" role="status">{{ m.content }}</div>
            <!-- Screenshot -->
            <img v-else-if="m.role === 'screenshot' && m.image" :src="'data:image/jpeg;base64,' + m.image"
              class="msg-shot" alt="Agent screenshot" />
            <!-- Error -->
            <div v-else-if="m.role === 'error'" class="msg-error">
              {{ m.content }}
            </div>
            <div v-else-if="m.role === 'stopped'" class="msg-stopped">
              {{ m.content }}
            </div>
          </div>
          <!-- Activity indicator -->
          <div v-if="chatSending" class="agent-activity">
            <span class="agent-activity__dot"></span>
            <span class="agent-activity__text">{{ chatActivity || 'Thinking...' }}</span>
          </div>
        </div>
        <!-- Input -->
        <div class="agent-composer">
          <input v-model="chatInput" @keyup.enter="!chatSending && chatSend()" :disabled="!selectedDevice"
            :placeholder="chatSending ? 'Agent is working... type your next message' : 'Tell the agent what to do...'"
            class="ui-input agent-input" aria-label="Message the agent" />
          <button v-if="chatSending" @click="chatStop"
            class="ui-btn ui-btn--danger agent-send">Stop</button>
          <button v-else @click="chatSend" :disabled="!selectedDevice || !chatInput.trim()"
            class="ui-btn ui-btn--primary agent-send">Send</button>
        </div>
      </div>

      <!-- RIGHT: Phone column (controls + stream + hw keys) -->
      <div class="phone-column">
        <!-- Top: device + stream controls -->
        <div class="phone-toolbar">
          <select v-model="selectedDevice" @change="onAgentDeviceChange" :disabled="chatSending" class="device-select ui-select ui-select--sm" aria-label="Agent console device">
            <option value="">--</option>
            <option v-for="d in devices" :key="d.serial" :value="d.serial">
              {{ phoneTag(d.serial) }}{{ d.nickname || d.model || d.serial }} · {{ lanIp(d) }}
            </option>
          </select>
          <button v-if="!streaming" class="ctrl-btn" :class="selectedIsIos ? 'ctrl-btn--mjpeg' : 'ctrl-btn--webrtc'"
            :title="selectedIsIos ? 'Start iOS WDA MJPEG stream' : 'Start Android WebRTC stream'"
            @click="singleStreamMode = selectedIsIos ? 'mjpeg' : 'rtc'; startStream()">Stream</button>
          <button v-if="!streaming && selectedIsIos" class="ctrl-btn ctrl-btn--webrtc"
            title="Start GhostAgent H.264 stream (beta — needs the patched WDA)"
            @click="singleStreamMode = 'h264'; startStream()">H.264</button>
          <span v-if="streaming" class="mode-pill"
            :title="streamInfoTitle(selectedDevice)"
            :class="singleStreamMode === 'rtc' ? 'is-live' : 'is-mjpeg'">
            {{ streamModeText(selectedDevice, singleStreamMode) }}
          </span>
          <span v-if="healthData[selectedDevice]" class="health-dots" :title="healthTitle(selectedDevice)">
            <span v-for="(c, i) in healthDots(selectedDevice)" :key="i" class="health-dot" :style="{ background: healthDotColor(c) }"></span>
          </span>
          <button v-if="iosRecoveryCanApplyFix(selectedDevice)" class="ctrl-btn ctrl-btn--fix"
            :disabled="iosRecoveryFixBusy(selectedDevice)"
            @click="fixIssue(selectedDevice, iosRecoveryAction(selectedDevice))"
            :title="iosRecoveryActionTitle(selectedDevice)">
            {{ iosRecoveryFixBusy(selectedDevice) ? 'Fixing...' : iosRecoveryActionLabel(selectedDevice) }}
          </button>
          <button v-if="selectedDevice" class="ctrl-btn ctrl-btn--record"
            :class="{ 'ctrl-btn--record-active': recordingState[selectedDevice] }"
            :disabled="recordingBusy[selectedDevice]"
            :title="recordingTitle(selectedDevice)"
            @click="toggleRecording(selectedDevice)">
            {{ recordingButtonText(selectedDevice) }}
          </button>
          <a v-if="recordingUrls[selectedDevice]" class="ctrl-btn ctrl-btn--record-link"
            :href="recordingUrls[selectedDevice]" target="_blank" rel="noopener"
            :title="recordingFilenames[selectedDevice] || 'Open recording'">MP4</a>
          <button v-if="streaming" class="ctrl-btn ctrl-btn--stop" @click="stopStream"><UiIcon name="stop" :size="11" />Stop</button>
          <button v-if="!selectedIsIos" class="ctrl-btn ctrl-btn--icon hw-key-btn--toggle" :class="{ 'is-on': overlayOn[selectedDevice] }" title="Toggle UI Grid" aria-label="Toggle UI grid" :aria-pressed="!!overlayOn[selectedDevice]" @click="toggleOverlay(selectedDevice)"><UiIcon name="hash" :size="13" /></button>
        </div>
        <div v-if="recordingStatus[selectedDevice]" class="recording-status-line" :title="recordingStatus[selectedDevice]">
          <span v-if="recordingState[selectedDevice]" class="recording-dot"></span>
          {{ recordingStatus[selectedDevice] }}
        </div>
        <div v-if="streamInfoStatus[selectedDevice]" class="recording-status-line" :title="streamInfoStatus[selectedDevice]">
          {{ streamInfoStatus[selectedDevice] }}
        </div>
        <div v-if="iosRecoveryVisible(selectedDevice)" class="ios-recovery-panel">
          <div class="ios-recovery-head">
            <span class="ios-recovery-state">{{ iosRecoveryState(selectedDevice) }}</span>
            <span class="ios-recovery-summary-inline" :title="iosRecoverySummary(selectedDevice)">{{ iosRecoverySummary(selectedDevice) }}</span>
            <button v-if="iosRecoveryCanApplyFix(selectedDevice)" class="ios-recovery-link ios-recovery-link--fix"
              :disabled="iosRecoveryFixBusy(selectedDevice)"
              @click="fixIssue(selectedDevice, iosRecoveryAction(selectedDevice))"
              :title="iosRecoveryActionTitle(selectedDevice)">
              {{ iosRecoveryFixBusy(selectedDevice) ? 'Fixing…' : iosRecoveryActionLabel(selectedDevice) }}
            </button>
            <button class="ios-recovery-link" @click="loadHealth(selectedDevice)">Refresh</button>
            <button class="ios-recovery-link" @click="toggleRecoveryDetails(selectedDevice)">
              {{ showRecoveryDetails[selectedDevice] ? 'Hide' : 'Details' }}
            </button>
          </div>
          <div v-if="healthFixStatus[selectedDevice]" class="ios-recovery-status">{{ healthFixStatus[selectedDevice] }}</div>
          <div v-if="showRecoveryDetails[selectedDevice]" class="ios-recovery-details">
            <div v-if="iosHealthDetails(selectedDevice).length" class="ios-health-details">
              <span v-for="row in iosHealthDetails(selectedDevice)" :key="row.label"
                class="ios-health-chip" :class="{ 'ios-health-chip--ok': row.ok }"
                :title="`${row.label}: ${row.value}`">
                {{ row.label }} {{ row.value }}
              </span>
            </div>
            <ol v-if="iosRecoverySteps(selectedDevice).length" class="ios-recovery-steps">
              <li v-for="(step, i) in iosRecoverySteps(selectedDevice)" :key="i">{{ step }}</li>
            </ol>
            <div v-if="iosRecoveryCommands(selectedDevice).length" class="ios-recovery-commands">
              <div v-for="command in iosRecoveryCommands(selectedDevice)" :key="command" class="ios-recovery-command">
                <code>{{ command }}</code>
                <button class="ios-copy-btn" @click="copyIosRecoveryCommand(selectedDevice, command)">Copy</button>
              </div>
            </div>
          </div>
        </div>
        <div v-if="selectedIsIos" class="news-panel">
          <div class="news-panel-head">
            <span class="news-title">Chrome News</span>
            <button class="ctrl-btn ctrl-btn--mjpeg ctrl-btn--tiny" :disabled="newsRunning || !selectedDevice"
              @click="runNewsSmoke">{{ newsRunning ? 'Reading...' : 'Read News' }}</button>
          </div>
          <div class="news-controls">
            <input v-model="newsUrl" class="news-input news-input--url" title="URL" />
            <input v-model="newsBundleId" class="news-input" title="iOS bundle id" />
            <input v-model.number="newsMaxHeadlines" class="news-number" type="number" min="1" max="10" title="Headlines" />
            <input v-model.number="newsMaxArticles" class="news-number" type="number" min="0" max="5" title="Articles" />
            <label class="news-toggle" title="Save front-page and article screenshots">
              <input v-model="newsSaveScreenshots" type="checkbox" />
              shots
            </label>
            <input v-if="newsSaveScreenshots" v-model="newsOutDir" class="news-input news-input--out" title="Screenshot output directory" />
          </div>
          <div v-if="newsRunning" class="news-status">Running read_news...</div>
          <div v-else-if="newsError" class="news-status news-status--error">{{ newsError }}</div>
          <div v-if="newsResult" class="news-result">
            <div class="news-summary">
              <span class="news-pill" :class="newsResult.ok ? 'news-pill--ok' : 'news-pill--error'">
                {{ newsResult.ok ? 'OK' : 'Fail' }}
              </span>
              <span>{{ newsHeadlines.length }} headlines</span>
              <span>{{ newsArticles.length }} articles</span>
              <span v-if="newsResult.completion">
                {{ newsResult.completion.articles_with_body || 0 }}/{{ newsResult.completion.requested_articles || 0 }} bodies
              </span>
              <span v-if="newsResult.current_url" :title="newsResult.current_url">{{ newsResult.current_url }}</span>
            </div>
            <div class="news-evidence">
              <span :title="JSON.stringify(newsExtraction.headlines || {})">
                H {{ compactEvidence(newsExtraction.headlines) }}
              </span>
              <span :title="JSON.stringify(newsExtraction.front_page_text || {})">
                Text {{ compactEvidence(newsExtraction.front_page_text) }}
              </span>
            </div>
            <div v-if="newsScreenshotEntries.length" class="news-artifacts">
              <span v-for="([name, path]) in newsScreenshotEntries" :key="name" :title="path">
                {{ name }} {{ path }}
              </span>
            </div>
            <div v-if="newsHeadlines.length" class="news-list">
              <div v-for="(headline, i) in newsHeadlines.slice(0, 5)" :key="i" class="news-headline">
                {{ headline.title || headline.text || headline.source_headline }}
              </div>
            </div>
            <div v-if="newsArticles.length" class="news-articles">
              <div v-for="(article, i) in newsArticles.slice(0, 3)" :key="i" class="news-article">
                <div class="news-article-title">{{ article.page_title || article.source_headline || `Article ${i + 1}` }}</div>
                <div class="news-article-meta">
                  {{ article.open_method || articleEvidence(i).open_method || 'open' }}
                  <span v-if="articleEvidence(i).text"> · {{ compactEvidence(articleEvidence(i).text) }}</span>
                </div>
                <div v-if="article.body_snippet" class="news-article-body">{{ article.body_snippet }}</div>
                <div v-else-if="article.error" class="news-article-error">{{ article.error }}</div>
              </div>
            </div>
            <div v-if="newsErrors.length" class="news-status news-status--error">{{ firstNewsError() }}</div>
          </div>
        </div>
        <!-- Stream -->
        <div class="stream-panel">
        <div class="stream-viewport">
          <canvas v-if="streaming && singleStreamMode === 'rtc'" :id="`rtc-video-single-${selectedDevice}`" class="stream-media"
              style="cursor: pointer; touch-action: none"
              @mousedown="mjpegMouseDown(selectedDevice, $event)"
              @mousemove="mjpegMove(selectedDevice)"
              @mouseup="mjpegMouseUp(selectedDevice, $event)"
              @mouseleave="videoMouseLeave(selectedDevice)"
              @touchstart="mjpegTouchStart(selectedDevice, $event)"
              @touchmove="mjpegTouchMove(selectedDevice, $event)"
              @touchend="mjpegTouchEnd(selectedDevice, $event)"
              @touchcancel="mjpegTouchCancel(selectedDevice)"
              @dragstart.prevent />
          <img v-else-if="streaming && singleStreamMode === 'mjpeg' && singleMjpegUrl"
            :id="`mjpeg-img-${selectedDevice}`"
            :src="singleMjpegUrl"
            class="stream-media"
            crossorigin="anonymous"
            draggable="false"
            @load="mjpegImageLoaded(selectedDevice)"
            @error="mjpegImageError(selectedDevice)"
            @mousedown="mjpegMouseDown(selectedDevice, $event)"
            @mousemove="mjpegMove(selectedDevice)"
            @mouseup="mjpegMouseUp(selectedDevice, $event)"
            @mouseleave="videoMouseLeave(selectedDevice)"
            @touchstart="mjpegTouchStart(selectedDevice, $event)"
            @touchmove="mjpegTouchMove(selectedDevice, $event)"
            @touchend="mjpegTouchEnd(selectedDevice, $event)"
            @touchcancel="mjpegTouchCancel(selectedDevice)"
            @dragstart.prevent />
          <canvas v-show="streaming && singleStreamMode === 'h264'"
            :id="`h264-canvas-${selectedDevice}`" class="stream-media"
            style="cursor: pointer; touch-action: none"
            @mousedown="mjpegMouseDown(selectedDevice, $event)"
            @mousemove="mjpegMove(selectedDevice)"
            @mouseup="mjpegMouseUp(selectedDevice, $event)"
            @mouseleave="videoMouseLeave(selectedDevice)"
            @touchstart="mjpegTouchStart(selectedDevice, $event)"
            @touchmove="mjpegTouchMove(selectedDevice, $event)"
            @touchend="mjpegTouchEnd(selectedDevice, $event)"
            @touchcancel="mjpegTouchCancel(selectedDevice)"
            @dragstart.prevent />
          <div v-if="streaming && singleStreamMode === 'h264' && h264ShowMetrics" class="h264-badge" @click="h264ShowMetrics = false" title="Click to hide">
            <template v-if="h264M.renderFps || h264M.recvFps">
              recv {{ h264M.recvFps }} · render {{ h264M.renderFps }}fps · q{{ h264M.queue }} · {{ h264M.latencyMs }}ms · {{ h264M.kbps }}kbps<span v-if="h264M.dropped"> · drop {{ h264M.dropped }}</span>
            </template>
            <template v-else>H.264 {{ h264Status }}</template>
          </div>
          <button v-if="streaming && singleStreamMode === 'h264' && !h264ShowMetrics" class="h264-badge h264-badge--show" @click="h264ShowMetrics = true" title="Show metrics">ⓘ</button>
          <div v-if="!streaming" class="stream-placeholder">
            Select device &amp; start stream<br/>or chat — auto-starts
          </div>
          <div v-if="blackWarning[selectedDevice]" class="black-warning">
            {{ blackWarningText(selectedDevice) }}
            <button v-if="!selectedIsIos" @click="blackWarning[selectedDevice] = false; toggleSingleMode()" class="black-warning-btn">Switch</button>
          </div>
          <!-- Frozen stream overlay -->
          <div v-if="streamFrozen[selectedDevice]" class="frozen-overlay" @click="reconnectStream(selectedDevice)">
            <div class="frozen-ghost"><UiIcon name="refresh" :size="22" /></div>
            <div class="frozen-text">Stream frozen</div>
            <div class="frozen-hint">Tap to reconnect</div>
          </div>
        </div>
      </div>
        <!-- Bottom: hardware keys -->
        <div class="phone-keys">
          <button v-for="key in hardwareKeys(selectedDevice)" :key="String(key.key)" class="ctrl-btn"
            :title="key.title" @click="sendKeyTo(selectedDevice, key.key)">
            {{ key.label }}
          </button>
        </div>
      </div>
    </div>

    <!-- ═══════════════ Multi Device ═══════════════ -->
    <div v-if="multiMounted" v-show="subTab === 'multi'" class="multi-dashboard-pane">
      <!-- Controls header -->
      <div class="multi-header is-compact">
        <div class="multi-header__row">
        <div class="ui-toolbar__group" role="group" aria-label="Stream actions">
          <button v-if="hasAndroidDevices" class="ctrl-btn ctrl-btn--webrtc start-all-btn" :class="'is-' + startAllState" :aria-pressed="startAllState === 'all'" @click="startAllStreams('rtc')" :title="startAllTitle"><UiIcon :name="startAllState === 'all' ? 'check' : 'bolt'" :size="13" /><span>{{ startAllText }}</span></button>
          <button v-if="hasIosDevices" class="ctrl-btn ctrl-btn--mjpeg" @click="startAllStreams('mjpeg')" :title="hasIosDevices ? 'Start WDA MJPEG on iOS and MJPEG on Android' : 'Start Android MJPEG streams'"><UiIcon name="play" :size="12" />Start All (MJPEG)</button>
          <button class="ctrl-btn ctrl-btn--stop" title="Stop every stream and turn continuous streaming off" @click="stopAllStreams"><UiIcon name="stop" :size="12" />Stop All</button>
        </div>
        <span class="ui-vdivider" aria-hidden="true"></span>
        <div class="ui-toolbar__group" role="group" aria-label="Synchronized control">
          <button class="ctrl-btn sync-all-btn" :class="{ 'is-on': syncEnabled, 'is-arming': syncArming }" :aria-pressed="syncEnabled" :disabled="!syncEnabled && syncPool.length < 2" @click="requestSyncAll" :title="syncEnabled ? 'Stop mirroring input' : 'Mirror input on one phone to ' + syncScopeLabel"><UiIcon name="layers" :size="13" />{{ syncActionLabel }}{{ syncEnabled ? ' on' : '' }}<span class="sync-all-count">{{ syncPool.length }}</span></button>
          <select v-model="syncScope" class="ui-select ui-select--sm sync-scope-select" aria-label="Sync All scope" title="Which phones are allowed to lead and receive mirrored input"><option value="all">All phones</option><option value="selected">Selected phones</option><option value="visible">Filtered</option></select>
          <button type="button" class="ctrl-btn ctrl-btn--tool groups-toggle" :class="{ 'is-on': groupsOpen }" :aria-expanded="groupsOpen" aria-label="Select phones and groups" title="Select phones by number or create and load saved groups" @click="groupsOpen = !groupsOpen"><UiIcon name="users" :size="13" /><span class="ctrl-btn__label">Groups</span></button>
        </div>
        <span class="ui-vdivider" aria-hidden="true"></span>
        <div class="ui-toolbar__group" role="group" aria-label="Fleet tools">
          <button v-if="hasAndroidDevices" class="ctrl-btn ctrl-btn--tool" aria-label="WiFi Connect" title="Connect phones over wireless ADB" @click="showWirelessModal = true"><UiIcon name="wifi" :size="13" /><span class="ctrl-btn__label">WiFi Connect</span></button>
          <button class="ctrl-btn ctrl-btn--tool" aria-label="Health" title="Refresh device health" @click="loadAllHealth"><UiIcon name="pulse" :size="13" /><span class="ctrl-btn__label">Health</span></button>
        </div>
        <span class="ui-toolbar__spacer"></span>
        <div class="ui-toolbar__group grid-view-options" role="group" aria-label="Grid view options">
          <div class="ui-search multi-search"><UiIcon name="search" :size="14" /><input v-model="deviceSearch" class="ui-input ui-input--sm" aria-label="Search fleet devices" placeholder="Search #, name or IP · 1-10" title="Phone number (5 or #5), range (1-10), model, name or IP" /></div>
          <select v-model="statusFilter" class="ui-select ui-select--sm" aria-label="Filter devices by stream state"><option value="all">All states</option><option value="live">Live</option><option value="stopped">Stopped</option><option value="attention">Needs attention</option></select>
          <select v-model="previewSize" class="ui-select ui-select--sm" aria-label="Phone preview size" title="Layout"><option v-for="item in GRID_DENSITIES" :key="item.value" :value="item.value">{{ item.label }}</option></select>
          <select v-if="!isFitLayout" v-model="gridColumns" class="ui-select ui-select--sm multi-view-select" aria-label="Device grid columns" :disabled="isFitLayout" :title="isFitLayout ? 'Fit screen chooses columns automatically' : 'Grid columns'"><option value="auto">Auto columns</option><option v-for="n in GRID_COLUMN_CHOICES.slice(1)" :key="n" :value="n">{{ n }} {{ n === '1' ? 'column' : 'columns' }}</option></select>
          <select v-model="gridQuality" class="ui-select ui-select--sm" aria-label="Grid stream quality" title="Grid tiles only; the control desk and device windows always stream HD"><option value="auto">Auto quality</option><option value="hd">HD 720p</option><option value="grid">Grid 480p</option><option value="wall">Wall 360p</option></select>
          <button type="button" class="ctrl-btn ctrl-btn--icon grid-focus-btn" :class="{ 'is-on': gridFocusOn }" :aria-pressed="gridFocusOn" :aria-label="gridFocusOn ? 'Exit focus mode' : 'Focus mode: hide the sidebar and top bar'" :title="gridFocusOn ? 'Exit focus mode (Esc)' : 'Focus mode: give the whole screen to the phones (Esc exits)'" @click="toggleGridFocus"><UiIcon :name="gridFocusOn ? 'close' : 'maximize'" :size="14" /></button>
        </div>
        </div>
        <div class="multi-header__meta multi-statusbar">
        <span class="device-count"><strong>{{ visibleDeviceCount }}</strong> / {{ devices.length }} devices</span>
        <span class="device-count device-count--live"><span class="tile-state-dot is-live" aria-hidden="true"></span><strong>{{ liveDeviceCount }}</strong> live</span>
        <button v-if="attentionDeviceCount" type="button" class="statusbar-link is-warn" @click="statusFilter = 'attention'"><span class="tile-state-dot is-attention" aria-hidden="true"></span>{{ attentionDeviceCount }} need attention</button>
        <span class="stream-intent-note" :class="{ 'is-on': streamIntent.all }" :title="streamIntent.all ? 'Streams stop only when you choose Stop All or Stop selected' : 'Stream choices are saved in this browser'"><span class="ui-dot" aria-hidden="true"></span><span class="lbl-full">{{ streamIntent.all ? 'Continuous streaming on' : 'Stream choices saved' }}</span><span class="lbl-short">{{ streamIntent.all ? 'Continuous' : 'Saved' }}</span></span>
        <span class="multi-statusbar__spec" :title="'Grid quality: ' + (gridQuality === 'auto' ? 'automatic for the ' + gridDensityInfo.label + ' layout' : gridQuality) + ' · HD in the control desk'">{{ gridQualitySpec }} per tile</span>
        <StreamOverview compact :visible="subTab === 'multi'" />
        <span v-if="focusedGridDevice && gridTouch" class="input-target" :title="'Input target ' + focusedGridDevice + ' · ' + (syncFollowersFor(focusedGridDevice).length ? 'mirrored to ' + syncFollowersFor(focusedGridDevice).length + ' phones' : 'input is never broadcast')"><UiIcon name="keyboard" :size="12" />{{ phoneTag(focusedGridDevice) }}{{ lanIp({ serial: focusedGridDevice }) }}{{ syncFollowersFor(focusedGridDevice).length ? ' · mirrored to ' + syncFollowersFor(focusedGridDevice).length : '' }}</span>
        <span class="ui-toolbar__spacer"></span>
        <div class="ui-toolbar__group" role="group" aria-label="Tile selection">
          <button type="button" class="statusbar-link" @click="selectVisibleTiles">Select shown</button>
          <button type="button" class="statusbar-link" :disabled="!selectedTiles.length" @click="selectedTiles = []"><span class="lbl-full">Clear selection</span><span class="lbl-short">Clear</span> ({{ selectedTiles.length }})</button>
          <button v-if="selectedTiles.length > 1" type="button" class="statusbar-link sync-selected-link" @click="requestSyncSelected" title="Mirror input only between the checked phones">Sync selected ({{ selectedTiles.length }})</button>
          <button type="button" class="statusbar-link is-danger" :disabled="!selectedTiles.length" @click="stopSelectedStreams">Stop selected ({{ selectedTiles.length }})</button>
          <button type="button" class="statusbar-link is-danger reboot-selected-link" :disabled="!selectedTiles.length" title="Restart the checked phones (asks for confirmation first)" @click="openReboot(selectedTiles.slice())"><UiIcon name="refresh" :size="11" /><span class="lbl-full">Reboot selected</span><span class="lbl-short">Reboot</span> ({{ selectedTiles.length }})</button>
          <button type="button" class="statusbar-link" title="Renumber the connected phones 1, 2, 3 … in IP order" @click="renumberAllByIp"><UiIcon name="hash" :size="11" />Renumber</button>
        </div>
        <div class="ui-toolbar__group" role="group" aria-label="Grid interaction">
          <label class="grid-touch-toggle ui-switch"><input v-model="gridTouch" type="checkbox" aria-label="Control phones directly in the grid" /> <span class="lbl-full">Interact in previews</span><span class="lbl-short">Interact</span></label>
          <label class="grid-touch-toggle ui-switch"><input v-model="showCardLogs" type="checkbox" aria-label="Show device logs in grid" /> <span class="lbl-full">Show logs</span><span class="lbl-short">Logs</span></label>
          <span class="grid-help-tip" tabindex="0" role="note" :aria-label="gridHelpText" :title="gridHelpText"><UiIcon name="info" :size="13" /></span>
        </div>
        </div>
      </div>
      <GridGroupsPanel v-if="groupsOpen" :devices="devices" :numbers="deviceNumbers" v-model:selected="selectedTiles" @sync="requestSyncSelected" @close="groupsOpen = false" />
      <div v-if="syncArming" class="sync-banner is-arming" role="alertdialog" aria-label="Confirm Sync All">
        <UiIcon name="alert" :size="15" />
        <div class="sync-banner__text"><strong>Mirror input between {{ syncPool.length }} phones?</strong><span>Only {{ syncScopeLabel }} can lead or receive mirrored input. Other phones remain independent.</span><span class="sync-members" :title="syncMemberSummary">Phones: {{ syncMemberSummary }}</span></div>
        <button class="ctrl-btn ctrl-btn--accent" :disabled="syncPool.length < 2" @click="confirmSyncAll">Turn on {{ syncActionLabel }}</button>
        <button class="ctrl-btn" @click="syncArming = false">Cancel</button>
      </div>
      <div v-else-if="syncEnabled" class="sync-banner is-on" role="status">
        <span class="sync-banner__pulse" aria-hidden="true"></span>
        <div class="sync-banner__text"><strong>{{ syncActionLabel }} on · {{ syncPool.length }} phones<span class="sync-instant" :class="{ 'is-partial': syncStatusLoaded && syncInstantCount < syncPool.length, 'is-pending': !syncStatusLoaded }" :title="syncInstantTitle"><UiIcon name="bolt" :size="10" />{{ syncStatusLoaded ? syncInstantCount + '/' + syncPool.length + ' instant' : 'connecting…' }}</span></strong><span :title="syncReport && syncReport.failed.length ? syncReport.failed.map(item => item.device + ': ' + item.error).join('\n') : ''">{{ syncReport ? `Last input from ${syncReport.leader}: ${syncReport.ok}/${syncReport.targets} mirrored${syncReport.pending ? ' · ' + syncReport.pending + ' pending' : ''}${syncReport.failed.length ? ' · ' + syncReport.failed.length + ' failed' : ''} · ${Math.round(syncReport.ms)} ms` : 'Use a phone in ' + syncScopeLabel + ' (' + syncMemberSummary + '); other phones remain independent.' }}</span></div>
        <div class="sync-banner__keys" role="toolbar" aria-label="Send a key to every synced phone"><button v-for="k in SYNC_KEYS" :key="k.key" type="button" class="hw-key-btn" :aria-label="k.label + ' on all synced phones'" @click="sendSyncKey(k.key)">{{ k.label }}</button></div>
        <button class="ctrl-btn" @click="syncEnabled = false">Turn off</button>
      </div>
      <div v-if="rebootEntries.length" class="reboot-banner" :class="{ 'is-active': rebootActiveCount > 0 }" role="status" aria-live="polite">
        <span class="reboot-banner__icon" aria-hidden="true"><UiIcon name="refresh" :size="13" /></span>
        <strong class="reboot-banner__title">{{ rebootActiveCount ? 'Rebooting ' + rebootActiveCount + (rebootActiveCount === 1 ? ' phone' : ' phones') : 'Reboot finished' }}</strong>
        <div class="reboot-banner__chips">
          <span v-for="[serial, r] in rebootEntries" :key="serial + '@' + r.started_at" class="reboot-chip" :class="'is-' + r.state" :title="rebootLabel(serial) + ' · ' + (r.message || rebootChipText(r))"><b>{{ rebootLabel(serial) }}</b>{{ rebootChipText(r) }}</span>
        </div>
        <span v-if="rebootEntries.some(([, r]) => r.state === 'needs_usb')" class="reboot-banner__hint">Plug these phones into the Ubuntu PC by USB — they reconnect automatically.</span>
        <button v-if="!rebootActiveCount" type="button" class="ctrl-btn" @click="reboot.dismissFinished()">Dismiss</button>
      </div>
      <RebootDialog :dialog="rebootDialog" :label="rebootLabel" @cancel="reboot.cancel()" @confirm="reboot.confirm()" @acknowledge="reboot.acknowledge($event)" />
      <div :ref="bindFitGrid" class="multi-grid" :class="['density-' + gridDensity, { 'is-syncing': syncEnabled, 'is-fit': isFitLayout, 'hub-compact': isFitLayout && hubCompact }]"
        :style="fleetGridStyle">
        <div v-for="d in devices" v-show="matchesDevice(d)" :key="d.serial" :ref="tileRef(d.serial)" class="multi-card" :class="['state-' + tileState(d.serial), { 'is-active-device': focusedGridDevice === d.serial || controlDevice === d.serial, 'is-sync-target': syncEnabled && syncPool.includes(d.serial), 'is-sync-failed': syncEnabled && syncFailedTiles.has(d.serial) }]" role="region" :aria-label="'Device tile ' + d.serial">
          <!-- Glass Hub: integrated phone bezel; plain number selects, identity opens controls, signals reflect real activity. -->
          <div class="multi-card-header glass-hub" :class="{ 'is-selected': selectedTiles.includes(d.serial) }">
            <label class="tile-select hub-chip" :class="['is-' + tileState(d.serial), { 'is-selected': selectedTiles.includes(d.serial) }]" :title="'Select phone ' + (deviceNumbers[d.serial] || d.serial)">
              <input v-model="selectedTiles" type="checkbox" :value="d.serial" :aria-label="'Select stream ' + d.serial" />
              <span class="tile-number">{{ deviceNumbers[d.serial] ? String(deviceNumbers[d.serial]).padStart(2, '0') : '—' }}</span>
              <span class="hub-dot" aria-hidden="true"></span>
            </label>
            <button type="button" class="multi-device-name device-name-control tile-id" :aria-label="'Control device ' + d.serial" :title="tileIdentityTitle(d)" @click="openDirectControl(d.serial)">
              <span class="device-name-text">{{ tileLabel(d) }}</span>
              <span class="tile-ip__addr" :class="'is-' + tileIpState(d.serial)">{{ tileIp(d) }}</span>
            </button>
            <FleetAiBadge :active="['running', 'waiting_for_owner', 'uncertain', 'manual_recovery'].includes(String(d.active_ai_state || '').toLowerCase())" :state="String(d.active_ai_state || '')" />
            <div class="hub-signals" role="group" :aria-label="'Activity signals ' + d.serial">
              <span v-for="slot in HUB_SLOTS" :key="slot.key" class="hub-signal" :class="['hub-signal--' + slot.key, 'is-' + hubSignal(d.serial, slot.key), { 'tile-sync-indicator': slot.key === 'sync' && syncEnabled && syncPool.includes(d.serial) }]" role="img" :aria-label="slot.label + ': ' + hubSignalTitle(d.serial, slot.key)" :title="slot.label + ' · ' + hubSignalTitle(d.serial, slot.key)"><UiIcon :name="slot.icon" :size="10" /></span>
            </div>
            <div class="hub-actions" :class="{ 'is-rebooting': !!rebootActiveFor(d.serial) }">
              <details class="tile-quick" @click.stop>
                <summary :aria-label="'Phone actions ' + d.serial" :title="'Phone ' + (deviceNumbers[d.serial] || '') + ' actions'"><UiIcon name="dots" :size="13" /></summary>
                <div class="tile-quick__menu" role="group" :aria-label="'Quick actions ' + d.serial">
                  <button v-if="!multiStreaming[d.serial]" type="button" class="tile-quick__btn" :aria-label="'Start stream ' + d.serial" @click="startMultiStream(d.serial, isIosDevice(d.serial) ? 'mjpeg' : 'rtc'); ($event.currentTarget as HTMLElement)?.closest('details')?.removeAttribute('open')"><UiIcon name="play" :size="12" /><span>Start</span></button>
                  <button v-else type="button" class="tile-quick__btn" :aria-label="'Stop stream ' + d.serial" @click="stopMultiStream(d.serial); ($event.currentTarget as HTMLElement)?.closest('details')?.removeAttribute('open')"><UiIcon name="stop" :size="12" /><span>Stop</span></button>
                  <button v-if="!isIosDevice(d.serial)" type="button" class="tile-quick__btn" :aria-label="'Open direct controls ' + d.serial" @click="openDirectControl(d.serial); ($event.currentTarget as HTMLElement)?.closest('details')?.removeAttribute('open')"><UiIcon name="maximize" :size="12" /><span>Open</span></button>
                  <button v-if="multiStreaming[d.serial] && !isIosDevice(d.serial)" type="button" class="tile-quick__btn" :aria-label="'Reconnect H.264 ' + d.serial" @click="toggleMultiMode(d.serial)"><UiIcon name="refresh" :size="12" /><span>Reconnect</span></button>
                  <button v-if="!isIosDevice(d.serial)" type="button" class="tile-quick__btn is-danger" :aria-label="'Reboot phone ' + d.serial" :disabled="!!rebootActiveFor(d.serial)" @click="openReboot([d.serial]); ($event.currentTarget as HTMLElement)?.closest('details')?.removeAttribute('open')"><UiIcon name="power" :size="12" /><span>Reboot</span></button>
                  <button v-if="rtcStatus[d.serial]?.includes('Portal') || rtcStatus[d.serial]?.includes('fix')" class="tile-quick__btn" @click="rtcFixPortal(d.serial)" title="Fix Portal">Fix Portal</button>
                  <span v-if="healthData[d.serial]" class="health-dots" :title="healthTitle(d.serial)"><span v-for="(c, i) in healthDots(d.serial)" :key="i" class="health-dot" :style="{ background: healthDotColor(c) }"></span></span>
                  <button v-if="iosRecoveryCanApplyFix(d.serial)" class="tile-quick__btn" :disabled="iosRecoveryFixBusy(d.serial)" @click="fixIssue(d.serial, iosRecoveryAction(d.serial))" :title="iosRecoveryActionTitle(d.serial)">{{ iosRecoveryFixBusy(d.serial) ? '...' : iosRecoveryActionLabel(d.serial) }}</button>
                  <button v-if="healthData[d.serial]?.wifi?.ip && !isIosDevice(d.serial) && d.connection !== 'wifi'" class="tile-quick__btn" @click="goWireless(d.serial)" :aria-label="'Go wireless ' + d.serial"><UiIcon name="wifi" :size="12" />Wireless</button>
                </div>
              </details>
              <button v-if="!isIosDevice(d.serial)" type="button" class="hub-power" :class="rebootActiveFor(d.serial) ? 'is-' + rebootActiveFor(d.serial) : ''" :disabled="!!rebootActiveFor(d.serial)" :aria-label="rebootActiveFor(d.serial) ? rebootTitle(d.serial) : 'Reboot phone ' + d.serial" :title="rebootActiveFor(d.serial) ? rebootTitle(d.serial) : 'Reboot phone ' + (deviceNumbers[d.serial] || '') + ' (asks for confirmation)'" @click.stop="openReboot([d.serial])"><UiIcon name="power" :size="12" /></button>
            </div>
          </div>
          <div v-if="iosRecoveryVisible(d.serial)" class="ios-recovery-compact" :title="healthTitle(d.serial)">
            <span>{{ iosRecoveryState(d.serial) }}</span>
            <span v-if="iosRecoveryCode(d.serial)">{{ iosRecoveryCode(d.serial) }}</span>
            <span v-if="healthFixStatus[d.serial]">{{ healthFixStatus[d.serial] }}</span>
          </div>
          <div v-if="recordingStatus[d.serial]" class="multi-recording-status" :title="recordingStatus[d.serial]">
            <span v-if="recordingState[d.serial]" class="recording-dot"></span>
            {{ recordingStatus[d.serial] }}
          </div>
          <div v-if="streamInfoStatus[d.serial]" class="multi-recording-status" :title="streamInfoStatus[d.serial]">
            {{ streamInfoStatus[d.serial] }}
          </div>

          <!-- Stream area -->
          <div class="multi-stream-area">
            <!-- WebRTC video -->
            <canvas v-if="multiStreaming[d.serial] && multiStreamMode[d.serial] === 'rtc'" :id="`rtc-video-multi-${d.serial}`" class="multi-stream-media"
              role="application" tabindex="0" :aria-label="'Live phone preview ' + d.serial" :title="gridTouch ? 'Tap, drag, wheel, type or paste to control this phone' : 'Click to open the dashboard control desk'"
              :style="{ cursor: 'pointer', touchAction: gridTouch ? 'none' : 'auto' }"
              @click="!gridTouch && openDirectControl(d.serial)" @keydown="gridKeyDown(d.serial, $event)"
              @wheel="gridInteraction(d.serial).wheel($event)" @paste="gridInteraction(d.serial).paste($event)" @focus="focusedGridDevice = d.serial"
              @pointerdown="gridPointerDown(d.serial, $event)" @pointermove="gridPointerMove(d.serial, $event)" @pointerup="gridPointerUp(d.serial, $event)"
              @pointercancel="gridPointerCancel(d.serial, $event)" @lostpointercapture="gridPointerCancel(d.serial, $event)" @contextmenu.prevent @dragstart.prevent />
            <!-- MJPEG img -->
            <img v-else-if="multiStreaming[d.serial] && multiStreamMode[d.serial] === 'mjpeg' && mjpegUrls[d.serial]"
              :id="`mjpeg-img-${d.serial}`"
              :src="mjpegUrls[d.serial]"
              class="multi-stream-media"
              crossorigin="anonymous"
              draggable="false"
              @load="mjpegImageLoaded(d.serial)"
              @error="mjpegImageError(d.serial)"
              @mousedown="mjpegMouseDown(d.serial, $event)"
              @mousemove="mjpegMove(d.serial)"
              @mouseup="mjpegMouseUp(d.serial, $event)"
              @mouseleave="videoMouseLeave(d.serial)"
              @touchstart="mjpegTouchStart(d.serial, $event)"
              @touchmove="mjpegTouchMove(d.serial, $event)"
              @touchend="mjpegTouchEnd(d.serial, $event)"
              @touchcancel="mjpegTouchCancel(d.serial)"
              @dragstart.prevent />
            <button v-else class="multi-stream-placeholder preview-control-button" :aria-label="'Open stopped device ' + d.serial" @click="openDirectControl(d.serial)">Click to use this phone · stream stopped in grid</button>
            <!-- Black screen warning -->
            <div v-if="blackWarning[d.serial]" class="black-warning black-warning--compact">
              {{ blackWarningText(d.serial) }}
              <button v-if="!isIosDevice(d.serial)" @click="blackWarning[d.serial] = false; toggleMultiMode(d.serial)" class="black-warning-btn">Switch</button>
            </div>
            <!-- Frozen stream overlay -->
            <div v-if="streamFrozen[d.serial]" class="frozen-overlay" @click="reconnectStream(d.serial)">
              <div class="frozen-ghost"><UiIcon name="refresh" :size="20" /></div>
              <div class="frozen-text">Frozen</div>
              <div class="frozen-hint">Tap to reconnect</div>
            </div>
          </div>

          <div v-if="tileActionMessage[d.serial]" class="tile-action-message">{{ tileActionMessage[d.serial] }}</div>
          <div class="multi-card-keybar" role="toolbar" :aria-label="'Phone keys ' + d.serial">
            <div class="multi-hw-keys">
              <button v-for="key in hardwareKeys(d.serial)" :key="String(key.key)" class="hw-key-btn"
                @click="sendKeyTo(d.serial, key.key)" :title="key.title" :aria-label="key.title + ' ' + d.serial">{{ key.label }}</button>
              <button v-if="!isIosDevice(d.serial)" class="hw-key-btn hw-key-btn--toggle" :class="{ 'is-on': overlayOn[d.serial] }" :disabled="overlayBusy[d.serial]" @click="toggleOverlay(d.serial)"
                :title="overlayBusy[d.serial] ? 'Updating UI Grid…' : 'Toggle UI Grid'" :aria-label="'Toggle UI grid ' + d.serial" :aria-pressed="!!overlayOn[d.serial]"><UiIcon name="hash" :size="12" /></button>
            </div>
            <div class="multi-card-tools">
              <button class="ctrl-btn ctrl-btn--record ctrl-btn--tiny"
                :class="{ 'ctrl-btn--record-active': recordingState[d.serial] }"
                :disabled="recordingBusy[d.serial]"
                :title="recordingTitle(d.serial)"
                @click="toggleRecording(d.serial)">
                {{ recordingTinyText(d.serial) }}
              </button>
              <a v-if="recordingUrls[d.serial]" class="ctrl-btn ctrl-btn--record-link ctrl-btn--tiny"
                :href="recordingUrls[d.serial]" target="_blank" rel="noopener"
                :title="recordingFilenames[d.serial] || 'Open recording'">MP4</a>
              <a v-if="!isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--tiny ctrl-btn--icon" :href="deviceWindowUrl(d.serial)" target="_blank" rel="noopener" title="Pop out into its own window" :aria-label="'Pop out device ' + d.serial" @click="openDeviceWindow(d.serial, $event)"><UiIcon name="external" :size="12" /></a>
              <button class="ctrl-btn ctrl-btn--tiny ctrl-btn--icon" :aria-label="'Expand device ' + d.serial" @click="selectedDevice = d.serial; subTab = 'single'; startStream()" title="Expand in Agent console"><UiIcon name="maximize" :size="12" /></button>
            </div>
          </div>
          <!-- Per-device logs -->
          <div v-show="showCardLogs" class="multi-logs-area">
            <div class="multi-logs-header">&#x25CF; Logs</div>
            <div class="multi-logs-scroll">
              <div v-for="(line, i) in (multiLogs[d.serial] || []).slice(-30)" :key="i"
                class="multi-log-line"
                :style="{
                  color: line.includes('[ERROR]') || line.includes('[CRITICAL]') ? '#f87171'
                    : line.includes('[WARN') ? '#fbbf24'
                    : line.includes('[INFO]') ? '#34d399'
                    : '#5b7a5e',
                  borderLeft: line.includes('[ERROR]') ? '2px solid #f87171'
                    : line.includes('[WARN') ? '2px solid #fbbf24'
                    : '2px solid transparent',
                  paddingLeft: '6px'
                }">{{ line }}</div>
              <div v-if="!(multiLogs[d.serial] || []).length" class="multi-log-empty">waiting for logs...</div>
            </div>
          </div>
        </div>
        <div v-if="!devices.length" class="multi-empty">
          No devices connected.
        </div>
      </div>
    </div>
    <!-- Wireless connect modal -->
    <div v-if="showWirelessModal && hasAndroidDevices" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50" @click.self="showWirelessModal = false">
      <div class="card" style="width: 360px">
        <h3 class="font-bold text-sm mb-3" style="color: var(--text-1)">Connect WiFi Device</h3>
        <div class="mb-2">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Device IP</label>
          <input v-model="wirelessIp" placeholder="192.0.2.42" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>
        <div class="mb-2">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Port (default 5555)</label>
          <input v-model="wirelessPort" placeholder="5555" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>
        <div class="mb-3">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Pairing code (Android 11+ only, leave blank for reconnect)</label>
          <input v-model="wirelessCode" placeholder="123456" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>
        <div v-if="wirelessResult" class="mb-2 p-2 rounded text-xs" style="background: var(--bg-deep); color: var(--text-2)">{{ wirelessResult }}</div>
        <div class="flex gap-2">
          <button class="btn btn-primary" @click="wirelessConnect">Connect</button>
          <button class="btn" @click="showWirelessModal = false">Close</button>
        </div>
      </div>
    </div>

    <!-- Save conversation as skill modal -->
    <div v-if="saveSkillModal" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50" @click.self="saveSkillModal = false">
      <div class="card" style="width: 480px; max-width: 92vw; max-height: 88vh; overflow-y: auto">
        <h3 class="font-bold text-sm mb-3" style="color: var(--text-1)">Save conversation as skill</h3>

        <!-- Kind toggle -->
        <div class="mb-3">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Type</label>
          <div class="flex gap-2">
            <button class="btn" :class="saveSkillKind === 'hard' ? 'btn-primary' : ''" @click="setSaveSkillKind('hard')">Hard (replay steps)</button>
            <button class="btn" :class="saveSkillKind === 'soft' ? 'btn-primary' : ''" @click="setSaveSkillKind('soft')">Soft (guidance)</button>
          </div>
        </div>

        <div class="mb-2">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Name</label>
          <input v-model="saveSkillName" placeholder="my_skill" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>
        <div class="mb-2">
          <label class="block text-xs mb-1" style="color: var(--text-3)">App package</label>
          <input v-model="saveSkillPackage" placeholder="com.example.app" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>
        <div class="mb-2">
          <label class="block text-xs mb-1" style="color: var(--text-3)">Description</label>
          <input v-model="saveSkillDesc" placeholder="What this skill does" class="w-full px-3 py-2 rounded-lg text-sm"
            style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
        </div>

        <!-- Hard: summary + editable steps JSON -->
        <template v-if="saveSkillKind === 'hard'">
          <div v-if="saveSkillLoading" class="mb-2 text-xs" style="color: var(--text-3)">Loading steps from conversation…</div>
          <div v-if="saveSkillSummary" class="mb-2 p-2 rounded text-xs" style="background: var(--bg-deep); color: var(--text-2)">{{ saveSkillSummary }}</div>
          <div class="mb-2">
            <label class="block text-xs mb-1" style="color: var(--text-3)">Steps (JSON)</label>
            <textarea v-model="saveSkillSteps" rows="10" class="w-full px-3 py-2 rounded-lg text-sm font-mono"
              style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
          </div>
        </template>

        <!-- Soft: guidance markdown -->
        <template v-else>
          <div class="mb-2">
            <label class="block text-xs mb-1" style="color: var(--text-3)">Guidance (markdown)</label>
            <textarea v-model="saveSkillGuidance" rows="10" placeholder="Describe how to accomplish the task…"
              class="w-full px-3 py-2 rounded-lg text-sm font-mono"
              style="background: var(--bg-deep); border: 1px solid var(--border); color: var(--text-1)" />
          </div>
        </template>

        <div v-if="saveSkillError" class="mb-2 p-2 rounded text-xs" style="background: #ef444411; color: #f87171">{{ saveSkillError }}</div>

        <div class="flex gap-2">
          <button class="btn btn-primary" :disabled="saveSkillSaving" @click="saveSkillFromChat">{{ saveSkillSaving ? 'Saving…' : 'Save' }}</button>
          <button v-if="saveSkillKind === 'hard'" class="btn" :disabled="saveSkillLoading" @click="loadDraftSteps">Reload steps</button>
          <button class="btn" @click="saveSkillModal = false">Close</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── Agent markdown ──────────────────────────────────────────────── */
.agent-md :deep(h1), .agent-md :deep(h2), .agent-md :deep(h3) { font-size: 13px; font-weight: 700; margin: 8px 0 4px; color: var(--text-1); }
.agent-md :deep(p) { margin: 4px 0; }
.agent-md :deep(ul), .agent-md :deep(ol) { margin: 4px 0; padding-left: 18px; }
.agent-md :deep(li) { margin: 2px 0; }
.agent-md :deep(code) { background: #1e293b; padding: 1px 4px; border-radius: 3px; font-size: 11px; color: #a5b4fc; }
.agent-md :deep(pre) { background: #0a0e14; padding: 8px; border-radius: 6px; overflow-x: auto; font-size: 10px; margin: 6px 0; }
.agent-md :deep(pre code) { background: none; padding: 0; }
.agent-md :deep(table) { border-collapse: collapse; width: 100%; font-size: 11px; margin: 6px 0; }
.agent-md :deep(th), .agent-md :deep(td) { border: 1px solid #1e293b; padding: 4px 8px; text-align: left; }
.agent-md :deep(th) { background: #111827; font-weight: 600; }
.agent-md :deep(strong) { color: var(--text-1); }
.agent-md :deep(hr) { border: none; border-top: 1px solid #1e293b; margin: 8px 0; }

/* ── Animation ──────────────────────────────────────────────────── */
@keyframes fadeInOut {
  0% { opacity: 0; transform: translateX(-50%) translateY(-4px); }
  8% { opacity: 1; transform: translateX(-50%) translateY(0); }
  75% { opacity: 1; }
  100% { opacity: 0; }
}

/* ── Root ────────────────────────────────────────────────────────── */
.phone-admin {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 80px);
  overflow: hidden;
}

/* ── Tab Bar ─────────────────────────────────────────────────────── */
.tab-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 0 12px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
  flex-shrink: 0;
}

.tab-group {
  display: flex;
  gap: 2px;
  background: var(--bg-deep);
  border-radius: 8px;
  padding: 3px;
  border: 1px solid var(--border);
}

.tab-btn {
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  border: none;
  cursor: pointer;
  background: transparent;
  color: var(--text-4);
  transition: all 0.15s;
}
.tab-btn:hover { color: var(--text-2); }
.tab-btn.active {
  background: var(--accent);
  color: #fff;
}

.status-text {
  font-size: 11px;
  color: var(--text-4);
}

/* ── Single Device Layout ────────────────────────────────────────── */
.single-layout {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 8px;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* Stream panel (left) */
.stream-panel {
  background: #070b10;
  border: 1px solid var(--border);
  border-radius: 8px;
  display: flex;
  align-items: stretch;
  justify-content: center;
  overflow: hidden;
  min-height: 0;
  min-width: 0;
}

.stream-viewport {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #070b10;
  overflow: hidden;
  width: 100%;
  height: 100%;
}

.stream-media {
  height: 100%;
  width: auto;
  max-width: 100%;
  object-fit: contain;
  cursor: pointer;
  touch-action: none;
  user-select: none;
}

.h264-badge {
  position: absolute; top: 6px; left: 6px; z-index: 3;
  font-size: 9px; padding: 2px 7px; border-radius: 10px;
  background: rgba(99,102,241,0.85); color: #fff; white-space: nowrap;
  cursor: pointer; border: none;
}
.h264-badge--show { opacity: 0.5; padding: 2px 6px; }
.h264-badge--show:hover { opacity: 1; }
.stream-placeholder {
  color: var(--text-4);
  font-size: 13px;
  text-align: center;
  padding: 40px;
}

/* Sidebar (right) */
.sidebar {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-section {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
}
.sidebar-section:last-child { border-bottom: none; }

.sidebar-section--grow {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.section-label {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--text-4);
  margin-bottom: 8px;
  text-transform: uppercase;
}

/* Device selector */
.device-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.device-select {
  flex: 1;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12px;
  background: var(--bg-deep);
  border: 1px solid var(--border);
  color: var(--text-1);
}

.nickname-row {
  margin-top: 6px;
}

.nickname-edit {
  display: flex;
  align-items: center;
  gap: 4px;
}

.nickname-input {
  flex: 1;
  padding: 4px 8px;
  border-radius: 5px;
  font-size: 12px;
  background: var(--bg-deep);
  border: 1px solid var(--border);
  color: var(--text-1);
}

/* Control buttons */
.ctrl-btn {
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-2);
  transition: all 0.12s;
  white-space: nowrap;
}
.ctrl-btn:hover { border-color: var(--accent); color: var(--text-1); }
.ctrl-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.ctrl-btn--webrtc { background: #0ea5e9; color: #fff; border-color: #0ea5e9; }
.ctrl-btn--webrtc:hover { background: #0284c7; border-color: #0284c7; }
.ctrl-btn--mjpeg { background: var(--accent); color: #fff; border-color: var(--accent); }
.ctrl-btn--mjpeg:hover { background: #4f46e5; border-color: #4f46e5; }
.ctrl-btn--stop { border-color: #475569; color: var(--text-3); }
.ctrl-btn--confirm { color: #34d399; border-color: #34d399; }
.ctrl-btn--fix { background: #f59e0b; color: #000; border: none; font-size: 9px; padding: 2px 6px; }
.ctrl-btn--fix:disabled,
.hw-key-btn:disabled {
  cursor: wait;
  opacity: 0.65;
}
.ctrl-btn--record {
  border-color: #7f1d1d;
  color: #fca5a5;
  background: #1f1118;
}
.ctrl-btn--record:hover:not(:disabled) {
  border-color: #ef4444;
  color: #fecaca;
}
.ctrl-btn--record-active {
  background: #ef444433;
  border-color: #ef444466;
  color: #fecaca;
}
.ctrl-btn--record-link {
  border-color: #164e63;
  color: #67e8f9;
  background: #082f49;
  text-decoration: none;
}

.ctrl-btn--tiny { font-size: 9px; padding: 2px 6px; }

.recording-status-line,
.multi-recording-status {
  display: flex;
  align-items: center;
  gap: 5px;
  min-height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}

.recording-status-line {
  padding: 0 6px;
  font-size: 9px;
}

.multi-recording-status {
  padding: 2px 6px 0;
  font-size: 8px;
}

.recording-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #ef4444;
  box-shadow: 0 0 0 2px #7f1d1d55;
  flex-shrink: 0;
}

.news-panel {
  margin: 0 4px;
  padding: 8px;
  border: 1px solid #1e293b;
  border-radius: 8px;
  background: #0a0f16;
  flex-shrink: 0;
  max-height: 260px;
  overflow-y: auto;
}

.news-panel-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.news-title {
  font-size: 10px;
  font-weight: 700;
  color: var(--text-2);
}

.news-panel-head .ctrl-btn {
  margin-left: auto;
}

.news-controls {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) 42px 42px 48px;
  gap: 4px;
}

.news-input,
.news-number {
  min-width: 0;
  padding: 4px 6px;
  border-radius: 5px;
  border: 1px solid #1e293b;
  background: #070b10;
  color: var(--text-2);
  font-size: 9px;
}

.news-input--out {
  grid-column: 1 / -1;
}

.news-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
  min-width: 0;
  padding: 4px 5px;
  border-radius: 5px;
  border: 1px solid #1e293b;
  background: #070b10;
  color: #94a3b8;
  font-size: 9px;
  white-space: nowrap;
}

.news-toggle input {
  width: 11px;
  height: 11px;
  accent-color: #6366f1;
}

.news-status {
  margin-top: 6px;
  padding: 4px 6px;
  border-radius: 5px;
  background: #111827;
  color: #94a3b8;
  font-size: 9px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.news-status--error {
  background: #3f1d26;
  color: #fca5a5;
  white-space: normal;
}

.news-result {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.news-summary,
.news-evidence,
.news-artifacts {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  color: var(--text-3);
  font-size: 9px;
}

.news-pill {
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 8px;
  font-weight: 700;
}

.news-pill--ok {
  background: #14532d;
  color: #86efac;
}

.news-pill--error {
  background: #7f1d1d;
  color: #fecaca;
}

.news-evidence span {
  padding: 2px 5px;
  border: 1px solid #1e293b;
  border-radius: 4px;
  background: #070b10;
  color: #67e8f9;
}

.news-artifacts span {
  max-width: 100%;
  padding: 2px 5px;
  border: 1px solid #1e293b;
  border-radius: 4px;
  background: #111827;
  color: #a5b4fc;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.news-list,
.news-articles {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.news-headline,
.news-article {
  padding: 5px 6px;
  border: 1px solid #1e293b;
  border-radius: 6px;
  background: #070b10;
}

.news-headline,
.news-article-title {
  color: var(--text-2);
  font-size: 10px;
  line-height: 1.35;
}

.news-article-meta {
  margin-top: 2px;
  color: #64748b;
  font-size: 8px;
}

.news-article-body,
.news-article-error {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 9px;
  line-height: 1.4;
  max-height: 58px;
  overflow: hidden;
  white-space: pre-line;
}

.news-article-error {
  color: #fca5a5;
}

.btn-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.stream-status-line {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.stream-status-text {
  font-size: 10px;
  color: var(--text-4);
}

/* Hardware keys grid */
.hw-keys-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 3px;
}
.hw-keys-grid .ctrl-btn {
  padding: 4px 2px;
  font-size: 9px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Log filter row */
.log-filter-row {
  display: flex;
  gap: 3px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.log-filter-btn {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 500;
  cursor: pointer;
  background: var(--bg-deep);
  color: var(--text-3);
  border: 1px solid var(--border);
  transition: all 0.12s;
}
.log-filter-btn.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
  font-weight: 700;
}

.log-scroll {
  flex: 1;
  min-height: 60px;
  max-height: 180px;
  overflow-y: auto;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--bg-deep);
  font-family: 'Courier New', monospace;
  font-size: 10px;
  line-height: 1.6;
}
.log-scroll--bot { color: #10b981; }

.log-line {
  color: var(--text-4);
  word-break: break-all;
}

.log-empty {
  color: var(--text-4);
  font-style: italic;
}

/* Bot log select */
.bot-log-select {
  width: 100%;
  font-size: 11px;
  padding: 4px 8px;
  margin-bottom: 6px;
  background: var(--bg-deep);
  color: var(--text-3);
  border: 1px solid var(--border);
  border-radius: 5px;
}

/* Black screen warning */
.black-warning {
  position: absolute;
  top: 8px;
  left: 50%;
  transform: translateX(-50%);
  background: #f59e0bee;
  color: #000;
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  z-index: 10;
  pointer-events: auto;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 8px;
  animation: fadeInOut 6s ease-in-out forwards;
}
.black-warning--compact {
  padding: 4px 10px;
  font-size: 9px;
  gap: 6px;
}

.black-warning-btn {
  background: #000;
  color: #f59e0b;
  border: none;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}

/* ── Frozen stream overlay ──────────────────────────────────────── */
.frozen-overlay {
  position: absolute; inset: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  background: rgba(10, 15, 12, 0.85);
  backdrop-filter: blur(4px);
  cursor: pointer; z-index: 10;
  border-radius: inherit;
}
.frozen-ghost { font-size: 48px; margin-bottom: 8px; opacity: 0.9; }
.frozen-text { font-size: 14px; font-weight: 700; color: #f59e0b; letter-spacing: 0.5px; }
.frozen-hint { font-size: 11px; color: #8a9a8d; margin-top: 4px; }

/* ── Multi Device Layout ─────────────────────────────────────────── */
.multi-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.device-count {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-4);
}

.multi-grid {
  display: grid;
  gap: 8px;
  height: calc(100vh - 180px);
}

.multi-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  min-height: 0;
}

.multi-card-header {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  flex-wrap: wrap;
}

.multi-device-name {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-2);
}

.health-dots {
  display: flex;
  gap: 3px;
  align-items: center;
  margin-left: 4px;
}
.health-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.ios-recovery-panel {
  margin: 0 4px 4px;
  padding: 8px 10px;
  border: 1px solid #f59e0b44;
  background: #0f172a;
  border-radius: 6px;
  color: var(--text-2);
  font-size: 10px;
}

.ios-recovery-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
  min-width: 0;
}

.ios-recovery-state {
  color: #fbbf24;
  font-weight: 700;
}

.ios-recovery-code {
  color: #fed7aa;
  border: 1px solid #f59e0b55;
  border-radius: 4px;
  padding: 1px 5px;
}

.ios-recovery-link {
  margin-left: auto;
  color: #93c5fd;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 10px;
  padding: 0;
}

.ios-recovery-summary {
  color: var(--text-3);
  line-height: 1.35;
}

.ios-recovery-summary-inline {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
  font-size: 11px;
  opacity: 0.85;
}

.ios-recovery-link--fix {
  margin-left: 0;
  color: #6ee7b7;
  font-weight: 700;
}

.ios-recovery-details {
  margin-top: 6px;
}

.ios-recovery-status {
  margin-top: 4px;
  color: #93c5fd;
  font-size: 9px;
  line-height: 1.3;
}

.ios-health-details {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.ios-health-chip {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 2px 5px;
  border: 1px solid #334155;
  border-radius: 4px;
  background: #020617;
  color: #94a3b8;
  font-size: 9px;
}

.ios-health-chip--ok {
  border-color: #16653466;
  color: #86efac;
  background: #052e1622;
}

.ios-recovery-steps {
  margin: 5px 0 0;
  padding-left: 16px;
  color: var(--text-4);
  line-height: 1.35;
}

.ios-recovery-commands {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 6px;
}

.ios-recovery-command {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px;
  align-items: center;
}

.ios-recovery-command code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 4px 6px;
  border: 1px solid #334155;
  border-radius: 4px;
  background: #020617;
  color: #c4b5fd;
  font-size: 9px;
}

.ios-copy-btn {
  padding: 3px 6px;
  border: 1px solid #334155;
  border-radius: 4px;
  background: #111827;
  color: #93c5fd;
  font-size: 9px;
  cursor: pointer;
}

.ios-copy-btn:hover {
  border-color: #60a5fa;
  color: #bfdbfe;
}

.ios-recovery-compact {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 3px 6px;
  border-top: 1px solid #f59e0b33;
  background: #f59e0b12;
  color: #fbbf24;
  font-size: 9px;
  line-height: 1.2;
}

.multi-hw-keys {
  display: flex;
  gap: 2px;
  margin-left: 4px;
}

.hw-key-btn {
  padding: 2px 5px;
  background: var(--bg-deep);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-3);
  font-size: 10px;
  cursor: pointer;
  transition: border-color 0.12s;
}
.hw-key-btn:hover { border-color: var(--accent); }

.multi-stream-btns {
  margin-left: auto;
  display: flex;
  gap: 3px;
  align-items: center;
}

.multi-mode-badge {
  font-size: 8px;
  padding: 1px 5px;
  border-radius: 3px;
}

/* Stream area */
.multi-stream-area {
  position: relative;
  flex: 4;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #070b10;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

.multi-stream-media {
  max-height: 100%;
  max-width: 100%;
  object-fit: contain;
  cursor: pointer;
  touch-action: none;
  user-select: none;
}

.multi-stream-placeholder {
  color: #334155;
  font-size: 11px;
}

/* Per-device logs */
.multi-logs-area {
  flex: 1;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  background: #050a0f;
  min-height: 0;
  overflow: hidden;
}

.multi-logs-header {
  padding: 3px 8px;
  font-size: 9px;
  font-weight: 600;
  color: #22c55e;
  border-bottom: 1px solid #0f1a12;
  flex-shrink: 0;
  background: #071209;
}

.multi-logs-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 4px 6px;
  font-family: 'Courier New', monospace;
  font-size: 9px;
  line-height: 1.5;
}

.multi-log-line {
  padding-left: 6px;
}

.multi-log-empty {
  color: #1a3020;
  font-style: italic;
}

.multi-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-4);
  font-size: 12px;
  padding: 40px 0;
}
.grid-touch-toggle { display:flex;align-items:center;gap:5px;font-size:11px;color:#bdd5c6; }
.stream-intent-note { color:#69c394;font-size:10px; }
.device-name-control { background:none;border:0;cursor:pointer;text-align:left;color:inherit;padding:0; }
.device-name-control:hover { color:#65dda0;text-decoration:underline; }
.preview-control-button { width:100%;border:0;background:transparent;cursor:pointer; }
.tile-action-message { font-size:10px;color:#83cba0;padding:4px 8px; }
.direct-control-overlay { position:fixed;inset:0;z-index:1000;background:#000b;display:flex;align-items:center;justify-content:center;padding:14px; }
.direct-control-dialog { width:min(560px,100%);height:min(940px,calc(100dvh - 28px));display:flex;flex-direction:column;background:#0b110e;border:1px solid #37694b;border-radius:12px;overflow:hidden;box-shadow:0 20px 80px #000b; }
.direct-control-bar { display:flex;gap:8px;align-items:center;padding:9px;color:#91bba2;font-size:10px; }.direct-control-bar span { margin-right:auto; }
.direct-control-dialog :deep(.device-window) { height:auto;min-height:0;flex:1;overflow:auto; }

/* Comfortable, scrollable fleet previews and a non-blocking control desk. */
.multi-dashboard-pane { flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding-right:3px; }
.multi-dashboard-pane .multi-header { position:sticky;top:0;z-index:4;background:#0b110ef5;backdrop-filter:blur(10px);padding:8px 3px;border-bottom:1px solid #294c37; }
.phone-admin.with-control-dock { padding-right:calc(clamp(370px, 40vw, 560px) + 18px); }
.multi-grid { height:auto; align-items:start; grid-auto-rows:auto; padding-bottom:16px; }
.multi-card { min-height:0; border-radius:11px; }
.multi-card.is-active-device { border-color:#5fca8c; box-shadow:0 0 0 1px #5fca8c55; }
.multi-stream-area { flex:none; height:var(--phone-preview-height,320px); min-height:180px; }
.multi-stream-media { width:100%; height:100%; max-width:100%; max-height:100%; }
.multi-stream-media:focus-visible { outline:2px solid #7bdea4;outline-offset:-3px; }
.multi-logs-area { flex:none; max-height:100px; }
.multi-logs-scroll { max-height:76px; }
.multi-stream-placeholder { width:100%;height:100%;font-size:12px;line-height:1.5;color:#91b9a3; }
.dashboard-interaction-help { display:flex;gap:7px 14px;flex-wrap:wrap;align-items:center;border:1px solid #294c37;background:#0d1e14;border-radius:8px;padding:9px 12px;margin-bottom:10px;color:#a8c8b5;font-size:11px;line-height:1.5; }
.dashboard-interaction-help strong { color:#86dba7; }.dashboard-interaction-help small { flex-basis:100%;color:#71af8b; }
.grid-touch-toggle select,.control-device-switcher select { min-width:0;border:1px solid #31523b;border-radius:6px;background:#112016;color:#caddcf;padding:5px;font:inherit; }
.direct-control-overlay { position:fixed;inset:88px 10px 10px auto;width:clamp(370px,40vw,560px);z-index:1000;background:none;display:flex;padding:0;pointer-events:none; }
.direct-control-dialog { width:100%;height:100%;min-height:0;display:flex;flex-direction:column;background:#0b110e;border:1px solid #37694b;border-radius:12px;overflow:hidden;box-shadow:0 14px 50px #0007;pointer-events:auto; }
.direct-control-bar { flex-shrink:0;display:flex;gap:7px;align-items:center;padding:8px;color:#91bba2;font-size:10px; }.direct-control-bar span { margin-right:auto; }
.control-device-switcher { flex-shrink:0;display:flex;align-items:center;gap:6px;padding:0 8px 8px;border-bottom:1px solid #294331; }.control-device-switcher select { flex:1;width:0;font-size:11px; }.control-device-switcher small { color:#7aa58a;font-size:10px; }
.direct-control-dialog :deep(.device-window) { height:0;min-height:0;flex:1;overflow:auto; }
@media(max-width:1000px) { .phone-admin.with-control-dock { padding-right:0; }.direct-control-overlay { width:min(540px,calc(100vw - 20px));inset:92px 10px 10px auto; }.direct-control-dialog { box-shadow:0 8px 50px #000b; } }
@media(pointer:coarse) { .grid-touch-toggle { min-height:40px; }.multi-stream-btns .ctrl-btn,.control-device-switcher button { min-height:36px; }.control-device-switcher select { min-height:38px;font-size:16px; } }

</style>

<style scoped>
/* ==========================================================================
   Desktop redesign layer (v2). Appended after the original rules so it wins
   on equal specificity while the original functional layout rules remain.
   ========================================================================== */
.phone-admin { height: calc(100vh - var(--ui-topbar-h) - 34px); gap: 12px; }
.phone-admin.is-document-view { height: auto; overflow: visible; }
.phone-admin.with-control-dock { padding-right: calc(clamp(380px, 34vw, 560px) + 16px); }

/* ---- Shared controls (ctrl-btn family) ---- */
.ctrl-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: var(--ui-control-h); padding: 0 11px; border-radius: var(--ui-radius-sm);
  border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-1);
  font-size: var(--ui-fs-xs); font-weight: 600; line-height: 1; text-decoration: none;
  transition: background-color 0.15s var(--ui-ease), border-color 0.15s var(--ui-ease), color 0.15s var(--ui-ease);
}
.ctrl-btn:hover:not(:disabled) { background: var(--ui-surface-hover); border-color: var(--ui-border-hover); color: var(--ui-text-1); }
.ctrl-btn:disabled { opacity: 0.42; }
.ctrl-btn--webrtc { background: var(--ui-accent); border-color: var(--ui-accent); color: var(--ui-on-accent); font-weight: 650; }
.ctrl-btn--webrtc:hover:not(:disabled) { background: var(--ui-accent-hover); border-color: var(--ui-accent-hover); color: var(--ui-on-accent); }
.ctrl-btn--mjpeg { background: var(--ui-info-soft); border-color: rgba(103, 170, 255, 0.4); color: #b6d4ff; }
.ctrl-btn--mjpeg:hover:not(:disabled) { background: rgba(103, 170, 255, 0.2); border-color: rgba(103, 170, 255, 0.55); color: #d4e6ff; }
.ctrl-btn--stop { background: var(--ui-surface-2); border-color: var(--ui-border-strong); color: var(--ui-text-2); }
.ctrl-btn--stop:hover:not(:disabled) { background: var(--ui-danger-soft); border-color: rgba(242, 109, 109, 0.45); color: #ffb0b0; }
.ctrl-btn--accent { background: var(--ui-accent-soft); border-color: var(--ui-accent-line); color: #93ebd2; }
.ctrl-btn--accent:hover:not(:disabled) { background: rgba(63, 208, 168, 0.18); border-color: rgba(63, 208, 168, 0.55); color: #c1f5e6; }
.ctrl-btn--confirm { background: var(--ui-success-soft); border-color: rgba(70, 207, 139, 0.4); color: #93e8bd; }
.ctrl-btn--fix { height: var(--ui-control-h-sm); padding: 0 8px; background: var(--ui-warn-soft); border: 1px solid rgba(240, 180, 76, 0.45); color: #f8d08a; font-size: var(--ui-fs-2xs); }
.ctrl-btn--record { background: var(--ui-surface-2); border-color: var(--ui-border-strong); color: #ffadad; }
.ctrl-btn--record:hover:not(:disabled) { background: var(--ui-danger-soft); border-color: rgba(242, 109, 109, 0.5); color: #ffc7c7; }
.ctrl-btn--record-active { background: rgba(242, 109, 109, 0.18); border-color: rgba(242, 109, 109, 0.55); color: #ffd0d0; }
.ctrl-btn--record-link { background: var(--ui-info-soft); border-color: rgba(103, 170, 255, 0.4); color: #b6d4ff; }
.ctrl-btn--tiny { height: var(--ui-control-h-sm); padding: 0 8px; font-size: var(--ui-fs-2xs); gap: 4px; }
.ctrl-btn--icon { width: var(--ui-control-h); padding: 0; }
.ctrl-btn--tiny.ctrl-btn--icon { width: var(--ui-control-h-sm); }
.hw-key-btn--toggle.is-on, .ctrl-btn.hw-key-btn--toggle.is-on { background: var(--ui-warn-soft); border-color: rgba(240, 180, 76, 0.55); color: #f8d08a; }

/* ---- Live grid: command bar + status ---- */
.multi-dashboard-pane { padding-right: 2px; }
.multi-dashboard-pane .multi-header,
.multi-header {
  position: sticky; top: 0; z-index: 4; margin: 0; padding: 0;
  display: flex; flex-direction: column; align-items: stretch; gap: 0;
  background: rgba(18, 22, 27, 0.96); backdrop-filter: blur(8px);
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg);
}
.multi-header__row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 12px; }
.multi-header__meta { display: flex; align-items: center; gap: 6px 16px; flex-wrap: wrap; margin: 0; padding: 8px 12px; border-top: 1px solid var(--ui-border); background: rgba(255, 255, 255, 0.012); border-radius: 0 0 var(--ui-radius-lg) var(--ui-radius-lg); }
.multi-header__meta .ui-toolbar__group { gap: 14px; }
.multi-search { width: clamp(180px, 16vw, 260px); }
.multi-search input { width: 100%; }
.multi-view-select { width: auto; }
.grid-touch-toggle { gap: 8px; color: var(--ui-text-2); font-size: var(--ui-fs-xs); font-weight: 550; }
.grid-touch-toggle select, .grid-size-control select { width: auto; border-color: var(--ui-border-strong); background: var(--ui-surface-sunken); color: var(--ui-text-1); padding: 0 24px 0 8px; height: var(--ui-control-h-sm); font-size: var(--ui-fs-xs); border-radius: var(--ui-radius-sm); }
.grid-size-control > span { color: var(--ui-text-3); }

.multi-statusbar { font-size: var(--ui-fs-xs); color: var(--ui-text-3); }
.device-count { color: var(--ui-text-2); font-size: var(--ui-fs-xs); }
.device-count strong { color: var(--ui-text-1); font-weight: 650; }
.stream-intent-note { display: inline-flex; align-items: center; gap: 7px; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.stream-intent-note .ui-dot { color: var(--ui-text-4); }
.stream-intent-note.is-on { color: #9be6c8; }
.stream-intent-note.is-on .ui-dot { color: var(--ui-success); box-shadow: 0 0 0 3px rgba(70, 207, 139, 0.15); }
.multi-statusbar__spec { color: var(--ui-text-4); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); }
.dashboard-interaction-help { display: flex; align-items: baseline; flex-wrap: wrap; margin: 10px 2px 12px; padding: 0; border: 0; background: none; color: var(--ui-text-3); font-size: var(--ui-fs-xs); gap: 4px 10px; }
.dashboard-interaction-help strong { color: var(--ui-text-1); font-weight: 650; }
.dashboard-interaction-help small { flex-basis: 100%; color: var(--ui-accent); font-size: var(--ui-fs-2xs); }

/* ---- Live grid: tiles ---- */
.multi-grid { gap: 12px; padding-bottom: 20px; }
.multi-card { background: var(--ui-surface-1); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); transition: border-color 0.15s var(--ui-ease), box-shadow 0.15s var(--ui-ease); }
.multi-card:hover { border-color: var(--ui-border-strong); }
.multi-card.is-active-device { border-color: var(--ui-accent-line); box-shadow: 0 0 0 1px var(--ui-accent-line); }
.multi-card-header { gap: 8px; padding: 8px 10px 6px; border-bottom: 0; flex-wrap: nowrap; min-height: 36px; }
.multi-card-header input[type='checkbox'] { flex: none; }
.multi-device-name { display: inline-flex; align-items: baseline; gap: 6px; min-width: 0; flex: 1 1 auto; font-size: var(--ui-fs-sm); font-weight: 650; color: var(--ui-text-1); overflow: hidden; }
.device-name-control:hover { color: var(--ui-text-1); text-decoration: none; }
.device-name-control:hover .device-name-text { text-decoration: underline; text-decoration-color: var(--ui-accent-line); text-underline-offset: 3px; }
.device-conn-icon { color: var(--ui-text-3); align-self: center; }
.device-name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.device-serial { flex: none; color: var(--ui-text-4); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); font-weight: 500; }
.multi-device-name { flex: 1 1 auto; }
.device-serial { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.health-dots { flex: none; margin-left: auto; }
.multi-stream-btns { margin: 0; gap: 4px; flex: 1 1 auto; min-width: 0; display: flex; align-items: center; justify-content: flex-end; }
.multi-mode-badge { display: inline-flex; align-items: center; height: 20px; padding: 0 8px; margin-right: auto; border-radius: 999px; font-size: var(--ui-fs-2xs); font-weight: 600; font-family: var(--ui-mono); white-space: nowrap; min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; flex: 0 1 auto; display: block; line-height: 20px; }
.multi-mode-badge:empty { display: none; }
.multi-mode-badge.is-live { background: var(--ui-success-soft); color: #93e8bd; }
.multi-mode-badge.is-mjpeg { background: var(--ui-info-soft); color: #b6d4ff; }
.multi-mode-badge.is-idle { background: var(--ui-surface-3); color: var(--ui-text-3); }
.multi-card-subbar { display: flex; align-items: center; gap: 8px; padding: 0 10px 8px; border-bottom: 1px solid var(--ui-border); min-height: 34px; }
.multi-card-keybar { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 6px 8px; border-top: 1px solid var(--ui-border); background: var(--ui-surface-2); border-radius: 0 0 var(--ui-radius-lg) var(--ui-radius-lg); }
.multi-hw-keys { display: flex; gap: 3px; margin: 0; flex-wrap: nowrap; min-width: 0; overflow: hidden; }
.multi-card-tools { display: flex; align-items: center; gap: 3px; flex: none; }
.multi-card-tools .ctrl-btn--tiny { height: 22px; }
.multi-card-tools .ctrl-btn--tiny.ctrl-btn--icon { width: 22px; }
.hw-key-btn { display: inline-flex; align-items: center; justify-content: center; gap: 4px; height: 22px; min-width: 22px; padding: 0 5px; border-radius: var(--ui-radius-xs); border: 1px solid var(--ui-border); background: var(--ui-surface-3); color: var(--ui-text-2); font-size: 10px; font-weight: 600; cursor: pointer; flex: none; transition: background-color 0.12s, border-color 0.12s, color 0.12s; }
.hw-key-btn:hover:not(:disabled) { background: var(--ui-surface-hover); border-color: var(--ui-border-hover); color: var(--ui-text-1); }
.health-dots { gap: 3px; }
.health-dot { width: 6px; height: 6px; }
.multi-stream-area { background: #07090c; }
.multi-stream-media:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: -2px; }
.multi-stream-placeholder, .preview-control-button { color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.preview-control-button:hover { color: var(--ui-text-1); background: rgba(255, 255, 255, 0.02); }
.tile-action-message { padding: 6px 10px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); border-top: 1px solid var(--ui-border); }
.multi-recording-status, .recording-status-line { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.multi-logs-area { background: var(--ui-surface-sunken); border-top: 1px solid var(--ui-border); }
.multi-logs-header { background: var(--ui-surface-2); color: var(--ui-text-3); border-bottom: 1px solid var(--ui-border); font-size: var(--ui-fs-2xs); letter-spacing: 0.04em; text-transform: uppercase; padding: 5px 10px; }
.multi-logs-scroll { font-family: var(--ui-mono); font-size: 10px; color: var(--ui-text-3); }
.multi-empty { color: var(--ui-text-3); }

/* ---- Frozen / warnings ---- */
.frozen-overlay { background: rgba(8, 10, 13, 0.82); }
.frozen-ghost { color: var(--ui-accent); font-size: 0; margin-bottom: 6px; }
.frozen-text { color: var(--ui-text-1); font-weight: 650; font-size: var(--ui-fs-sm); }
.frozen-hint { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.black-warning { background: var(--ui-warn-soft); border: 1px solid rgba(240, 180, 76, 0.4); color: #f8d08a; border-radius: var(--ui-radius-sm); }
.black-warning-btn { border-radius: var(--ui-radius-xs); }

/* ---- Direct control dock ---- */
.direct-control-overlay { inset: calc(var(--ui-topbar-h) + 12px) 14px 14px auto; width: clamp(380px, 34vw, 560px); }
.direct-control-dialog { background: var(--ui-surface-1); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-lg); box-shadow: var(--ui-shadow-pop); }
.direct-control-bar { gap: 8px; padding: 10px 12px; color: var(--ui-text-3); font-size: var(--ui-fs-xs); border-bottom: 1px solid var(--ui-border); }
.direct-control-bar .dock-title { display: flex; align-items: center; gap: 9px; margin-right: auto; color: var(--ui-accent); min-width: 0; }
.direct-control-bar .dock-title div { display: grid; line-height: 1.2; min-width: 0; }
.direct-control-bar .dock-title strong { color: var(--ui-text-1); font-size: var(--ui-fs-sm); font-weight: 650; }
.direct-control-bar .dock-title span { margin: 0; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.control-device-switcher { gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-2); }
.control-device-switcher select { height: var(--ui-control-h); border: 1px solid var(--ui-border-strong); background: var(--ui-surface-sunken); color: var(--ui-text-1); border-radius: var(--ui-radius-sm); padding: 0 10px; font-size: var(--ui-fs-xs); }
.control-device-switcher small { color: var(--ui-text-3); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); white-space: nowrap; }

/* ---- Agent console ---- */
.single-layout { grid-template-columns: minmax(0, 1fr) clamp(330px, 27vw, 460px); gap: 12px; }
.agent-panel { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; background: var(--ui-surface-1); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); }
.agent-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex-shrink: 0; padding: 10px 14px; border-bottom: 1px solid var(--ui-border); }
.agent-state-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.agent-header__title { font-size: var(--ui-fs-sm); font-weight: 650; color: var(--ui-text-1); margin-right: 4px; }
.agent-select { height: var(--ui-control-h-sm); padding: 0 22px 0 8px; font-size: var(--ui-fs-xs); background-color: var(--ui-surface-sunken); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); color: var(--ui-text-2); }
.agent-select--conversation { max-width: 180px; }
.agent-select--model { max-width: 190px; }
.agent-header__actions { margin-left: auto; display: flex; align-items: center; gap: 6px; }
.agent-chip-btn { display: inline-flex; align-items: center; height: 24px; padding: 0 9px; border-radius: var(--ui-radius-xs); border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; cursor: pointer; white-space: nowrap; }
.agent-chip-btn:hover:not(:disabled) { background: var(--ui-surface-hover); color: var(--ui-text-1); border-color: var(--ui-border-hover); }
.agent-chip-btn:disabled { opacity: 0.45; }
.agent-chip-btn.is-on { background: var(--ui-accent-soft); border-color: var(--ui-accent-line); color: #93ebd2; }
.agent-chip-btn--accent { color: #93ebd2; border-color: var(--ui-accent-line); }
.agent-chip-btn--danger { color: #ffa3a3; border-color: rgba(242, 109, 109, 0.4); }
.agent-chip-btn--danger:hover:not(:disabled) { background: var(--ui-danger-soft); color: #ffc4c4; }
.agent-pulse { animation: pulse 1.5s infinite; }
.agent-pull { display: inline-flex; align-items: center; gap: 4px; margin-left: 2px; }
.agent-pull__input { height: 24px; width: 120px; padding: 0 8px; font-size: var(--ui-fs-2xs); }
.agent-capabilities { display: inline-flex; align-items: center; gap: 4px; color: var(--ui-text-4); font-size: var(--ui-fs-2xs); white-space: nowrap; }
.agent-capability { padding: 2px 5px; border: 1px solid var(--ui-border); border-radius: 999px; }
.agent-capability.is-supported { color: #93e8bd; border-color: rgba(70, 207, 139, 0.3); }
.agent-download { color: #f6cd82; font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); white-space: nowrap; }
.agent-tokens { font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); color: var(--ui-text-4); }
.agent-options { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--ui-border); background: var(--ui-surface-2); color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.agent-options label { display: inline-flex; align-items: center; gap: 4px; }
.agent-options input { width: 76px; height: 23px; padding: 0 5px; color: var(--ui-text-1); background: var(--ui-surface-sunken); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-xs); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); }
.agent-options__hint { margin-left: auto; color: var(--ui-text-4); }
.agent-messages { flex: 1; overflow-y: auto; padding: 18px 18px 12px; display: flex; flex-direction: column; gap: 10px; }
.agent-empty { margin: auto; max-width: 420px; padding: 40px 20px; text-align: center; color: var(--ui-text-3); font-size: var(--ui-fs-sm); line-height: 1.7; border: 1px dashed var(--ui-border-strong); border-radius: var(--ui-radius-lg); background: var(--ui-surface-sunken); }
.msg { padding: 9px 13px; font-size: var(--ui-fs-sm); line-height: 1.6; border-radius: 10px; }
.msg--user { background: var(--ui-accent-strong); color: #f3fffb; border-bottom-right-radius: 3px; }
.msg--assistant { background: var(--ui-surface-3); color: var(--ui-text-1); border: 1px solid var(--ui-border); border-bottom-left-radius: 3px; }
.msg-tool { display: inline-flex; align-items: center; padding: 3px 9px; font-size: var(--ui-fs-2xs); font-family: var(--ui-mono); color: #a7cbff; background: var(--ui-info-soft); border: 1px solid rgba(103, 170, 255, 0.25); border-radius: 999px; }
.msg-tool-result { margin-left: 10px; padding: 3px 10px; max-height: 64px; overflow: hidden; text-overflow: ellipsis; font-size: var(--ui-fs-2xs); font-family: var(--ui-mono); color: var(--ui-text-4); border-left: 2px solid var(--ui-border-strong); }
.msg-shot { width: 100%; border-radius: var(--ui-radius); border: 1px solid var(--ui-border); }
.msg-error { padding: 7px 11px; font-size: var(--ui-fs-xs); color: #ffb0b0; background: var(--ui-danger-soft); border-radius: var(--ui-radius-sm); border-left: 2px solid var(--ui-danger); }
.msg-system { padding: 6px 10px; font-size: var(--ui-fs-xs); color: var(--ui-text-3); background: var(--ui-surface-2); border-radius: var(--ui-radius-sm); border-left: 2px solid var(--ui-border-strong); }
.msg-system.is-error { color: var(--ui-danger); border-left-color: var(--ui-danger); }
.msg-stopped { padding: 6px 10px; font-size: var(--ui-fs-xs); color: #f6cd82; background: rgba(245, 158, 11, 0.08); border-radius: var(--ui-radius-sm); border-left: 2px solid var(--ui-warn); }
.agent-activity { align-self: flex-start; display: flex; align-items: center; gap: 8px; padding: 4px 4px; }
.agent-activity__dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ui-warn); animation: pulse 1s infinite; }
.agent-activity__text { font-size: var(--ui-fs-2xs); color: #f6cd82; font-family: var(--ui-mono); }
.agent-composer { display: flex; gap: 8px; flex-shrink: 0; padding: 12px 14px; border-top: 1px solid var(--ui-border); background: var(--ui-surface-2); }
.agent-input { flex: 1; height: var(--ui-control-h-lg); font-size: var(--ui-fs-sm); }
.agent-send { height: var(--ui-control-h-lg); min-width: 76px; }

.phone-column { display: flex; flex-direction: column; gap: 8px; min-height: 0; min-width: 0; background: var(--ui-surface-1); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); padding: 10px; }
.phone-toolbar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.phone-toolbar .device-select { flex: 1 1 140px; min-width: 120px; width: auto; }
.phone-toolbar .device-select, .device-select { height: var(--ui-control-h-sm); padding: 0 24px 0 8px; line-height: normal; font-size: var(--ui-fs-xs); border-radius: var(--ui-radius-sm); }
.phone-toolbar .ctrl-btn { height: var(--ui-control-h-sm); padding: 0 9px; font-size: var(--ui-fs-2xs); }
.phone-toolbar .ctrl-btn--icon { width: var(--ui-control-h-sm); padding: 0; }
.mode-pill { display: inline-flex; align-items: center; height: 20px; padding: 0 8px; border-radius: 999px; font-size: var(--ui-fs-2xs); font-weight: 600; white-space: nowrap; }
.mode-pill.is-live { background: var(--ui-success-soft); color: #93e8bd; border: 1px solid rgba(70, 207, 139, 0.3); }
.mode-pill.is-mjpeg { background: var(--ui-info-soft); color: #b6d4ff; border: 1px solid rgba(103, 170, 255, 0.3); }
.stream-panel { flex: 1; background: #07090c; border: 1px solid var(--ui-border); border-radius: var(--ui-radius); }
.stream-viewport { background: #07090c; }
.stream-placeholder { color: var(--ui-text-3); font-size: var(--ui-fs-xs); line-height: 1.7; text-align: center; }
.phone-keys { display: flex; gap: 6px; justify-content: center; flex-wrap: wrap; padding-top: 2px; }
.phone-keys .ctrl-btn { height: var(--ui-control-h-sm); padding: 0 12px; font-size: var(--ui-fs-2xs); }
.h264-badge { font-family: var(--ui-mono); border-radius: var(--ui-radius-xs); }

/* ---- iOS / news auxiliary panels ---- */
.ios-recovery-panel, .news-panel { background: var(--ui-surface-2); border: 1px solid var(--ui-border); border-radius: var(--ui-radius); }
.news-input, .news-number { background: var(--ui-surface-sunken); border: 1px solid var(--ui-border-strong); color: var(--ui-text-1); border-radius: var(--ui-radius-xs); }

@media (max-width: 1440px) {
  .multi-statusbar__spec { display: none; }
  .dashboard-interaction-help { flex-basis: 100%; justify-content: flex-start; text-align: left; }
}
@media (max-width: 1366px) {
  .multi-search { width: 180px; }
  .grid-size-control > span { display: none; }
}

/* ---- Live grid at fleet scale: Sync All, tile states, dense layouts ---- */
.sync-all-btn { gap: 6px; }
.sync-all-btn.is-on { background: var(--ui-accent-soft); border-color: var(--ui-accent-line); color: var(--ui-accent); }
.sync-all-btn.is-arming { border-color: var(--ui-warn); color: var(--ui-warn); }
.sync-all-count { display: inline-flex; align-items: center; justify-content: center; min-width: 20px; height: 18px; padding: 0 6px; border-radius: 9px; background: var(--ui-surface-3); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 650; font-variant-numeric: tabular-nums; }
.sync-all-btn.is-on .sync-all-count { background: var(--ui-accent); color: var(--ui-on-accent); }
.sync-scope-select { width: 116px; }
.statusbar-link { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 8px; border: 1px solid transparent; border-radius: var(--ui-radius-xs); background: none; color: var(--ui-text-2); font: inherit; font-size: var(--ui-fs-xs); cursor: pointer; }
.statusbar-link:hover:not(:disabled) { background: var(--ui-surface-hover); color: var(--ui-text-1); }
.statusbar-link:focus-visible { outline: 2px solid var(--ui-border-focus); outline-offset: 1px; }
.statusbar-link:disabled { color: var(--ui-text-4); cursor: default; }
.statusbar-link.is-warn { color: var(--ui-warn); }
.device-count--live { display: inline-flex; align-items: center; gap: 6px; }
.tile-state-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--ui-text-4); }
.tile-state-dot.is-live { background: var(--ui-success); }
.tile-state-dot.is-attention { background: var(--ui-warn); }

.sync-banner { display: flex; align-items: center; gap: 12px; margin: 10px 0 0; padding: 10px 14px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius); background: var(--ui-surface-2); color: var(--ui-text-2); font-size: var(--ui-fs-xs); }
.sync-banner.is-arming { border-color: var(--ui-warn); background: var(--ui-warn-soft); color: var(--ui-text-2); }
.sync-banner.is-arming > svg { flex: none; color: var(--ui-warn); }
.sync-banner.is-on { border-color: var(--ui-accent-line); background: var(--ui-accent-soft); }
.sync-banner__text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 auto; }
.sync-banner__text strong { color: var(--ui-text-1); font-size: var(--ui-fs-sm); font-weight: 650; }
.sync-banner__text span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sync-banner__keys { display: flex; gap: 4px; flex: none; }
.sync-banner__keys .hw-key-btn { min-width: 52px; }
.sync-banner__pulse { flex: none; width: 9px; height: 9px; border-radius: 50%; background: var(--ui-accent); box-shadow: 0 0 0 4px var(--ui-accent-soft); animation: sync-pulse 1.6s ease-in-out infinite; }
@keyframes sync-pulse { 50% { box-shadow: 0 0 0 7px transparent; } }
@media (prefers-reduced-motion: reduce) { .sync-banner__pulse { animation: none; } }
.dock-sync-note { color: var(--ui-accent) !important; }

.multi-card.is-sync-target { border-color: var(--ui-accent-line); }
.multi-card.state-attention { border-color: var(--ui-warn); }

/* Quick tile actions replace the sub-bar in dense layouts. */
.tile-quick { display: none; flex: none; gap: 3px; }
.tile-quick__btn { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; padding: 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-xs); background: var(--ui-surface-2); color: var(--ui-text-2); cursor: pointer; }
.tile-quick__btn:hover { color: var(--ui-text-1); border-color: var(--ui-border-strong); background: var(--ui-surface-hover); }
.tile-quick__btn:focus-visible { outline: 2px solid var(--ui-border-focus); outline-offset: 1px; }
.multi-grid.density-wall .tile-quick, .multi-grid.density-compact .tile-quick { display: inline-flex; }

/* Wall (about 50 phones per desktop screen) and Compact: picture first, one-line header. */
.multi-grid.density-wall { gap: 8px; }
.multi-grid.density-compact { gap: 10px; }
.multi-grid.density-wall .multi-card-subbar, .multi-grid.density-compact .multi-card-subbar,
.multi-grid.density-wall .multi-card-keybar, .multi-grid.density-compact .multi-card-tools,
.multi-grid.density-wall .multi-recording-status, .multi-grid.density-compact .multi-recording-status,
.multi-grid.density-wall .ios-recovery-compact, .multi-grid.density-wall .health-dots,
.multi-grid.density-wall .device-serial, .multi-grid.density-compact .device-serial,
.multi-grid.density-wall .device-conn-icon, .multi-grid.density-wall .tile-action-message,
.multi-grid.density-wall .multi-logs-area, .multi-grid.density-compact .multi-logs-area { display: none !important; }
.multi-grid.density-wall .multi-card-header, .multi-grid.density-compact .multi-card-header { gap: 5px; padding: 4px 6px; min-height: 28px; }
.multi-grid.density-wall .multi-card-header input[type='checkbox'] { width: 12px; height: 12px; margin: 0; }
.multi-grid.density-wall .multi-device-name { font-size: var(--ui-fs-2xs); }
.multi-grid.density-compact .multi-device-name { font-size: var(--ui-fs-xs); }
.multi-grid.density-wall .multi-stream-area { border-radius: 0 0 var(--ui-radius-lg) var(--ui-radius-lg); }
.multi-grid.density-wall .multi-stream-placeholder { padding: 8px; font-size: var(--ui-fs-2xs); line-height: 1.35; }
.multi-grid.density-compact .multi-card-keybar { justify-content: center; padding: 4px; }
.multi-grid.density-compact .multi-hw-keys { gap: 2px; flex-wrap: nowrap; }
.multi-grid.density-compact .hw-key-btn { min-width: 18px; height: 20px; padding: 0 3px; font-size: 10px; }
.multi-grid.density-compact .tile-action-message { font-size: var(--ui-fs-2xs); padding: 2px 6px; }
@media (max-width: 1600px) {
  .sync-banner__keys .hw-key-btn { min-width: 0; }
}
@media (max-width: 1366px) {
  .sync-banner { flex-wrap: wrap; }
  .sync-banner__text { flex-basis: 100%; }
}
/* ── Live grid toolbar: commands row + view row, never a stretched control ── */
.multi-header .ui-select { width: auto; flex: none; }
.multi-header .grid-view-options { flex-wrap: nowrap; }
.multi-header .grid-view-options .multi-search { width: clamp(160px, 14vw, 240px); }
.multi-header .multi-view-select:disabled { opacity: .5; cursor: not-allowed; }
/* ── Fit screen: wall sized by script so every shown phone fits on one screen ── */
.multi-grid.is-fit { gap: 6px; align-content: start; }
.multi-grid.is-fit .multi-stream-area { min-height: 0; }
.multi-grid.is-fit .multi-card-header { min-height: 24px; padding: 3px 5px; }
.multi-grid.density-wall .multi-device-name { font-family: var(--ui-mono); letter-spacing: .01em; }
/* Dense tiles: the short id always stays readable; state moves to a thin top edge */
.multi-grid.density-wall .multi-device-name { flex: 0 1 auto; min-width: 22px; }
.multi-grid.density-wall .multi-card-header .tile-state-dot { display: none; }
.multi-grid.density-wall .tile-quick { margin-left: auto; gap: 2px; }
.multi-grid.density-wall .tile-quick__btn { width: 17px; height: 17px; }
.multi-grid.density-wall .multi-card.state-live { box-shadow: inset 0 2px 0 var(--ui-accent); }
.multi-dashboard-pane:has(.multi-grid.is-fit) .dashboard-interaction-help > span { display: none; }
/* Header width is owned by the structured identity layout below, including tiny Fit cards. */
.multi-grid.density-wall .multi-card { container-type: inline-size; container-name: tile; }
/* ── Compact Live grid: slim two-row command bar, phones get the rest of the screen ── */
.phone-admin.is-live-grid { height: calc(100vh - var(--ui-topbar-h) - 18px); gap: 8px; }
.phone-admin.is-live-grid.is-grid-focus { height: calc(100vh - 10px); }
.phone-admin.is-live-grid .multi-grid { gap: 8px; padding-bottom: 6px; margin-top: 8px; }
.phone-admin.is-live-grid .multi-grid.is-fit { gap: 6px; }
.multi-header.is-compact .multi-header__row { gap: 6px; row-gap: 5px; padding: 5px 8px; }
.multi-header.is-compact .multi-header__meta { gap: 4px 12px; min-height: 26px; padding: 2px 8px 2px 10px; }
.multi-header.is-compact .multi-header__meta .ui-toolbar__group { gap: 6px; }
.multi-header.is-compact .ui-toolbar__group { gap: 5px; }
.multi-header.is-compact .ctrl-btn { height: 28px; padding: 0 10px; gap: 6px; font-size: var(--ui-fs-xs); }
.multi-header.is-compact .ctrl-btn--icon { width: 28px; padding: 0; justify-content: center; }
.multi-header.is-compact .ui-select--sm, .multi-header.is-compact .ui-input--sm { height: 28px; }
.multi-header.is-compact .ui-vdivider { height: 18px; margin: 0 3px; }
.multi-header.is-compact .grid-touch-toggle { font-size: var(--ui-fs-2xs); gap: 6px; }
.multi-header.is-compact .statusbar-link { height: 20px; padding: 0 6px; gap: 4px; font-size: var(--ui-fs-2xs); }
.multi-header.is-compact .device-count, .multi-header.is-compact .stream-intent-note { font-size: var(--ui-fs-2xs); white-space: nowrap; }
.multi-header.is-compact .stream-intent-note { gap: 6px; }
/* Start All shows the real state: Start All / Start N more / Starting x/y / Streaming x/y */
.start-all-btn { min-width: 128px; justify-content: center; font-variant-numeric: tabular-nums; }
.start-all-btn.is-all { background: var(--ui-accent-soft); border-color: var(--ui-accent-line); color: var(--ui-accent); }
.start-all-btn.is-all:hover { background: var(--ui-accent-soft); filter: brightness(1.15); }
.start-all-btn.is-starting { cursor: progress; opacity: .92; }
.grid-focus-btn.is-on { background: var(--ui-accent-soft); border-color: var(--ui-accent-line); color: var(--ui-accent); }
.statusbar-link.is-danger:not(:disabled) { color: #ff9b9b; }
/* Zero flex-basis: the label only uses spare room and truncates, so pressing a phone can never wrap the row and shift the grid mid-tap. */
.input-target { display: block; flex: 1 1 0; min-width: 0; max-width: max-content; overflow: hidden; text-overflow: ellipsis; color: var(--ui-accent); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); white-space: nowrap; }
.input-target svg { vertical-align: -2px; margin-right: 5px; }
.grid-help-tip { display: inline-grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; color: var(--ui-text-3); cursor: help; }
.grid-help-tip:hover, .grid-help-tip:focus-visible { color: var(--ui-text-1); background: var(--ui-surface-hover); outline: none; }
/* Sync banner: one slim line */
.phone-admin.is-live-grid .sync-banner { margin: 6px 0 0; padding: 4px 6px 4px 12px; gap: 10px; }
.phone-admin.is-live-grid .sync-banner__text { flex-direction: row; align-items: baseline; gap: 10px; }
.phone-admin.is-live-grid .sync-banner__text strong { font-size: var(--ui-fs-xs); white-space: nowrap; }
.phone-admin.is-live-grid .sync-banner .ctrl-btn { height: 26px; }
.phone-admin.is-live-grid .sync-banner__keys .hw-key-btn { height: 24px; }
/* Phone numbers */
.multi-device-name { align-items: center; }
.tile-number { flex: none; display: inline-grid; place-items: center; min-width: 20px; height: 18px; padding: 0 5px; border-radius: 5px; border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-1); font-family: var(--ui-mono); font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; letter-spacing: 0; }
.multi-card.is-active-device .tile-number { border-color: var(--ui-accent); background: var(--ui-accent); color: var(--ui-on-accent); }
.multi-grid.density-wall .tile-number { min-width: 17px; height: 15px; padding: 0 3px; border-radius: 4px; font-size: 10px; }
/* The number is the tile's identity; the short IP only shows when there is room for it (full id stays in the tooltip) */
@container tile (max-width: 150px) {
  .multi-grid.density-wall .multi-device-name .tile-number ~ .device-name-text { display: none; }
}
@media (max-width: 1366px) {
  .phone-admin.is-live-grid .sync-banner { flex-wrap: nowrap; }
  .phone-admin.is-live-grid .sync-banner__text { flex-basis: auto; }
  .phone-admin.is-live-grid .sync-banner__text span { display: none; }
}
/* Header responsiveness follows the header's own width, so the sidebar and the control desk count too */
.multi-header.is-compact { container-type: inline-size; container-name: gridhdr; }
.multi-header .lbl-short { display: none; }
@container gridhdr (max-width: 1420px) {
  .multi-header.is-compact .ctrl-btn--tool .ctrl-btn__label { display: none; }
  .multi-header.is-compact .ctrl-btn--tool { width: 28px; padding: 0; justify-content: center; }
  .multi-header.is-compact .grid-view-options .ui-select--sm { width: 112px; padding-right: 22px; text-overflow: ellipsis; }
  .multi-header.is-compact .sync-scope-select { width: 104px; padding-right: 22px; text-overflow: ellipsis; }
  .multi-header.is-compact .grid-view-options .multi-search { width: 150px; }
  .multi-header.is-compact .start-all-btn { min-width: 120px; padding: 0 9px; }
}
@container gridhdr (max-width: 1700px) {
  .multi-header.is-compact .multi-statusbar__spec { display: none; }
}
@container gridhdr (max-width: 1300px) {
  .multi-header.is-compact .lbl-full { display: none; }
  .multi-header.is-compact .lbl-short { display: inline; }
  .multi-header.is-compact .input-target { display: none; }
  .multi-header.is-compact .multi-header__row { gap: 5px; }
  .multi-header.is-compact .multi-header__meta { gap: 4px 10px; }
  .multi-header.is-compact .ui-vdivider { margin: 0 1px; }
}
@container gridhdr (max-width: 980px) {
  .multi-header.is-compact .ui-vdivider { display: none; }
  .multi-header.is-compact .grid-view-options .multi-search { width: 120px; }
  .multi-header.is-compact .grid-view-options .ui-select--sm { width: 100px; }
  .multi-header.is-compact .statusbar-link .ui-icon, .multi-header.is-compact .statusbar-link svg { display: none; }
}
/* Glass Hub: integrated phone bezel. One continuous outer frame, zero inner panel or divider.
   Identity is above the complete video, never an overlay. Selected number and Sync rim are separate states. */
.multi-grid .multi-card { --phone-serif: "Times New Roman", Georgia, serif; position: relative; container-type: inline-size; container-name: tile; min-width: 0; border: 1px solid #62728b; border-radius: 12px; background: var(--phone-surface, #07090c); box-shadow: 0 0 0 1px #151c27, 0 8px 18px -12px rgba(0, 0, 0, .7); transition: border-color .18s ease, box-shadow .18s ease; }
.multi-grid .multi-card:hover:not(.is-sync-target):not(.is-sync-failed) { border-color: #7888a1; }
.multi-grid .multi-stream-area { border-radius: 0 0 11px 11px; background: var(--phone-surface, #07090c); }
.multi-grid .multi-card > .multi-card-header.glass-hub { position: relative; z-index: 3; display: grid; grid-template-columns: 24px minmax(0, 1fr); grid-template-rows: 32px 16px; align-items: center; column-gap: 6px; row-gap: 4px; box-sizing: border-box; height: 60px; min-height: 0; margin: 0; padding: 4px 7px; border-radius: 0; border: 0; background-color: var(--phone-surface, #07090c); background-image: linear-gradient(180deg, var(--phone-cap-top, var(--phone-surface, #07090c)), var(--phone-surface, #07090c)); box-shadow: none; font-family: var(--phone-serif); }
.multi-grid .multi-card:hover > .multi-card-header.glass-hub { background-color: var(--phone-surface, #07090c); background-image: linear-gradient(180deg, var(--phone-cap-top, var(--phone-surface, #07090c)), var(--phone-surface, #07090c)); }
.multi-grid .multi-card-header.glass-hub.is-selected { background-color: var(--phone-surface, #07090c); background-image: linear-gradient(180deg, var(--phone-cap-top, var(--phone-surface, #07090c)), var(--phone-surface, #07090c)); }
/* Unboxed number selects the phone; underline denotes selection, tiny dot denotes stream state. */
.multi-grid .glass-hub .tile-select.hub-chip { position: relative; grid-row: 1; grid-column: 1; display: grid; place-items: center; flex: none; width: 24px; height: 26px; border-radius: 0; cursor: pointer; border: 0; background: transparent; box-shadow: none; }
.multi-grid .multi-card-header.glass-hub .tile-select input[type='checkbox'] { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; z-index: 1; }
.multi-grid .glass-hub .tile-select .tile-number { width: auto; min-width: 0; height: auto; padding: 0; border: 0; border-radius: 0; background: none; color: var(--phone-fg, #f4f7fb); font: 700 12px/1 var(--phone-serif); letter-spacing: 0; font-variant-numeric: tabular-nums; text-shadow: none; }
.multi-grid .glass-hub .tile-select.hub-chip.is-selected { background: transparent; border: 0; box-shadow: none; }
.multi-grid .glass-hub .tile-select.hub-chip.is-selected .tile-number { color: var(--phone-selected, #a1b7ff); text-shadow: none; text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 4px; }
.multi-grid .glass-hub .tile-select:focus-within { outline: 2px solid #8fd0ff; outline-offset: 2px; }
.hub-chip > .hub-dot { position: absolute; top: 1px; right: -1px; width: 5px; height: 5px; border-radius: 50%; border: 1px solid #07090c; background: #4b5869; }
.hub-chip.is-live > .hub-dot { background: #3ef0a6; box-shadow: 0 0 7px rgba(62, 240, 166, .9); }
.hub-chip.is-attention > .hub-dot { background: #f5bd55; box-shadow: 0 0 7px rgba(245, 189, 85, .75); }
/* Identity gets its own row: the number, model and full IP are never covered by status or action icons. */
.multi-grid .multi-card-header.glass-hub .multi-device-name.tile-id { grid-row: 1; grid-column: 2; display: flex; flex-direction: column; align-items: flex-start; justify-content: center; gap: 8px; flex: 1 1 auto; min-width: 0; max-width: none; height: 100%; padding: 0; border: 0; background: transparent; text-align: left; overflow: hidden; letter-spacing: 0; cursor: pointer; }
.multi-grid .glass-hub .tile-id .device-name-text { display: block; max-width: calc(100% - 46px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--phone-fg, #e4e9f1); font: 700 10.5px/1.15 var(--phone-serif); letter-spacing: 0; }
.multi-grid .glass-hub .tile-ip__addr { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--phone-muted, #a5b2c5); font: 500 10px/1.15 var(--phone-serif); font-variant-numeric: tabular-nums; letter-spacing: 0; }
.multi-grid .glass-hub .tile-ip__addr.is-proxy { color: var(--phone-proxy, #6ff0c4); }
.multi-grid .glass-hub .tile-ip__addr.is-proxy-unverified { color: var(--phone-warning, #f5c46b); }
.multi-grid .glass-hub .tile-id:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 1px; border-radius: 4px; }
/* Signal glyphs: no boxes; off = faint, on = the signal's own colour with a soft glow (Sync cyan, Proxy mint, Automation violet),
   amber = pending or unverified, red = failed. Always in the same place. */
.multi-grid .glass-hub .hub-signals { grid-row: 2; grid-column: 1 / -1; justify-self: end; align-self: center; position: static; transform: none; display: flex; align-items: center; gap: 3px; opacity: 1; pointer-events: auto; }
.hub-signal { --sig: #8fe0ff; --sig-glow: rgba(120, 210, 255, .8); position: relative; display: inline-grid; place-items: center; flex: none; width: 12px; height: 12px; color: var(--phone-idle, rgba(150, 172, 205, .3)); cursor: help; transition: color .18s ease, filter .18s ease; }
.hub-signal .ui-icon { width: 12px; height: 12px; }
.hub-signal--sync { --sig: var(--phone-sync, #5fe3ff); --sig-glow: rgba(95, 227, 255, .85); }
.hub-signal--proxy { --sig: var(--phone-proxy, #4ff0b4); --sig-glow: rgba(79, 240, 180, .8); }
.hub-signal--agent { --sig: var(--phone-agent, #b79cff); --sig-glow: rgba(183, 156, 255, .9); }
.hub-signal.is-on { color: var(--sig); filter: drop-shadow(0 0 4px var(--sig-glow)); }
.hub-signal.is-warn { color: var(--phone-warning, #f5c46b); filter: drop-shadow(0 0 3px rgba(245, 196, 107, .65)); }
.hub-signal.is-bad { color: var(--phone-error, #ff8a93); filter: drop-shadow(0 0 4px rgba(255, 110, 122, .85)); }
/* Swapped positions: power + menu at upper right; status glyphs in their own lower row. No hover swapping or overlap. */
.multi-grid .glass-hub .hub-actions { grid-row: 1; grid-column: 2; justify-self: end; align-self: start; position: relative; z-index: 4; display: flex; align-items: center; gap: 2px; padding: 0; opacity: 1; pointer-events: auto; }
.hub-power { display: inline-grid; place-items: center; flex: none; box-sizing: border-box; width: 18px; height: 18px; padding: 0; border-radius: 50%; color: #6ff2cc; background: radial-gradient(circle at 50% 35%, rgba(26, 72, 64, .96), rgba(6, 16, 24, .96)); border: 1.5px solid rgba(80, 235, 195, .8); box-shadow: 0 0 12px -2px rgba(70, 230, 190, .8), inset 0 0 6px rgba(70, 230, 190, .25); cursor: pointer; }
.multi-grid .glass-hub .hub-power .ui-icon, .multi-grid .glass-hub .tile-quick summary .ui-icon { width: 10px; height: 10px; }
.hub-power:hover:not(:disabled) { color: #fff; background: radial-gradient(circle at 50% 35%, rgba(34, 104, 90, .98), rgba(10, 34, 32, .98)); }
.hub-power:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 2px; }
.hub-power:disabled { cursor: default; }
.hub-power.is-rebooting, .hub-power.is-recovering, .hub-power.is-needs_usb { color: #f5c46b; border-color: rgba(245, 196, 107, .8); box-shadow: 0 0 10px -2px rgba(245, 196, 107, .7); }
.hub-power.is-rebooting .ui-icon { animation: reboot-spin 1.4s linear infinite; }
.multi-grid .glass-hub .tile-quick { position: relative; display: inline-flex; flex: none; margin: 0; }
.multi-grid .tile-quick summary { display: grid; place-items: center; box-sizing: border-box; width: 18px; height: 18px; list-style: none; color: #e2ecf8; border-radius: 50%; background: radial-gradient(circle at 50% 35%, rgba(30, 50, 84, .96), rgba(8, 14, 28, .96)); border: 1px solid rgba(140, 195, 255, .38); box-shadow: 0 4px 12px -4px rgba(0, 0, 0, .7); cursor: pointer; }
.multi-grid .tile-quick summary::-webkit-details-marker { display: none; }
.multi-grid .tile-quick summary:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 1px; }
.multi-grid .tile-quick__menu { position: absolute; top: 58px; right: -20px; box-sizing: border-box; width: min(132px, calc(100cqw - 12px)); min-width: 0; max-height: max(56px, calc(var(--phone-preview-height, 160px) - 10px)); overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; display: flex; flex-direction: column; gap: 2px; padding: 5px; background: linear-gradient(180deg, rgba(18, 30, 52, .97), rgba(9, 15, 28, .97)); border: 1px solid rgba(120, 186, 255, .26); border-radius: 11px; box-shadow: 0 14px 30px rgba(0, 0, 0, .6), 0 0 18px -8px rgba(80, 150, 255, .7), inset 0 1px 0 rgba(255, 255, 255, .06); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); z-index: 6; }
.multi-grid .glass-hub .tile-quick__btn { flex-shrink: 0; display: flex; align-items: center; justify-content: flex-start; gap: 7px; width: 100%; height: 27px; padding: 0 8px; border: 0; border-radius: 7px; background: transparent; color: #e2e8f0; font-size: 11.5px; }
.multi-grid .glass-hub .tile-quick__btn:hover { background: rgba(110, 180, 255, .12); }
.multi-grid .tile-quick__btn:focus-visible, .multi-grid .multi-device-name:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 1px; }
.multi-grid .multi-card:has(.tile-quick[open]) { z-index: 5; overflow: hidden; }
/* Synced phones get the cyan outer frame; failed members get red. Selection marks the plain number. */
.multi-grid .multi-card.is-sync-target, .multi-grid.density-wall .multi-card.is-sync-target { border-color: #3fd3dc; box-shadow: 0 0 0 1px rgba(63, 211, 220, .32), 0 0 22px -6px rgba(63, 211, 220, .7); }
.multi-grid .multi-card.is-sync-failed, .multi-grid.density-wall .multi-card.is-sync-failed { border-color: #ef8e94; box-shadow: 0 0 0 1px rgba(239, 142, 148, .4), 0 0 18px -8px rgba(239, 120, 128, .7); }
.multi-grid.density-wall .multi-card.state-live:not(.is-sync-target):not(.is-sync-failed) { box-shadow: 0 6px 16px -10px rgba(0, 0, 0, .6); }
/* Wider tiles: larger signal glyphs centred on the right edge. */
@container tile (min-width: 236px) {
  .multi-grid .glass-hub .hub-signals { gap: 7px; }
  .multi-grid .glass-hub .hub-signal { width: 16px; height: 16px; }
  .multi-grid .glass-hub .hub-signal .ui-icon { width: 15px; height: 15px; }
}
/* Compact bezel keeps model and complete IP on two lines with fixed signal positions. */
.multi-grid.hub-compact .multi-card > .multi-card-header.glass-hub { height: 54px; grid-template-columns: 20px minmax(0, 1fr); grid-template-rows: 32px 12px; column-gap: 5px; row-gap: 2px; padding: 4px 6px; border-radius: 0; }
.multi-grid.hub-compact .glass-hub .tile-select.hub-chip { width: 20px; height: 24px; border-radius: 0; }
.multi-grid.hub-compact .glass-hub .tile-select .tile-number { font-size: 11px; }
.multi-grid.hub-compact .hub-chip > .hub-dot { top: 1px; right: -1px; width: 4px; height: 4px; border-width: 1px; }
.multi-grid.hub-compact .glass-hub .tile-id .device-name-text { display: block; max-width: calc(100% - 46px); font-size: 9px; }
.multi-grid.hub-compact .glass-hub .tile-ip__addr { max-width: 100%; font-size: 9.5px; }
.multi-grid.hub-compact .glass-hub .hub-signals { position: static; transform: none; margin-left: 0; gap: 3px; }
.multi-grid.hub-compact .glass-hub .hub-signal { width: 11px; height: 11px; }
.multi-grid.hub-compact .glass-hub .hub-signal .ui-icon { width: 10px; height: 10px; }
.multi-grid.hub-compact .glass-hub .hub-actions { position: relative; padding: 0; gap: 2px; }
.multi-grid.hub-compact .glass-hub .hub-power, .multi-grid.hub-compact .glass-hub .tile-quick summary { width: 18px; height: 18px; }
.multi-grid.hub-compact .glass-hub .hub-power .ui-icon, .multi-grid.hub-compact .glass-hub .tile-quick summary .ui-icon { width: 10px; height: 10px; }
.multi-grid.hub-compact .glass-hub .tile-quick__menu { top: 52px; }
.multi-grid.hub-compact .glass-hub .tile-id { gap: 10px; }
/* Control desk Glass Hub: the full status bar for the phone being controlled. */
.control-hub { container-type: inline-size; flex-shrink: 0; display: flex; align-items: center; gap: 8px; margin: 8px 10px 2px; padding: 8px 10px; border-radius: 12px; border: 1px solid rgba(92, 176, 255, .55); background: linear-gradient(180deg, rgba(28, 60, 104, .62), rgba(9, 20, 38, .92)); box-shadow: 0 0 0 1px rgba(80, 170, 255, .16), 0 0 22px -8px rgba(64, 160, 255, .85), inset 0 1px 0 rgba(255, 255, 255, .1); }
.control-hub__id { display: flex; align-items: center; gap: 8px; flex: 1 1 auto; min-width: 0; }
.control-hub .hub-chip { position: relative; display: grid; place-items: center; flex: none; width: 38px; height: 38px; border-radius: 10px; border: 1px solid rgba(120, 192, 255, .6); background: linear-gradient(160deg, rgba(64, 130, 220, .4), rgba(10, 24, 46, .92)); box-shadow: inset 0 1px 0 rgba(255, 255, 255, .18), 0 0 12px -3px rgba(80, 170, 255, .85); }
.control-hub .hub-chip .tile-number { color: #f4f9ff; font: 700 17px/1 var(--ui-font); font-variant-numeric: tabular-nums; text-shadow: 0 0 10px rgba(120, 200, 255, .6); }
.control-hub .hub-chip > .hub-dot { width: 10px; height: 10px; top: -3px; right: -3px; border-width: 2px; }
.control-hub__name { display: grid; gap: 4px; min-width: 0; }
.control-hub__name strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #f3f7fc; font: 650 13px/1.1 var(--ui-font); }
.control-hub__name span { display: inline-flex; align-items: center; gap: 4px; color: #8ec8ff; font: 500 11px/1.1 var(--ui-font); font-variant-numeric: tabular-nums; white-space: nowrap; }
.control-hub__cell { display: flex; align-items: center; gap: 6px; flex: none; padding-left: 8px; border-left: 1px solid rgba(140, 190, 255, .16); cursor: help; }
.control-hub__icon { position: relative; display: grid; place-items: center; width: 26px; height: 26px; border-radius: 8px; color: rgba(150, 175, 205, .5); border: 1px solid rgba(120, 170, 230, .2); background: rgba(10, 22, 40, .85); }
.control-hub__cell.is-on .control-hub__icon { color: #8fe0ff; border-color: rgba(88, 196, 255, .75); background: linear-gradient(160deg, rgba(40, 110, 200, .55), rgba(10, 24, 46, .9)); box-shadow: 0 0 12px -3px rgba(72, 186, 255, .9); }
.control-hub__cell.is-warn .control-hub__icon { color: #f3c06a; border-color: rgba(243, 192, 106, .6); }
.control-hub__cell.is-bad .control-hub__icon { color: #ff9298; border-color: rgba(255, 120, 128, .7); }
.control-hub__cell.is-on .control-hub__icon::after, .control-hub__cell.is-warn .control-hub__icon::after, .control-hub__cell.is-bad .control-hub__icon::after { content: ''; position: absolute; top: -3px; right: -3px; width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid #0a1628; background: #37e3a0; box-shadow: 0 0 6px rgba(55, 227, 160, .9); }
.control-hub__cell.is-warn .control-hub__icon::after { background: #f3c06a; box-shadow: none; }
.control-hub__cell.is-bad .control-hub__icon::after { background: #ff6b75; }
.control-hub__text { display: grid; gap: 2px; min-width: 0; }
.control-hub__text b { color: #e9f1fb; font: 600 11px/1.1 var(--ui-font); white-space: nowrap; }
.control-hub__text em { max-width: 84px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #7f93ab; font: normal 500 10.5px/1.1 var(--ui-font); font-variant-numeric: tabular-nums; }
.control-hub__cell.is-on .control-hub__text em { color: #5fe8b4; }
.control-hub__cell.is-warn .control-hub__text em { color: #f3c06a; }
.control-hub__cell.is-bad .control-hub__text em { color: #ff9298; }
.control-hub__power { display: grid; place-items: center; flex: none; width: 36px; height: 36px; margin-left: 2px; padding: 0; border-radius: 50%; color: #72f0c9; background: radial-gradient(circle, rgba(10, 30, 34, .95), rgba(6, 16, 26, .95)); border: 2px solid rgba(70, 228, 186, .85); box-shadow: 0 0 14px -2px rgba(60, 228, 180, .85), inset 0 0 8px rgba(60, 228, 180, .22); cursor: pointer; }
.control-hub__power:hover:not(:disabled) { color: #fff; background: rgba(18, 64, 56, .95); }
.control-hub__power:focus-visible { outline: 2px solid #8fd0ff; outline-offset: 2px; }
.control-hub__power:disabled { cursor: default; color: #f3c06a; border-color: rgba(243, 192, 106, .8); box-shadow: 0 0 12px -2px rgba(243, 192, 106, .7); }
.control-hub__power.is-rebooting .ui-icon { animation: reboot-spin 1.4s linear infinite; }
@container (max-width: 480px) {
  .control-hub__text { display: none; }
  .control-hub__cell { padding-left: 7px; }
}
@media (prefers-reduced-motion: reduce) {
  .multi-grid .multi-card-header.glass-hub, .hub-signal, .multi-grid .glass-hub .hub-actions { transition: none; }
  .hub-power.is-rebooting .ui-icon, .control-hub__power.is-rebooting .ui-icon { animation: none; }
}
/* Sync All v2: instant-control count, failed phones flagged */
.sync-instant { display: inline-flex; align-items: center; gap: 3px; margin-left: 8px; padding: 1px 7px; border-radius: 999px; background: var(--ui-accent-soft); color: var(--ui-accent); font-size: var(--ui-fs-2xs); font-weight: 600; font-variant-numeric: tabular-nums; vertical-align: 1px; cursor: help; }
.sync-instant.is-partial { background: var(--ui-warn-soft); color: var(--ui-warn); }
.sync-instant.is-pending { background: var(--ui-surface-3); color: var(--ui-text-3); }

/* Selected sync and saved groups keep the existing compact command bar. */
.groups-toggle.is-on, .sync-selected-link { color: var(--ui-accent); }
.sync-members { max-width: 620px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Reboot: tile badge, menu item, status banner and confirmation dialog. */
.multi-grid .tile-quick__btn.is-danger:not(:disabled) { color: #ff9b9b; }
.reboot-banner { display: flex; align-items: center; gap: 10px; margin: 6px 0 0; padding: 5px 8px 5px 12px; min-height: 30px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); background: var(--ui-surface-2); font-size: var(--ui-fs-xs); color: var(--ui-text-2); }
.reboot-banner.is-active { border-color: rgba(242, 109, 109, .35); background: var(--ui-danger-soft); }
.reboot-banner__icon { display: inline-grid; place-items: center; color: var(--ui-danger); }
.reboot-banner.is-active .reboot-banner__icon :deep(svg) { animation: reboot-spin 1.4s linear infinite; }
.reboot-banner__title { color: var(--ui-text-1); font-weight: 650; white-space: nowrap; }
.reboot-banner__chips { display: flex; flex-wrap: wrap; gap: 6px; min-width: 0; flex: 1 1 auto; }
.reboot-chip { display: inline-flex; align-items: center; gap: 6px; height: 20px; padding: 0 8px 0 3px; border-radius: 999px; background: var(--ui-surface-3); color: var(--ui-text-2); white-space: nowrap; font-variant-numeric: tabular-nums; }
.reboot-chip b { display: inline-grid; place-items: center; min-width: 16px; height: 16px; padding: 0 4px; border-radius: 999px; background: rgba(255, 255, 255, .1); color: var(--ui-text-1); font-size: var(--ui-fs-2xs); }
.reboot-chip.is-needs_usb, .reboot-chip.is-recovering { color: var(--ui-warn); background: var(--ui-warn-soft); }
.reboot-chip.is-online { color: var(--ui-success); background: var(--ui-success-soft); }
.reboot-chip.is-failed { color: var(--ui-danger); background: var(--ui-danger-soft); }
.reboot-banner__hint { color: var(--ui-warn); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
@keyframes reboot-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .reboot-banner.is-active .reboot-banner__icon :deep(svg) { animation: none; } }
</style>
