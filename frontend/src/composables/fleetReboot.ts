/**
 * Phone reboot flow for the Live Grid.
 *
 * Rebooting drops wireless ADB on these Android 9 phones; only phones attached to
 * the Ubuntu host by USB come back automatically.  The dialog therefore runs a
 * server preflight first and, when any chosen phone lacks USB, requires an
 * explicit acknowledgement before the reboot request is sent.
 */
import { computed, ref } from 'vue'

export type RebootState = 'rebooting' | 'recovering' | 'needs_usb' | 'online' | 'failed'
export interface RebootRecord {
  state: RebootState
  message: string
  started_at: number
  elapsed_s: number
  usb_connected: boolean
  back_after_s?: number
}
export interface RebootCheck { online: boolean; usb_connected: boolean; agent_busy: boolean; rebooting: boolean }
export interface RebootDialog {
  devices: string[]
  checks: Record<string, RebootCheck> | null
  loading: boolean
  busy: boolean
  error: string
  acknowledged: boolean
}
export interface RebootPlan { eligible: string[]; withUsb: string[]; withoutUsb: string[]; busy: string[]; offline: string[]; already: string[] }
type ApiFn = <T = any>(path: string, opts?: RequestInit) => Promise<T>

export const ACTIVE_REBOOT_STATES: ReadonlySet<string> = new Set(['rebooting', 'recovering', 'needs_usb'])

export function isRebootActive(record?: { state: string } | null): boolean {
  return !!record && ACTIVE_REBOOT_STATES.has(record.state)
}

/** Same skip order as the server: already rebooting, offline, then Agent task running. */
export function rebootPlan(devices: string[], checks: Record<string, RebootCheck> | null): RebootPlan {
  const plan: RebootPlan = { eligible: [], withUsb: [], withoutUsb: [], busy: [], offline: [], already: [] }
  if (!checks) return plan
  for (const serial of devices) {
    const check = checks[serial]
    if (check?.rebooting) plan.already.push(serial)
    else if (!check || !check.online) plan.offline.push(serial)
    else if (check.agent_busy) plan.busy.push(serial)
    else {
      plan.eligible.push(serial)
      ;(check.usb_connected ? plan.withUsb : plan.withoutUsb).push(serial)
    }
  }
  return plan
}

export function canConfirmReboot(dialog: RebootDialog | null): boolean {
  if (!dialog || dialog.loading || dialog.busy || !dialog.checks) return false
  const plan = rebootPlan(dialog.devices, dialog.checks)
  return plan.eligible.length > 0 && (plan.withoutUsb.length === 0 || dialog.acknowledged)
}

export function rebootChipText(record: RebootRecord): string {
  switch (record.state) {
    case 'rebooting': return `Restarting · ${Math.round(record.elapsed_s)} s`
    case 'recovering': return 'Restoring via USB'
    case 'needs_usb': return 'Plug in USB'
    case 'online': return record.back_after_s != null ? `Back in ${record.back_after_s} s` : 'Back online'
    default: return 'Failed'
  }
}

export function apiErrorText(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error || '')
  const match = raw.match(/^API (\d+): ([\s\S]*)$/)
  if (!match) return raw.slice(0, 160) || 'Request failed'
  try {
    const detail = JSON.parse(match[2] ?? '').detail
    if (typeof detail === 'string') return detail.slice(0, 160)
  } catch { /* plain-text body */ }
  return `Request failed (${match[1]})`
}

export function useFleetReboot(apiFn: ApiFn, options: { pollMs?: number; onBack?: () => void } = {}) {
  const status = ref<Record<string, RebootRecord>>({})
  const dialog = ref<RebootDialog | null>(null)
  const notice = ref('')
  const dismissed = ref<string[]>([])
  const pollMs = options.pollMs ?? 3000
  let timer: ReturnType<typeof setTimeout> | null = null
  let disposed = false
  let token = 0

  const entries = computed(() => Object.entries(status.value)
    .filter(([serial, record]) => !dismissed.value.includes(serial + '@' + record.started_at))
    .sort((a, b) => a[1].started_at - b[1].started_at))
  const activeCount = computed(() => entries.value.filter(([, record]) => isRebootActive(record)).length)

  function schedule(ms: number) {
    if (disposed) return
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { timer = null; void refresh() }, ms)
  }

  async function refresh() {
    if (disposed) return
    try {
      const previous = status.value
      const result = await apiFn<{ devices?: Record<string, RebootRecord>; active?: number }>('/api/phone/reboot/status')
      if (disposed) return
      const next = result.devices || {}
      status.value = next
      const cameBack = Object.entries(next).some(([serial, record]) => record.state === 'online' && previous[serial] && previous[serial].state !== 'online')
      if (cameBack) options.onBack?.()
      if ((result.active || 0) > 0) schedule(pollMs)
    } catch {
      schedule(pollMs * 3)
    }
  }

  async function open(devices: string[]) {
    const list = [...new Set(devices.filter(Boolean))]
    if (!list.length) return
    const mine = ++token
    dialog.value = { devices: list, checks: null, loading: true, busy: false, error: '', acknowledged: false }
    try {
      const result = await apiFn<{ devices: Record<string, RebootCheck> }>('/api/phone/reboot/preflight', { method: 'POST', body: JSON.stringify({ devices: list }) })
      if (dialog.value && mine === token) dialog.value = { ...dialog.value, checks: result.devices || {}, loading: false }
    } catch (error) {
      if (dialog.value && mine === token) dialog.value = { ...dialog.value, loading: false, error: apiErrorText(error) }
    }
  }

  function acknowledge(value: boolean) {
    if (dialog.value) dialog.value = { ...dialog.value, acknowledged: value }
  }

  function cancel() {
    token++
    dialog.value = null
  }

  async function confirm() {
    const current = dialog.value
    if (!current || !canConfirmReboot(current)) return
    const plan = rebootPlan(current.devices, current.checks)
    const mine = ++token
    dialog.value = { ...current, busy: true, error: '' }
    try {
      const result = await apiFn<{ started: string[]; skipped: { device: string; reason: string }[]; failed: { device: string }[] }>('/api/phone/reboot', {
        method: 'POST',
        body: JSON.stringify({ devices: plan.eligible, confirm: true }),
      })
      if (mine !== token) return
      const parts = [`${result.started.length} rebooting`]
      if (result.skipped.length) parts.push(`${result.skipped.length} skipped`)
      if (result.failed.length) parts.push(`${result.failed.length} failed`)
      notice.value = 'Reboot: ' + parts.join(' · ')
      dialog.value = null
      await refresh()
    } catch (error) {
      if (dialog.value && mine === token) dialog.value = { ...current, busy: false, error: apiErrorText(error) }
    }
  }

  function dismissFinished() {
    dismissed.value = [...dismissed.value, ...entries.value.filter(([, r]) => !isRebootActive(r)).map(([s, r]) => s + '@' + r.started_at)]
    notice.value = ''
  }

  function dispose() {
    disposed = true
    token++
    if (timer) clearTimeout(timer)
    timer = null
  }

  return { status, dialog, notice, entries, activeCount, open, acknowledge, cancel, confirm, refresh, dismissFinished, dispose }
}
