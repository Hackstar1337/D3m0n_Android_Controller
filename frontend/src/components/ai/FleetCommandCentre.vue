<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import FleetJobMonitor from '@/components/ai/FleetJobMonitor.vue'
import { useAiFleet, type FleetDevice, type FleetProfile } from '@/composables/aiFleet'

const props = defineProps<{ devices: any[] }>()
const fleet = useAiFleet()
const intent = ref('')
const scope = ref<'navigation' | 'input' | 'app_state' | 'admin'>('navigation')
const selected = ref<Set<string>>(new Set())
const selectedRole = ref<'planner' | 'executor' | 'verifier'>('planner')
const mode = ref<'direct' | 'reasoning'>('direct')
const selectedGroup = ref('')
const profileKey = ref('')
const profileName = ref('')
const profileProvider = ref('openai')
const profileRole = ref('all')
const profileModel = ref('')
const profileUrl = ref('https://api.openai.com/v1')
watch(profileProvider, provider => { profileUrl.value = provider === 'openai' ? 'https://api.openai.com/v1' : provider === 'openrouter' ? 'https://openrouter.ai/api/v1' : '' })
const profileVersion = ref('')
const profileExpiry = ref('')
const profilePrivacy = ref('No raw observations persisted; vision off')
const profilePrice = ref('')
const profileNotice = ref('')
const planStage = ref<'intent' | 'review'>('intent')

const devices = computed<FleetDevice[]>(() => props.devices.map((device, index) => ({ serial: String(device?.serial || ''), label: String(device?.nickname || device?.name || device?.model || device?.serial || 'Device'), nickname: device?.nickname, model: device?.model, online: device?.online !== false && device?.status !== 'offline', busy: device?.busy === true, number: Number(device?.number || index + 1) } )).filter(device => device.serial))
const chosen = computed(() => devices.value.filter(device => selected.value.has(device.serial)))
const selectedCount = computed(() => chosen.value.length)
const groups = computed(() => fleet.groups.value)
const profiles = computed<FleetProfile[]>(() => fleet.profiles.value)
const canReview = computed(() => intent.value.trim().length > 0 && selectedCount.value > 0 && selectedCount.value <= 20 && !fleet.loading.value)
const canApprove = computed(() => Boolean(fleet.activePlan.value && fleet.approvalToken.value) && fleet.runtimeEnabled.value)

