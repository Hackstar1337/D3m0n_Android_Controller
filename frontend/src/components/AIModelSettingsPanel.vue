<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import providerGuides from '@/assets/aiProviderGuides.json'

type KeySource = 'saved' | 'environment' | 'none' | 'not-required'
type Provider = {
  id: string
  label: string
  base_url: string
  default_model: string
  models: string[]
  api_key_configured: boolean
  key_source: KeySource
  requires_api_key: boolean
  notes: string
}
type Settings = {
  default_provider: string
  default_model: string
  providers: Provider[]
  defaults: { context_length: number; max_output_tokens: number; max_turns: number }
}
type Draft = { baseUrl: string; defaultModel: string; models: string; apiKey: string; clearApiKey: boolean }
type TestResult = { tone: 'success' | 'warn' | 'danger'; text: string }

defineProps<{ locked?: boolean }>()
const emit = defineEmits<{ close: []; saved: [] }>()

const loading = ref(true)
const saving = ref('')
const testing = ref('')
const error = ref('')
const notice = ref('')
const settings = ref<Settings | null>(null)
const selectedId = ref('')
const drafts = reactive<Record<string, Draft>>({})
const testResults = reactive<Record<string, TestResult>>({})

const selectedProvider = computed(() => settings.value?.providers.find(provider => provider.id === selectedId.value) || null)
const selectedDraft = computed(() => selectedId.value ? drafts[selectedId.value] : undefined)
const selectedIsDefault = computed(() => settings.value?.default_provider === selectedId.value && settings.value.default_model === selectedDraft.value?.defaultModel)
const selectedTestResult = computed(() => selectedId.value ? testResults[selectedId.value] : undefined)

const PROVIDER_GUIDES: Record<string, { summary: string; steps: string[]; links: { label: string; href: string }[] }> = providerGuides

function guideFor(provider: Provider | null) {
  if (!provider) return null
  const id = provider.id.toLowerCase()
  if (PROVIDER_GUIDES[id]) return PROVIDER_GUIDES[id]
  if (id.includes('claude')) return PROVIDER_GUIDES['claude-code']
  if (id.includes('on') && id.includes('device') || id.includes('on_device')) return PROVIDER_GUIDES['on-device']
  return { summary: provider.notes || 'Configure this provider using the server-reported fields below.', steps: ['Use only the endpoint, model, and credential fields supported by this provider.', 'Test checks metadata/readiness only; it does not launch a device or perform paid inference.'], links: [] }
}

function isEndpointProvider(provider: Provider | null) {
  if (!provider) return false
  const id = provider.id.toLowerCase()
  return !id.includes('claude-code') && !id.includes('claudecode') && !id.includes('on-device') && !id.includes('on_device')
}
function isKeyProvider(provider: Provider | null) {
  return !!provider && (provider.requires_api_key || provider.id === 'vllm') && !provider.id.toLowerCase().includes('claude-code') && !provider.id.toLowerCase().includes('on-device') && !provider.id.toLowerCase().includes('on_device')
}
function keyLabel(provider: Provider) {
  if (provider.key_source === 'environment') return 'API key (environment fallback active)'
  if (provider.key_source === 'saved') return 'API key (saved override active)'
  return provider.requires_api_key ? 'API key' : 'Credential (optional)'
}
function keyStatus(provider: Provider) {
  if (provider.key_source === 'saved') return 'Saved override configured; value is never shown.'
  if (provider.key_source === 'environment') return 'Environment fallback configured; no saved value is shown.'
  if (provider.key_source === 'not-required') return 'This provider does not require an API key.'
  return provider.requires_api_key ? 'No key configured.' : 'Optional; leave blank to keep the server fallback.'
}

function providerDraft(provider: Provider): Draft {
  return { baseUrl: provider.base_url || '', defaultModel: provider.default_model || '', models: (provider.models || []).join('\n'), apiKey: '', clearApiKey: false }
}
function seedDrafts(value: Settings) {
  for (const provider of value.providers) drafts[provider.id] = providerDraft(provider)
  selectedId.value = value.providers.some(provider => provider.id === value.default_provider) ? value.default_provider : (value.providers[0]?.id || '')
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...init, headers: { 'Content-Type': 'application/json', ...(init.headers || {}) } })
  } catch {
    throw new Error('The AI settings service could not be reached.')
  }
  if (!response.ok) throw new Error('The AI settings request could not be completed.')
  try { return await response.json() as T } catch { throw new Error('The AI settings service returned an invalid response.') }
}

