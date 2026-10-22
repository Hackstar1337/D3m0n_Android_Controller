import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
const source = await readFile(new URL('./src/views/PhoneAdminView.vue', import.meta.url), 'utf8')
const documentViews = source.match(/'is-document-view':\s*([^,}]+)/)?.[1]
assert.ok(documentViews, 'Phone workspace must declare document views')
for (const tab of ['fleet', 'proxies', 'ai']) {
  assert.match(documentViews, new RegExp(`subTab === '${tab}'`), `${tab} must use document scrolling`)
}
assert.doesNotMatch(documentViews, /'multi'|'single'|'windows'/, 'Fixed-height control views must not change')
assert.match(source, /\.phone-admin\.is-document-view\s*\{\s*height:\s*auto;\s*overflow:\s*visible;/, 'Document content must not be clipped')
assert.match(source, /\.phone-admin\s*\{[^}]*overflow:\s*hidden;/s, 'Live Grid and Agent Console retain original containment')
console.log('Fleet AI scroll containment regression passed')
