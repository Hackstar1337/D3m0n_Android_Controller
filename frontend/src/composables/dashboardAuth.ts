// Dashboard-wide unlock state.
//
// The server owns authentication: a password unlock sets an HttpOnly session
// cookie that the browser sends automatically with every same-origin request
// and WebSocket. Nothing secret is kept here or in any browser storage; the
// password is sent once to /api/auth/unlock and immediately discarded.
import { reactive } from 'vue'

export type DashboardAuthPhase = 'checking' | 'locked' | 'unlocked' | 'unconfigured' | 'offline'

export const dashboardAuth = reactive({
  phase: 'checking' as DashboardAuthPhase,
  /** True for direct local access on the phone host (always trusted, no lock). */
  local: false,
  busy: false,
  error: '',
  notice: '',
  attemptsLeft: null as number | null,
  retryUntil: 0,
  sessionDays: 30,
})

const nativeFetch: typeof window.fetch = window.fetch.bind(window)
let guardInstalled = false

function requestPath(input: RequestInfo | URL): string {
  try {
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const url = new URL(raw, window.location.href)
    return url.origin === window.location.origin ? url.pathname : ''
  } catch {
    return ''
  }
}

/** Show the lock screen if any same-origin API call reports that the session ended. */
export function installDashboardAuthGuard(): void {
  if (guardInstalled) return
  guardInstalled = true
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const response = await nativeFetch(input, init)
    if (response.status === 401 && response.headers.get('x-d3m0n-auth') === 'locked') {
      const path = requestPath(input)
      if (path.startsWith('/api/') && !path.startsWith('/api/auth/') && dashboardAuth.phase === 'unlocked' && !dashboardAuth.local) {
        dashboardAuth.phase = 'locked'
        dashboardAuth.notice = 'Your session ended. Enter the password to unlock the dashboard again.'
      }
    }
    return response
  }
}

export async function refreshDashboardAuth(): Promise<void> {
  try {
    const response = await nativeFetch('/api/auth/status', { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) throw new Error(String(response.status))
    const data = await response.json() as { configured?: boolean; unlocked?: boolean; local?: boolean; session_days?: number }
    dashboardAuth.local = data.local === true
    if (typeof data.session_days === 'number' && data.session_days > 0) dashboardAuth.sessionDays = data.session_days
    dashboardAuth.phase = data.unlocked ? 'unlocked' : data.configured === false ? 'unconfigured' : 'locked'
  } catch {
    dashboardAuth.phase = 'offline'
  }
}

export async function unlockDashboard(password: string): Promise<boolean> {
  if (dashboardAuth.busy) return false
  dashboardAuth.error = ''
  if (!password) {
    dashboardAuth.error = 'Enter the dashboard password.'
    return false
  }
  dashboardAuth.busy = true
  try {
    const response = await nativeFetch('/api/auth/unlock', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    let data: Record<string, unknown> = {}
    try { data = await response.json() as Record<string, unknown> } catch { data = {} }
    if (response.ok) {
      dashboardAuth.attemptsLeft = null
      dashboardAuth.retryUntil = 0
      dashboardAuth.notice = ''
      dashboardAuth.phase = 'unlocked'
      return true
    }
    if (response.status === 429) {
      const wait = Number(data.retry_after ?? response.headers.get('retry-after') ?? 60)
      dashboardAuth.retryUntil = Date.now() + Math.max(1, wait) * 1000
      dashboardAuth.error = `Too many incorrect attempts. Try again in ${Math.max(1, Math.ceil(wait / 60))} min.`
    } else if (response.status === 401) {
      const left = typeof data.attempts_left === 'number' ? data.attempts_left : null
      dashboardAuth.attemptsLeft = left
      dashboardAuth.error = left !== null && left <= 3 ? `Incorrect password. ${left} attempt${left === 1 ? '' : 's'} left before a temporary lockout.` : 'Incorrect password.'
    } else if (response.status === 503) {
      dashboardAuth.phase = 'unconfigured'
    } else {
      dashboardAuth.error = 'The dashboard could not verify the password. Try again.'
    }
    return false
  } catch {
    dashboardAuth.error = 'The dashboard server is not reachable. Check the connection and try again.'
    return false
  } finally {
    dashboardAuth.busy = false
  }
}

export async function lockDashboard(): Promise<void> {
  try {
    await nativeFetch('/api/auth/lock', { method: 'POST', credentials: 'same-origin', cache: 'no-store' })
  } catch {
    /* the cookie is HttpOnly; if the server is unreachable we still hide the UI */
  }
  if (dashboardAuth.local) return
  dashboardAuth.notice = 'Dashboard locked.'
  dashboardAuth.error = ''
  dashboardAuth.phase = 'locked'
}
