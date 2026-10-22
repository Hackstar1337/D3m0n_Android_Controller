import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = fs.existsSync(path.join(root, "phoneInteractions.ts")) ? path.join(root, "phoneInteractions.ts") : path.join(root, "src/composables/phoneInteractions.ts");

function loadTypeScript() {
  const candidates = [
    process.env.TYPESCRIPT_PATH,
    "typescript",
    path.join(root, "node_modules", "typescript"),
    path.join(process.env.APPDATA ?? "", "npm", "node_modules", "typescript"),
    "/usr/local/lib/node_modules/typescript",
    "/usr/lib/node_modules/typescript",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); } catch { /* keep looking */ }
  }
  const globalRoot = execFileSync(process.platform === "win32" ? "npm.cmd" : "npm", ["root", "-g"], { encoding: "utf8" }).trim();
  return require(path.join(globalRoot, "typescript"));
}

const ts = loadTypeScript();
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
  fileName: sourcePath,
  reportDiagnostics: true,
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, strict: true, noUncheckedIndexedAccess: true, lib: ["ES2020", "DOM"] },
});
const errors = (compiled.diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error);
assert.equal(errors.length, 0, errors.map((d) => ts.flattenDiagnosticMessageText(d.messageText, "\n")).join("\n"));
const moduleRecord = { exports: {} };
new Function("exports", "module", "require", "setTimeout", "clearTimeout", "Date", compiled.outputText)(moduleRecord.exports, moduleRecord, require, setTimeout, clearTimeout, Date);
const { createPhoneInteractions } = moduleRecord.exports;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const turn = () => new Promise((resolve) => setImmediate(resolve));

// 100x200 canvas shown in a 300x300 box at (10,20): scale 1.5, left 85, top 20.
function canvasFixture() {
  const canvas = {
    width: 100, height: 200, ownerDocument: { activeElement: null }, captures: [], releases: [],
    getBoundingClientRect() { return { left: 10, top: 20, width: 300, height: 300 }; },
    setPointerCapture(id) { this.captures.push(id); },
    releasePointerCapture(id) { this.releases.push(id); },
  };
  canvas.ownerDocument.activeElement = canvas;
  return canvas;
}
const event = (overrides = {}) => ({ pointerId: 1, isPrimary: true, button: 0, clientX: 160, clientY: 50, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...overrides });

function portFixture({ ready = true, downId = 42, keyResult = true } = {}) {
  const calls = [];
  const port = {
    ready: () => port.isReady,
    isReady: ready,
    down: (x, y) => { calls.push(["down", x, y]); return downId; },
    move: (id, x, y) => calls.push(["move", id, x, y]),
    up: (id, x, y) => calls.push(["up", id, x, y]),
    cancel: (id) => calls.push(["cancel", id]),
    key: (key) => { calls.push(["key", key]); return keyResult; },
  };
  return { port, calls };
}

function setup({ port = null, enabled = () => true, sendDelay = 0 } = {}) {
  const canvas = canvasFixture();
  const sends = [];
  const controls = createPhoneInteractions({
    element: () => canvas,
    enabled,
    send: (p, body) => { sends.push([p, body]); return sendDelay ? pause(sendDelay) : Promise.resolve({ ok: true }); },
    live: () => port,
  });
  return { canvas, sends, controls };
}

const close = (a, b) => Math.abs(a - b) < 1e-9;

// 1. A live gesture streams down -> move -> up with normalized coordinates and no HTTP request.
{
  const { port, calls } = portFixture();
  const { sends, controls, canvas } = setup({ port });
  controls.pointerDown(event());
  controls.pointerMove(event({ clientX: 175, clientY: 80 }));
  controls.pointerMove(event({ pointerId: 9, clientX: 200, clientY: 200 })); // other pointer ignored
  controls.pointerUp(event({ clientX: 175, clientY: 80 }));
  await turn();
  assert.deepEqual(calls.map((c) => c[0]), ["down", "move", "up"]);
  assert.ok(close(calls[0][1], 50 / 99) && close(calls[0][2], 20 / 199), "down normalized to the phone frame");
  assert.equal(calls[1][1], 42);
  assert.ok(close(calls[1][2], 60 / 99) && close(calls[1][3], 40 / 199));
  assert.deepEqual(calls[2].slice(0, 2), ["up", 42]);
  assert.equal(sends.length, 0, "no HTTP tap/swipe for a live gesture");
  assert.deepEqual(canvas.captures, [1]);
  assert.deepEqual(canvas.releases, [1]);
  // Moves after release are ignored.
  controls.pointerMove(event({ clientX: 190 }));
  assert.equal(calls.length, 3);
}

