<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import BotView from '@/views/BotView.vue'
import TestsView from '@/views/TestsView.vue'
import SchedulerView from '@/views/SchedulerView.vue'
import PhoneAdminView from '@/views/PhoneAdminView.vue'
import DeviceWindowView from '@/views/DeviceWindowView.vue'
import SkillHubView from '@/views/SkillHubView.vue'
import SkillCreatorView from '@/views/SkillCreatorView.vue'
import ExplorerView from '@/views/ExplorerView.vue'
import ToolsHubView from '@/views/ToolsHubView.vue'
import DeviceHealthView from '@/views/DeviceHealthView.vue'
import EmulatorTab from '@/components/emulator/EmulatorTab.vue'
import BenchmarkTab from '@/components/benchmark/BenchmarkTab.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { dashboardAuth, lockDashboard } from '@/composables/dashboardAuth'
import { phoneSubTab, phoneSubTabSeeded, PHONE_VIEW_META, type PhoneSubTab } from '@/composables/shellNavigation'
import { gridFocus } from '@/composables/gridFocus'

const windowDevice = new URLSearchParams(window.location.search).get('device-window') || ''

type NavItem = { id: string; label: string; icon: string; phone?: PhoneSubTab; description?: string }
type NavGroup = { id: string; label: string; items: NavItem[] }

const coreGroups: NavGroup[] = [
  { id: 'operations', label: 'Operations', items: [
    { id: 'phone:multi', label: 'Live grid', icon: 'grid', phone: 'multi' },
    { id: 'phone:single', label: 'Agent console', icon: 'console', phone: 'single' },
    { id: 'phone:ai', label: 'Fleet AI', icon: 'bolt', phone: 'ai', description: 'Review bounded Mobilerun fleet plans and owner checkpoints.' },
    { id: 'phone:windows', label: 'Windows', icon: 'windows', phone: 'windows' },
    { id: 'phone:fleet', label: 'Fleet controls', icon: 'sliders', phone: 'fleet' },
    { id: 'phone:proxies', label: 'Proxy manager', icon: 'globe', phone: 'proxies' },
    { id: 'phone:discover', label: 'Discover devices', icon: 'search', phone: 'discover' },
  ] },
  { id: 'automation', label: 'Automation', items: [
    { id: 'scheduler', label: 'Scheduler', icon: 'clock', description: 'Queue, schedule and monitor automated phone jobs.' },
    { id: 'skills', label: 'Skill Hub', icon: 'box', description: 'Browse, run and manage installed agent skills.' },
    { id: 'creator', label: 'Skill Creator', icon: 'wrench', description: 'Record and author reusable phone automation skills.' },
    { id: 'explorer', label: 'Skill-Miner', icon: 'search', description: 'Explore apps to discover screens and automatable flows.' },
    { id: 'bot', label: 'Manual Run', icon: 'play', description: 'Launch a single bot or skill run on demand.' },
  ] },
  { id: 'system', label: 'System', items: [
    { id: 'tools', label: 'Tools', icon: 'tool', description: 'Agent tools and device utilities.' },
    { id: 'tests', label: 'Tests', icon: 'flask', description: 'Run and review automated test suites.' },
    { id: 'health', label: 'Device Health', icon: 'pulse', description: 'Battery, connectivity and service health per device.' },
    { id: 'emulators', label: 'Emulators', icon: 'monitor', description: 'Create and manage Android emulators.' },
    { id: 'benchmarks', label: 'Benchmarks', icon: 'chart', description: 'Model and agent benchmark results.' },
  ] },
]

