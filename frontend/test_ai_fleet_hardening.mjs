import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const jobs = await readFile(resolve(root, '../ops/ai_fleet_jobs.py'), 'utf8')
const api = await readFile(resolve(root, '../ops/ai_fleet_api.py'), 'utf8')
const fleet = await readFile(resolve(root, 'src/composables/aiFleet.ts'), 'utf8')
const centre = await readFile(resolve(root, 'src/components/ai/FleetCommandCentre.vue'), 'utf8')
const monitor = await readFile(resolve(root, 'src/components/ai/FleetJobMonitor.vue'), 'utf8')

assert.match(fleet, /\/api\/fleet\/groups/)
assert.match(fleet, /loadGroups/)
assert.match(centre, /fleet\.groups\.value/)
assert.doesNotMatch(centre, /device\?\.group/)
assert.match(api, /\/jobs\/\{job_id\}\/receipts/)
assert.match(api, /"bounded": True/)
assert.match(api, /"summary": receipt\.summary/)
assert.doesNotMatch(api, /"evidence": receipt\.evidence/)
assert.match(fleet, /\/receipts\?limit=100/)
assert.match(fleet, /childReceipts\.push\(receipt\)/)
assert.match(centre, /activePlan\.value\.intent\?\.text/)
assert.match(centre, /activePlan\.value\.targets/)
assert.match(centre, /activePlan\.value\.policy/)
assert.match(centre, /setRoleBinding/)
assert.match(centre, /\['planner', 'executor', 'verifier'\]/)
assert.match(jobs, /Pause fenced the active child/)
assert.match(jobs, /state IN \(\?,\?,\?\)/)
assert.match(jobs, /never replay an uncertain mutation|never replay/i)
assert.match(monitor, /child\.receipts/)

console.log('AI fleet hardening contract checks passed')