// 2. Port not ready -> unchanged HTTP tap.
{
  const { port, calls } = portFixture({ ready: false });
  const { sends, controls } = setup({ port });
  controls.pointerDown(event());
  controls.pointerMove(event({ clientX: 161 }));
  controls.pointerUp(event());
  await turn();
  assert.equal(calls.length, 0);
  assert.equal(sends.length, 1);
  assert.equal(sends[0][0], "/api/phone/tap");
  assert.deepEqual(sends[0][1], { x: 50, y: 20, stream_w: 100, stream_h: 200 });
}

// 3. No port configured at all (other callers) -> unchanged HTTP swipe.
{
  const canvas = canvasFixture();
  const sends = [];
  const controls = createPhoneInteractions({ element: () => canvas, enabled: () => true, send: (p, b) => { sends.push([p, b]); return Promise.resolve(); } });
  controls.pointerDown(event());
  controls.pointerMove(event({ clientX: 175, clientY: 140 }));
  controls.pointerUp(event({ clientX: 175, clientY: 140 }));
  await turn();
  assert.equal(sends[0][0], "/api/phone/swipe");
}

// 4. down() refused (returns null) -> the gesture falls back to HTTP on release.
{
  const { port, calls } = portFixture({ downId: null });
  const { sends, controls } = setup({ port });
  controls.pointerDown(event());
  controls.pointerMove(event({ clientX: 175, clientY: 140 }));
  controls.pointerUp(event({ clientX: 175, clientY: 140 }));
  await turn();
  assert.deepEqual(calls.map((c) => c[0]), ["down"]);
  assert.equal(sends[0][0], "/api/phone/swipe");
}

// 5. pointercancel, view cancel() and dispose() all lift the live finger exactly once.
for (const how of ["pointerCancel", "cancel", "dispose"]) {
  const { port, calls } = portFixture();
  const { sends, controls } = setup({ port });
  controls.pointerDown(event());
  if (how === "pointerCancel") controls.pointerCancel(event());
  else controls[how]();
  controls.pointerUp(event());
  await turn();
  assert.deepEqual(calls.map((c) => c[0]), ["down", "cancel"], how);
  assert.equal(calls[1][1], 42);
  assert.equal(sends.length, 0, how + " must not send an HTTP gesture");
}

// 6. Losing permission mid-gesture (stream stopped) cancels the live touch.
{
  const { port, calls } = portFixture();
  let allowed = true;
  const { controls } = setup({ port, enabled: () => allowed });
  controls.pointerDown(event());
  allowed = false;
  controls.pointerMove(event({ clientX: 170 }));
  assert.deepEqual(calls.map((c) => c[0]), ["down", "cancel"]);
}

// 7. Queued HTTP input keeps order: with typed text pending, the next touch uses HTTP too.
{
  const { port, calls } = portFixture();
  const { sends, controls, canvas } = setup({ port, sendDelay: 30 });
  controls.keydown(event({ target: canvas, key: "a", isComposing: false, ctrlKey: false, metaKey: false, altKey: false }));
  controls.pointerDown(event());
  controls.pointerUp(event());
  await pause(80);
  assert.equal(calls.length, 0, "live path skipped while HTTP input is queued");
  assert.deepEqual(sends.map((s) => s[0]), ["/api/phone/input", "/api/phone/tap"]);
}

// 8. Letterbox margins are still not device input.
{
  const { port, calls } = portFixture();
  const { sends, controls } = setup({ port });
  controls.pointerDown(event({ clientX: 30 }));
  controls.pointerUp(event({ clientX: 30 }));
  await turn();
  assert.equal(calls.length, 0);
  assert.equal(sends.length, 0);
}

// 9. Mapped keys go live when accepted, HTTP otherwise.
{
  const { port, calls } = portFixture();
  const { sends, controls, canvas } = setup({ port });
  const esc = event({ target: canvas, key: "Escape", isComposing: false, ctrlKey: false, metaKey: false, altKey: false });
  controls.keydown(esc);
  await turn();
  assert.deepEqual(calls, [["key", "BACK"]]);
  assert.equal(sends.length, 0);
  assert.ok(esc.defaultPrevented);
  const refused = portFixture({ keyResult: false });
  const second = setup({ port: refused.port });
  second.controls.keydown(event({ target: second.canvas, key: "Enter", isComposing: false, ctrlKey: false, metaKey: false, altKey: false }));
  await turn();
  assert.deepEqual(refused.calls, [["key", "ENTER"]]);
  assert.deepEqual(second.sends, [["/api/phone/key", { key: "ENTER" }]]);
}

// 10. A throwing port never breaks input: it falls back to HTTP.
{
  const port = { ready: () => true, down: () => { throw new Error("boom"); }, move() {}, up() {}, cancel() {} };
  const { sends, controls } = setup({ port });
  controls.pointerDown(event());
  controls.pointerUp(event());
  await turn();
  assert.equal(sends[0][0], "/api/phone/tap");
}

console.log("phoneInteractions live-path checks passed");