const CORE_NAV_IDS = new Set(coreGroups.flatMap(group => group.items.map(item => item.id)))
function normalizePremiumTabs(value: unknown, reservedIds: ReadonlySet<string> = CORE_NAV_IDS): { id: string; label: string; component?: string }[] {
  if (!Array.isArray(value)) return []
  const seen = new Set(reservedIds)
  const result: { id: string; label: string; component?: string }[] = []
  for (const entry of value) {
    if (!entry || typeof entry !== 'object') continue
    const item = entry as Record<string, unknown>
    const id = typeof item.id === 'string' ? item.id.trim() : ''
    const label = typeof item.label === 'string' ? item.label.trim() : ''
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(id) || !label || label.length > 80 || /[\u0000-\u001f\u007f]/.test(label) || seen.has(id)) continue
    seen.add(id)
    const component = typeof item.component === 'string' && item.component.trim() ? item.component.trim() : undefined
    result.push(component ? { id, label, component } : { id, label })
  }
  return result
}

const premiumTabs = ref<{ id: string; label: string; component?: string }[]>([])
const navGroups = computed<NavGroup[]>(() => premiumTabs.value.length
  ? [...coreGroups, { id: 'extensions', label: 'Extensions', items: premiumTabs.value.map(tab => ({ id: tab.id, label: tab.label, icon: 'layers', description: 'Premium extension' })) }]
  : coreGroups)

const activeTab = ref('phone')
const restarting = ref(false)

const isPhone = computed(() => activeTab.value === 'phone')
// Live grid gets edge-to-edge space; focus mode also hides the sidebar and top bar.
const isLiveGrid = computed(() => isPhone.value && phoneSubTab.value === 'multi')
const gridFocusActive = computed(() => gridFocus.value && isLiveGrid.value)
const activeItem = computed<NavItem | undefined>(() => {
  for (const group of navGroups.value) for (const item of group.items) if (isActive(item)) return item
  return undefined
})
const activeGroup = computed(() => navGroups.value.find(group => group.items.some(item => isActive(item)))?.label || 'Operations')
const pageTitle = computed(() => isPhone.value ? PHONE_VIEW_META[phoneSubTab.value].title : activeItem.value?.label || 'Dashboard')
const pageDescription = computed(() => isPhone.value ? PHONE_VIEW_META[phoneSubTab.value].description : activeItem.value?.description || '')

function isActive(item: NavItem) {
  return item.phone ? activeTab.value === 'phone' && phoneSubTab.value === item.phone : activeTab.value === item.id
}
function navigate(item: NavItem) {
  if (item.phone) {
    phoneSubTabSeeded.value = true
    phoneSubTab.value = item.phone
    activeTab.value = 'phone'
  } else activeTab.value = item.id
}

// Sidebar collapse preference (UI preference only; never sensitive data).
const SHELL_KEY = 'd3m0n.shell.v1'
function readCollapsed(): boolean {
  try {
    const saved = JSON.parse(localStorage.getItem(SHELL_KEY) || 'null')
    if (saved && typeof saved.collapsed === 'boolean') return saved.collapsed
  } catch {}
  return window.innerWidth < 1280
}
const collapsed = ref(readCollapsed())
watch(collapsed, value => { try { localStorage.setItem(SHELL_KEY, JSON.stringify({ collapsed: value })) } catch {} })

// Status — polls device state for the sidebar/top-bar indicator
const ghostState = ref<'disconnected' | 'idle' | 'working' | 'error'>('disconnected')
const deviceCount = ref(0)
const GHOST_IMAGES: Record<string, string> = {
  disconnected: '/mascot/47-sleeping.png',
  idle: '/mascot/01-front-view.png',
  working: '/mascot/05-focused.png',
  error: '/mascot/49-dead.png',
}
const GHOST_LABELS: Record<string, string> = { disconnected: 'No devices', idle: 'Ready', working: 'Running jobs', error: 'Backend error' }
let ghostTimer: number | null = null
async function pollGhostState() {
  try {
    const resp = await fetch('/api/phone/devices')
    if (!resp.ok) { ghostState.value = 'error'; return }
    const data = await resp.json()
    const devs = data.devices || data || []
    deviceCount.value = Array.isArray(devs) ? devs.length : 0
    if (!devs.length) { ghostState.value = 'disconnected'; return }
    try {
      const qr = await fetch('/api/scheduler/queue')
      if (qr.ok) {
        const qd = await qr.json()
        const running = (qd.queue || []).some((j: any) => j.status === 'running')
        ghostState.value = running ? 'working' : 'idle'
        return
      }
    } catch {}
    ghostState.value = 'idle'
  } catch { ghostState.value = 'disconnected' }
}
const publicView = new URLSearchParams(window.location.search).has('public')