function toggle(serial: string) { const next = new Set(selected.value); next.has(serial) ? next.delete(serial) : next.add(serial); selected.value = next }
function selectOnline() { selected.value = new Set(devices.value.filter(device => device.online && !device.busy).slice(0, 20).map(device => device.serial)) }
function clearSelection() { selected.value = new Set() }
function selectGroup() {
  const group = groups.value.find(item => item.id === selectedGroup.value)
  selected.value = new Set(devices.value.filter(device => !group || group.devices.includes(device.serial)).map(device => device.serial))
}
function setRoleBinding(role: 'planner' | 'executor' | 'verifier', event: Event) {
  fleet.roleBindings.value = { ...fleet.roleBindings.value, [role]: (event.target as HTMLSelectElement).value }
}
type RoleName = 'planner' | 'executor' | 'verifier'
function applyDefaultBindings() {
  const configured = profiles.value.filter(profile => profile.configured !== false && profile.id)
  if (!configured.length) return
  const current = fleet.roleBindings.value
  const pick = (role: RoleName) => (current[role] && configured.some(profile => profile.id === current[role])) ? current[role] : (configured.find(profile => profile.roles?.includes(role))?.id || configured[0]?.id || '')
  const next = { planner: pick('planner'), executor: pick('executor'), verifier: pick('verifier') }
  if (next.planner !== current.planner || next.executor !== current.executor || next.verifier !== current.verifier) fleet.roleBindings.value = next
}
watch([profiles, () => fleet.roleBindings.value], applyDefaultBindings, { immediate: true })
const presets = [
  { id: 'settings', label: 'Settings round-trip · verified', text: 'Go Home, open Settings, then return Home', scope: 'navigation', mode: 'direct' },
  { id: 'playstore', label: 'Open Play Store home', text: 'Open the Google Play Store app and stop on its home screen, then return Home', scope: 'navigation', mode: 'direct' },
  { id: 'status', label: 'Fleet status report · read-only', text: "Report this phone's group, proxy, stream, Sync and recovery status without changing anything", scope: 'admin', mode: 'direct' },
  { id: 'reboot', label: 'Reboot · asks you first', text: 'Reboot this phone once using the native reboot tool and confirm it comes back online', scope: 'admin', mode: 'direct' },
] as const
function usePreset(preset: typeof presets[number]) { intent.value = preset.text; scope.value = preset.scope; mode.value = preset.mode }
async function reviewPlan() { if (!canReview.value) return; await fleet.createPlan(intent.value, chosen.value, scope.value, mode.value); planStage.value = 'review' }
async function approve() { await fleet.approvePlan(); planStage.value = 'intent' }
function clearEphemeralKey() { profileKey.value = '' }
function resetProfileForm() { profileKey.value = ''; profileName.value = ''; profileModel.value = ''; profileVersion.value = ''; profileExpiry.value = ''; profilePrice.value = ''; profileNotice.value = 'Draft cleared; no key was persisted.' }
async function saveProfileDraft() {
  if (!profileName.value || !profileModel.value || !profileKey.value) { profileNotice.value = 'Enter a profile name, model, and key for the authenticated save.'; return }
  const draft = { profile_id: profileName.value.trim(), provider: profileProvider.value, model: profileModel.value.trim(), endpoint_origin: profileUrl.value.trim(), roles: profileRole.value === 'all' ? ['planner', 'executor', 'verifier'] : [profileRole.value], capabilities: ['observe', 'navigate'], expires_at: profileExpiry.value ? new Date(profileExpiry.value).toISOString() : null, secret: profileKey.value }
  try { await fleet.saveProfile(draft); profileNotice.value = 'Encrypted profile saved. The key was cleared and is never read back.'; await Promise.all([fleet.loadProfiles(), fleet.loadRoleBindings()]) }
  catch { profileNotice.value = 'Profile save was rejected; the key was cleared.' }
  finally { clearEphemeralKey() }
}
function profileLabel(profile: FleetProfile) { return `${profile.name || profile.id} · ${profile.provider}/${profile.model}` }
function profileMeta(profile: FleetProfile) {
  const parts = [profile.version ? `v${profile.version}` : '', profile.privacy || 'metadata only · vision off', profile.configured ? 'key stored (encrypted)' : 'no key', profile.expires_at ? `expires ${new Date(profile.expires_at).toLocaleString()}` : '']
  return parts.filter(Boolean).join(' · ')
}
let poll: ReturnType<typeof setInterval> | undefined
onMounted(() => { void fleet.refresh(); poll = setInterval(() => { void fleet.loadJobs(); void fleet.loadStatus(); if (fleet.selectedJob.value) void fleet.loadJob(fleet.selectedJob.value.job_id) }, 2500) })
onUnmounted(() => { if (poll) clearInterval(poll); clearEphemeralKey() })
</script>