async function loadSettings() {
  loading.value = true
  error.value = ''
  try {
    const value = await requestJson<Settings>('/api/ai-settings')
    if (!value || !Array.isArray(value.providers)) throw new Error('The AI settings response was invalid.')
    settings.value = value
    seedDrafts(value)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'The AI settings could not be loaded.'
  } finally { loading.value = false }
}

function selectProvider(id: string) {
  if (saving.value || testing.value) return
  selectedId.value = id
  notice.value = ''
}
function modelList(draft: Draft) {
  return draft.models.split(/\r?\n|,/).map(model => model.trim()).filter(Boolean).filter((model, index, list) => list.indexOf(model) === index)
}
function providerBody(provider: Provider, draft: Draft) {
  const body: Record<string, unknown> = { default_model: draft.defaultModel.trim(), models: modelList(draft) }
  if (isEndpointProvider(provider)) body.base_url = draft.baseUrl.trim()
  if (draft.clearApiKey) body.clear_api_key = true
  else if (draft.apiKey.trim()) body.api_key = draft.apiKey.trim()
  return body
}
function clearDraftSecret(providerId = selectedId.value) {
  if (drafts[providerId]) { drafts[providerId].apiKey = ''; drafts[providerId].clearApiKey = false }
}
function clearAllDraftSecrets() { for (const provider of Object.values(drafts)) { provider.apiKey = ''; provider.clearApiKey = false } }
function closePanel() {
  clearAllDraftSecrets()
  emit('close')
}

async function saveProvider(provider: Provider) {
  const draft = drafts[provider.id]
  if (!draft || saving.value || testing.value) return
  saving.value = provider.id
  error.value = ''
  notice.value = ''
  try {
    const updated = await requestJson<Provider>(`/api/ai-settings/${encodeURIComponent(provider.id)}`, { method: 'PUT', body: JSON.stringify(providerBody(provider, draft)) })
    const index = settings.value?.providers.findIndex(item => item.id === provider.id) ?? -1
    const current = settings.value?.providers[index]
    if (current && settings.value) {
      const merged = { ...current, ...updated, models: Array.isArray(updated.models) ? updated.models : current.models }
      settings.value.providers[index] = merged
      drafts[provider.id] = providerDraft(merged)
    }
    clearDraftSecret(provider.id)
    notice.value = `${provider.label} settings saved.`
    emit('saved')
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'The provider settings could not be saved.'
  } finally { saving.value = ''; clearDraftSecret(provider.id) }
}

async function saveDefault() {
  const provider = selectedProvider.value
  const draft = selectedDraft.value
  if (!provider || !draft || saving.value || testing.value || !draft.defaultModel.trim()) return
  saving.value = '__default__'
  error.value = ''
  notice.value = ''
  try {
    await requestJson<{ ok: true }>('/api/ai-settings/default', { method: 'PUT', body: JSON.stringify({ provider: provider.id, model: draft.defaultModel.trim(), context_length: settings.value?.defaults.context_length, max_output_tokens: settings.value?.defaults.max_output_tokens, max_turns: settings.value?.defaults.max_turns }) })
    if (settings.value) { settings.value.default_provider = provider.id; settings.value.default_model = draft.defaultModel.trim() }
    notice.value = `${provider.label} / ${draft.defaultModel.trim()} is the default for new chats.`
    emit('saved')
  } catch (reason) { error.value = reason instanceof Error ? reason.message : 'The new-chat default could not be saved.' }
  finally { saving.value = ''; clearAllDraftSecrets() }
}

async function testProvider(provider: Provider) {
  if (testing.value || saving.value) return
  testing.value = provider.id
  error.value = ''
  notice.value = ''
  delete testResults[provider.id]
  try {
    const result = await requestJson<{ ok: boolean; status?: string; message?: string; models?: string[]; capabilities?: string[] }>(`/api/ai-settings/${encodeURIComponent(provider.id)}/test`, { method: 'POST' })
    const modelHint = Array.isArray(result.models) && result.models.length ? ` · ${result.models.length} model${result.models.length === 1 ? '' : 's'} reported` : ''
    testResults[provider.id] = result.ok ? { tone: 'success', text: `${result.message || 'Readiness check passed.'}${modelHint}` } : { tone: 'warn', text: result.message || 'Readiness check needs attention.' }
  } catch (reason) {
    testResults[provider.id] = { tone: 'danger', text: reason instanceof Error ? reason.message : 'The provider test could not be completed.' }
  } finally { testing.value = '' }
}