async function fetchFeatures() {
  if (publicView) return
  try {
    const resp = await fetch('/api/features')
    if (resp.ok) {
      const data = await resp.json()
      premiumTabs.value = normalizePremiumTabs(data?.premium_tabs)
    }
  } catch {}
}

onMounted(() => {
  if (windowDevice) return
  pollGhostState()
  fetchFeatures()
  ghostTimer = window.setInterval(pollGhostState, 10000)
})
onUnmounted(() => { if (ghostTimer) clearInterval(ghostTimer) })

async function restartServer() {
  if (!confirm('Restart the backend server?')) return
  restarting.value = true
  try {
    await fetch('/api/server/restart', { method: 'POST' })
  } catch {}
  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 1000))
    try {
      const resp = await fetch('/api/stats')
      if (resp.ok) { restarting.value = false; return }
    } catch {}
  }
  restarting.value = false
}
</script>

<template>
  <DeviceWindowView v-if="windowDevice" :device-serial="windowDevice" />
  <div v-else class="app-shell" :class="{ 'is-collapsed': collapsed, 'is-grid-focus': gridFocusActive }">
    <aside class="app-sidebar" aria-label="Primary navigation">
      <div class="brand">
        <img :src="GHOST_IMAGES[ghostState]" alt="" class="brand-mark" />
        <div class="brand-text">
          <strong>D3m0n</strong>
          <span>in the droid</span>
        </div>
      </div>

      <nav class="nav">
        <div v-for="group in navGroups" :key="group.id" class="nav-group">
          <div class="nav-group__label">{{ group.label }}</div>
          <button
            v-for="item in group.items"
            :key="item.id"
            type="button"
            class="nav-item"
            :class="{ 'is-active': isActive(item) }"
            :aria-current="isActive(item) ? 'page' : undefined"
            :title="collapsed ? item.label : undefined"
            @click="navigate(item)"
          >
            <UiIcon :name="item.icon" :size="17" />
            <span class="nav-item__label">{{ item.label }}</span>
          </button>
        </div>
      </nav>

      <div class="sidebar-footer">
        <a class="nav-item" href="http://localhost:4321" target="_blank" rel="noopener" :title="collapsed ? 'Docs' : undefined">
          <UiIcon name="book" :size="17" /><span class="nav-item__label">Docs</span>
        </a>
        <button v-if="!dashboardAuth.local" type="button" class="nav-item nav-item--lock" :title="collapsed ? 'Lock dashboard' : 'Lock this browser; the password is needed to unlock again'" @click="lockDashboard">
          <UiIcon name="lock" :size="17" /><span class="nav-item__label">Lock dashboard</span>
        </button>
        <button type="button" class="nav-item" :aria-pressed="collapsed" :title="collapsed ? 'Expand sidebar' : undefined" @click="collapsed = !collapsed">
          <UiIcon :name="collapsed ? 'chevronRight' : 'chevronLeft'" :size="17" /><span class="nav-item__label">Collapse sidebar</span>
        </button>
      </div>
    </aside>

    <div class="app-main">
      <header class="app-topbar">
        <div class="topbar-title">
          <div class="crumbs"><span>{{ activeGroup }}</span><UiIcon name="chevronRight" :size="12" /></div>
          <h1>{{ pageTitle }}</h1>
          <span v-if="pageDescription" class="topbar-desc">{{ pageDescription }}</span>
        </div>
        <div class="topbar-actions">
          <div id="d3m0n-topbar-status" class="topbar-status" aria-live="polite"></div>
          <span class="fleet-chip" :class="'is-' + ghostState" :title="'Server status: ' + GHOST_LABELS[ghostState]">
            <span class="ui-dot"></span>
            <span>{{ GHOST_LABELS[ghostState] }}</span>
            <span class="fleet-chip__sep"></span>
            <UiIcon name="phone" :size="13" />
            <span>{{ deviceCount }}</span>
          </span>
          <button type="button" class="ui-btn ui-btn--sm" :disabled="restarting" @click="restartServer">
            <UiIcon name="refresh" :size="13" />{{ restarting ? 'Restarting…' : 'Restart server' }}
          </button>
        </div>
      </header>

      <main class="app-content" :class="{ 'legacy-view': !isPhone, 'is-phone': isPhone, 'is-live-grid': isLiveGrid }">
        <PhoneAdminView v-if="activeTab === 'phone'" />
        <SchedulerView v-else-if="activeTab === 'scheduler'" />
        <SkillHubView v-else-if="activeTab === 'skills'" />
        <SkillCreatorView v-else-if="activeTab === 'creator'" />
        <ExplorerView v-else-if="activeTab === 'explorer'" />
        <ToolsHubView v-else-if="activeTab === 'tools'" />
        <BotView v-else-if="activeTab === 'bot'" />
        <TestsView v-else-if="activeTab === 'tests'" />
        <DeviceHealthView v-else-if="activeTab === 'health'" />
        <EmulatorTab v-else-if="activeTab === 'emulators'" />
        <BenchmarkTab v-else-if="activeTab === 'benchmarks'" />
        <!-- Premium tabs: rendered as iframes from premium frontend server -->
        <iframe
          v-else-if="premiumTabs.some(t => t.id === activeTab)"
          :src="`http://localhost:6176/#/${activeTab}`"
          class="premium-frame"
        />
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-shell {
  display: grid;
  grid-template-columns: var(--ui-sidebar-w) minmax(0, 1fr);
  min-height: 100vh;
  background: var(--ui-bg);
}
.app-shell.is-collapsed { grid-template-columns: var(--ui-sidebar-w-collapsed) minmax(0, 1fr); }