<template>
  <section class="fleet-ai" aria-labelledby="fleet-ai-title">
    <div class="fleet-ai__banner" :class="{ 'is-ready': fleet.runtimeEnabled.value }" role="status">
      <UiIcon :name="fleet.runtimeEnabled.value ? 'check' : 'info'" :size="15" />
      <div><strong>{{ fleet.runtimeEnabled.value ? 'Fleet runtime ready' : 'Fleet runtime inactive' }}</strong><span>{{ fleet.status.value.reason || 'Cloud work starts only after runtime activation and explicit job approval.' }}</span></div>
    </div>
    <div v-if="fleet.error.value" class="ui-callout ui-callout--danger" role="alert">{{ fleet.error.value }}</div>
    <div v-if="fleet.notice.value" class="ui-callout ui-callout--success" role="status">{{ fleet.notice.value }}</div>

    <div class="fleet-ai__header"><div><span class="ui-eyebrow">Mobilerun-only · owner controlled</span><h2 id="fleet-ai-title">Fleet AI Command Centre</h2><p>Freeze a numbered target set, review a bounded plan, then approve. Existing Live Grid, Sync, native controls and Agent Console remain authoritative.</p></div><div class="fleet-ai__actions"><span class="ui-badge ui-badge--muted">{{ selectedCount }}/20 selected</span><button type="button" class="ui-btn ui-btn--sm" :disabled="fleet.loading.value" @click="fleet.setActivation(!fleet.runtimeEnabled.value)">{{ fleet.runtimeEnabled.value ? 'Disable activation' : 'Enable activation' }}</button></div></div>
    <div class="fleet-ai__grid">
      <section class="ui-panel fleet-ai__panel" aria-labelledby="targets-heading">
        <div class="fleet-ai__panel-head"><div><span class="ui-eyebrow">01 · Scope</span><h3 id="targets-heading">Select phones</h3></div><div class="fleet-ai__actions"><select v-if="groups.length" v-model="selectedGroup" aria-label="Target group" @change="selectGroup"><option value="">All groups</option><option v-for="group in groups" :key="group.id" :value="group.id">{{ group.name }}</option></select><button type="button" class="ui-btn ui-btn--sm" @click="selectOnline">Select ready</button><button type="button" class="ui-btn ui-btn--sm ui-btn--ghost" @click="clearSelection">Clear</button></div></div>
        <p class="ui-hint">Selection is explicit and frozen into the plan. Busy or offline devices are never silently substituted.</p>
        <div class="fleet-targets" role="group" aria-label="Numbered fleet devices">
          <label v-for="device in devices" :key="device.serial" class="fleet-target" :class="{ 'is-selected': selected.has(device.serial), 'is-disabled': !device.online || device.busy }"><input type="checkbox" :checked="selected.has(device.serial)" :disabled="!device.online || device.busy" @change="toggle(device.serial)" /><span class="fleet-target__number">{{ device.number }}.</span><span class="fleet-target__body"><strong>{{ device.label }}</strong><small>{{ device.serial }} · {{ device.online ? (device.busy ? 'busy' : 'online') : 'offline' }}</small></span><span v-if="device.busy" class="ui-badge ui-badge--warn">busy</span></label>
          <div v-if="!devices.length" class="ui-empty"><strong>No device inventory</strong><span>Local preview has no phone fixture loaded.</span></div>
        </div>
      </section>

      <section class="ui-panel fleet-ai__panel" aria-labelledby="intent-heading">
        <div class="fleet-ai__panel-head"><div><span class="ui-eyebrow">02 · Intent</span><h3 id="intent-heading">Describe a bounded task</h3></div><span class="ui-badge ui-badge--muted">3 workers max</span></div>
        <div class="fleet-presets" role="group" aria-label="Quick tasks"><span class="ui-label">Quick tasks</span><div class="fleet-ai__actions"><button v-for="preset in presets" :key="preset.id" type="button" class="ui-btn ui-btn--sm" :class="{ 'is-active': intent === preset.text }" @click="usePreset(preset)">{{ preset.label }}</button></div></div><label class="ui-field"><span class="ui-label">Task intent</span><textarea v-model="intent" rows="4" maxlength="4000" placeholder="Example: Open the approved test app and verify its home screen." aria-describedby="intent-hint"></textarea><span id="intent-hint" class="ui-hint">Do not include credentials, OTPs, screenshots, raw UI trees, or account secrets. Missing owner inputs pause the phone.</span></label>
        <div class="fleet-ai__form-row"><label class="ui-field"><span class="ui-label">Scope</span><select v-model="scope"><option value="navigation">Navigation · reversible</option><option value="input">Input · owner-reviewed</option><option value="app_state">App state · owner-reviewed</option><option value="admin">Admin · checkpoint required</option></select></label><label class="ui-field"><span class="ui-label">Execution mode</span><select v-model="mode"><option value="direct">Direct · bounded tools</option><option value="reasoning">Reasoning · owner-reviewed</option></select></label><label v-for="role in (['planner', 'executor', 'verifier'] as const)" :key="role" class="ui-field"><span class="ui-label">{{ role }} profile</span><select :value="fleet.roleBindings.value[role]" :aria-label="`${role} model profile`" @change="setRoleBinding(role, $event)"><option value="">Select configured profile</option><option v-for="profile in profiles.filter(p => p.configured !== false)" :key="profile.id" :value="profile.id">{{ profileLabel(profile) }}</option></select></label></div>
      <button type="button" class="ui-btn ui-btn--primary" :disabled="!canReview" @click="reviewPlan"><UiIcon name="chevronRight" :size="14" />Review frozen plan</button>
      </section>
    </div>

    <section v-if="planStage === 'review' && fleet.activePlan.value" class="ui-panel fleet-ai__review" aria-labelledby="review-heading">
      <div class="fleet-ai__panel-head"><div><span class="ui-eyebrow">03 · Review</span><h3 id="review-heading">Approve this exact scope</h3></div><span class="ui-badge ui-badge--warn">No automatic expansion</span></div>
      <div class="fleet-review-grid"><div><span class="ui-label">Intent</span><p>{{ fleet.activePlan.value.intent?.text || 'Frozen intent unavailable' }}</p></div><div><span class="ui-label">Targets</span><p>{{ (fleet.activePlan.value.targets || []).map((device, index) => `${index + 1}. ${device.label || device.serial}`).join(', ') }}</p></div><div><span class="ui-label">Policy</span><p>{{ fleet.activePlan.value.policy?.scope || 'Frozen policy unavailable' }} · {{ fleet.activePlan.value.policy?.max_targets || 0 }} target{{ (fleet.activePlan.value.policy?.max_targets || 0) === 1 ? '' : 's' }} · vision {{ fleet.activePlan.value.policy?.allow_vision ? 'on' : 'off' }} · USD {{ fleet.activePlan.value.policy?.budget_usd ?? '20' }} ceiling</p></div><div><span class="ui-label">Data sharing</span><p>Metadata-only history; raw screens, prompts, provider responses and keys are not persisted.</p></div></div>
      <div class="fleet-ai__actions"><button type="button" class="ui-btn ui-btn--primary" :disabled="!canApprove" @click="approve"><UiIcon name="check" :size="14" />Approve and queue</button><button type="button" class="ui-btn" @click="planStage = 'intent'">Edit intent</button></div><p v-if="!fleet.runtimeEnabled.value" class="ui-hint">Approval is disabled while the local runtime is disabled. Enable the configured runtime before queueing phone actions.</p>
    </section>

    <section class="ui-panel fleet-ai__profiles" aria-labelledby="profiles-heading">
      <div class="fleet-ai__panel-head"><div><span class="ui-eyebrow">04 · Profiles</span><h3 id="profiles-heading">Role profiles and model metadata</h3></div><span class="ui-badge ui-badge--muted">Keys never read back</span></div>
      <div class="fleet-profile-list"><div v-for="profile in profiles" :key="profile.id" class="fleet-profile"><strong>{{ profileLabel(profile) }}</strong><span>{{ profileMeta(profile) }}</span><div class="fleet-ai__actions"><button type="button" class="ui-btn ui-btn--sm" @click="fleet.validateProfile(profile.id)">Check model policy</button><button type="button" class="ui-btn ui-btn--sm" @click="fleet.clearProfileSecret(profile.id)">Clear key</button><button type="button" class="ui-btn ui-btn--sm ui-btn--ghost" @click="fleet.revokeProfile(profile.id)">Revoke</button></div></div><p v-if="!profiles.length" class="ui-hint">No server profile metadata is available. Configure encrypted profiles only through the protected runtime setup.</p></div>
      <details class="fleet-profile-form"><summary>Configure encrypted model profile</summary><form @submit.prevent="saveProfileDraft"><div class="fleet-ai__form-row"><label class="ui-field"><span class="ui-label">Profile name</span><input v-model="profileName" autocomplete="off" maxlength="80" placeholder="planner-primary" /></label><label class="ui-field"><span class="ui-label">Role</span><select v-model="profileRole"><option value="all">All three roles · shared key/model</option><option value="planner">Planner</option><option value="executor">Executor</option><option value="verifier">Verifier</option></select></label></div><div class="fleet-ai__form-row"><label class="ui-field"><span class="ui-label">Provider</span><select v-model="profileProvider"><option value="openai">OpenAI</option><option value="openrouter">OpenRouter</option><option value="generic">Owner-allowlisted compatible endpoint</option></select></label><label class="ui-field"><span class="ui-label">Model ID</span><input v-model="profileModel" autocomplete="off" maxlength="120" placeholder="provider/model" /></label></div><label class="ui-field"><span class="ui-label">Protected key input <small>(cleared after submit)</small></span><input v-model="profileKey" type="password" autocomplete="new-password" placeholder="Enter only when protected setup is enabled" /><span class="ui-hint">Sent only to the authenticated server for encrypted storage; never read back. The field is cleared after submission or leaving this workspace.</span></label><div class="fleet-ai__form-row"><label class="ui-field"><span class="ui-label">Approved HTTPS base URL</span><input v-model="profileUrl" type="url" autocomplete="off" /></label></div><div class="fleet-ai__form-row"><label class="ui-field"><span class="ui-label">Expiry</span><input v-model="profileExpiry" type="datetime-local" /></label></div><div class="fleet-ai__actions"><button type="submit" class="ui-btn" :disabled="!profileName || !profileModel">Save encrypted profile</button><button type="button" class="ui-btn ui-btn--ghost" @click="resetProfileForm">Reset</button></div><p v-if="profileNotice" class="ui-hint" role="status">{{ profileNotice }}</p></form></details>
    </section>

    <FleetJobMonitor :fleet="fleet" />
  </section>