onMounted(loadSettings)
onBeforeUnmount(clearAllDraftSecrets)
</script>

<template>
  <div class="ai-settings-overlay" role="presentation">
    <section class="ai-settings-panel" role="dialog" aria-modal="true" aria-labelledby="ai-settings-title">
      <header class="ai-settings-header">
        <div>
          <div class="ui-section-label">Configuration</div>
          <h2 id="ai-settings-title">AI model settings</h2>
          <p>Provider credentials are redacted. Blank key fields retain the server-side value.</p>
        </div>
        <button type="button" class="ui-btn ui-btn--ghost ui-btn--icon" :disabled="locked" aria-label="Close AI model settings" title="Close" @click="closePanel"><UiIcon name="close" :size="17" /></button>
      </header>

      <div v-if="loading" class="ai-settings-state"><UiIcon name="refresh" :size="16" /> Loading AI settings…</div>
      <div v-else-if="error && !settings" class="ai-settings-state ai-settings-state--error" role="alert"><UiIcon name="alert" :size="16" /> {{ error }}<button type="button" class="ui-btn ui-btn--sm" @click="loadSettings">Retry</button></div>
      <div v-else-if="settings" class="ai-settings-body">
        <aside class="ai-settings-providers" aria-label="AI providers">
          <div class="ui-section-label">Providers</div>
          <button v-for="provider in settings.providers" :key="provider.id" type="button" class="ai-provider-tab" :class="{ 'is-active': provider.id === selectedId }" @click="selectProvider(provider.id)">
            <span class="ai-provider-tab__name">{{ provider.label }}</span>
            <span class="ai-provider-tab__meta">{{ provider.key_source === 'saved' || provider.key_source === 'environment' ? 'configured' : provider.requires_api_key ? 'key needed' : 'local / CLI' }}</span>
          </button>
          <div class="ai-settings-default-note"><span class="ui-section-label">New-chat default</span><strong>{{ settings.default_provider }} / {{ settings.default_model }}</strong></div>
        </aside>

        <div v-if="selectedProvider && selectedDraft" class="ai-settings-form">
          <div class="ai-settings-form__title"><div><h3>{{ selectedProvider.label }}</h3><p>{{ selectedProvider.notes || guideFor(selectedProvider)?.summary }}</p></div><span class="ui-badge" :class="selectedIsDefault ? 'ui-badge--success' : 'ui-badge--muted'">{{ selectedIsDefault ? 'Default for new chats' : 'Configured provider' }}</span></div>
          <div v-if="error" class="ui-callout ui-callout--danger" role="alert"><UiIcon name="alert" :size="15" />{{ error }}</div>
          <div v-if="notice" class="ui-callout ui-callout--success" role="status"><UiIcon name="check" :size="15" />{{ notice }}</div>

          <div class="ai-settings-grid">
            <label v-if="isEndpointProvider(selectedProvider)" class="ui-field ai-settings-grid__wide"><span class="ui-label">Endpoint</span><input v-model="selectedDraft.baseUrl" class="ui-input" type="url" inputmode="url" autocomplete="off" placeholder="https://…" :disabled="!!locked || !!saving || !!testing" /><span class="ui-hint">Only use the official cloud origin or an approved local/private endpoint. Never put a credential in this URL.</span></label>
            <label class="ui-field"><span class="ui-label">Default model</span><input v-model="selectedDraft.defaultModel" class="ui-input" type="text" autocomplete="off" placeholder="Model ID" :disabled="!!locked || !!saving || !!testing" /></label>
            <label class="ui-field"><span class="ui-label">Editable model list</span><textarea v-model="selectedDraft.models" class="ui-input ai-model-list" rows="4" autocomplete="off" :disabled="!!locked || !!saving || !!testing" aria-describedby="ai-model-list-hint"></textarea><span id="ai-model-list-hint" class="ui-hint">One model ID per line. The server may refresh discovery results.</span></label>
            <label v-if="isKeyProvider(selectedProvider)" class="ui-field ai-settings-grid__wide"><span class="ui-label">{{ keyLabel(selectedProvider) }}</span><input v-model="selectedDraft.apiKey" class="ui-input" type="password" autocomplete="new-password" placeholder="Leave blank to retain the saved key" :disabled="!!locked || !!saving || !!testing" /><span class="ui-hint">{{ keyStatus(selectedProvider) }} Replacement-only input; the current value is never rendered.</span><span class="ai-clear-key"><input v-model="selectedDraft.clearApiKey" type="checkbox" :disabled="!!locked || !!saving || !!testing" /> Clear the saved override instead</span></label>
          </div>

          <fieldset class="ai-default-options" v-if="settings">
            <legend>Defaults for new chats</legend>
            <label class="ui-field"><span class="ui-label">Context tokens</span><input class="ui-input" type="number" v-model.number="settings.defaults.context_length" min="1024" max="32768" step="1024" :disabled="!!locked || !!saving || !!testing" /></label>
            <label class="ui-field"><span class="ui-label">Output tokens</span><input class="ui-input" type="number" v-model.number="settings.defaults.max_output_tokens" min="128" max="8192" step="128" :disabled="!!locked || !!saving || !!testing" /></label>
            <label class="ui-field"><span class="ui-label">Maximum turns</span><input class="ui-input" type="number" v-model.number="settings.defaults.max_turns" min="1" max="24" :disabled="!!locked || !!saving || !!testing" /></label>
          </fieldset>
          <div class="ai-settings-actions">
            <button type="button" class="ui-btn ui-btn--primary" :disabled="!!locked || !!saving || !!testing" @click="saveProvider(selectedProvider)"><UiIcon name="check" :size="14" />{{ saving === selectedProvider.id ? 'Saving…' : 'Save provider' }}</button>
            <button type="button" class="ui-btn ui-btn--accent" :disabled="!!locked || !!saving || !!testing" @click="testProvider(selectedProvider)"><UiIcon name="pulse" :size="14" />{{ testing === selectedProvider.id ? 'Testing…' : 'Test readiness' }}</button>
            <button type="button" class="ui-btn" :disabled="!!locked || !!saving || !!testing || !selectedDraft.defaultModel.trim()" @click="saveDefault"><UiIcon name="check" :size="14" />{{ saving === '__default__' ? 'Setting default…' : 'Use for new chats' }}</button>
          </div>
          <div v-if="selectedTestResult" class="ai-test-result" :class="`is-${selectedTestResult?.tone || 'danger'}`" role="status"><UiIcon :name="selectedTestResult?.tone === 'success' ? 'check' : selectedTestResult?.tone === 'warn' ? 'alert' : 'info'" :size="14" />{{ selectedTestResult?.text }}</div>

          <details class="ai-guide" open>
            <summary><UiIcon name="book" :size="15" /> Setup guidance</summary>
            <div v-if="guideFor(selectedProvider)">
              <p>{{ guideFor(selectedProvider)?.summary }}</p>
              <ol><li v-for="step in guideFor(selectedProvider)?.steps" :key="step">{{ step }}</li></ol>
              <div v-if="guideFor(selectedProvider)?.links.length" class="ai-guide-links"><a v-for="link in guideFor(selectedProvider)?.links" :key="link.href" :href="link.href" target="_blank" rel="noopener noreferrer">{{ link.label }} <UiIcon name="external" :size="11" /></a></div>
            </div>
          </details>
        </div>
        <div v-else class="ai-settings-state">No providers were returned by the server.</div>
      </div>

      <footer class="ai-settings-footer"><span><UiIcon name="shield" :size="14" /> Changes affect new chats; an active chat keeps its provider and model.</span><button type="button" class="ui-btn" :disabled="locked" @click="closePanel">Cancel</button></footer>
    </section>
  </div>
