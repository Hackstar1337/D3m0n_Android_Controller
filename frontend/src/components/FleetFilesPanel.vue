<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

type StagedFile = {
  id: string
  name: string
  size: number
  is_apk: boolean
  created_at?: string
}

type DeviceResult = {
  device: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | string
  ok?: boolean
  error?: string
  bytes?: number
  duration_ms?: number
  download_id?: string
  filename?: string
}

type Job = {
  id: string
  operation: 'push' | 'pull' | 'install' | string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | string
  devices: string[]
  created_at?: string
  updated_at?: string
  progress: { done: number; total: number }
  results: DeviceResult[]
  file_id?: string
  filename?: string
  remote_path?: string
}

type JobResponse = { ok?: boolean; job?: Job }

type UploadResponse = { ok?: boolean; file?: StagedFile; detail?: unknown; error?: unknown; message?: unknown }

const props = defineProps<{
  selectedDevices: string[]
  visible: boolean
}>()

const fileInput = ref<HTMLInputElement | null>(null)
const stagedFile = ref<StagedFile | null>(null)
const uploading = ref(false)
const uploadProgress = ref(0)
const uploadError = ref('')
const operationError = ref('')
const jobsError = ref('')
const jobs = ref<Job[]>([])
const destinationFolder = ref('/sdcard/Download/D3m0n')
const phoneFilePath = ref('')
const trustApk = ref(false)
const replaceExisting = ref(false)
const startingOperation = ref('')
const cancellingJobId = ref('')
const jobsLoading = ref(false)

let alive = true
let pollTimer: ReturnType<typeof setInterval> | null = null
let jobsController: AbortController | null = null
let uploadRequest: XMLHttpRequest | null = null

const selectedCount = computed(() => props.selectedDevices.length)
const hasTarget = computed(() => selectedCount.value > 0)
const canPush = computed(() => hasTarget.value && Boolean(stagedFile.value) && !uploading.value && !startingOperation.value)
const canPull = computed(() => hasTarget.value && Boolean(phoneFilePath.value.trim()) && !startingOperation.value)
const canInstall = computed(() => hasTarget.value && Boolean(stagedFile.value?.is_apk) && trustApk.value && !uploading.value && !startingOperation.value)

