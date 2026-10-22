import fs from 'node:fs'
import assert from 'node:assert/strict'
import ts from 'typescript'
const source=fs.readFileSync('src/views/PhoneAdminView.vue','utf8')
const start=source.indexOf('function mjpegImagePoint('), end=source.indexOf('function mjpegMouseDown(',start)
assert(start>0 && end>start)
const js=ts.transpile('export '+source.slice(start,end),{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022})
const {mjpegImagePoint}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'))
const el={width:360,height:720,getBoundingClientRect:()=>({left:0,top:0,width:300,height:150})}
assert.deepEqual(mjpegImagePoint(el,150,75),{x:180,y:360,width:360,height:720})
assert.equal(mjpegImagePoint(el,10,75),null,'Black sidebars must not send phone taps')
assert.equal(mjpegImagePoint(el,290,75),null)
assert.equal(mjpegImagePoint({...el,width:0},150,75),null)
assert.equal(mjpegImagePoint({...el,getBoundingClientRect:()=>({left:0,top:0,width:0,height:0})},0,0),null)
const landscape={width:720,height:360,getBoundingClientRect:()=>({left:10,top:20,width:300,height:200})}
assert.deepEqual(mjpegImagePoint(landscape,160,120),{x:360,y:180,width:720,height:360})
assert.equal(mjpegImagePoint(landscape,160,25),null,'Top letterbox must not send taps')
assert(source.includes('v-if="multiMounted" v-show="subTab === \'multi\'"'))
assert(!source.includes("previous === 'multi' && value !== 'multi') stopAllStreams()"))
assert(source.includes('rtc-video-multi-${d.serial}') && source.includes('rtc-video-single-${selectedDevice}'))
assert(source.includes("persistStreamIntent(stopAllIntent())"))
assert(source.includes('wantsStream(streamIntent.value, d.serial)'))
assert(source.includes('role="complementary"') && !source.includes('aria-modal="true"'))
assert(source.includes("@click=\"!gridTouch && openDirectControl(d.serial)\""))
assert(source.includes('gridInteraction(serial).pointerDown(event)') && source.includes('gridInteractions.get(serial)?.cancel()'))
assert(source.includes('window.removeEventListener(\'storage\', syncStreamIntent)'))
console.log('CARD_GEOMETRY_AND_STREAM_CONTRACT_TESTS=16 PASS')
