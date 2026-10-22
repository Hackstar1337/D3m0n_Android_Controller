<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
// compact: one-line strip for the Live grid status bar (same data, same 5 s refresh).
const props = defineProps<{ visible: boolean; compact?: boolean }>()
const data = ref<any>(null), error = ref(''), updated = ref('')
let timer: ReturnType<typeof setInterval> | null = null, busy = false, alive = true
type StatusPayload = { streams: any[]; viewers: any[] }
function normalizeStatusPayload(value: unknown): StatusPayload | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Record<string, unknown>
  if (!Array.isArray(candidate.streams) || !Array.isArray(candidate.viewers)) return null
  return { streams: candidate.streams, viewers: candidate.viewers }
}
function summarizeStatus(value: unknown) {
  const payload = normalizeStatusPayload(value)
  if (!payload) return null
  const streams = payload.streams
  const viewers = payload.viewers
  const current = viewers.filter((viewer: any) => {
    const metrics = viewer && typeof viewer === 'object' ? viewer.events?.metrics : null
    return metrics && typeof metrics === 'object' && Number.isFinite(Number(metrics.at)) && Number(metrics.at) > Date.now() / 1000 - 20
  })
  const queued = current.map((viewer: any) => {
    const detail = viewer.events.metrics.detail
    return detail && typeof detail === 'object' ? Number(detail.queue) || 0 : 0
  })
  const streamErrors = streams.filter((stream: any) => stream && typeof stream === 'object' && stream.error).length
  const viewerErrors = viewers.filter((viewer: any) => viewer && typeof viewer === 'object' && (viewer.events?.['decoder-error'] || viewer.events?.['decode-failure'])).length
  return { encoders: streams.filter((stream: any) => stream && typeof stream === 'object' && stream.running).length, viewers: viewers.length,
    errors: streamErrors + viewerErrors,
    idle: streams.filter((stream: any) => stream && typeof stream === 'object' && stream.running && !stream.error && Number(stream.frames) > 0 && Number(stream.last_frame_age) > 2).length,
    maxQueue: Math.max(0, ...queued), kbps: current.reduce((sum: number, viewer: any) => {
      const detail = viewer.events.metrics.detail
      return sum + (detail && typeof detail === 'object' ? Number(detail.kbps) || 0 : 0)
    }, 0) }
}
async function refresh() {
  if (busy || !alive || !props.visible || document.hidden) return
  busy = true
  try {
    const response = await fetch('/api/phone/android-h264/status', { signal: AbortSignal.timeout(7000), cache: 'no-store' })
    if (!response.ok) throw new Error(`Status API ${response.status}`)
    const result = await response.json()
    const normalized = normalizeStatusPayload(result)
    if (!normalized) throw new Error('Status response was invalid')
    if (alive) { data.value = normalized; error.value = ''; updated.value = new Date().toLocaleTimeString() }
  } catch (e) { if (alive) error.value = e instanceof Error ? e.message : 'Status unavailable' }
  finally { busy = false }
}
const summary = computed(() => {
  return summarizeStatus(data.value) || { encoders: 0, viewers: 0, errors: 0, idle: 0, maxQueue: 0, kbps: 0 }
})
const state = computed(() => error.value ? 'error' : data.value ? 'ok' : 'pending')
watch(() => props.visible, value => { if (value) void refresh() })
onMounted(() => { void refresh(); timer = setInterval(refresh, 5000) })
onUnmounted(() => { alive = false; if (timer) clearInterval(timer) })
</script>
<template>
  <div v-if="compact" class="overview-inline" :class="`is-${state}`" role="status" aria-label="H.264 fleet monitor"
    :title="(error || 'Unchanged screens can show 0 FPS. Decode latency is browser processing time, not end-to-end network latency.') + (updated ? ' · updated ' + updated : '')">
    <span class="oi-title"><i aria-hidden="true"></i>H.264</span>
    <template v-if="data">
      <span class="oi-kpi" title="Shared encoders"><b>{{ summary.encoders }}</b> enc</span>
      <span class="oi-kpi oi-kpi--viewers" title="Viewers"><b>{{ summary.viewers }}</b> viewers</span>
      <span class="oi-kpi" :class="{ warning: summary.errors }" title="Stream errors"><b>{{ summary.errors }}</b> errors</span>
      <span class="oi-kpi oi-kpi--minor" title="Unchanged screens (can show 0 FPS)"><b>{{ summary.idle }}</b> unchanged</span>
      <span class="oi-kpi oi-kpi--minor" :class="{ warning: summary.maxQueue > 4 }" title="Max decode queue"><b>{{ summary.maxQueue }}</b> queue</span>
      <span class="oi-kpi oi-kpi--kbps" title="kbps received by reporting viewers"><b>{{ summary.kbps }}</b> kbps</span>
    </template>
    <span v-else class="oi-kpi">{{ error ? 'Metrics unavailable' : 'Loading…' }}</span>
    <button class="oi-refresh" :disabled="busy" @click="refresh" aria-label="Refresh stream metrics" title="Refresh metrics"><UiIcon name="refresh" :size="11" /></button>
  </div>
  <aside v-else class="stream-overview" :class="`is-${state}`" aria-label="Live streaming overview">
    <div class="overview-title">
      <span class="overview-icon"><UiIcon name="activity" :size="15" /></span>
      <div class="overview-heading">
        <strong>H.264 fleet monitor</strong>
        <span class="overview-state"><i aria-hidden="true"></i>{{ error ? 'Status unavailable' : data ? 'Connected' : 'Checking' }}<template v-if="updated"> · {{ updated }}</template></span>
      </div>
    </div>
    <div v-if="data" class="overview-stats">
      <span class="kpi"><b>{{ summary.encoders }}</b><small>shared encoders</small></span>
      <span class="kpi"><b>{{ summary.viewers }}</b><small>viewers</small></span>
      <span class="kpi" :class="{ warning: summary.errors }"><b>{{ summary.errors }}</b><small>stream errors</small></span>
      <span class="kpi"><b>{{ summary.idle }}</b><small>unchanged screens</small></span>
      <span class="kpi" :class="{ warning: summary.maxQueue > 4 }"><b>{{ summary.maxQueue }}</b><small>max decode queue</small></span>
      <span class="kpi"><b>{{ summary.kbps }}</b><small>kbps received by reporting viewers</small></span>
    </div>
    <div v-else class="overview-stats overview-stats--empty">{{ error ? 'Metrics are temporarily unavailable.' : 'Loading stream metrics…' }}</div>
    <button class="overview-refresh" :disabled="busy" @click="refresh" aria-label="Refresh stream metrics" title="Refresh metrics"><UiIcon name="refresh" :size="13" /><span>Refresh metrics</span></button>
    <p class="overview-note">{{ error || 'Unchanged screens can show 0 FPS. Decode latency is browser processing time, not end-to-end network latency.' }}</p>
  </aside>
