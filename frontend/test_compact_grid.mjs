import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const fleet = readFileSync('src/views/PhoneAdminView.vue', 'utf8')
const shell = readFileSync('src/App.vue', 'utf8')
const nav = readFileSync('src/composables/shellNavigation.ts', 'utf8')
const focus = readFileSync('src/composables/gridFocus.ts', 'utf8')
const overview = readFileSync('src/components/StreamOverview.vue', 'utf8')

const checks = [
  // Compact header: the big monitor card is replaced by an inline strip on the Live grid only.
  [fleet.includes('<StreamOverview compact :visible="subTab === \'multi\'" />'), 'Live grid shows the compact monitor strip'],
  [fleet.includes("subTab !== 'single' && subTab !== 'proxies' && subTab !== 'multi'\" :visible=\"true\""), 'full monitor card stays on the other workspace views'],
  [overview.includes("compact?: boolean") && overview.includes('v-if="compact" class="overview-inline"') && overview.includes("fetch('/api/phone/android-h264/status'"), 'monitor keeps the same data source in compact mode'],
  [!fleet.includes('<div class="dashboard-interaction-help" role="note">'), 'full-width help line is gone'],
  [fleet.includes('class="grid-help-tip"') && fleet.includes('Direct controls enabled: tap or drag a phone'), 'help guidance moved to a tooltip'],
  [fleet.includes('class="multi-header is-compact"'), 'compact header'],
  // Start All reflects the real state.
  [fleet.includes('{{ startAllText }}') && fleet.includes(':aria-pressed="startAllState === \'all\'"'), 'Start All label is state-driven'],
  [fleet.includes("case 'all': return `Streaming ${androidLiveCount.value}/${total}`"), 'running fleet shows Streaming x/y'],
  [fleet.includes("case 'partial': return `Start ${total - requestedStreamCount.value} more`"), 'partially running fleet offers Start N more'],
  [fleet.includes("default: return 'Start All (H.264)'"), 'idle fleet keeps Start All (H.264)'],
  [fleet.includes('if (token === startAllToken) startingAll.value = false') && fleet.includes('startAllToken++; startingAll.value = false'), 'starting state always clears'],
  [fleet.includes('if (batch === startBatch) void reconcileStreams()'), 'Start All on a running fleet only reconnects stalled viewers'],
  // Phone numbers.
  [fleet.includes("api('/api/fleet/numbers')") && fleet.includes("'/api/fleet/numbers/assign'"), 'numbers load and auto-assign'],
  [fleet.includes("String(deviceNumbers[d.serial]).padStart(2, '0')"), 'tiles show their actual padded phone number'],
  [fleet.includes('{{ phoneTag(phone.serial) }}'), 'control desk picker is numbered'],
  [fleet.includes('@click="renumberPhone(controlDevice)"') && fleet.includes('@click="renumberAllByIp"'), 'numbers can be changed'],
  [fleet.includes('devices.value = sortByNumber(resp.devices || resp || [])'), 'grid is ordered by number'],
  [fleet.includes('/^#?(\\d{1,4})\\s*-\\s*#?(\\d{1,4})$/'), 'search accepts number ranges'],
  // Fit screen is the default layout (migrated once, then the saved choice wins).
  [fleet.includes("dashboardPreferences.layoutVersion === GRID_LAYOUT_VERSION") && fleet.includes(": 'fit')"), 'Fit screen is the default layout'],
  [fleet.includes('layoutVersion: GRID_LAYOUT_VERSION'), 'chosen layout is remembered'],
  [fleet.includes('<select v-if="!isFitLayout" v-model="gridColumns"'), 'columns selector hidden while Fit chooses columns'],
  // Focus mode.
  [focus.includes("'d3m0n.grid-focus.v1'") && !/localStorage|sessionStorage/.test(nav), 'focus preference lives outside memory-only navigation'],
  [shell.includes("'is-grid-focus': gridFocusActive") && shell.includes('.app-shell.is-grid-focus .app-sidebar'), 'focus mode hides the sidebar and top bar'],
  [fleet.includes("window.addEventListener('keydown', exitGridFocusOnEscape)") && fleet.includes("window.removeEventListener('keydown', exitGridFocusOnEscape)"), 'Esc handler is registered and removed'],
  [fleet.includes(".multi-card, .direct-control-overlay"), 'Esc inside a phone or the desk is not stolen'],
  [fleet.includes('@click="toggleGridFocus"'), 'focus button exists'],
  // Every original command is still reachable.
  ...[
    ["@click=\"startAllStreams('rtc')\"", 'Start All'], ['@click="stopAllStreams"', 'Stop All'], ['@click="stopSelectedStreams"', 'Stop selected'],
    ['@click="requestSyncAll"', 'Sync All'], ['aria-label="Sync All scope"', 'Sync scope'], ['@click="showWirelessModal = true"', 'WiFi Connect'],
    ['@click="loadAllHealth"', 'Health'], ['aria-label="Search fleet devices"', 'Search'], ['aria-label="Filter devices by stream state"', 'State filter'],
    ['aria-label="Phone preview size"', 'Layout'], ['aria-label="Grid stream quality"', 'Quality'], ['@click="selectVisibleTiles"', 'Select shown'],
    ['aria-label="Control phones directly in the grid"', 'Interact toggle'], ['aria-label="Show device logs in grid"', 'Logs toggle'],
    ['@click="confirmSyncAll"', 'Sync confirm'], ["startAllStreams('mjpeg')", 'iOS MJPEG start'],
  ].map(([needle, label]) => [fleet.includes(needle), `${label} still present`]),
]
for (const [ok, label] of checks) assert.ok(ok, label)
console.log(`COMPACT_GRID_TESTS=${checks.length} PASS`)
