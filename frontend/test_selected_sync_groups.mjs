import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const source = n => fs.readFileSync(n, 'utf8')
const js = s => ts.transpileModule(s, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText
const module = { exports: {} }
vm.runInNewContext(js(source('src/composables/numberedSelection.ts')), { exports: module.exports, module, Error })
const selectNumberedPhones = module.exports.selectNumberedPhones
const serials = Array.from({ length: 20 }, (_, i) => `192.168.50.${i < 9 ? 37 + i : 38 + i}:5555`)
const numbers = Object.fromEntries(serials.map((s, i) => [s, i + 1]))
const plain = v => JSON.parse(JSON.stringify(v))
assert.deepEqual(plain(selectNumberedPhones('1-5', numbers, serials)), serials.slice(0, 5))
assert.deepEqual(plain(selectNumberedPhones('#5-#1, 3, 7', numbers, serials)), [...serials.slice(0, 5), serials[6]])
assert.deepEqual(plain(selectNumberedPhones('1, 3, 5', numbers, serials)), [serials[0], serials[2], serials[4]])
for (const bad of ['', 'all', '1-50', '0', '1-x', '1-99999']) assert.throws(() => selectNumberedPhones(bad, numbers, serials))
assert.throws(() => selectNumberedPhones('1-5', numbers, serials.slice(0, 4)), /not connected/)

const fleet = source('src/views/PhoneAdminView.vue'), desk = source('src/views/DeviceWindowView.vue')
const fn = (s, name) => { const result = s.match(new RegExp(`function ${name}\\([^]*?\\n}`)); assert.ok(result, name); return result[0] }
const syncEnabled = { value: true }, syncPool = { value: serials.slice(0, 5) }
const gate = { syncEnabled, syncPool, MAX_SYNC_DEVICES: 100, isIosDevice: s => s.startsWith('ios:') }
vm.runInNewContext(js(fn(fleet, 'syncFollowersFor') + '\n' + fn(fleet, 'withSync') + '\nglobalThis.actions={syncFollowersFor,withSync}'), gate)
assert.deepEqual(plain(gate.actions.syncFollowersFor(serials[0])), serials.slice(1, 5))
for (const s of serials.slice(5)) {
  assert.deepEqual(plain(gate.actions.syncFollowersFor(s)), [], 'outside phone must never broadcast')
  assert.equal(gate.actions.withSync(s, { key: 'HOME' }).sync, undefined)
}
syncEnabled.value = false
assert.deepEqual(plain(gate.actions.syncFollowersFor(serials[0])), [])
syncEnabled.value = true
assert.equal(gate.actions.syncFollowersFor('ios:test').length, 0)
const propsDesk = { deviceSerial: serials[6], syncDevices: serials.slice(0, 5) }
const ctxDesk = { props: propsDesk }
vm.runInNewContext(js(fn(desk, 'syncFollowers') + '\nglobalThis.followers=syncFollowers'), ctxDesk)
assert.deepEqual(plain(ctxDesk.followers()), [])
propsDesk.deviceSerial = serials[2]
assert.deepEqual(plain(ctxDesk.followers()), [serials[0], serials[1], serials[3], serials[4]])
assert.ok(fleet.includes("watch(syncScope, resetSyncScope, { flush: 'sync' })"))
assert.ok(fleet.includes("if (syncScope.value === 'selected') resetSyncScope()"))
assert.ok(fleet.includes('<GridGroupsPanel v-if="groupsOpen"'))
assert.ok(fleet.includes('@click="requestSyncSelected"'))

// Execute the actual new group panel against an isolated in-memory mock of the existing API.
const calls = [], events = [], groups = [], props = {
  devices: serials.map(s => ({ serial: s, model: 'SM G9500' })), numbers, selected: [],
}
const emit = (event, value) => { events.push({ event, value }); if (event === 'update:selected') props.selected = value }
const ctx = {
  props, emit, selectNumberedPhones, console, Set, Map, Error,
  ref: value => ({ value }), computed: getter => ({ get value() { return getter() } }), onMounted() {},
  fetch: async (path, init) => {
    const method = init.method || 'GET'; calls.push({ path, method, body: init.body ? JSON.parse(init.body) : null })
    let body = { ok: true, groups: [...groups] }, ok = true, status = 200
    if (method === 'POST') {
      const data = JSON.parse(init.body)
      if (groups.some(g => g.name === data.name)) { ok = false; status = 409; body = { detail: 'A group with that name already exists' } }
      else { const group = { id: 'test-group-' + groups.length, ...data }; groups.push(group); body = { ok: true, group } }
    }
    if (method === 'PATCH') { const id = path.split('/').at(-1), index = groups.findIndex(g => g.id === id); groups[index] = { ...groups[index], ...JSON.parse(init.body) }; body = { ok: true, group: groups[index] } }
    if (method === 'DELETE') { groups.splice(groups.findIndex(g => g.id === path.split('/').at(-1)), 1); body = { ok: true } }
    return { ok, status, json: async () => body }
  },
}
let panel = source('src/components/GridGroupsPanel.vue').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
panel = panel.replace(/^import[^\n]*\n/gm, '').replace(/^const props = defineProps[^\n]*\n/m, '').replace(/^const emit = defineEmits[^\n]*\n/m, '')
panel += '\nglobalThis.actions={range,name,groupId,error,message,deleteArmed,refresh,selectRange,chooseGroup,save,removeGroup,toggle,selectedOnline,offlineMembers}'
vm.runInNewContext(js(panel), ctx)
const a = ctx.actions
a.range.value = '1-5'; a.selectRange()
assert.deepEqual(plain(props.selected), serials.slice(0, 5))
a.range.value = '1-50'; a.selectRange()
assert.match(a.error.value, /not connected/)
assert.deepEqual(plain(props.selected), serials.slice(0, 5), 'invalid selection leaves the previous selection intact')
a.name.value = 'Phones 1–5'; await a.save(false)
assert.equal(groups.length, 1); assert.deepEqual(plain(groups[0].devices), serials.slice(0, 5))
assert.match(a.message.value, /Available in Fleet controls/)
props.selected = []; a.chooseGroup(); assert.deepEqual(plain(props.selected), serials.slice(0, 5))
a.name.value = 'Renamed group'; a.toggle(serials[5]); await a.save(true)
assert.equal(groups[0].name, 'Renamed group'); assert.equal(groups[0].devices.length, 6)
props.devices = props.devices.filter(d => d.serial !== serials[4])
assert.equal(a.offlineMembers.value.length, 1); assert.equal(a.selectedOnline.value.length, 5)
await a.save(true); assert.ok(groups[0].devices.includes(serials[4]), 'offline saved members are preserved')
await a.save(false); assert.match(a.error.value, /already exists/); assert.equal(groups.length, 1)
await a.removeGroup(); assert.equal(groups.length, 1, 'deletion requires confirmation')
await a.removeGroup(); assert.equal(groups.length, 0)
assert.ok(calls.every(c => c.path.startsWith('/api/fleet/groups')), 'group operations never route phone input or proxies')
console.log('SELECTED_SYNC_AND_GROUPS=PASS (number ranges, member-only grid/desk input, group CRUD, offline members, explicit confirmation)')
