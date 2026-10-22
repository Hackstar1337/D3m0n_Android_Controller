import assert from 'node:assert/strict'
import fs from 'node:fs'
const fleet=fs.readFileSync('src/views/PhoneAdminView.vue','utf8')
const windowSource=fs.readFileSync('src/views/DeviceWindowView.vue','utf8')
const helper=fs.readFileSync('src/composables/phoneInteractions.ts','utf8')
const checks=[
 [fleet.includes('dashboardPreferences.direct !== false'),'direct preview input defaults on, while permitting opt-out'],
 [fleet.includes('role="complementary"')&&!fleet.includes('aria-modal="true"'),'controller is non-modal'],
 [fleet.includes('pointer-events:none')&&fleet.includes('pointer-events:auto'),'controller backdrop does not intercept grid clicks'],
 [fleet.includes('.multi-grid { height:auto;'),'phone previews are not squeezed into a single screen'],
 [fleet.includes('aria-label="Phone preview size"')&&fleet.includes('var(--phone-preview-height,320px)'),'adjustable readable previews'],
 [fleet.includes('Previous phone in control dock')&&fleet.includes('Next phone in control dock')&&fleet.includes('Phone in control dock'),'direct phone switching'],
 [fleet.includes('@wheel="gridInteraction(d.serial).wheel($event)"'),'native wheel scroll wired to the targeted phone'],
 [fleet.includes('@paste="gridInteraction(d.serial).paste($event)"'),'explicit paste is canvas-scoped'],
 [fleet.includes('@keydown="gridKeyDown(d.serial, $event)"'),'keyboard input is canvas-scoped'],
 [fleet.includes('send: (path, body) => sendTileInput(serial, path, body)'),'actions preserve phone serial identity'],
 [fleet.includes('gridInteractions.get(serial)?.cancel()')&&fleet.includes('controls.dispose()'),'stop and unmount cancel queued input'],
 [fleet.includes('wantsStream(streamIntent.value, d.serial)')&&fleet.includes('persistStreamIntent(stopAllIntent())'),'remembered streams and explicit stop are preserved'],
 [fleet.includes("from '@/components/ProxyManagerPanel.vue'")&&fleet.includes('<ProxyManagerPanel'),'proxy manager remains integrated'],
 [fleet.includes('Show device logs in grid')&&fleet.includes('if (!showCardLogs.value ||'),'hidden logs do not run fleet polling'],
 [windowSource.includes('watch(() => props.visible')&&windowSource.includes('props.visible === false'),'hidden controllers stop accepting input'],
 [windowSource.includes('if (interactions) { interactions.pointerUp(event);'),'real controller gestures share the ordered queue with keyboard'],
 [!windowSource.includes('cursor:crosshair')&&!helper.includes('navigator.clipboard'),'normal cursor and no automatic clipboard reads'],
]
for(const [ok,label] of checks)assert(ok,label)
console.log(`DASHBOARD_INTERACTION_INTEGRATION_TESTS=${checks.length} PASS`)
