// Glass Hub (successor to the Glass HUD overlay): structure, signal truth, actions, sizing and Control desk bar contracts.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const s = fs.readFileSync('src/views/PhoneAdminView.vue', 'utf8')
const icons = fs.readFileSync('src/components/ui/UiIcon.vue', 'utf8')
const start = s.indexOf('<!-- Glass Hub:')
const hub = s.slice(start, s.indexOf('<div v-if="iosRecoveryVisible', start))
assert.ok(start > 0 && hub.length > 400, 'Glass Hub tile markup present')
assert.ok(!/glass-hud|hud-badge|dock-identity|cap-slot|offset-signal-cap/.test(s), 'old HUD, badge, cap and dock-identity markup and styles removed')

// Identity: the number chip selects the phone and carries the stream dot; name + complete IP open the Control desk.
const chip = hub.slice(hub.indexOf('<label class="tile-select hub-chip"'), hub.indexOf('</label>'))
for (const needle of ['type="checkbox"', 'v-model="selectedTiles"', 'class="tile-number"', 'padStart(2', 'class="hub-dot"', "'is-' + tileState(d.serial)"])
  assert.ok(chip.includes(needle), 'number chip keeps ' + needle)
const id = hub.slice(hub.indexOf('class="multi-device-name device-name-control tile-id"'), hub.indexOf('class="hub-signals"'))
for (const needle of ['tileLabel(d)', 'tileIp(d)', "'is-' + tileIpState(d.serial)", '@click="openDirectControl(d.serial)"', 'tileIdentityTitle(d)'])
  assert.ok(id.includes(needle), 'identity keeps ' + needle)

// Signals: one fixed chip per slot, each with a truthful level and an accessible description.
const sig = hub.slice(hub.indexOf('class="hub-signals"'), hub.indexOf('class="hub-actions"'))
assert.equal((sig.match(/<span v-for="slot in HUB_SLOTS"/g) || []).length, 1, 'signal chips come from the single slot list')
for (const needle of ["'is-' + hubSignal(d.serial, slot.key)", 'role="img"', 'hubSignalTitle(d.serial, slot.key)', "'tile-sync-indicator': slot.key === 'sync' && syncEnabled && syncPool.includes(d.serial)"])
  assert.ok(sig.includes(needle), 'signal chip keeps ' + needle)
assert.match(s, /const HUB_SLOTS = \[\s*\{ key: 'sync', label: 'Sync', icon: 'refresh' \},\s*\{ key: 'proxy', label: 'Proxy', icon: 'shieldLock' \},\s*\{ key: 'agent', label: 'Automation', icon: 'bot' \},\s*\] as const/, 'Sync, Proxy, Automation in a fixed order')

// Actions: power (reboot with confirmation) then the ⋯ menu; power is disabled and shows progress during a reboot.
const actions = hub.slice(hub.indexOf('class="hub-actions"'))
assert.ok(actions.indexOf('<details class="tile-quick"') >= 0 && actions.indexOf('<details class="tile-quick"') < actions.indexOf('class="hub-power"'), 'menu is left of power in both DOM and visual order')
for (const needle of ['@click.stop="openReboot([d.serial])"', ':disabled="!!rebootActiveFor(d.serial)"', "'is-rebooting': !!rebootActiveFor(d.serial)", 'v-if="!isIosDevice(d.serial)"', '<UiIcon name="power"'])
  assert.ok(actions.includes(needle), 'power keeps ' + needle)
for (const label of ['Phone actions ', 'Start stream ', 'Stop stream ', 'Open direct controls ', 'Reconnect H.264 ', 'Reboot phone '])
  assert.ok(hub.includes(label), label + 'accessible')