function errorText(payload: any, fallback: string): string {
  const detail = payload?.detail ?? payload?.error ?? payload?.message
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    return detail.map(item => typeof item === 'string' ? item : item?.msg || JSON.stringify(item)).join('; ')
  }
  return fallback
}

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '0 B'
  if (size < 1024) return `${Math.round(size)} B`
  const units = ['KiB', 'MiB', 'GiB']
  let value = size / 1024
  let unit = units[0]
  for (let index = 0; value >= 1024 && index < units.length - 1; index += 1) {
    value /= 1024
    unit = units[index + 1]
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${unit}`
}

async function fetchJson(path: string, init?: RequestInit): Promise<any> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error(error instanceof Error ? `Network error: ${error.message}` : 'Network error while contacting the fleet service')
  }
  const raw = await response.text()
  let payload: any = null
  if (raw) {
    try { payload = JSON.parse(raw) } catch { payload = null }
  }
  if (!response.ok || payload?.ok === false) {
    throw new Error(errorText(payload, `Fleet service returned HTTP ${response.status}`))
  }
  return payload
}

function clearInputSelection() {
  if (fileInput.value) fileInput.value.value = ''
}

async function clearStagedFile() {
  if (stagedFile.value) {
    try { await fetchJson(`/api/fleet/files/${encodeURIComponent(stagedFile.value.id)}`, { method: 'DELETE' }) }
    catch (error) { uploadError.value = error instanceof Error ? error.message : 'Could not clear staged copy'; return }
  }
  stagedFile.value = null
  trustApk.value = false
  replaceExisting.value = false
  uploadError.value = ''
  clearInputSelection()
}

function uploadSelectedFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 256 * 1024 * 1024) {
    uploadError.value = 'Maximum file size is 256 MiB.'
    clearInputSelection()
    return
  }

  uploadError.value = ''
  operationError.value = ''
  stagedFile.value = null
  trustApk.value = false
  replaceExisting.value = false
  uploadProgress.value = 0
  uploading.value = true

  const request = new XMLHttpRequest()
  uploadRequest = request
  request.open('POST', '/api/fleet/files')
  request.responseType = 'text'
  request.upload.addEventListener('progress', (progressEvent) => {
    if (!alive || !progressEvent.lengthComputable) return
    uploadProgress.value = Math.min(100, Math.round((progressEvent.loaded / progressEvent.total) * 100))
  })
  request.addEventListener('load', () => {
    if (!alive || uploadRequest !== request) return
    let payload: UploadResponse | null = null
    try { payload = request.responseText ? JSON.parse(request.responseText) : null } catch { payload = null }
    if (request.status >= 200 && request.status < 300 && payload?.ok !== false && payload?.file) {
      stagedFile.value = payload.file
      uploadProgress.value = 100
    } else {
      uploadError.value = errorText(payload, `Upload failed (HTTP ${request.status || 'unknown'})`)
      uploadProgress.value = 0
    }
    uploading.value = false
    uploadRequest = null
  })
  request.addEventListener('error', () => {
    if (!alive || uploadRequest !== request) return
    uploadError.value = 'Upload failed because the fleet service could not be reached.'
    uploading.value = false
    uploadRequest = null
    uploadProgress.value = 0
  })
  request.addEventListener('abort', () => {
    if (!alive || uploadRequest !== request) return
    uploading.value = false
    uploadRequest = null
    uploadProgress.value = 0
  })
  const formData = new FormData()
  formData.append('file', file, file.name)
  request.send(formData)
}

function targetSnapshot(): string[] | null {
  const devices = [...props.selectedDevices]
  if (!devices.length) {
    operationError.value = 'Select at least one device before starting a file operation.'
    return null
  }
  return devices
}

async function startJob(operation: 'push' | 'pull' | 'install') {
  if (startingOperation.value) return
  const devices = targetSnapshot()
  if (!devices) return
  operationError.value = ''

  if ((operation === 'push' || operation === 'install') && !stagedFile.value) {
    operationError.value = 'Upload a file first. Uploading only stages a server copy.'
    return
  }
  if (operation === 'install' && (!stagedFile.value?.is_apk || !trustApk.value)) {
    operationError.value = 'Confirm that you trust this APK before installing it.'
    return
  }
  if (operation === 'pull' && !phoneFilePath.value.trim()) {
    operationError.value = 'Enter the full phone file path to download.'
    return
  }

  startingOperation.value = operation
  const body: Record<string, unknown> = {
    targets: { devices, group_ids: [], all: false },
    operation,
    replace: operation === 'install' ? replaceExisting.value : false,
  }
  if (operation === 'push') {
    body.file_id = stagedFile.value?.id
    body.remote_path = destinationFolder.value.trim()
  } else if (operation === 'pull') {
    body.remote_path = phoneFilePath.value.trim()
  } else {
    body.file_id = stagedFile.value?.id
    body.confirm_install = true
  }

  try {
    const payload = await fetchJson('/api/fleet/file-jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }) as JobResponse
    if (!payload?.job) throw new Error('The fleet service did not return a job.')
    jobs.value = [payload.job, ...jobs.value.filter(job => job.id !== payload.job?.id)].slice(0, 20)
    await refreshJobs()
  } catch (error) {
    operationError.value = error instanceof Error ? error.message : 'Could not start the file operation.'
  } finally {
    startingOperation.value = ''
  }
}

async function refreshJobs() {
  if (!alive || !props.visible || jobsLoading.value) return
  jobsLoading.value = true
  jobsError.value = ''
  const controller = new AbortController()
  jobsController?.abort()
  jobsController = controller
  try {
    const payload = await fetchJson('/api/fleet/jobs', { signal: controller.signal })
    if (!alive || controller.signal.aborted) return
    jobs.value = Array.isArray(payload?.jobs) ? payload.jobs.slice(0, 20) : []
  } catch (error) {
    if (!alive || controller.signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) return
    jobsError.value = error instanceof Error ? error.message : 'Could not load file jobs.'
  } finally {
    if (jobsController === controller) jobsController = null
    jobsLoading.value = false
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  jobsController?.abort()
  jobsController = null
}

function startPolling() {
  stopPolling()
  if (!props.visible || !alive) return
  void refreshJobs()
  pollTimer = setInterval(() => { void refreshJobs() }, 2000)
}

async function cancelJob(job: Job) {
  if (cancellingJobId.value || !['queued', 'running'].includes(job.status)) return
  cancellingJobId.value = job.id
  jobsError.value = ''
  try {
    const payload = await fetchJson(`/api/fleet/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST' }) as JobResponse
    if (payload?.job) jobs.value = jobs.value.map(item => item.id === job.id ? payload.job as Job : item)
    await refreshJobs()
  } catch (error) {
    jobsError.value = error instanceof Error ? error.message : 'Could not cancel this job.'
  } finally {
    cancellingJobId.value = ''
  }
}

