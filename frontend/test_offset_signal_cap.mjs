import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const s=fs.readFileSync('src/views/PhoneAdminView.vue','utf8')
const a=s.indexOf('type AgentRunState ='),b=s.indexOf('function syncLeaseTargets()',a)
assert.ok(a>0&&b>a)
let now=10000
const ctx={ref:value=>({value}),watch:()=>{},Date:{now:()=>now},subTab:{value:'multi'},document:{hidden:false},proxyRoutingFresh:{value:true},proxyRoutingUpdatedAt:{value:now},tileRoute:serial=>routes[serial]||null}
const routes={}
const js=ts.transpileModule(s.slice(a,b)+ '\nglobalThis.h={ aggregateAgentRuns, tileDockState, tileDockTitle, agentRunState, agentRunsBySerial, agentRunsFresh, agentRunsUpdatedAt, dockClock, clearAgentRunState };',{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText
vm.runInNewContext(js,ctx)
const h=ctx.h,serial='192.168.50.37:5555',other='192.168.50.38:5555'
const apply=rows=>{h.agentRunsBySerial.value=h.aggregateAgentRuns(rows);h.agentRunsFresh.value=true;h.agentRunsUpdatedAt.value=now;h.dockClock.value=now}
assert.equal(h.tileDockState(serial,'agent'),'unknown','initial unavailable state is not off or running')
apply([]);assert.equal(h.tileDockState(serial,'agent'),'off','fresh empty registry means no executing task')
apply([{device:serial,running:false,stopping:false,model:'loaded'}]);assert.equal(h.tileDockState(serial,'agent'),'off','loaded model/session does not light robot')
apply([{device:serial,running:true,stopping:false}]);assert.equal(h.tileDockState(serial,'agent'),'active');assert.equal(h.tileDockState(other,'agent'),'off','serial isolation')
apply([{device:serial,running:true,stopping:false},{device:serial,running:false,stopping:true}]);assert.equal(h.tileDockState(serial,'agent'),'active','another stopping session cannot hide a running one')
apply([{device:serial,running:false,stopping:true}]);assert.equal(h.tileDockState(serial,'agent'),'unknown');assert.match(h.tileDockTitle(serial,'agent'),/stopping/)
apply([{device:serial,running:false,stopping:false}]);assert.equal(h.tileDockState(serial,'agent'),'off','completed run clears robot')
for(const invalid of [{unexpected:true},[{device:serial}], [{device:serial,running:'true',stopping:false}],null])assert.throws(()=>h.aggregateAgentRuns(invalid),/unavailable/)
apply([{device:serial,running:true,stopping:false}]);now+=8000;h.dockClock.value=now;assert.equal(h.tileDockState(serial,'agent'),'unknown','stale positive state expires');h.clearAgentRunState();assert.equal(h.tileDockState(serial,'agent'),'unknown','failure is unknown not falsely off')
now=10000;h.dockClock.value=now
assert.equal(h.tileDockState(serial,'proxy'),'off');routes[serial]={mode:'proxy',exitIp:'',verified:false};assert.equal(h.tileDockState(serial,'proxy'),'unknown');routes[serial]={mode:'proxy',exitIp:'203.0.113.11',verified:false};assert.equal(h.tileDockState(serial,'proxy'),'unknown','server-only exit not on-device verified');routes[serial].verified=true;assert.equal(h.tileDockState(serial,'proxy'),'active');ctx.proxyRoutingFresh.value=false;assert.equal(h.tileDockState(serial,'proxy'),'unknown');ctx.proxyRoutingFresh.value=true;now+=46000;h.dockClock.value=now;assert.equal(h.tileDockState(serial,'proxy'),'unknown','old route metadata is not positive active proof')
const hudStart=s.indexOf('<!-- Glass Hub:');assert.ok(hudStart>0,'Glass Hub markup present')
const header=s.slice(hudStart,s.indexOf('<div v-if="iosRecoveryVisible',hudStart))
assert.equal((header.match(/class="cap-slot /g)||[]).length,0,'no empty reserved slots remain')
assert.equal((header.match(/class="hub-signal"/g)||[]).length,1,'one signal chip template for Sync, proxy and automation')
assert.equal((header.match(/class="hud-badge/g)||[]).length,0,'old badges removed')
assert.ok(header.includes('class="hub-power"')&&header.includes(':disabled="!!rebootActiveFor(d.serial)"'),'reboot progress shows on the power button')
assert.equal((header.match(/'tile-sync-indicator'/g)||[]).length,1,'no duplicate Sync logo')
assert.ok(header.includes('syncEnabled && syncPool.includes(d.serial)'))
assert.ok(header.includes('tileLabel(d)')&&header.includes('tileIp(d)')&&header.includes('padStart(2'))
assert.ok(header.includes("'is-' + hubSignal(d.serial, slot.key)"),'idle robot and shield are dim, never shown as active')
for(const label of ['Select stream ','Control device ','Phone actions ','Start stream ','Stop stream ','Open direct controls ','Reconnect H.264 ','Reboot phone '])assert.ok(header.includes(label),label+' accessible')
assert.ok(s.includes('controller.abort(), 1800')&&s.includes('agentRunPollBusy')&&s.includes("cache: 'no-store'")&&s.includes('document.removeEventListener(\'visibilitychange\''))
assert.ok(s.includes('if (!agentRunPollStopped && !controller.signal.aborted)'),'unmounted aborted response cannot restore state')
assert.ok(s.includes('const FIT_MIN_WIDTH = 140')&&s.includes('Math.min(FIT_MIN_WIDTH, width)'))
console.log('SIGNAL_HELPERS=PASS (run isolation, Stop, freshness, proxy verification, Glass Hub signal structure and control contracts)')