/* Sidebar */
.app-sidebar {
  position: sticky; top: 0; height: 100vh;
  display: flex; flex-direction: column; min-width: 0;
  background: var(--ui-bg-elevated);
  border-right: 1px solid var(--ui-border);
  z-index: 40;
}
.brand { display: flex; align-items: center; gap: 10px; height: var(--ui-topbar-h); padding: 0 14px; border-bottom: 1px solid var(--ui-border); flex: none; }
.brand-mark { width: 30px; height: 30px; object-fit: contain; flex: none; }
.brand-text { display: grid; line-height: 1.1; min-width: 0; }
.brand-text strong { font-size: 14px; font-weight: 700; letter-spacing: 0.01em; color: var(--ui-text-1); }
.brand-text span { font-size: 10.5px; color: var(--ui-text-3); letter-spacing: 0.04em; }

.nav { flex: 1 1 auto; overflow-y: auto; padding: 10px 10px 12px; display: grid; align-content: start; gap: 14px; }
.nav-group { display: grid; gap: 2px; }
.nav-group__label { padding: 4px 10px 6px; font-size: 10px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: var(--ui-text-4); white-space: nowrap; overflow: hidden; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 10px; width: 100%;
  height: 34px; padding: 0 10px; border: 0; border-radius: var(--ui-radius-sm);
  background: transparent; color: var(--ui-text-3); text-decoration: none;
  font-size: var(--ui-fs-sm); font-weight: 550; text-align: left; cursor: pointer;
  transition: background-color 0.12s var(--ui-ease), color 0.12s var(--ui-ease);
}
.nav-item:hover { background: var(--ui-surface-2); color: var(--ui-text-1); }
.nav-item.is-active { background: var(--ui-surface-3); color: var(--ui-text-1); }
.nav-item.is-active::before { content: ''; position: absolute; left: -10px; top: 8px; bottom: 8px; width: 3px; border-radius: 0 3px 3px 0; background: var(--ui-accent); }
.nav-item.is-active :deep(.ui-icon) { color: var(--ui-accent); }
.nav-item__label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sidebar-footer { flex: none; display: grid; gap: 2px; padding: 8px 10px 12px; border-top: 1px solid var(--ui-border); }