</template>

<style scoped>
.ai-default-options { margin: 16px 0 0; padding: 12px; display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 12px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); }
.ai-default-options legend { padding: 0 6px; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.ai-settings-overlay { position: fixed; inset: 0; z-index: 1200; display: grid; place-items: center; padding: 28px; background: rgba(3, 5, 7, .78); backdrop-filter: blur(6px); }
.ai-settings-panel { display: flex; flex-direction: column; width: min(1020px, 100%); max-height: min(800px, calc(100vh - 56px)); overflow: hidden; color: var(--ui-text-1); background: var(--ui-surface-1); border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-lg); box-shadow: var(--ui-shadow-pop); }
.ai-settings-header { display: flex; align-items: flex-start; gap: 20px; padding: 18px 20px 15px; border-bottom: 1px solid var(--ui-border); }.ai-settings-header > div { min-width: 0; flex: 1; }.ai-settings-header h2 { margin: 3px 0 2px; font-size: var(--ui-fs-2xl); }.ai-settings-header p { margin: 0; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }
.ai-settings-body { display: grid; grid-template-columns: 210px minmax(0, 1fr); min-height: 0; overflow: auto; }.ai-settings-providers { padding: 16px 10px; border-right: 1px solid var(--ui-border); background: var(--ui-surface-sunken); }.ai-provider-tab { display: grid; width: 100%; gap: 2px; margin-top: 6px; padding: 9px 10px; text-align: left; border: 1px solid transparent; border-radius: var(--ui-radius-sm); background: transparent; color: var(--ui-text-2); cursor: pointer; }.ai-provider-tab:hover { background: var(--ui-surface-2); color: var(--ui-text-1); }.ai-provider-tab.is-active { border-color: var(--ui-accent-line); background: var(--ui-accent-soft); color: var(--ui-text-1); }.ai-provider-tab__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--ui-fs-sm); font-weight: 650; }.ai-provider-tab__meta { color: var(--ui-text-4); font-size: var(--ui-fs-2xs); }.ai-settings-default-note { display: grid; gap: 4px; margin: 18px 5px 0; padding-top: 14px; border-top: 1px solid var(--ui-border); }.ai-settings-default-note strong { color: var(--ui-text-2); font-size: var(--ui-fs-xs); word-break: break-word; }
.ai-settings-form { min-width: 0; padding: 18px 20px; overflow: auto; }.ai-settings-form__title { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 16px; }.ai-settings-form__title > div { min-width: 0; flex: 1; }.ai-settings-form h3 { margin: 0 0 3px; font-size: var(--ui-fs-xl); }.ai-settings-form p { margin: 0; color: var(--ui-text-3); font-size: var(--ui-fs-xs); }.ai-settings-form__title .ui-badge { margin-top: 2px; }.ai-settings-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; margin-top: 16px; }.ai-settings-grid__wide { grid-column: 1 / -1; }.ai-model-list { height: 93px; }.ai-clear-key { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }.ai-clear-key input { width: 13px; height: 13px; }.ai-settings-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--ui-border); }.ai-test-result { display: flex; align-items: flex-start; gap: 7px; margin-top: 12px; padding: 9px 11px; border-radius: var(--ui-radius); font-size: var(--ui-fs-xs); }.ai-test-result.is-success { color: #9be6c8; background: var(--ui-success-soft); }.ai-test-result.is-warn { color: #f6cd82; background: var(--ui-warn-soft); }.ai-test-result.is-danger { color: #ffa3a3; background: var(--ui-danger-soft); }.ai-guide { margin-top: 20px; border-top: 1px solid var(--ui-border); padding-top: 13px; color: var(--ui-text-2); font-size: var(--ui-fs-xs); }.ai-guide summary { display: flex; align-items: center; gap: 7px; color: var(--ui-text-1); font-weight: 650; cursor: pointer; }.ai-guide p { margin: 10px 0 7px; }.ai-guide ol { margin: 0 0 10px 20px; padding: 0; color: var(--ui-text-3); }.ai-guide li { margin: 3px 0; }.ai-guide-links { display: flex; flex-wrap: wrap; gap: 6px 16px; }.ai-guide-links a { display: inline-flex; align-items: center; gap: 4px; color: #9bdcc8; text-decoration: none; }.ai-guide-links a:hover { text-decoration: underline; }.ai-settings-state { display: flex; align-items: center; justify-content: center; gap: 9px; min-height: 240px; padding: 20px; color: var(--ui-text-3); }.ai-settings-state--error { flex-wrap: wrap; color: #ffabab; }.ai-settings-footer { display: flex; align-items: center; gap: 10px; padding: 12px 20px; border-top: 1px solid var(--ui-border); color: var(--ui-text-3); font-size: var(--ui-fs-2xs); }.ai-settings-footer > span { display: inline-flex; align-items: center; gap: 6px; margin-right: auto; }
@media (max-width: 760px) { .ai-settings-overlay { padding: 10px; }.ai-settings-panel { max-height: calc(100vh - 20px); }.ai-settings-body { display: block; }.ai-settings-providers { display: flex; gap: 6px; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--ui-border); padding: 8px; }.ai-settings-providers > .ui-section-label, .ai-settings-default-note { display: none; }.ai-provider-tab { min-width: 140px; margin: 0; }.ai-settings-grid { grid-template-columns: 1fr; }.ai-settings-grid__wide { grid-column: auto; }.ai-settings-footer > span { display: none; } }
</style>