function resultFor(job: Job, device: string): DeviceResult | undefined {
  return job.results?.find(result => result.device === device)
}

function downloadUrl(result: DeviceResult | undefined): string {
  return result?.download_id ? `/api/fleet/downloads/${encodeURIComponent(result.download_id)}` : '#'
}

function jobStatusLabel(status: string): string {
  return status === 'interrupted' ? 'interrupted after restart' : status
}

watch(() => props.visible, (visible) => {
  if (visible) startPolling()
  else stopPolling()
})

onMounted(() => {
  if (props.visible) startPolling()
})

onUnmounted(() => {
  alive = false
  stopPolling()
  if (uploadRequest) uploadRequest.abort()
  uploadRequest = null
})
</script>

<template>
  <section v-if="visible" class="fleet-files" aria-labelledby="fleet-files-title">
    <div class="panel-header">
      <div>
        <p class="eyebrow">Stage 2</p>
        <h2 id="fleet-files-title">Files &amp; APKs</h2>
      </div>
      <span class="target-count" aria-live="polite">{{ selectedCount }} selected</span>
    </div>

    <p class="notice">
      Files are staged in a private server cache. Uploading does not copy a file to a phone,
      install an APK, or launch an app.
    </p>

    <div class="file-grid">
    <div class="file-card">
      <h3>Stage a file</h3>
      <label class="file-picker" for="fleet-file-input">Choose file to upload</label>
      <input id="fleet-file-input" ref="fileInput" type="file" class="native-file-input" @change="uploadSelectedFile" />
      <progress v-if="uploading" class="upload-progress" max="100" :value="uploadProgress">{{ uploadProgress }}%</progress>
      <p v-if="uploading" class="muted" aria-live="polite">Uploading to the private server cache: {{ uploadProgress }}%</p>
      <p v-if="uploadError" class="error" role="alert">{{ uploadError }}</p>

      <div v-if="stagedFile" class="staged-file" aria-live="polite">
        <div>
          <strong>{{ stagedFile.name }}</strong>
          <span>{{ formatBytes(stagedFile.size) }}<template v-if="stagedFile.is_apk"> · standalone APK</template><template v-else> · file</template></span>
        </div>
        <button type="button" class="quiet-button" @click="clearStagedFile">Clear selection</button>
      </div>
      <p v-if="stagedFile?.is_apk" class="caveat">
        APK detection only checks the uploaded archive for an Android manifest; it does not verify its signature or trustworthiness.
        Only standalone <code>.apk</code> files are supported; split packages such as <code>.apks</code> and <code>.xapk</code> are not.
      </p>
    </div>

    <div class="file-card">
      <h3>Push to selected phones</h3>
      <label for="destination-folder">Destination folder on phones</label>
      <input id="destination-folder" v-model="destinationFolder" type="text" autocomplete="off" spellcheck="false" />
      <p class="hint">The uploaded file is copied into this folder using its staged filename.</p>
      <button type="button" :disabled="!canPush" @click="startJob('push')">
        {{ startingOperation === 'push' ? 'Starting push…' : 'Push file to selected devices' }}
      </button>
    </div>

    <div class="file-card">
      <h3>Pull from selected phones</h3>
      <label for="phone-file-path">Phone file path to download</label>
      <input id="phone-file-path" v-model="phoneFilePath" type="text" autocomplete="off" spellcheck="false" placeholder="/sdcard/Download/report.pdf" />
      <p class="hint">Enter the full path to one regular file in shared storage, for example <code>/sdcard/Download/report.pdf</code>.</p>
      <button type="button" :disabled="!canPull" @click="startJob('pull')">
        {{ startingOperation === 'pull' ? 'Starting download…' : 'Start download' }}
      </button>
    </div>

    <div v-if="stagedFile?.is_apk" class="file-card apk-card">
      <h3>Install APK</h3>
      <label class="checkbox-label">
        <input v-model="trustApk" type="checkbox" />
        <span>I trust this APK and want to install it on selected devices</span>
      </label>
      <label class="checkbox-label secondary-check">
        <input v-model="replaceExisting" type="checkbox" />
        <span>Replace existing app</span>
      </label>
      <p class="hint">Installation is never automatic and does not launch the app. Replace is off by default.</p>
      <button type="button" :disabled="!canInstall" @click="startJob('install')">
        {{ startingOperation === 'install' ? 'Starting installation…' : 'Install APK on selected devices' }}
      </button>
    </div>
    </div>

    <p v-if="operationError" class="error" role="alert">{{ operationError }}</p>

    <div class="jobs-section">
      <div class="jobs-heading">
        <h3>Recent file jobs</h3>
        <span v-if="jobsLoading" class="muted">Refreshing…</span>
      </div>
      <p v-if="jobsError" class="error" role="alert">{{ jobsError }}</p>
      <p v-if="!jobsLoading && !jobs.length && !jobsError" class="muted">No file jobs yet.</p>

      <div class="job-grid">
      <article v-for="job in jobs" :key="job.id" class="job-card">
        <div class="job-title-row">
          <div>
            <strong>{{ job.operation }}</strong>
            <span class="job-id">{{ job.id }}</span>
          </div>
          <span class="status" :class="`status-${job.status}`">{{ jobStatusLabel(job.status) }}</span>
        </div>
        <div class="job-meta">
          <span>{{ job.progress?.done ?? 0 }}/{{ job.progress?.total ?? job.devices.length }} devices complete</span>
          <span v-if="job.filename">{{ job.filename }}</span>
          <span v-if="job.remote_path">{{ job.remote_path }}</span>
        </div>
        <ul class="device-results" aria-label="Per-device job results">
          <li v-for="device in job.devices" :key="`${job.id}-${device}`">
            <span class="device-name">{{ device }}</span>
            <span class="device-result-status">{{ resultFor(job, device)?.status || 'queued' }}</span>
            <a
              v-if="resultFor(job, device)?.download_id"
              class="download-link"
              :href="downloadUrl(resultFor(job, device))"
              :download="resultFor(job, device)?.filename || undefined"
            >Download{{ resultFor(job, device)?.filename ? ` ${resultFor(job, device)?.filename}` : '' }}</a>
            <span v-if="resultFor(job, device)?.error" class="device-error">{{ resultFor(job, device)?.error }}</span>
          </li>
        </ul>
        <p v-if="job.status === 'cancelled'" class="hint">Cancellation cannot undo transfers or installations already completed.</p>
        <button
          v-if="job.status === 'queued' || job.status === 'running'"
          type="button"
          class="cancel-button"
          :disabled="cancellingJobId === job.id"
          @click="cancelJob(job)"
        >
          {{ cancellingJobId === job.id ? 'Cancelling…' : 'Cancel job' }}
        </button>
      </article>
      </div>
    </div>
  </section>