</template>
<style scoped>
.stream-overview {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; column-gap: 20px; row-gap: 6px;
  margin: 0 0 12px; padding: 12px 16px; color: var(--ui-text-2);
  background: var(--ui-surface-1); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg);
}
.overview-title { display: flex; align-items: center; gap: 10px; min-width: 210px; }
.overview-icon { display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: var(--ui-radius-sm); background: var(--ui-accent-soft); color: var(--ui-accent); border: 1px solid var(--ui-accent-line); }
.overview-heading { display: grid; gap: 2px; line-height: 1.2; }
.overview-heading strong { color: var(--ui-text-1); font-size: var(--ui-fs-sm); font-weight: 650; }
.overview-state { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }
.overview-state i { width: 6px; height: 6px; border-radius: 50%; background: var(--ui-text-4); }
.is-ok .overview-state i { background: var(--ui-success); box-shadow: 0 0 0 3px rgba(70, 207, 139, 0.14); }
.is-error .overview-state i { background: var(--ui-danger); }
.is-error .overview-icon { background: var(--ui-danger-soft); color: #ffb0b0; border-color: rgba(242, 109, 109, 0.4); }
.overview-stats { display: flex; align-items: stretch; flex-wrap: wrap; min-width: 0; }
.overview-stats--empty { color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.kpi { display: grid; gap: 1px; padding: 0 18px; border-left: 1px solid var(--ui-border); min-width: 0; }
.kpi:first-child { border-left: 0; padding-left: 0; }
.kpi b { color: var(--ui-text-1); font-size: 17px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.25; }
.kpi small { color: var(--ui-text-3); font-size: var(--ui-fs-2xs); white-space: nowrap; }
.kpi.warning b { color: var(--ui-warn); }
.overview-refresh { display: inline-flex; align-items: center; gap: 6px; height: var(--ui-control-h-sm); padding: 0 10px; border-radius: var(--ui-radius-sm); border: 1px solid var(--ui-border-strong); background: var(--ui-surface-3); color: var(--ui-text-2); font-size: var(--ui-fs-2xs); font-weight: 600; cursor: pointer; }
.overview-refresh:hover:not(:disabled) { background: var(--ui-surface-hover); color: var(--ui-text-1); }
.overview-refresh:disabled { opacity: 0.5; }
.overview-note { grid-column: 1 / -1; margin: 2px 0 0; color: var(--ui-text-4); font-size: var(--ui-fs-2xs); line-height: 1.5; }
/* Compact strip */
.overview-inline { display: inline-flex; align-items: center; gap: 10px; min-width: 0; height: 22px; padding: 0 4px 0 9px; border: 1px solid var(--ui-border); border-radius: 999px; background: var(--ui-surface-2); color: var(--ui-text-3); font-size: var(--ui-fs-2xs); white-space: nowrap; }
.oi-title { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-text-2); font-weight: 650; letter-spacing: .02em; }
.oi-title i { width: 6px; height: 6px; border-radius: 50%; background: var(--ui-text-4); }
.overview-inline.is-ok .oi-title i { background: var(--ui-success); box-shadow: 0 0 0 3px rgba(70, 207, 139, 0.14); }
.overview-inline.is-error .oi-title i { background: var(--ui-danger); }
.oi-kpi b { color: var(--ui-text-1); font-weight: 650; font-variant-numeric: tabular-nums; }
.oi-kpi.warning b { color: var(--ui-warn); }
.oi-refresh { display: inline-grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 50%; background: none; color: var(--ui-text-3); cursor: pointer; }
.oi-refresh:hover:not(:disabled) { background: var(--ui-surface-hover); color: var(--ui-text-1); }
.oi-refresh:focus-visible { outline: 2px solid var(--ui-border-focus); outline-offset: 1px; }
.oi-refresh:disabled { opacity: .5; }
@container gridhdr (max-width: 1700px) { .oi-kpi--minor { display: none; } }
@container gridhdr (max-width: 980px) { .oi-kpi--viewers, .oi-kpi--kbps { display: none; } }
@media (max-width: 1440px) {
  .kpi { padding: 0 12px; }
  .overview-refresh span { display: none; }
  .overview-refresh { width: var(--ui-control-h-sm); padding: 0; justify-content: center; }
}
@media (max-width: 1280px) {
  .stream-overview { grid-template-columns: minmax(0, 1fr) auto; }
  .overview-stats { grid-column: 1 / -1; grid-row: 2; }
}
</style>
