import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const fleet = await readFile(resolve(root, 'src/composables/aiFleet.ts'), 'utf8')
const centre = await readFile(resolve(root, 'src/components/ai/FleetCommandCentre.vue'), 'utf8')
const monitor = await readFile(resolve(root, 'src/components/ai/FleetJobMonitor.vue'), 'utf8')
const shell = await readFile(resolve(root, 'src/composables/shellNavigation.ts'), 'utf8')
const app = await readFile(resolve(root, 'src/App.vue'), 'utf8')

// Contract coverage: the UI only uses the authenticated metadata-only endpoints.
for (const route of ['/api/ai-fleet/status', '/api/ai-fleet/profiles', '/api/ai-fleet/profiles/roles', '/api/ai-fleet/jobs', '/api/ai-fleet/plans', '/api/ai-fleet/jobs/', '/reconcile', '/actions']) {
  assert.match(fleet, new RegExp(route.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), `missing contract route ${route}`)
}
assert.match(fleet, /max_active_workers: 3/)
assert.match(fleet, /budget_usd: 20/)
assert.match(fleet, /allow_vision: false/)
assert.match(fleet, /target_ids: selected\.join\(','\)/)
assert.match(fleet, /targets: targetRefs/)
assert.match(fleet, /profiles: profileRefs/)
assert.match(fleet, /role_bindings: bindings/)
assert.match(fleet, /child_id: childId/)
assert.doesNotMatch(fleet, /localStorage/)
assert.doesNotMatch(centre, /localStorage/)
assert.doesNotMatch(monitor, /localStorage/)
assert.doesNotMatch(fleet, /console\.(log|debug|info|error)/)
assert.doesNotMatch(centre, /provider_response|raw_screenshot|ui_tree|authorization\s*:/i)

// Functional UI safety checks: target selection is explicit, no offline/busy target can be selected,
// approval is runtime-gated, and uncertain children expose reconciliation rather than auto-retry.
assert.match(centre, /:disabled="!device\.online \|\| device\.busy"/)
assert.match(centre, /const canApprove = computed\(\(\) => Boolean\(fleet\.activePlan\.value && fleet\.approvalToken\.value\) && fleet\.runtimeEnabled\.value\)/)
assert.match(centre, /maxTargets|selectedCount|selected\.length <= 20/)
assert.match(monitor, /\['uncertain', 'manual_recovery', 'waiting_for_owner'\]\.includes\(String\(child\.state\)\)/)
assert.match(monitor, /Reconcile/)
assert.match(monitor, /@click="act\('stop'\)"/)

// Navigation is additive and preserves the existing Agent Console and Fleet controls.
assert.match(shell, /PhoneSubTab = 'single'.*'fleet'.*'ai'.*'proxies'/s)
assert.match(app, /phone:single.*Agent console/s)
assert.match(app, /phone:fleet.*Fleet controls/s)
assert.match(app, /phone:ai.*Fleet AI/s)

console.log('AI fleet command contract/safety checks passed')