// Styles: in-flow bar (never covers the phone), reserved-row actions, distinct selection and Sync frame.
const css = s.slice(s.indexOf('/* Glass Hub: integrated phone bezel.'))
const rule = sel => { const i = css.indexOf(sel + ' {'); assert.ok(i >= 0, 'rule ' + sel); return css.slice(i, css.indexOf('}', i)) }
const bar = rule('.multi-grid .multi-card > .multi-card-header.glass-hub')
assert.match(rule('.multi-grid .multi-card'), /border: 1px solid #62728b; border-radius: 12px/, 'one visible continuous outer phone frame')
assert.match(rule('.multi-grid .multi-card'), /--phone-serif: \"Times New Roman\", Georgia, serif/, 'newspaper-style local serif font stack')
assert.match(rule('.multi-grid .glass-hub .tile-select .tile-number'), /font: 700 12px\/1 var\(--phone-serif\)/, 'number is smaller at 12px')
assert.match(rule('.multi-grid.hub-compact .glass-hub .tile-select .tile-number'), /font-size: 11px/, 'compact number is smaller at 11px')
assert.ok(!/position: absolute/.test(bar) && /border: 0/.test(bar) && /border-radius: 0/.test(bar) && /margin: 0/.test(bar) && /box-shadow: none/.test(bar), 'bezel has no inner panel, border, inset or shadow and never overlays the feed')
assert.ok(!css.includes('glass-hub::after'), 'no divider between bezel and phone')
assert.match(bar, /background-color: var\(--phone-surface, #07090c\)/, 'adaptive material matches the phone top color')
assert.match(bar, /background-image: linear-gradient/, 'subtle tint continues into the source top color')
assert.match(rule('.multi-grid .multi-stream-area'), /background: var\(--phone-surface, #07090c\)/, 'stream letterbox and bezel share the adaptive material')
for (const needle of ['createPhoneTintManager()', 'phoneTint.attach(serial,', 'phoneTint.start()', 'phoneTint.stop()', ':ref="tileRef(d.serial)"']) assert.ok(s.includes(needle), 'tint lifecycle keeps ' + needle)
const acts = rule('.multi-grid .glass-hub .hub-actions')
assert.match(bar, /display: grid; grid-template-columns: 24px minmax\(0, 1fr\); grid-template-rows: 32px 16px/, 'identity and icons have real reserved rows')
assert.match(acts, /grid-row: 1; grid-column: 2; justify-self: end/, 'power/menu move to the upper-right identity row')
assert.match(acts, /position: relative/); assert.match(acts, /opacity: 1; pointer-events: auto/, 'controls are visible in their own space')
assert.match(rule('.multi-grid .glass-hub .hub-signals'), /grid-row: 2; grid-column: 1 \/ -1; justify-self: end/, 'status glyphs move to the lower-right row')
assert.match(rule('.multi-grid .glass-hub .tile-id .device-name-text'), /max-width: calc\(100% - 46px\)/, 'model reserves room for status glyphs')
assert.match(rule('.multi-grid.hub-compact .glass-hub .hub-signals'), /position: static/, 'compact glyphs keep the lower-right row')
assert.ok(!css.includes('.glass-hub:hover .hub-signals') && !css.includes('.glass-hub:hover .hub-actions'), 'hover never hides or swaps visuals')
assert.match(rule('.hub-power'), /width: 18px; height: 18px/, 'slightly smaller action targets in both layouts')
const menuRule = rule('.multi-grid .tile-quick__menu')
assert.match(menuRule, /width: min\(132px, calc\(100cqw - 12px\)\)/, 'menu stays within its own phone width')
assert.match(menuRule, /max-height: max\(56px, calc\(var\(--phone-preview-height, 160px\) - 10px\)\)/, 'menu is bounded to its phone feed height')
assert.match(menuRule, /overflow-y: auto/, 'short tiles scroll the menu instead of covering neighboring phones')
assert.match(menuRule, /top: 58px/, 'full menu opens below the entire bezel')
assert.match(menuRule, /right: -20px/, 'left-side trigger keeps the menu aligned to the whole phone edge')
assert.match(rule('.multi-grid.hub-compact .glass-hub .tile-quick__menu'), /top: 52px/, 'compact menu opens below the entire bezel')
assert.match(rule('.multi-grid .glass-hub .tile-quick__btn'), /flex-shrink: 0/, 'menu buttons do not shrink when scrolling')
assert.ok(!hub.includes('HUB_SLOTS.map') && sig.includes('hubSignalTitle(d.serial, slot.key)'), 'actions tooltip stays short; each visible status retains its own description')
assert.ok(css.includes('.multi-grid .multi-card-header.glass-hub.is-selected {'), 'selection remains on the number, not a second panel')
assert.ok(css.includes('.multi-grid .multi-card.is-sync-target, .multi-grid.density-wall .multi-card.is-sync-target { border-color: #3fd3dc;'), 'synced phones keep the cyan frame')
for (const level of ['is-on', 'is-warn', 'is-bad']) assert.ok(css.includes('.hub-signal.' + level + ' {'), 'signal level style ' + level)
assert.match(css, /@container tile \(min-width: 236px\) \{/, 'wider tiles enlarge the signal chips')

// Sizing: zero-inset bezel CSS heights must equal Fit constants, or Fit mis-sizes the phone.
const px = (text, re) => Number((re.exec(text) || [])[1])
const full = px(bar, /height: (\d+)px/), inset = px(bar, /margin: (\d+);/)
const compactBar = rule('.multi-grid.hub-compact .multi-card > .multi-card-header.glass-hub')
const compact = px(compactBar, /height: (\d+)px/)
const konst = name => px(s, new RegExp('const ' + name + ' = (\\d+)'))
assert.equal(full + inset, konst('HUB_FULL_HEIGHT'), 'full hub CSS height matches HUB_FULL_HEIGHT')
assert.equal(compact + inset, konst('HUB_COMPACT_HEIGHT'), 'compact hub CSS height matches HUB_COMPACT_HEIGHT')
assert.ok(s.includes("'hub-compact': isFitLayout && hubCompact"), 'grid switches to the compact hub only in Fit')
assert.ok(s.includes('if (next.height < HUB_COMPACT_BELOW) { next = solve(HUB_COMPACT_HEIGHT); compact = true }'), 'compact hub only when the full hub would leave a small preview')
assert.ok(s.includes('if (!isFitLayout.value) { if (hubCompact.value) hubCompact.value = false; return }'), 'leaving Fit restores the full hub')

// Signal levels and values (executed): every slot maps to on/off/warn/bad and a short truthful value.
const a = s.indexOf('const HUB_SLOTS = ['), b = s.indexOf('function onAgentRunVisibility', a)
let sync = {}, dock = {}, route = {}, run = {}
const ctx = {
  tileSyncState: serial => sync[serial] || 'inactive', tileSyncTitle: () => 'sync title',
  tileDockState: (serial, slot) => dock[slot] || 'off', tileDockTitle: (serial, slot) => slot + ' title',
  tileRoute: () => route.value || null, agentRunState: () => run.value || null,
}
vm.runInNewContext(ts.transpileModule(s.slice(a, b) + '\nglobalThis.h = { hubSignal, hubSignalValue, hubSignalTitle }', { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, ctx)
const { hubSignal, hubSignalValue, hubSignalTitle } = ctx.h, p = '192.168.50.38:5555'
for (const [state, level, value] of [['inactive', 'off', 'Off'], ['pending', 'warn', 'Connecting'], ['ready', 'on', 'Connected'], ['fallback', 'warn', 'ADB fallback'], ['failed', 'bad', 'Failed'], ['stale-fail', 'warn', 'Check']]) {
  sync = { [p]: state }; assert.equal(hubSignal(p, 'sync'), level, 'sync ' + state); assert.equal(hubSignalValue(p, 'sync'), value)
}
sync = {}; assert.equal(hubSignalTitle(p, 'sync'), 'Sync off · not in the Sync set')
dock = { proxy: 'active' }; route = { value: { mode: 'proxy', exitIp: '76.33.248.177', verified: true } }
assert.equal(hubSignal(p, 'proxy'), 'on'); assert.equal(hubSignalValue(p, 'proxy'), '76.33.248.177', 'proxy value is the verified exit IP')
dock = { proxy: 'off' }; route = {}; assert.equal(hubSignal(p, 'proxy'), 'off'); assert.equal(hubSignalValue(p, 'proxy'), 'Off · LAN')
dock = { proxy: 'unknown' }; route = { value: { mode: 'proxy', exitIp: '', verified: false } }; assert.equal(hubSignal(p, 'proxy'), 'warn'); assert.equal(hubSignalValue(p, 'proxy'), 'Unverified')
route = {}; assert.equal(hubSignalValue(p, 'proxy'), 'Checking', 'stale routing is not shown as off or on')
dock = { agent: 'active' }; assert.equal(hubSignal(p, 'agent'), 'on'); assert.equal(hubSignalValue(p, 'agent'), 'Running')
dock = { agent: 'off' }; assert.equal(hubSignalValue(p, 'agent'), 'Idle')
dock = { agent: 'unknown' }; run = { value: { stopping: true } }; assert.equal(hubSignal(p, 'agent'), 'warn'); assert.equal(hubSignalValue(p, 'agent'), 'Stopping')
run = {}; assert.equal(hubSignalValue(p, 'agent'), 'Unavailable')

// Control desk: the full Glass Hub bar for the controlled phone.
const desk = s.slice(s.indexOf('<!-- Control desk Glass Hub:'), s.indexOf('<div class="control-device-switcher">'))
for (const needle of ['class="control-hub"', 'class="hub-chip"', "'is-' + tileState(controlDevice)", 'tileLabel(controlDeviceInfo)', 'lanIp(controlDeviceInfo)', 'v-for="slot in HUB_SLOTS"', "'is-' + hubSignal(controlDevice, slot.key)", 'hubSignalValue(controlDevice, slot.key)', 'class="control-hub__power"', '@click="openReboot([controlDevice])"', ':disabled="!!rebootActiveFor(controlDevice)"'])
  assert.ok(desk.includes(needle), 'Control desk bar keeps ' + needle)
assert.match(css, /@container \(max-width: 480px\) \{\s*\.control-hub__text \{ display: none; \}/, 'narrow desks show icon-only cells')
for (const name of ['power', 'bot', 'shieldLock']) assert.ok(new RegExp('\\n  ' + name + ": '<").test(icons), 'icon ' + name)
// Pressing a phone shows the input-target label; with a zero flex-basis it can never wrap the status row and shift the grid mid-tap.
assert.ok(s.includes('.input-target { display: block; flex: 1 1 0; min-width: 0; max-width: max-content; overflow: hidden; text-overflow: ellipsis;'), 'input-target label never wraps the status row')
console.log('GLASS_HUB_TESTS=PASS (identity chip, truthful signal levels/values, reserved-row power + menu, in-flow bar, CSS/Fit size parity, Control desk bar, icons)')
