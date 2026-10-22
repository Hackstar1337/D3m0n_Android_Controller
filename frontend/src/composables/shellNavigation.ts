import { ref } from 'vue'

/**
 * Shared, in-memory navigation state for the desktop shell.
 *
 * The phone workspace historically owned its own sub-tab ref. It now reads the
 * same ref object exported here, so the sidebar and in-page actions (for
 * example "Expand" on a phone tile) stay in sync without changing any of the
 * workspace's watchers or stream lifecycle logic.
 */
export type PhoneSubTab = 'single' | 'multi' | 'windows' | 'fleet' | 'ai' | 'proxies' | 'discover'

export function initialPhoneSubTab(search: string, prefersMulti: boolean): PhoneSubTab {
  const params = new URLSearchParams(search)
  if (params.has('discover')) return 'discover'
  if (params.has('proxies')) return 'proxies'
  if (params.has('fleet')) return 'fleet'
  if (params.has('ai') || params.has('fleet-ai')) return 'ai'
  if (params.has('windows')) return 'windows'
  if (params.has('multi') || prefersMulti) return 'multi'
  return 'single'
}

// Seeded by PhoneAdminView on first setup (it knows the persisted stream intent).
export const phoneSubTab = ref<PhoneSubTab>(initialPhoneSubTab(typeof location === 'undefined' ? '' : location.search, false))
export const phoneSubTabSeeded = { value: false }

export const PHONE_VIEW_META: Record<PhoneSubTab, { title: string; description: string; icon: string }> = {
  multi: { title: 'Live grid', description: 'Every phone streaming over H.264 with direct in-preview control.', icon: 'grid' },
  single: { title: 'Agent console', description: 'Drive one phone with the AI agent while watching its live screen.', icon: 'console' },
  windows: { title: 'Windows workspace', description: 'Arrange free-floating phone windows for side-by-side work.', icon: 'windows' },
  fleet: { title: 'Fleet controls', description: 'Groups, synchronized actions, files, APK installs and Unicode text.', icon: 'sliders' },
  ai: { title: 'Fleet AI', description: 'Owner-reviewed Mobilerun plans with bounded workers, checkpoints and receipts.', icon: 'bolt' },
  proxies: { title: 'Proxy manager', description: 'Per-device proxy profiles, readiness, tests and explicit routing jobs.', icon: 'globe' },
  discover: { title: 'Discover devices', description: 'Find USB and authorized wireless-debugging phones on your local network.', icon: 'search' },
}
