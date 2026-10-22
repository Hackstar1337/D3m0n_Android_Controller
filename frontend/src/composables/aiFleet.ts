import { computed, ref } from 'vue'
import { api } from '@/composables/useApi'

export type FleetDevice = { serial: string; label?: string; nickname?: string; model?: string; online?: boolean; busy?: boolean; number?: number; group?: string }
export type FleetGroup = { id: string; name: string; devices: string[]; created_at?: string; updated_at?: string }
export type FleetProfile = { id: string; profile_id?: string; name: string; provider: string; model: string; base_url?: string; endpoint_origin?: string; version?: string | number; expires_at?: string | null; privacy?: string; price_known?: boolean; configured?: boolean; roles?: string[]; capabilities?: string[] }
export type FleetStatus = { runtime_enabled: boolean; mode?: 'local-preview' | 'runtime-disabled' | 'ready' | string; reason?: string; max_active_workers?: number; budget_usd?: number; portal_mode?: string }
export type FleetPlan = { plan_id: string; state: string; target_count: number; created_at?: string; approval_expires_at?: string; intent?: { text?: string; metadata?: Record<string, string> }; targets?: FleetDevice[]; policy?: FleetPolicy; scope?: string; capabilities?: string[] }
export type FleetReceipt = { step_id?: string; child_id?: string; sequence?: number; capability?: string; outcome?: string; summary?: string; created_at?: string }
export type FleetChild = { child_id?: string; target_id?: string; serial?: string; label?: string; state?: string; phase?: string; latest_action?: string; retries?: number; evidence_at?: string; summary?: string; receipts?: FleetReceipt[]; checkpoints?: Array<{ checkpoint_id?: string; status?: string; reason?: string; capability?: string; created_at?: string }> }
export type FleetJob = { job_id: string; plan_id?: string; state: string; target_count?: number; completed_count?: number; uncertain_count?: number; budget_reserved_usd?: number; budget_settled_usd?: number; children?: FleetChild[]; checkpoints?: FleetChild['checkpoints'] }

export type FleetPolicy = {
  scope: 'navigation' | 'input' | 'app_state' | 'admin'
  capabilities: string[]
  max_targets: number
  max_active_workers: number
  budget_usd: number
  allow_vision: boolean
  expiry?: string
  version: string
}

function normalizeProfile(raw: any): FleetProfile {
  const id = String(raw?.id ?? raw?.profile_id ?? '')
  return {
    id,
    profile_id: id,
    name: String(raw?.name ?? id),
    provider: String(raw?.provider ?? ''),
    model: String(raw?.model ?? ''),
    base_url: typeof raw?.base_url === 'string' ? raw.base_url : raw?.endpoint_origin,
    endpoint_origin: typeof raw?.endpoint_origin === 'string' ? raw.endpoint_origin : raw?.base_url,
    version: raw?.version,
    expires_at: raw?.expires_at ?? null,
    privacy: raw?.privacy,
    price_known: raw?.price_known,
    configured: raw?.configured === true,
    roles: Array.isArray(raw?.roles) ? raw.roles.map((role: unknown) => String(role)) : [],
    capabilities: Array.isArray(raw?.capabilities) ? raw.capabilities.map((capability: unknown) => String(capability)) : [],
  }
}

function normalizeChild(raw: any): FleetChild {
  const target = raw?.target && typeof raw.target === 'object' ? raw.target : {}
  return {
    ...raw,
    target_id: String(raw?.target_id ?? target.target_id ?? ''),
    serial: String(raw?.serial ?? target.serial ?? ''),
    label: String(raw?.label ?? target.label ?? target.serial ?? 'Target'),
    summary: raw?.summary ?? raw?.latest_summary,
    latest_action: raw?.latest_action ?? (raw?.latest_step ? `Step ${raw.latest_step}` : undefined),
  }
}

function normalizeJob(raw: any): FleetJob {
  return {
    ...raw,
    children: Array.isArray(raw?.children) ? raw.children.map(normalizeChild) : undefined,
    checkpoints: Array.isArray(raw?.checkpoints) ? raw.checkpoints : undefined,
  }
}

function safeError(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback
  const message = error.message.replace(/\s+/g, ' ').trim()
  if (!message || message.length > 180 || /(?:api[_ -]?key|authorization|bearer|token|secret|password|credential)/i.test(message)) return fallback
  return message.startsWith('API ') ? `Fleet service unavailable (${message.slice(4, 8)})` : message
}

