import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = fs.existsSync(path.join(root, "liveInput.ts")) ? path.join(root, "liveInput.ts") : path.join(root, "src/composables/liveInput.ts");

function loadTypeScript() {
  const candidates = [
    process.env.TYPESCRIPT_PATH, "typescript", path.join(root, "node_modules", "typescript"),
    path.join(process.env.APPDATA ?? "", "npm", "node_modules", "typescript"),
    "/usr/local/lib/node_modules/typescript", "/usr/lib/node_modules/typescript",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch { /* keep looking */ }
  }
  const globalRoot = execFileSync(process.platform === "win32" ? "npm.cmd" : "npm", ["root", "-g"], { encoding: "utf8" }).trim();
  return require(path.join(globalRoot, "typescript"));
}

const ts = loadTypeScript();
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
  fileName: sourcePath, reportDiagnostics: true,
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, strict: true, noUncheckedIndexedAccess: true, lib: ["ES2020", "DOM"] },
});
const errors = (compiled.diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error);
assert.equal(errors.length, 0, errors.map((d) => ts.flattenDiagnosticMessageText(d.messageText, "\n")).join("\n"));
const moduleRecord = { exports: {} };
new Function("exports", "module", "require", compiled.outputText)(moduleRecord.exports, moduleRecord, require);
const { createLiveInputChannel, LIVE_KEYS, liveInputChannel } = moduleRecord.exports;
assert.equal(typeof createLiveInputChannel, "function");
assert.equal(liveInputChannel.isReady(), false, "the page singleton never connects until acquired");

function harness() {
  let clock = 0;
  let timers = [];
  let seq = 0;
  const sockets = [];
  class FakeSocket {
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; this.closed = null; sockets.push(this); }
    send(data) { if (this.readyState !== 1) throw new Error("not open"); this.sent.push(JSON.parse(data)); }
    close(code, reason) { this.closed = [code, reason]; this.readyState = 3; }
    open() { this.readyState = 1; this.onopen?.({}); }
    hello() { this.open(); this.deliver({ t: "hello", v: 1, max_sync: 100, idle_release_ms: 4000 }); }
    deliver(message) { this.onmessage?.({ data: JSON.stringify(message) }); }
    drop() { this.readyState = 3; this.onclose?.({}); }
    of(t) { return this.sent.filter((m) => m.t === t); }
  }
  const channel = createLiveInputChannel({
    url: () => "wss://droid.example/api/phone/input/ws",
    createSocket: (url) => new FakeSocket(url),
    now: () => clock,
    setTimer: (fn, ms) => { const timer = { at: clock + ms, fn, id: ++seq }; timers.push(timer); return timer; },
    clearTimer: (timer) => { timers = timers.filter((t) => t !== timer); },
  });
  function advance(ms) {
    const end = clock + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = timers[0];
      if (!next || next.at > end) break;
      timers.shift();
      clock = next.at;
      next.fn();
    }
    clock = end;
  }
  return { channel, sockets, advance, pending: () => timers.length };
}

// 1. Connects on acquire; ready only after the server hello.
{
  const { channel, sockets } = harness();
  const release = channel.acquire();
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, "wss://droid.example/api/phone/input/ws");
  assert.equal(channel.down("dev1", [], 0.5, 0.5), null, "not ready before hello");
  sockets[0].open();
  assert.equal(channel.isReady(), false);
  sockets[0].hello();
  assert.equal(channel.isReady(), true);
  const second = channel.acquire();
  assert.equal(sockets.length, 1, "one socket per page");
  release(); release();
  assert.equal(channel.isReady(), true, "still held by the second user");
  second();
  assert.equal(channel.isReady(), false);
  assert.deepEqual(sockets[0].closed, [1000, "released"]);
}

// 2. Gesture messages: down shape, coalesced moves at <=1 per 8 ms with the latest point, up after a final move.
{
  const { channel, sockets, advance } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  const acks = [];
  const id = channel.down("dev1", ["dev2", "", "dev3", 7], 0.1234567, 1.5, (ack) => acks.push(ack));
  assert.equal(typeof id, "number");
  assert.deepEqual(ws.of("down")[0], { t: "down", id, d: "dev1", s: ["dev2", "dev3"], x: 0.12346, y: 1 });
  channel.move(id, 0.2, 0.2);
  channel.move(id, 0.3, 0.3);
  channel.move(id, 0.4, 0.4);
  assert.equal(ws.of("move").length, 0, "first move waits for the 8 ms slot");
  advance(8);
  assert.deepEqual(ws.of("move"), [{ t: "move", id, x: 0.4, y: 0.4 }], "only the latest point is sent");
  advance(20);
  channel.move(id, 0.5, 0.5);
  assert.equal(ws.of("move").length, 2, "an idle slot sends immediately");
  channel.move(id, 0.6, 0.6);
  channel.up(id, 0.7, 0.7);
  assert.deepEqual(ws.sent.slice(-2), [{ t: "move", id, x: 0.6, y: 0.6 }, { t: "up", id, x: 0.7, y: 0.7 }]);
  channel.move(id, 0.9, 0.9);
  assert.equal(ws.of("move").length, 3, "moves after up are ignored");
  ws.deliver({ t: "ack", id, phase: "down", ok: true, via: "control" });
  ws.deliver({ t: "ack", id, phase: "up", ok: true, via: "control", sync: { ok: 2, targets: 2 } });
  ws.deliver({ t: "ack", id, phase: "up", ok: true });
  assert.deepEqual(acks.map((a) => a.phase), ["down", "up"], "listener survives the down ack and is removed after up");
}

