import fs from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source=fs.readFileSync(new URL('../frontend/src/composables/deviceLayout.ts', import.meta.url),'utf8')
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const sandbox={exports:{}};vm.runInNewContext(js,sandbox)
const {cleanLayout,tileLayout}=sandbox.exports
let count=0
function test(name,fn){fn();count++;console.log('PASS '+name)}
const allowed=new Set(['phone1','phone2'])
test('invalid storage is safely ignored',()=>assert.equal(cleanLayout(null,allowed,500).length,0))
test('unknown and duplicate devices are excluded',()=>{const r=cleanLayout([{device:'phone1'},{device:'phone1'},{device:'unknown'}],allowed,500);assert.equal(r.length,1)})
test('coordinates and sizes are clamped',()=>{const [p]=cleanLayout([{device:'phone1',left:50000,top:-4,width:10000,height:0}],allowed,500);assert.equal(p.width,480);assert.equal(p.height,480);assert.equal(p.left,10);assert.equal(p.top,0)})
test('NaN values use finite defaults',()=>{const [p]=cleanLayout([{device:'phone1',width:NaN,left:Infinity}],allowed,500);assert.ok(Number.isFinite(p.width)&&Number.isFinite(p.left))})
test('desktop tiles do not overlap',()=>{const p=tileLayout([{device:'phone1'},{device:'phone2'}],1200);assert.ok(p[0].left+p[0].width<p[1].left)})
test('narrow layouts stack vertically',()=>{const p=tileLayout([{device:'phone1'},{device:'phone2'}],440);assert.ok(p[0].top+p[0].height<p[1].top);assert.ok(p[0].width<440)})
test('layout serialization round trips',()=>{const r=cleanLayout(JSON.parse(JSON.stringify([{device:'phone1',left:20,top:30,width:330,height:690,z:3}])),allowed,900);assert.equal(r[0].left,20);assert.equal(r[0].width,330)})
console.log('LAYOUT_TESTS_PASSED='+count)