.app-shell.is-collapsed .brand { justify-content: center; padding: 0; }
.app-shell.is-collapsed .brand-text,
.app-shell.is-collapsed .nav-item__label { display: none; }
.app-shell.is-collapsed .nav-group__label { visibility: hidden; height: 6px; padding: 0; }
.app-shell.is-collapsed .nav-item { justify-content: center; padding: 0; }
.app-shell.is-collapsed .nav, .app-shell.is-collapsed .sidebar-footer { padding-left: 8px; padding-right: 8px; }
.app-shell.is-collapsed .nav-item.is-active::before { left: -8px; }

/* Main column */
.app-main { min-width: 0; display: flex; flex-direction: column; }
.app-topbar {
  position: sticky; top: 0; z-index: 30;
  display: flex; align-items: center; gap: 16px;
  height: var(--ui-topbar-h); padding: 0 var(--ui-content-pad-x);
  background: rgba(10, 12, 15, 0.92);
  backdrop-filter: saturate(140%) blur(8px);
  border-bottom: 1px solid var(--ui-border);
}
.topbar-title { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 auto; }
.crumbs { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-text-4); font-size: var(--ui-fs-xs); font-weight: 600; white-space: nowrap; }
.topbar-title h1 { margin: 0; font-size: var(--ui-fs-lg); font-weight: 650; color: var(--ui-text-1); white-space: nowrap; }
.topbar-desc { min-width: 0; margin-left: 6px; padding-left: 12px; border-left: 1px solid var(--ui-border-strong); color: var(--ui-text-3); font-size: var(--ui-fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topbar-actions { display: flex; align-items: center; gap: 10px; flex: none; }
.topbar-status { display: flex; align-items: center; max-width: 340px; min-width: 0; }
.topbar-status:empty { display: none; }
.topbar-status :deep(.status-text) { display: inline-block; max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.topbar-status :deep(.status-text:empty) { display: none; }
.fleet-chip { display: inline-flex; align-items: center; gap: 7px; height: 26px; padding: 0 10px; border-radius: 999px; border: 1px solid var(--ui-border-strong); background: var(--ui-surface-1); color: var(--ui-text-2); font-size: var(--ui-fs-xs); font-weight: 600; white-space: nowrap; }
.fleet-chip .ui-dot { color: var(--ui-text-4); }
.fleet-chip.is-idle .ui-dot { color: var(--ui-success); box-shadow: 0 0 0 3px rgba(70, 207, 139, 0.15); }
.fleet-chip.is-working .ui-dot { color: var(--ui-info); box-shadow: 0 0 0 3px rgba(103, 170, 255, 0.15); }
.fleet-chip.is-error .ui-dot { color: var(--ui-danger); }
.fleet-chip__sep { width: 1px; height: 12px; background: var(--ui-border-strong); }

.app-content { flex: 1 1 auto; min-width: 0; padding: 18px var(--ui-content-pad-x) 28px; }
.app-content.is-live-grid { padding: 10px 12px 8px; }
.app-shell.is-grid-focus { grid-template-columns: minmax(0, 1fr); }
.app-shell.is-grid-focus .app-sidebar,
.app-shell.is-grid-focus .app-topbar { display: none; }
.app-shell.is-grid-focus .app-content.is-live-grid { padding: 6px 8px 4px; }
.premium-frame { width: 100%; border: none; min-height: calc(100vh - 120px); border-radius: var(--ui-radius-lg); background: var(--ui-surface-1); }

@media (max-width: 1440px) {
  .topbar-desc { display: none; }
}
</style>