// 3. A resting finger sends hold keep-alives; moves count as activity.
{
  const { channel, sockets, advance } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  const id = channel.down("dev1", [], 0.5, 0.5);
  advance(1000);
  assert.deepEqual(ws.of("hold"), [{ t: "hold", id }]);
  advance(1000);
  assert.equal(ws.of("hold").length, 2);
  channel.up(id, 0.5, 0.5);
  advance(5000);
  assert.equal(ws.of("hold").length, 2, "keep-alive stops after release");
}

// 4. Congested socket: intermediate moves wait until the buffer drains.
{
  const { channel, sockets, advance } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  const id = channel.down("dev1", [], 0.5, 0.5);
  advance(10);
  ws.bufferedAmount = 64 * 1024;
  channel.move(id, 0.6, 0.6);
  advance(40);
  assert.equal(ws.of("move").length, 0);
  ws.bufferedAmount = 0;
  advance(8);
  assert.deepEqual(ws.of("move"), [{ t: "move", id, x: 0.6, y: 0.6 }]);
}

// 5. New touch cancels an unfinished one; cancel drops its listener.
{
  const { channel, sockets } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  const acks = [];
  const first = channel.down("dev1", [], 0.1, 0.1, (a) => acks.push(a));
  const second = channel.down("dev1", [], 0.2, 0.2);
  assert.notEqual(first, second);
  assert.deepEqual(ws.sent.map((m) => m.t), ["down", "cancel", "down"]);
  ws.deliver({ t: "ack", id: first, phase: "up", ok: true, cancelled: true, reason: "superseded" });
  assert.equal(acks.length, 0);
}

// 6. Keys: only server-mapped keys go live.
{
  const { channel, sockets } = harness();
  channel.acquire();
  const ws = sockets[0];
  assert.equal(channel.key("dev1", [], "BACK"), false, "not ready");
  ws.hello();
  assert.equal(channel.key("dev1", ["dev2"], "NOT_A_KEY"), false);
  const acks = [];
  assert.equal(channel.key("dev1", ["dev2"], "APP_SWITCH", (a) => acks.push(a)), true);
  const sent = ws.of("key")[0];
  assert.deepEqual({ ...sent, id: 0 }, { t: "key", id: 0, d: "dev1", s: ["dev2"], k: "APP_SWITCH" });
  ws.deliver({ t: "ack", id: sent.id, phase: "key", ok: true, via: "control" });
  assert.equal(acks[0].ok, true);
  for (const key of ["HOME", "BACK", "APP_SWITCH", "WAKEUP", "VOLUME_UP", "VOLUME_DOWN", "ENTER", "DEL", "FORWARD_DEL", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "MOVE_HOME", "MOVE_END"]) {
    assert.ok(LIVE_KEYS.has(key), key);
  }
}

// 7. Server errors for a pending id reach the caller.
{
  const { channel, sockets } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  const acks = [];
  const id = channel.down("dev1", [], 0.1, 0.1, (a) => acks.push(a));
  ws.deliver({ t: "error", id, error: "Invalid device serial" });
  assert.equal(acks[0].ok, false);
  assert.equal(acks[0].error, "Invalid device serial");
  channel.move(id, 0.2, 0.2);
  assert.equal(ws.of("move").length, 0, "the gesture ended on error");
}

// 8. Ping measures round-trip time.
{
  const { channel, sockets, advance } = harness();
  channel.acquire();
  const ws = sockets[0];
  ws.hello();
  advance(5000);
  const ping = ws.of("ping")[0];
  assert.ok(ping);
  advance(23);
  ws.deliver({ t: "pong", id: ping.id });
  assert.equal(channel.rttMs(), 23);
}

// 9. Lost connection: pending callers are told, the gesture ends, reconnect uses backoff, then recovers.
{
  const { channel, sockets, advance } = harness();
  channel.acquire();
  sockets[0].hello();
  const acks = [];
  const id = channel.down("dev1", [], 0.1, 0.1, (a) => acks.push(a));
  sockets[0].drop();
  assert.equal(channel.isReady(), false);
  assert.deepEqual(acks.map((a) => [a.ok, a.error]), [[false, "Live input connection lost"]]);
  channel.move(id, 0.3, 0.3);
  advance(499);
  assert.equal(sockets.length, 1);
  advance(1);
  assert.equal(sockets.length, 2, "reconnects after 500 ms");
  sockets[1].drop();
  advance(999);
  assert.equal(sockets.length, 2);
  advance(1);
  assert.equal(sockets.length, 3, "second attempt waits 1 s");
  sockets[2].hello();
  assert.equal(channel.isReady(), true);
  sockets[2].drop();
  advance(500);
  assert.equal(sockets.length, 4, "a successful hello resets the backoff");
}

// 10. After release nothing reconnects.
{
  const { channel, sockets, advance, pending } = harness();
  const release = channel.acquire();
  sockets[0].hello();
  release();
  sockets[0].drop();
  advance(10000);
  assert.equal(sockets.length, 1);
  assert.equal(pending(), 0, "no timers left behind");
}

console.log("liveInput channel checks passed");
