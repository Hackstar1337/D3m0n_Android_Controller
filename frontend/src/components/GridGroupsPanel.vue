<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { selectNumberedPhones } from '@/composables/numberedSelection'

type SavedGroup = { id: string; name: string; devices: string[] }
const props = defineProps<{ devices: any[]; numbers: Record<string, number>; selected: string[] }>()
const emit = defineEmits<{ 'update:selected': [devices: string[]]; sync: []; close: [] }>()
const groups = ref<SavedGroup[]>([]), groupId = ref(''), name = ref(''), range = ref('')
const loading = ref(false), saving = ref(false), error = ref(''), message = ref(''), deleteArmed = ref(false)
const android = computed(() => props.devices.filter(d => d.serial && !String(d.serial).startsWith('ios:')))
const online = computed(() => new Set<string>(android.value.map(d => d.serial)))
const selectedOnline = computed(() => props.selected.filter(s => online.value.has(s)))
const savedGroup = computed(() => groups.value.find(g => g.id === groupId.value) || null)
const offlineMembers = computed(() => props.selected.filter(s => !online.value.has(s)))
const selectionSummary = computed(() => props.selected.map(s => props.numbers[s] ? String(props.numbers[s]) : s.replace(/:5555$/, '')).join(', ') || 'None')
function label(serial: string): string {
  const d = props.devices.find(d => d.serial === serial)
  return `${props.numbers[serial] ? props.numbers[serial] + '. ' : ''}${d?.nickname || d?.model || 'Android'} · ${serial.replace(/:5555$/, '')}`
}
async function request(path: string, init: RequestInit = {}) {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' })
  const body = await response.json().catch(() => null)
  if (!response.ok || body?.ok === false) throw new Error(typeof body?.detail === 'string' ? body.detail : `Group service returned HTTP ${response.status}`)
  return body
}
async function refresh() {
  loading.value = true; error.value = ''
  try {
    const body = await request('/api/fleet/groups')
    groups.value = Array.isArray(body?.groups) ? body.groups : []
    if (groupId.value && !savedGroup.value) { groupId.value = ''; name.value = '' }
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not load groups.' }
  finally { loading.value = false }
}
function chooseGroup() {
  deleteArmed.value = false; error.value = ''; message.value = ''
  const group = savedGroup.value
  name.value = group?.name || ''
  if (group) { emit('update:selected', [...group.devices]); message.value = `Loaded ${group.name}. Only selected connected phones can sync.` }
}
function selectRange() {
  error.value = ''; message.value = ''; deleteArmed.value = false
  try {
    const list = selectNumberedPhones(range.value, props.numbers, [...online.value])
    emit('update:selected', list)
    message.value = `${list.length} phones selected. Press Sync selected to begin.`
  } catch (e) { error.value = e instanceof Error ? e.message : 'Invalid phone numbers.' }
}
function toggle(serial: string) {
  const next = new Set(props.selected)
  if (next.has(serial)) next.delete(serial); else next.add(serial)
  emit('update:selected', [...next]); message.value = ''; deleteArmed.value = false
}
async function save(update: boolean) {
  error.value = ''; message.value = ''; deleteArmed.value = false
  const trimmed = name.value.trim()
  if (!trimmed || trimmed.length > 60) { error.value = 'Enter a group name (1–60 characters).'; return }
  if (!props.selected.length || props.selected.length > 50) { error.value = 'Choose 1–50 phones for a saved group.'; return }
  if (update && !savedGroup.value) return
  const members = [...props.selected]
  const id = update ? savedGroup.value!.id : ''
  saving.value = true
  try {
    const body = await request(id ? `/api/fleet/groups/${encodeURIComponent(id)}` : '/api/fleet/groups', {
      method: id ? 'PATCH' : 'POST', body: JSON.stringify({ name: trimmed, devices: members }),
    })
    if (!body?.group) throw new Error('The server did not return the saved group.')
    groupId.value = body.group.id; name.value = body.group.name
    // Set the returned group before refreshing, so its identity is retained.
    groups.value = [...groups.value.filter(g => g.id !== body.group.id), body.group]
    await refresh()
    message.value = `Saved ${body.group.name} (${members.length} phones). Available in Fleet controls too.`
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not save the group.' }
  finally { saving.value = false }
}
async function removeGroup() {
  if (!savedGroup.value || saving.value) return
  if (!deleteArmed.value) { deleteArmed.value = true; return }
  saving.value = true; error.value = ''; message.value = ''
  try {
    await request(`/api/fleet/groups/${encodeURIComponent(savedGroup.value.id)}`, { method: 'DELETE' })
    groupId.value = ''; name.value = ''; deleteArmed.value = false
    await refresh(); message.value = 'Group deleted. Phones and the current selection were not removed.'
  } catch (e) { error.value = e instanceof Error ? e.message : 'Could not delete the group.' }
  finally { saving.value = false }
}
onMounted(() => { void refresh() })
</script>

<template>
  <section class="grid-groups-panel" aria-label="Select phones and saved groups">
    <header class="groups-heading"><UiIcon name="users" :size="15" /><strong>Selection & groups</strong><span>{{ selectedOnline.length }} connected selected</span><span class="groups-spacer"></span><button type="button" class="ctrl-btn ctrl-btn--icon" aria-label="Close groups panel" @click="emit('close')"><UiIcon name="close" :size="14" /></button></header>
    <div class="groups-controls">
      <form class="groups-field groups-range" @submit.prevent="selectRange"><label for="grid-number-range">Select by phone number</label><div><input id="grid-number-range" v-model="range" class="ui-input" placeholder="1-5 or 1, 3, 5" autocomplete="off" /><button type="submit" class="ctrl-btn">Select</button></div></form>
      <div class="groups-field"><label for="grid-saved-group">Saved group</label><div><select id="grid-saved-group" v-model="groupId" class="ui-select" :disabled="loading || saving" @change="chooseGroup"><option value="">New group / current selection</option><option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.devices.length }})</option></select><button type="button" class="ctrl-btn ctrl-btn--icon" aria-label="Refresh saved groups" :disabled="loading || saving" @click="refresh"><UiIcon name="refresh" :size="13" /></button></div></div>
      <form class="groups-field" @submit.prevent="save(!!savedGroup)"><label for="grid-group-name">Group name</label><div><input id="grid-group-name" v-model="name" class="ui-input" placeholder="e.g. Phones 1–5" maxlength="60" :disabled="saving" /><button type="button" class="ctrl-btn" :disabled="saving || !selected.length" @click="save(false)">Create group</button><button v-if="savedGroup" type="submit" class="ctrl-btn" :disabled="saving || !selected.length">Save changes</button></div></form>
    </div>
    <div class="group-device-list" role="group" aria-label="Choose phones for selection"><label v-for="d in android" :key="d.serial" class="group-device" :class="{ 'is-selected': selected.includes(d.serial) }" :title="label(d.serial)"><input type="checkbox" :checked="selected.includes(d.serial)" :aria-label="'Select phone ' + (numbers[d.serial] || d.serial) + ' for sync'" @change="toggle(d.serial)" /><strong>{{ numbers[d.serial] ? numbers[d.serial] + '.' : '–' }}</strong><span>{{ d.nickname || d.model || 'Android' }}</span><small>{{ String(d.serial).replace(/:5555$/, '') }}</small></label></div>
    <div class="groups-footer"><span class="group-summary" :title="selectionSummary">Selected: {{ selectionSummary }}</span><span v-if="offlineMembers.length" class="groups-offline">{{ offlineMembers.length }} offline member(s) retained; not synced</span><span class="groups-spacer"></span><button v-if="savedGroup" type="button" class="ctrl-btn group-delete" :disabled="saving" @click="removeGroup">{{ deleteArmed ? 'Confirm delete group' : 'Delete group' }}</button><button type="button" class="ctrl-btn" :disabled="!selected.length" @click="emit('update:selected', [])">Clear selection</button><button type="button" class="ctrl-btn ctrl-btn--accent" :disabled="selectedOnline.length < 2" @click="emit('sync')">Sync selected ({{ selectedOnline.length }})</button></div>
    <p v-if="error" class="groups-error" role="alert">{{ error }}</p><p v-else-if="message" class="groups-message" role="status">{{ message }}</p><p v-else class="groups-hint">Select only the phones you want. Use any selected phone as the leader; other phones remain independent. Editing the selection stops sync until you enable it again.</p>
  </section>