function makeRequestId(): string {
  try { return crypto.randomUUID() } catch { return `fleet-${Date.now()}` }
}

export function useAiFleet() {
  const status = ref<FleetStatus>({ runtime_enabled: false, mode: 'local-preview', reason: 'Local preview only' })
  const profiles = ref<FleetProfile[]>([])
  const groups = ref<FleetGroup[]>([])
  const roleBindings = ref<Record<'planner' | 'executor' | 'verifier', string>>({ planner: '', executor: '', verifier: '' })
  const jobs = ref<FleetJob[]>([])
  const selectedJob = ref<FleetJob | null>(null)
  const loading = ref(false)
  const error = ref('')
  const notice = ref('')
  const approvalToken = ref('')
  const activePlan = ref<FleetPlan | null>(null)
  const runtimeEnabled = computed(() => status.value.runtime_enabled === true)

  async function setActivation(enabled: boolean) {
    loading.value = true; error.value = ''
    try {
      const raw = await api<any>('/api/ai-fleet/activation', { method: 'POST', body: JSON.stringify({ enabled }) })
      const runtime = raw?.status?.runtime || raw?.runtime || raw
      status.value = { ...status.value, runtime_enabled: raw?.status?.activation === 'enabled' || (enabled && runtime?.ready === true), mode: runtime?.ready === true ? 'ready' : 'runtime-disabled', reason: String(runtime?.reason || '') }
      return raw
    } catch (cause) { error.value = safeError(cause, 'Fleet activation was rejected'); throw cause }
    finally { loading.value = false }
  }

  async function saveProfile(input: { profile_id: string; provider: string; model: string; endpoint_origin: string; roles?: string[]; capabilities?: string[]; expires_at?: string | null; secret: string }) {
    const secret = input.secret
    try {
      const payload = { ...input }
      delete (payload as any).secret
      return normalizeProfile(await api<any>('/api/ai-fleet/profiles', { method: 'POST', body: JSON.stringify({ ...payload, secret }) }))
    } finally { input.secret = '' }
  }

  async function patchProfile(profileId: string, input: Record<string, unknown>) {
    const payload = { ...input }
    const secret = typeof payload.secret === 'string' ? payload.secret : undefined
    try { delete payload.secret; return normalizeProfile(await api<any>(`/api/ai-fleet/profiles/${encodeURIComponent(profileId)}`, { method: 'PATCH', body: JSON.stringify({ ...payload, ...(secret ? { secret } : {}) }) })) }
    finally { if ('secret' in input) input.secret = '' }
  }

  async function clearProfileSecret(profileId: string) { return normalizeProfile(await api<any>(`/api/ai-fleet/profiles/${encodeURIComponent(profileId)}`, { method: 'PATCH', body: JSON.stringify({ clear_secret: true }) })) }
  async function revokeProfile(profileId: string) { return normalizeProfile(await api<any>(`/api/ai-fleet/profiles/${encodeURIComponent(profileId)}`, { method: 'PATCH', body: JSON.stringify({ revoke: true }) })) }

  async function loadStatus() {
    try {
      const raw = await api<any>('/api/ai-fleet/status')
      const runtime = raw?.runtime && typeof raw.runtime === 'object' ? raw.runtime : raw
      status.value = {
        runtime_enabled: raw?.activation === 'enabled' ? runtime?.ready === true : raw?.runtime_enabled === true,
        mode: runtime?.ready === true ? 'ready' : (raw?.activation === 'disabled' ? 'runtime-disabled' : 'local-preview'),
        reason: String(runtime?.reason ?? raw?.reason ?? ''),
        max_active_workers: Number(raw?.worker_cap ?? raw?.max_active_workers ?? 3),
        budget_usd: Number(raw?.budget_ceiling_usd ?? raw?.budget_usd ?? 20),
        portal_mode: runtime?.portal_mode,
      }
    }
    catch (cause) { status.value = { runtime_enabled: false, mode: 'local-preview', reason: 'Fleet runtime is not enabled on localhost' }; error.value = safeError(cause, 'Fleet status is unavailable') }
  }
  async function loadProfiles() {
    try {
      const response = await api<FleetProfile[] | { profiles?: FleetProfile[] }>('/api/ai-fleet/profiles')
      profiles.value = (Array.isArray(response) ? response : (response.profiles || [])).map(normalizeProfile)
    }
    catch (cause) { profiles.value = []; error.value = safeError(cause, 'Profile metadata is unavailable') }
  }
  async function loadRoleBindings() {
    try {
      const response = await api<{ planner?: string; executor?: string; verifier?: string }>('/api/ai-fleet/profiles/roles')
      roleBindings.value = { planner: String(response.planner || ''), executor: String(response.executor || ''), verifier: String(response.verifier || '') }
    } catch { roleBindings.value = { planner: '', executor: '', verifier: '' } }
  }
  async function loadGroups() {
    try {
      const response = await api<{ groups?: Array<{ id?: string; name?: string; devices?: unknown[] }> }>('/api/fleet/groups')
      groups.value = (response.groups || []).map(group => ({
        id: String(group.id || ''),
        name: String(group.name || group.id || 'Unnamed group'),
        devices: Array.isArray(group.devices) ? group.devices.map(serial => String(serial)).filter(Boolean) : [],
      })).filter(group => group.id)
    } catch (cause) {
      groups.value = []
      error.value = safeError(cause, 'Saved fleet groups are unavailable')
    }
  }
  async function loadJobs() {
    try {
      const response = await api<{ jobs?: FleetJob[] } | FleetJob[]>('/api/ai-fleet/jobs')
      jobs.value = (Array.isArray(response) ? response : (response.jobs || [])).map(normalizeJob)
    } catch (cause) { error.value = safeError(cause, 'Fleet jobs are unavailable') }
  }
  async function loadJob(jobId: string) {
    try {
      const job = normalizeJob(await api<FleetJob>(`/api/ai-fleet/jobs/${encodeURIComponent(jobId)}`))
      const response = await api<{ receipts?: FleetReceipt[] }>(`/api/ai-fleet/jobs/${encodeURIComponent(jobId)}/receipts?limit=100`)
      const receipts = Array.isArray(response.receipts) ? response.receipts : []
      const byChild = new Map<string, FleetReceipt[]>()
      for (const receipt of receipts) {
        const childId = String(receipt.child_id || '')
        if (!childId) continue
        const childReceipts = byChild.get(childId) || []
        childReceipts.push(receipt)
        byChild.set(childId, childReceipts)
      }
      job.children = job.children?.map(child => ({ ...child, receipts: byChild.get(String(child.child_id || '')) || [] }))
      selectedJob.value = job
    }
    catch (cause) { error.value = safeError(cause, 'Job details are unavailable') }
  }

  async function createPlan(intentText: string, targets: FleetDevice[], scope: FleetPolicy['scope'] = 'navigation', runtimeMode: 'direct' | 'reasoning' = 'direct') {
    const clean = intentText.trim()
    const selected = targets.slice(0, 100).map(target => String(target.serial).trim()).filter(Boolean)
    if (!clean || !selected.length) throw new Error('Enter an intent and select at least one device')
    if (selected.length > 100) throw new Error('A plan may target at most 100 devices')
    loading.value = true; error.value = ''; notice.value = ''
    try {
      const configured = profiles.value.filter(profile => profile.id && profile.provider && profile.model && (profile.endpoint_origin || profile.base_url))
      if (!configured.length) throw new Error('A fleet profile is required before plan creation')
      const primary = configured[0]
      if (!primary) throw new Error('A configured fleet profile is required before plan creation')
      const byRole = (role: 'planner' | 'executor' | 'verifier') => roleBindings.value[role] || configured.find(profile => profile.roles?.includes(role))?.id || primary.id
      const bindings = { planner: byRole('planner'), executor: byRole('executor'), verifier: byRole('verifier') }
      const profileRefs = configured.map(profile => ({
        profile_id: profile.id,
        provider: profile.provider,
        model: profile.model,
        endpoint_origin: profile.endpoint_origin || profile.base_url,
        configured: profile.configured === true,
        expires_at: profile.expires_at || null,
        capabilities: profile.capabilities || [],
      }))
      if (Object.values(bindings).some(id => !profileRefs.some(profile => profile.profile_id === id))) throw new Error('Fleet role bindings are unavailable')
      const targetRefs = targets.filter(target => selected.includes(String(target.serial).trim())).map(target => ({ target_id: target.serial, serial: target.serial, label: target.label || target.serial, online: target.online !== false, metadata: target.number ? { number: String(target.number) } : {} }))
      const policy: FleetPolicy = { scope, capabilities: scope === 'navigation' ? ['observe', 'navigate', 'input', 'app_state'] : scope === 'admin' ? ['observe', 'inventory', 'group_read', 'proxy_read', 'reboot_preflight', 'recovery_status', 'sync_status', 'stream_status', 'reboot', 'file_write', 'apk_install'] : ['observe', scope], max_targets: selected.length, max_active_workers: 3, budget_usd: 20, allow_vision: false, version: '1' }
      // The foundation DTO is closed: selected serials are display-safe intent metadata, never credentials or observations.
      const created = await api<{ plan: FleetPlan; approval_token: string; review?: { intent?: FleetPlan['intent']; targets?: { targets?: FleetDevice[] }; policy?: FleetPolicy } }>('/api/ai-fleet/plans', {
        method: 'POST', body: JSON.stringify({ intent: { text: clean.slice(0, 4000), request_id: makeRequestId(), metadata: { target_ids: selected.join(','), target_count: String(selected.length), runtime_mode: runtimeMode } }, targets: targetRefs, profiles: profileRefs, role_bindings: bindings, policy, idempotency_key: makeRequestId() })
      })
      const review = created.review
      const frozenTargets = Array.isArray(review?.targets?.targets) ? review.targets.targets.map((target: any, index) => ({ serial: String(target.serial), label: String(target.label || target.serial), online: target.online !== false, number: index + 1 })) : []
      activePlan.value = { ...created.plan, intent: review?.intent, targets: frozenTargets, policy: review?.policy }
      approvalToken.value = created.approval_token
      notice.value = 'Plan created. Review the frozen scope before approval.'
      return activePlan.value
    } catch (cause) { error.value = safeError(cause, 'Plan creation was blocked'); throw cause }
    finally { loading.value = false }
  }
  async function approvePlan() {
    if (!activePlan.value || !approvalToken.value) throw new Error('Create a plan before approving it')
    loading.value = true; error.value = ''
    try {
      const job = normalizeJob(await api<FleetJob>(`/api/ai-fleet/plans/${encodeURIComponent(activePlan.value.plan_id)}/approve`, { method: 'POST', body: JSON.stringify({ approval_token: approvalToken.value, idempotency_key: makeRequestId() }) }))
      approvalToken.value = ''
      selectedJob.value = job; jobs.value = [job, ...jobs.value.filter(item => item.job_id !== job.job_id)]
      notice.value = 'Plan approved. The owner-controlled job is ready to run.'
      return job
    } catch (cause) { error.value = safeError(cause, 'Approval was rejected'); throw cause }
    finally { loading.value = false }
  }
  async function actionJob(jobId: string, action: 'pause' | 'resume' | 'stop' | 'retry') {
    try { const job = normalizeJob(await api<FleetJob>(`/api/ai-fleet/jobs/${encodeURIComponent(jobId)}/actions`, { method: 'POST', body: JSON.stringify({ action }) })); selectedJob.value = job; await loadJobs(); return job }
    catch (cause) { error.value = safeError(cause, `Could not ${action} this job`); throw cause }
  }
  async function reconcile(childId: string, outcome: 'succeeded' | 'failed' | 'manual_recovery' | 'retry', summary: string, grant?: { capability: string; operation: string }) {
    try { const job = normalizeJob(await api<FleetJob>(`/api/ai-fleet/jobs/${encodeURIComponent(selectedJob.value?.job_id || '')}/reconcile`, { method: 'POST', body: JSON.stringify({ child_id: childId, outcome, summary: summary.slice(0, 240), ...(grant ? { grant_capability: grant.capability, grant_operation: grant.operation } : {}) }) })); selectedJob.value = job; await loadJobs(); return job }
    catch (cause) { error.value = safeError(cause, 'Reconciliation was rejected'); throw cause }
  }
  async function validateProfile(profileId: string) {
    try { await api(`/api/ai-fleet/profiles/${encodeURIComponent(profileId)}/validate`, { method: 'POST' }); await loadStatus(); notice.value = 'Verified model policy ready. Provider access is checked by a bounded approved run.' }
    catch (cause) { error.value = safeError(cause, 'Model policy check failed'); throw cause }
  }
  async function refresh() { await Promise.all([loadStatus(), loadProfiles(), loadRoleBindings(), loadGroups(), loadJobs()]) }
  return { status, profiles, groups, roleBindings, jobs, selectedJob, activePlan, loading, error, notice, runtimeEnabled, approvalToken, loadStatus, loadProfiles, loadRoleBindings, loadGroups, loadJobs, loadJob, createPlan, approvePlan, actionJob, reconcile, setActivation, saveProfile, patchProfile, clearProfileSecret, revokeProfile, validateProfile, refresh }
}