</template>

<style scoped>
.fleet-ai { display:grid; gap:14px; max-width:1760px; margin:0 auto; padding:18px; color:var(--ui-text-1); }
.fleet-ai__banner { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; border:1px solid var(--ui-border-strong); border-radius:var(--ui-radius-sm); background:rgba(245,158,11,.08); color:var(--ui-text-2); }
.fleet-ai__banner.is-ready { background:rgba(63,208,168,.08); border-color:var(--ui-accent-line); }
.fleet-ai__banner strong,.fleet-ai__banner span { display:block; }.fleet-ai__banner strong { color:var(--ui-text-1); font-size:12px; }.fleet-ai__banner span { font-size:11px; margin-top:2px; }
.fleet-ai__header { display:flex; justify-content:space-between; gap:16px; align-items:start; }.fleet-ai__header h2 { margin:2px 0 3px; font-size:22px; }.fleet-ai__header p { color:var(--ui-text-3); margin:0; font-size:12px; max-width:760px; }.ui-eyebrow { display:block; color:var(--ui-text-4); font-size:10px; letter-spacing:.1em; text-transform:uppercase; font-weight:700; }
.fleet-presets { display:grid; gap:5px; margin-bottom:10px; }.fleet-presets .ui-btn.is-active { border-color:var(--ui-accent-line); background:var(--ui-accent-soft); color:var(--ui-accent); }.fleet-ai__grid { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(360px,.75fr); gap:14px; }.fleet-ai__panel,.fleet-ai__review,.fleet-ai__profiles { padding:15px; }.fleet-ai__panel-head { display:flex; justify-content:space-between; gap:12px; align-items:start; margin-bottom:8px; }.fleet-ai h3 { margin:2px 0 0; font-size:14px; }.fleet-ai__actions { display:flex; gap:7px; flex-wrap:wrap; align-items:center; }.fleet-targets { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:7px; margin-top:10px; }.fleet-target { display:flex; align-items:center; gap:7px; min-height:48px; padding:7px 8px; border:1px solid var(--ui-border); border-radius:var(--ui-radius-sm); background:var(--ui-surface-sunken); cursor:pointer; }.fleet-target:hover,.fleet-target.is-selected { border-color:var(--ui-accent-line); background:var(--ui-accent-soft); }.fleet-target.is-disabled { opacity:.55; cursor:not-allowed; }.fleet-target__number { color:var(--ui-accent); font-family:var(--ui-mono); font-size:11px; }.fleet-target__body { min-width:0; flex:1; }.fleet-target__body strong,.fleet-target__body small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.fleet-target__body strong { font-size:12px; }.fleet-target__body small { color:var(--ui-text-4); font-size:10px; }.fleet-ai__form-row,.fleet-review-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }.fleet-review-grid { grid-template-columns:repeat(4,minmax(0,1fr)); margin:4px 0 13px; }.fleet-review-grid p { font-size:12px; color:var(--ui-text-2); margin:3px 0 0; }.fleet-profile-list { display:grid; gap:5px; }.fleet-profile { display:flex; justify-content:space-between; gap:12px; padding:8px 10px; border:1px solid var(--ui-border); border-radius:var(--ui-radius-sm); }.fleet-profile span { color:var(--ui-text-3); font-size:11px; text-align:right; }.fleet-profile-form { margin-top:12px; border-top:1px solid var(--ui-border); padding-top:10px; }.fleet-profile-form summary { cursor:pointer; color:var(--ui-text-2); font-size:12px; font-weight:650; }.fleet-profile-form form { display:grid; gap:10px; padding-top:12px; }.ui-field { display:grid; gap:4px; }.ui-label { color:var(--ui-text-2); font-size:11px; font-weight:650; }.ui-label small { color:var(--ui-text-4); font-weight:400; }.ui-hint { color:var(--ui-text-4); font-size:11px; margin:5px 0 9px; }.fleet-ai textarea,.fleet-ai input,.fleet-ai select { width:100%; box-sizing:border-box; min-height:34px; padding:7px 9px; }.fleet-ai textarea { resize:vertical; }.fleet-ai .ui-btn { margin-top:4px; }.fleet-ai__panel > .ui-btn { margin-top:8px; }
.fleet-target input[type="checkbox"] { width:14px; height:14px; min-height:14px; min-width:14px; flex:0 0 14px; margin:0; padding:0; accent-color:var(--ui-accent); }
@media (max-width:900px) { .fleet-ai__grid,.fleet-review-grid,.fleet-ai__form-row { grid-template-columns:1fr; }.fleet-profile { display:grid; }.fleet-profile span { text-align:left; }.fleet-ai__header { display:grid; } }
</style>