</template>

<style scoped>
.grid-groups-panel { flex: none; margin: 0 0 8px; padding: 10px 12px; border: 1px solid var(--ui-border-strong); border-radius: 8px; background: var(--ui-surface-1); color: var(--ui-text-2); }
.groups-heading { display: flex; align-items: center; gap: 8px; margin-bottom: 9px; font-size: 12px; }
.groups-heading strong { color: var(--ui-text-1); }
.groups-heading > span:not(.groups-spacer) { font-size: 11px; color: var(--ui-text-3); }
.groups-spacer { flex: 1; min-width: 0; }
.groups-controls { display: grid; grid-template-columns: minmax(180px, .7fr) minmax(220px, 1fr) minmax(300px, 1.3fr); gap: 12px; }
.groups-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; margin: 0; }
.groups-field > label { font-size: 11px; font-weight: 600; }
.groups-field > div { display: flex; gap: 5px; min-width: 0; }
.groups-field input, .groups-field select { width: 100%; min-width: 0; flex: 1; height: 30px; font-size: 12px; }
.groups-field .ctrl-btn, .groups-footer .ctrl-btn { white-space: nowrap; font-size: 11px; min-height: 28px; }
.ctrl-btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; flex: none; padding: 5px 9px; border: 1px solid var(--ui-border-strong); border-radius: 5px; background: var(--ui-surface-2); color: var(--ui-text-2); font-family: inherit; font-weight: 600; cursor: pointer; }
.ctrl-btn:hover:not(:disabled) { background: var(--ui-surface-3); border-color: var(--ui-accent-line); color: var(--ui-text-1); }
.ctrl-btn:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.ctrl-btn:disabled { opacity: .45; cursor: not-allowed; }
.ctrl-btn--accent { background: var(--ui-accent); border-color: var(--ui-accent); color: var(--ui-on-accent); }
.ctrl-btn--accent:hover:not(:disabled) { background: var(--ui-accent); color: var(--ui-on-accent); filter: brightness(1.08); }
.ctrl-btn--icon { width: 28px; height: 28px; padding: 0; }
.group-device-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 5px; margin-top: 10px; max-height: 132px; overflow-y: auto; }
.group-device { display: grid; grid-template-columns: 14px 24px minmax(0, 1fr); align-items: center; gap: 3px 4px; padding: 5px 7px; border: 1px solid var(--ui-border); border-radius: 5px; background: var(--ui-surface-2); cursor: pointer; font-size: 11px; }
.group-device.is-selected { border-color: var(--ui-accent-line); background: var(--ui-accent-soft); }
.group-device input { margin: 0; width: 12px; height: 12px; accent-color: var(--ui-accent); }
.group-device strong { color: var(--ui-text-1); }
.group-device span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.group-device small { grid-column: 2 / -1; font-family: var(--ui-mono); font-size: 10px; color: var(--ui-text-3); }
.groups-footer { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-top: 9px; font-size: 11px; }
.group-summary { max-width: 40%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.groups-offline, .group-delete { color: var(--ui-warn); }
.groups-error, .groups-message, .groups-hint { margin: 7px 0 0; font-size: 11px; line-height: 1.5; }
.groups-error { color: var(--ui-danger); }
.groups-message { color: var(--ui-accent); }
.groups-hint { color: var(--ui-text-3); }
@media (max-width: 1400px) { .groups-controls { grid-template-columns: minmax(170px, .8fr) minmax(230px, 1.2fr); gap: 8px; } .groups-field:last-child { grid-column: 1 / -1; } }
</style>
