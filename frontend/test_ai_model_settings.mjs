import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.dirname(fileURLToPath(import.meta.url))
const panelPath = path.join(root, 'src', 'components', 'AIModelSettingsPanel.vue')
const phonePath = path.join(root, 'src', 'views', 'PhoneAdminView.vue')
const iconPath = path.join(root, 'src', 'components', 'ui', 'UiIcon.vue')
const panel = fs.readFileSync(panelPath, 'utf8')
const phone = fs.readFileSync(phonePath, 'utf8')
const icons = fs.readFileSync(iconPath, 'utf8')

// Endpoint and request-shape behavior.
for (const endpoint of ['/api/ai-settings', '/api/ai-settings/${encodeURIComponent(provider.id)}', '/api/ai-settings/default']) {
  assert.ok(panel.includes(endpoint), `panel includes ${endpoint}`)
}
assert.match(panel, /method: 'PUT', body: JSON\.stringify\(providerBody\(provider, draft\)\)/, 'provider save uses PUT and the provider body')
assert.match(panel, /body: JSON\.stringify\(\{ provider: provider\.id, model: draft\.defaultModel\.trim\(\)/, 'default save uses the exact provider/model body')
assert.match(panel, /\/test`[\s\S]*method: 'POST'/, 'provider test is a POST without credential payload')
assert.match(panel, /body\.clear_api_key = true/, 'clear API key sends explicit clear_api_key')
assert.match(panel, /body\.api_key = draft\.apiKey\.trim\(\)/, 'non-blank key is sent only as a replacement')
assert.match(panel, /apiKey: ''/, 'draft secret starts blank')
assert.match(panel, /function clearAllDraftSecrets/, 'draft secrets have a central clear path')
assert.match(panel, /onBeforeUnmount\(clearAllDraftSecrets\)/, 'draft secrets clear on unmount')
assert.match(panel, /function closePanel\(\)[\s\S]*clearAllDraftSecrets\(\)[\s\S]*emit\('close'\)/, 'cancel/close clears draft secrets before emitting')
assert.match(panel, /finally \{ saving\.value = ''; clearDraftSecret\(provider\.id\) \}/, 'save clears the draft key on success or failure')

// Privacy and generic error behavior: no browser persistence, URL credentials, or raw response bodies.
assert.ok(!panel.includes('localStorage'), 'settings panel does not use localStorage')
assert.ok(!panel.includes('sessionStorage'), 'settings panel does not use sessionStorage')
assert.ok(!/console\.(log|error|warn|debug)\s*\(/.test(panel), 'settings panel does not log')
assert.ok(!panel.includes('response.text()'), 'settings panel does not render raw server error bodies')
assert.match(panel, /The AI settings request could not be completed/, 'server errors are generic')
assert.match(panel, /credentials: 'same-origin'/, 'requests retain dashboard-session credentials')
assert.match(panel, /cache: 'no-store'/, 'settings reads are no-store')

// Active-run guard and live-chat preservation are wired at the integration boundary.
assert.match(phone, /import AIModelSettingsPanel from '@\/components\/AIModelSettingsPanel\.vue'/)
assert.match(phone, /<AIModelSettingsPanel v-if="aiSettingsOpen" :locked="chatSending" @close="closeAiSettings" @saved="handleAiSettingsSaved" \/>/)
assert.match(phone, /@click="openAiSettings" :disabled="chatSending"/)
assert.match(phone, /function openAiSettings\(\) \{ if \(!chatSending\.value\) aiSettingsOpen\.value = true \}/)
assert.match(phone, /if \(chatSessionId\.value\) \{ chatProvider\.value = activeProvider; chatModel\.value = activeModel \}/)
assert.match(phone, /<button[^>]+@click="chatStop"|chatStop\(\)/, 'existing Stop behavior remains in the view')

// Every icon used by the panel must already exist in UiIcon.
const usedIcons = [...panel.matchAll(/UiIcon name="([^"]+)"/g)].map(match => match[1])
for (const name of new Set(usedIcons)) assert.match(icons, new RegExp(`\\b${name}:`), `UiIcon has ${name}`)

console.log('test_ai_model_settings: request/state/privacy/integration checks passed')
