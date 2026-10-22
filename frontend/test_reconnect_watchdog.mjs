import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
const source=fs.readFileSync('src/composables/useH264Stream.ts','utf8')
const js=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText
let clock=0,id=0
const timers=new Map(),sockets=[]
function later(fn,ms,interval=false){const key=++id;timers.set(key,{fn,at:clock+ms,ms,interval});return key}
function advance(ms){const end=clock+ms;for(let n=0;n<10000;n++){let next;for(const [key,t] of timers)if(t.at<=end&&(!next||t.at<next[1].at))next=[key,t];if(!next)break;clock=next[1].at;timers.delete(next[0]);if(next[1].interval)timers.set(next[0],{...next[1],at:clock+next[1].ms});next[1].fn()}clock=end}
class Socket{constructor(){sockets.push(this)}close(){this.closed=true;if(this.onclose)this.onclose()}}
const module={exports:{}}
const context={exports:module.exports,module,require:n=>{assert.equal(n,'vue');return {ref:value=>({value})}},window:{VideoDecoder:function(){},setTimeout:(f,m)=>later(f,m),setInterval:(f,m)=>later(f,m,true)},WebSocket:Socket,URL,URLSearchParams,crypto:{randomUUID:()=> 'test-viewer'},location:{search:''},document:{hidden:false},performance:{now:()=>clock},Math:{...Math,round:Math.round,min:Math.min,max:Math.max,random:()=>0},console,fetch:()=>Promise.resolve(),clearInterval:k=>timers.delete(k),clearTimeout:k=>timers.delete(k)}
vm.runInNewContext(js,context)
const stream=module.exports.startH264Stream('wss://dashboard.example.test/api/phone/android-h264/192.168.50.37%3A5555',{getContext:()=>null})
assert.equal(sockets.length,1)
advance(35000);assert.equal(sockets.length,1)
advance(2000);assert.equal(sockets.length,2,'Watchdog must let its timer reconnect, not perpetually postpone it')
const active=sockets.at(-1);active.onopen();active.onmessage({data:JSON.stringify({status:'alive'})})
advance(30000);assert.equal(sockets.length,2,'Static-screen heartbeats must not cause a reset')
active.onmessage({data:JSON.stringify({status:'alive'})});advance(30000);assert.equal(sockets.length,2)
active.onclose();advance(1500);assert.equal(sockets.length,3,'Closed socket must retry once without user Start All')
stream.stop();const before=sockets.length;advance(120000);assert.equal(sockets.length,before,'Explicit Stop must prevent later retries')
assert.equal(stream.status.value,'stopped');assert.equal(timers.size,0)
console.log('RECONNECT_TIMER_AND_IDLE_HEARTBEAT_TESTS=PASS')
