import fs from 'node:fs'
import assert from 'node:assert/strict'
const windowSource=fs.readFileSync('src/views/DeviceWindowView.vue','utf8')
const fleet=fs.readFileSync('src/views/PhoneAdminView.vue','utf8')
assert(!windowSource.includes('cursor:crosshair') && !windowSource.includes('cursor: crosshair'))
assert(!fleet.includes('crosshair'))
assert(fleet.includes("subTab.value === 'multi' && (!controlDevice.value || focusedGridDevice.value === serial)"))
assert(fleet.includes("document.hidden || controlDevice.value || subTab.value !== 'multi'"))
assert(fleet.includes('Continuous streaming on'))
assert(windowSource.includes('@pointercancel='))
console.log('NORMAL_CURSOR_AND_FOCUS_LOAD_TESTS=PASS')