</template>

<style scoped>
:global(*) { box-sizing: border-box; }
/* Files & APKs — desktop redesign (class names unchanged). */
.fleet-files {
  width: 100%;
  margin: 0;
  padding: 18px;
  color: var(--ui-text-2);
  background: var(--ui-surface-1);
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-lg);
  font: var(--ui-fs-sm)/1.5 var(--ui-font);
}
.panel-header, .job-title-row, .jobs-heading, .staged-file, .job-meta { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.panel-header { margin-bottom: 12px; }
.eyebrow { margin: 0; color: var(--ui-accent); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.1em; text-transform: uppercase; }
h2, h3, p { margin-top: 0; }
h2 { margin: 3px 0 0; color: var(--ui-text-1); font-size: var(--ui-fs-lg); font-weight: 650; }
h3 { margin-bottom: 10px; color: var(--ui-text-1); font-size: var(--ui-fs-md); font-weight: 650; }
.target-count { display: inline-flex; align-items: center; height: 26px; padding: 0 11px; border-radius: 999px; border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-1); font-size: var(--ui-fs-xs); font-weight: 600; white-space: nowrap; }
.notice, .hint, .muted, .caveat { color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.notice { margin: 0 0 14px; padding: 10px 12px; background: var(--ui-info-soft); border: 1px solid rgba(103, 170, 255, 0.22); border-left: 3px solid var(--ui-info); border-radius: var(--ui-radius-sm); color: #c5dcff; }
.file-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; align-items: stretch; }
.file-card, .job-card { margin: 0; padding: 14px; background: var(--ui-surface-2); border: 1px solid var(--ui-border); border-radius: var(--ui-radius); }
.file-card { display: flex; flex-direction: column; min-width: 0; }
.file-card > button { margin-top: auto; }
.file-card > .hint + button, .file-card > input + button { margin-top: 12px; }
.file-card > :last-child, .job-card > :last-child { margin-bottom: 0; }
.apk-card { border-color: rgba(240, 180, 76, 0.35); }
label { display: block; margin-bottom: 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-weight: 650; letter-spacing: 0.04em; text-transform: uppercase; }
input[type="text"] { width: 100%; height: var(--ui-control-h); padding: 0 11px; color: var(--ui-text-1); background: var(--ui-surface-sunken); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); font: inherit; font-family: var(--ui-mono); font-size: var(--ui-fs-xs); }
input[type="text"]:focus { outline: none; border-color: var(--ui-accent); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
input[type="text"]:focus-visible, button:focus-visible, .file-picker:focus-visible, input[type="checkbox"]:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.native-file-input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; clip-path: inset(50%); }
.file-picker, button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: var(--ui-control-h); padding: 0 14px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-sm); font: inherit; font-size: var(--ui-fs-xs); font-weight: 600; cursor: pointer; white-space: nowrap; transition: background-color 0.15s, border-color 0.15s; }
.file-picker { width: 100%; margin: 0; text-transform: none; letter-spacing: 0; font-size: var(--ui-fs-xs); border-style: dashed; color: var(--ui-text-1); background: var(--ui-surface-sunken); height: 56px; }
.file-picker:hover { border-color: var(--ui-accent); background: var(--ui-accent-soft); }
button:not(.quiet-button):not(.cancel-button) { color: var(--ui-on-accent); background: var(--ui-accent); border-color: var(--ui-accent); }
button:not(:disabled):not(.quiet-button):not(.cancel-button):hover { background: var(--ui-accent-hover); border-color: var(--ui-accent-hover); }
button { width: 100%; margin-top: 12px; }
button:disabled, button:not(.quiet-button):not(.cancel-button):disabled { color: var(--ui-text-4); background: var(--ui-surface-3); border-color: var(--ui-border); cursor: not-allowed; }
.quiet-button { width: auto; margin: 0; height: var(--ui-control-h-sm); padding: 0 10px; color: var(--ui-text-1); background: var(--ui-surface-3); border-color: var(--ui-border-strong); font-size: var(--ui-fs-2xs); }
.quiet-button:hover { background: var(--ui-surface-hover); }
.cancel-button { width: auto; height: var(--ui-control-h-sm); color: #ffb8b8; background: var(--ui-danger-soft); border-color: rgba(242, 109, 109, 0.45); font-size: var(--ui-fs-2xs); }
.upload-progress { display: block; width: 100%; height: 6px; margin-top: 10px; accent-color: var(--ui-accent); }
.staged-file { margin-top: 10px; padding: 9px 11px; background: var(--ui-surface-sunken); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); }
.staged-file strong, .staged-file span { display: block; overflow-wrap: anywhere; }
.staged-file strong { color: var(--ui-text-1); font-size: var(--ui-fs-sm); }
.staged-file span { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.caveat { margin-top: 10px; margin-bottom: 0; color: #f8d08a; }
.checkbox-label { display: flex; align-items: flex-start; gap: 9px; color: var(--ui-text-1); font-size: var(--ui-fs-xs); font-weight: 600; letter-spacing: 0; text-transform: none; }
.checkbox-label input { flex: 0 0 auto; width: 15px; height: 15px; margin-top: 1px; accent-color: var(--ui-accent); }
.secondary-check { margin-top: 10px; font-weight: 500; color: var(--ui-text-2); }
.hint { margin: 8px 0 0; line-height: 1.5; }
.error { margin: 12px 0 0; padding: 9px 11px; color: #ffc4c4; background: var(--ui-danger-soft); border: 1px solid rgba(242, 109, 109, 0.4); border-radius: var(--ui-radius-sm); overflow-wrap: anywhere; font-size: var(--ui-fs-xs); }
.jobs-section { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--ui-border); }
.jobs-heading { margin-bottom: 10px; }
.jobs-heading h3 { margin: 0; }
.job-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 12px; }
.job-card { background: var(--ui-surface-2); }
.job-title-row { align-items: flex-start; }
.job-title-row strong { display: block; color: var(--ui-text-1); text-transform: capitalize; }
.job-id { display: block; max-width: 18rem; color: var(--ui-text-4); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); overflow-wrap: anywhere; }
.status { display: inline-flex; align-items: center; height: 22px; padding: 0 9px; border-radius: 999px; color: var(--ui-text-2); background: var(--ui-surface-3); border: 1px solid var(--ui-border-strong); font-size: var(--ui-fs-2xs); font-weight: 650; white-space: nowrap; }
.status-running { color: #f8d08a; background: var(--ui-warn-soft); border-color: rgba(240, 180, 76, 0.45); }
.status-failed, .status-interrupted { color: #ffb8b8; background: var(--ui-danger-soft); border-color: rgba(242, 109, 109, 0.45); }
.status-cancelled { color: #f8d08a; background: var(--ui-warn-soft); border-color: rgba(240, 180, 76, 0.35); }
.status-completed { color: #93e8bd; background: var(--ui-success-soft); border-color: rgba(70, 207, 139, 0.4); }
.job-meta { align-items: flex-start; flex-wrap: wrap; margin-top: 8px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); font-family: var(--ui-mono); }
.device-results { margin: 10px 0 0; padding: 0; list-style: none; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); overflow: hidden; }
.device-results li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 10px; padding: 6px 10px; border-top: 1px solid var(--ui-border); background: var(--ui-surface-sunken); }
.device-results li:first-child { border-top: 0; }
.device-name, .device-error { overflow-wrap: anywhere; }
.device-name { color: var(--ui-text-1); font-family: var(--ui-mono); font-size: var(--ui-fs-2xs); }
.device-result-status { color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; text-transform: capitalize; }
.device-error { grid-column: 1 / -1; color: #ffb0b0; font-size: var(--ui-fs-2xs); }
.download-link { grid-column: 1 / -1; color: var(--ui-accent); font-size: var(--ui-fs-xs); }
code { color: var(--ui-text-1); font-family: var(--ui-mono); font-size: 0.92em; overflow-wrap: anywhere; }
</style>
