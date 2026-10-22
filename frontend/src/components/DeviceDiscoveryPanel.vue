<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

type InterfaceRow = { name: string; address: string; netmask: string; cidr: string; up: boolean; private?: boolean }
type Candidate = {
  candidate_id: string
  source: string
  host: string
  port: number
  service: string
  transport: string
  pairing_required: boolean
  interface?: InterfaceRow
  expires_at: string
}
type UsbRow = { state: 'device' | 'unauthorized' | 'offline'; transport: string; identity: string }
type Status = { usb: UsbRow[]; mdns: { available: boolean; diagnostic: string } }

const props = withDefaults(defineProps<{
  apiBase?: string
  headers?: Record<string, string>
  autoPassive?: boolean
}>(), { apiBase: '', autoPassive: true })
const emit = defineEmits<{
  (event: 'enrolled', payload: unknown): void
  (event: 'error', message: string): void
}>()

const interfaces = ref<InterfaceRow[]>([])
const selectedInterface = ref('')
const selectedCidr = ref('')
const candidates = ref<Candidate[]>([])
const status = ref<Status>({ usb: [], mdns: { available: false, diagnostic: 'Not checked' } })
const busy = ref(false)
const message = ref('')
const pairingCandidate = ref('')
const pairingCode = ref('')

const api = (path: string) => `${props.apiBase.replace(/\/$/, '')}${path}`
const request = async (path: string, init: RequestInit = {}) => {
  const response = await fetch(api(path), {
    credentials: 'include',
    ...init,
    headers: { 'Content-Type': 'application/json', ...(props.headers ?? {}), ...(init.headers ?? {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`)
  return body
}

const loadInterfaces = async () => {
  const body = await request('/api/phone/discovery/interfaces')
  interfaces.value = (body.interfaces ?? []).filter((item: InterfaceRow) => item.private !== false)
  const first = interfaces.value.find((item) => item.up)
  if (first && !selectedInterface.value) {
    selectedInterface.value = first.name
    selectedCidr.value = first.cidr
  }
}
const loadStatus = async () => { status.value = await request('/api/phone/discovery/status') }
const scan = async (methods: string[], confirmed = false) => {
  busy.value = true
  message.value = ''
  try {
    const body = await request('/api/phone/discovery/scan', {
      method: 'POST', body: JSON.stringify({ methods, interface: selectedInterface.value || undefined, cidr: selectedCidr.value || undefined, confirmed }),
    })
    candidates.value = body.candidates ?? []
    if (body.mdns) status.value.mdns = body.mdns
    message.value = `${candidates.value.length} candidate${candidates.value.length === 1 ? '' : 's'} found`
  } catch (error) {
    message.value = error instanceof Error ? error.message : 'Discovery failed'
    emit('error', message.value)
  } finally { busy.value = false }
}
const passiveScan = async () => {
  try { await Promise.all([loadInterfaces(), loadStatus()]); await scan(['mdns']) }
  catch (error) { message.value = error instanceof Error ? error.message : 'Discovery unavailable'; emit('error', message.value) }
}
const scanSelectedLan = async () => {
  if (!selectedCidr.value) return
  if (!window.confirm(`Probe up to 1,024 hosts on ${selectedCidr.value}? This is a private-LAN TCP probe and will not call adb connect.`)) return
  await scan(['mdns', 'probe'], true)
}
const enroll = async (candidate: Candidate, action: 'connect' | 'pair') => {
  if (action === 'pair') {
    pairingCandidate.value = candidate.candidate_id
    return
  }
  await doEnroll(candidate.candidate_id, 'connect')
}
const doEnroll = async (candidateId: string, action: 'connect' | 'pair') => {
  busy.value = true
  message.value = ''
  const code = action === 'pair' ? pairingCode.value : undefined
  try {
    // The code is sent once in the request body, never logged, rendered, or persisted.
    const body = await request('/api/phone/discovery/enroll', { method: 'POST', body: JSON.stringify({ candidate_id: candidateId, action, ...(code ? { pairing_code: code } : {}) }) })
    pairingCode.value = ''
    pairingCandidate.value = ''
    message.value = 'Device enrolled; refreshing devices'
    emit('enrolled', body)
    await loadStatus()
  } catch (error) {
    pairingCode.value = ''
    message.value = error instanceof Error ? error.message : 'Enrollment failed'
    emit('error', message.value)
  } finally { busy.value = false }
}
const submitPair = async (candidate: Candidate) => doEnroll(candidate.candidate_id, 'pair')
const ownerInstruction = computed(() => status.value.usb.some((row) => row.state === 'unauthorized')
  ? 'Unlock the phone and accept the USB debugging trust prompt for this computer, then refresh.'
  : status.value.usb.some((row) => row.state === 'offline')
    ? 'Reconnect the USB cable and keep the phone awake, then refresh.' : '')

onMounted(() => { if (props.autoPassive) void passiveScan() })
</script>

<template>
  <section class="device-discovery-panel" aria-labelledby="device-discovery-title">
    <div class="discovery-heading">
      <div><p class="eyebrow">PHONE DISCOVERY</p><h2 id="device-discovery-title">Find Android devices on this computer</h2></div>
      <button class="discovery-button" :disabled="busy" @click="passiveScan">Refresh</button>
    </div>
    <p class="discovery-note">Passive USB and Android mDNS discovery runs first. LAN probing is opt-in, private-network only, and never runs <code>adb connect</code> during scanning.</p>

    <div v-if="ownerInstruction" class="discovery-warning" role="status"><strong>USB action needed.</strong> {{ ownerInstruction }}</div>
    <div class="discovery-status" role="status">
      <span>USB: {{ status.usb.length ? status.usb.map((row) => row.state).join(', ') : 'none reported' }}</span>
      <span>mDNS: {{ status.mdns.diagnostic }}</span>
    </div>

    <div class="discovery-lan-controls">
      <label>Local interface
        <select v-model="selectedInterface" @change="selectedCidr = interfaces.find((item) => item.name === selectedInterface)?.cidr ?? ''">
          <option value="">Choose an interface</option><option v-for="item in interfaces" :key="`${item.name}-${item.address}`" :value="item.name">{{ item.name }} · {{ item.address }} · {{ item.cidr }}</option>
        </select>
      </label>
      <button class="discovery-button primary" :disabled="busy || !selectedCidr" @click="scanSelectedLan">Scan selected LAN</button>
    </div>

    <p v-if="message" class="discovery-message" role="status">{{ message }}</p>
    <div v-if="candidates.length" class="candidate-list">
      <article v-for="candidate in candidates" :key="candidate.candidate_id" class="candidate-card">
        <div><strong>{{ candidate.host }}:{{ candidate.port }}</strong><small>{{ candidate.source }} · {{ candidate.service }} · expires {{ candidate.expires_at }}</small></div>
        <div class="candidate-actions">
          <button v-if="!candidate.pairing_required" class="discovery-button primary" :disabled="busy" @click="enroll(candidate, 'connect')">Connect</button>
          <button v-else class="discovery-button" :disabled="busy" @click="enroll(candidate, 'pair')">Pair &amp; connect</button>
        </div>
        <form v-if="pairingCandidate === candidate.candidate_id" class="pairing-form" @submit.prevent="submitPair(candidate)">
          <label>Wireless Debugging code <input v-model="pairingCode" inputmode="numeric" autocomplete="one-time-code" maxlength="64" required /></label>
          <button class="discovery-button primary" :disabled="busy || !pairingCode">Confirm pairing</button>
        </form>
      </article>
    </div>
    <p v-else class="discovery-empty">No candidates yet. Keep Wireless debugging enabled on the phone, or choose a local interface for an explicit scan.</p>
  </section>
</template>

<style scoped>
.device-discovery-panel { padding: 1.25rem; border: 1px solid var(--border, #2f3542); border-radius: 14px; background: var(--bg-panel, #161a22); color: var(--text-1, #f4f6fb); }
.discovery-heading, .discovery-lan-controls, .candidate-card, .candidate-actions, .discovery-status { display: flex; align-items: center; gap: .75rem; }
.discovery-heading { justify-content: space-between; } .eyebrow { margin: 0; font-size: .68rem; letter-spacing: .12em; color: #85a7ff; } h2 { margin: .2rem 0 0; font-size: 1.12rem; }
.discovery-note, .discovery-empty, .discovery-message { color: var(--text-2, #aeb6c7); font-size: .84rem; line-height: 1.45; } code { color: #b9cbff; }
.discovery-status { justify-content: space-between; flex-wrap: wrap; margin: .9rem 0; padding: .65rem .8rem; border-radius: 8px; background: var(--bg-deep, #0e1117); font-size: .76rem; color: var(--text-2, #aeb6c7); }
.discovery-lan-controls { flex-wrap: wrap; align-items: end; } label { display: grid; gap: .3rem; font-size: .76rem; color: var(--text-2, #aeb6c7); } select, input { min-height: 2rem; min-width: 15rem; padding: .35rem .5rem; color: inherit; background: var(--bg-deep, #0e1117); border: 1px solid var(--border, #2f3542); border-radius: 6px; }
.discovery-button { padding: .45rem .75rem; border: 1px solid var(--border, #394150); border-radius: 7px; color: inherit; background: transparent; cursor: pointer; font-size: .78rem; white-space: nowrap; } .discovery-button.primary { color: #fff; border-color: #4f7cff; background: #315ee8; } .discovery-button:disabled { opacity: .5; cursor: wait; }
.discovery-warning { margin: .8rem 0; padding: .7rem .8rem; color: #f6d58c; background: #342916; border: 1px solid #6a5120; border-radius: 8px; font-size: .8rem; }
.candidate-list { display: grid; gap: .6rem; margin-top: 1rem; } .candidate-card { flex-wrap: wrap; justify-content: space-between; padding: .75rem; border: 1px solid var(--border, #2f3542); border-radius: 9px; } .candidate-card small { display: block; margin-top: .25rem; color: var(--text-2, #aeb6c7); font-size: .72rem; } .pairing-form { display: flex; align-items: end; gap: .5rem; width: 100%; }
@media (max-width: 620px) { .discovery-heading, .candidate-card { align-items: flex-start; flex-direction: column; } .discovery-lan-controls, .pairing-form { align-items: stretch; flex-direction: column; } select, input { min-width: 0; width: 100%; } }
</style>
