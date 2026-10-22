// Memory-only Proxy Manager session.
//
// Lives at module scope so switching dashboard sections no longer unmounts the
// device list, editors or job tracking. Access is governed by the dashboard
// unlock (server session cookie); there is no separate proxy key. Nothing here
// is ever written to any browser storage: a reload or locking the dashboard
// clears everything, including unsaved credential drafts.
import { reactive, ref, watch } from 'vue'
import { dashboardAuth } from './dashboardAuth'

export type ProxyType = 'http' | 'https' | 'socks5'
export type DesiredMode = 'normal' | 'proxy'
export type AppliedMode = 'normal' | 'proxy' | 'unknown'
export type JobAction = 'test' | 'apply' | 'disable' | 'prepare'
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed'
export type ClientStatus = 'ready' | 'client_missing' | 'other_vpn' | 'unknown' | 'offline'

export type PublicProfile = {
  serial: string
  label: string
  proxy_type: ProxyType
  host: string
  port: number
  username_set: boolean
  password_set: boolean
  desired_mode: DesiredMode
  applied_mode: AppliedMode
  last_result: Record<string, unknown> | null
  updated_at: string
}

export type Device = {
  serial: string
  label: string
  online: boolean
  profile: PublicProfile | null
}

export type ClientState = { serial: string; status: ClientStatus; message: string; vpn_consent?: boolean | null }

export type EditorState = {
  label: string
  proxy_type: ProxyType
  host: string
  port: string
  username: string
  password: string
  clearUsername: boolean
  clearPassword: boolean
  busy: boolean
  error: string
  savedMessage: string
  hint: string
}

export type JobResult = {
  serial: string
  ok: boolean
  status: string
  message: string
  exit_ip?: string
  verified_device?: boolean
  installed_now?: boolean
  version?: string
}

export type Job = {
  id: string
  action: JobAction
  status: JobStatus
  devices: string[]
  results: JobResult[]
  created_at: string
}

export const proxySession = {
  devices: ref<Device[]>([]),
  jobs: ref<Job[]>([]),
  clients: ref<Record<string, ClientState>>({}),
  clientsCheckedAt: ref(''),
  selected: ref<Set<string>>(new Set()),
  editors: reactive<Record<string, EditorState>>({}),
  activeSerial: ref(''),
}

export function clearProxySession() {
  const s = proxySession
  s.devices.value = []
  s.jobs.value = []
  s.clients.value = {}
  s.clientsCheckedAt.value = ''
  s.selected.value = new Set()
  s.activeSerial.value = ''
  for (const serial of Object.keys(s.editors)) delete s.editors[serial]
}

// Locking the dashboard (or an expired session) wipes all proxy data from memory.
watch(() => dashboardAuth.phase, phase => { if (phase !== 'unlocked') clearProxySession() })
