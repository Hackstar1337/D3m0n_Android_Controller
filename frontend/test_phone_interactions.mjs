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
    path.join(process.env.APPDATA ?? "", "npm", "node_modules", "typescript"),
    "C:\\Users\\devel\\Downloads\\QoderIDE-CustomModels-Fixed100406-NoAutoUpdate\\Qoder IDE\\resources\\app\\extensions\\node_modules\\typescript",
    "C:\\Users\\devel\\Downloads\\QoderIDE-CustomModels-Fixed100406-NoAutoUpdate\\Qoder IDE\\resources\\app\\node_modules\\typescript",
    "C:\\Users\\devel\\OneDrive\\Desktop\\MANUS\\.work\\modelmatrix-windows\\ModelMatrix\\Dashboard\\node_modules\\typescript",
    "C:\\Users\\devel\\OneDrive\\Desktop\\MANUS\\.work\\modelmatrix-expanded\\ModelMatrix\\Dashboard\\node_modules\\typescript",
    "/usr/local/lib/node_modules/typescript",
    "/usr/lib/node_modules/typescript",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      // Keep looking; the test deliberately uses a remote/existing install.
    }
  }
  try {
    const globalRoot = execFileSync(process.platform === "win32" ? "npm.cmd" : "npm", ["root", "-g"], { encoding: "utf8" }).trim();
    return require(path.join(globalRoot, "typescript"));
  } catch {
    throw new Error("An existing TypeScript installation is required; no dependency was installed.");
  }
}

const ts = loadTypeScript();
const source = fs.readFileSync(sourcePath, "utf8");
const compiled = ts.transpileModule(source, {
  fileName: sourcePath,
  reportDiagnostics: true,
  compilerOptions: {
    target: ts.ScriptTarget.ES2020,
    module: ts.ModuleKind.CommonJS,
    strict: true,
    noUncheckedIndexedAccess: true,
    lib: ["ES2020", "DOM"],
  },
});
const diagnostics = (compiled.diagnostics ?? []).filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error);
assert.equal(diagnostics.length, 0, diagnostics.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"));
const moduleRecord = { exports: {} };
const execute = new Function("exports", "module", "require", "setTimeout", "clearTimeout", "Date", compiled.outputText);
execute(moduleRecord.exports, moduleRecord, require, setTimeout, clearTimeout, Date);
const { createPhoneInteractions } = moduleRecord.exports;
assert.equal(typeof createPhoneInteractions, "function");

const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const turn = () => new Promise((resolve) => setImmediate(resolve));

function canvasFixture() {
  const canvas = {
    width: 100,
    height: 200,
    ownerDocument: { activeElement: null },
    captures: [],
    releases: [],
    getBoundingClientRect() {
      return { left: 10, top: 20, width: 300, height: 300 };
    },
    setPointerCapture(pointerId) { this.captures.push(pointerId); },
    releasePointerCapture(pointerId) { this.releases.push(pointerId); },
  };
  canvas.ownerDocument.activeElement = canvas;
  return canvas;
}

function eventBase(overrides = {}) {
  return {
    target: null,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; this.prevented = (this.prevented ?? 0) + 1; },
    ...overrides,
  };
}

function pointer(overrides = {}) {
  return eventBase({ pointerId: 1, isPrimary: true, clientX: 160, clientY: 50, ...overrides });
}

function keyboard(canvas, overrides = {}) {
  return eventBase({ target: canvas, key: "a", ctrlKey: false, metaKey: false, altKey: false, isComposing: false, ...overrides });
}

function wheel(overrides = {}) {
  return eventBase({ clientX: 160, clientY: 50, deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, metaKey: false, ...overrides });
}

function clipboard(canvas, text, overrides = {}) {
  return eventBase({
    target: canvas,
    clipboardData: { getData(type) { assert.equal(type, "text/plain"); return text; } },
    ...overrides,
  });
}

function harness({ send, enabled = () => true, report } = {}) {
  const canvas = canvasFixture();
  const calls = [];
  const reports = [];
  const sender = send ?? (() => Promise.resolve({ ok: true }));
  const helper = createPhoneInteractions({
    element: () => canvas,
    enabled,
    send: async (pathName, body) => {
      calls.push({ path: pathName, body });
      return sender(pathName, body);
    },
    focus: () => { canvas.focused = true; canvas.ownerDocument.activeElement = canvas; },
    report: report ?? ((message) => reports.push(message)),
  });
  return { canvas, helper, calls, reports };
}

async function testGeometryAndGestures() {
  const clock = Date.now;
  let now = 1000;
  Date.now = () => now;
  try {
    const h = harness();
    const margin = pointer({ clientX: 20, clientY: 25 });
    h.helper.pointerDown(margin);
    assert.deepEqual(h.canvas.captures, []);
    const down = pointer({ pointerId: 4 });
    h.helper.pointerDown(down);
    assert.deepEqual(h.canvas.captures, [4]);
    assert.equal(h.canvas.focused, true);
    now += 40;
    h.helper.pointerUp(pointer({ pointerId: 4 }));
    await turn();
    assert.equal(h.calls[0]?.path, "/api/phone/tap");
    assert.deepEqual(h.calls[0]?.body, { x: 50, y: 20, stream_w: 100, stream_h: 200 });

    now += 20;
    h.helper.pointerDown(pointer({ pointerId: 5 }));
    now += 600;
    h.helper.pointerUp(pointer({ pointerId: 5 }));
    await turn();
    assert.equal(h.calls[1]?.path, "/api/phone/swipe");
    assert.equal(h.calls[1]?.body.x1, 50);
    assert.equal(h.calls[1]?.body.y1, 20);
    assert.equal(h.calls[1]?.body.duration >= 500, true);
    assert.equal(h.calls[1]?.body.duration <= 1500, true);

    now += 20;
    h.helper.pointerDown(pointer({ pointerId: 6 }));
    now += 80;
    h.helper.pointerUp(pointer({ pointerId: 6, clientX: 500, clientY: 500 }));
    await turn();
    assert.equal(h.calls[2]?.path, "/api/phone/swipe");
    assert.deepEqual([h.calls[2]?.body.x2, h.calls[2]?.body.y2], [99, 199]);
    assert.deepEqual(h.canvas.releases, [4, 5, 6]);
    h.helper.dispose();
  } finally {
    Date.now = clock;
  }
}

async function testWheelCoalescingAndModes() {
  const h = harness();
  const first = wheel({ deltaX: 4, deltaY: 10 });
  const second = wheel({ deltaX: 3, deltaY: 15 });
  h.helper.wheel(first);
  h.helper.wheel(second);
  assert.equal(first.prevented, 1);
  assert.equal(second.prevented, 1);
  assert.equal(h.calls.length, 0);
  await pause(125);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].path, "/api/phone/swipe");
  assert.equal(h.calls[0].body.x1 > h.calls[0].body.x2, true);
  assert.equal(h.calls[0].body.y1 > h.calls[0].body.y2, true);

  const line = wheel({ deltaMode: 1, deltaY: 1 });
  h.helper.wheel(line);
  await pause(125);
  assert.equal(h.calls[1].body.y1 - h.calls[1].body.y2, 11);
  const page = wheel({ deltaMode: 2, deltaY: 1 });
  h.helper.wheel(page);
  await pause(125);
  assert.equal(h.calls[2].body.y1 - h.calls[2].body.y2, 180);

  const outside = wheel({ clientX: 20, clientY: 25, deltaY: 20 });
  h.helper.wheel(outside);
  const zoom = wheel({ ctrlKey: true, deltaY: 20 });
  h.helper.wheel(zoom);
  await pause(120);
  assert.equal(outside.prevented ?? 0, 0);
  assert.equal(zoom.prevented ?? 0, 0);
  assert.equal(h.calls.length, 3);
  h.helper.dispose();
}

async function testTextOrderingAndKeyboardIsolation() {
  const pending = [];
  const h = harness({ send: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
  h.helper.keydown(keyboard(h.canvas, { key: "a" }));
  h.helper.keydown(keyboard(h.canvas, { key: "ArrowRight" }));
  await turn();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0].body, { action: "text", text: "a" });
  pending.shift()?.resolve({ ok: true });
  await turn();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1].body, { key: "DPAD_RIGHT" });
  pending.shift()?.resolve({ ok: true });
  await turn();

  const before = h.calls.length;
  h.canvas.ownerDocument.activeElement = { tagName: "INPUT" };
  h.helper.keydown(keyboard(h.canvas, { key: "ArrowUp" }));
  h.canvas.ownerDocument.activeElement = h.canvas;
  h.helper.keydown(keyboard({ tagName: "TEXTAREA" }, { key: "ArrowUp", target: { tagName: "TEXTAREA" } }));
  h.helper.keydown(keyboard(h.canvas, { key: "ArrowUp", ctrlKey: true }));
  h.helper.keydown(keyboard(h.canvas, { key: "ArrowUp", isComposing: true }));
  h.helper.keydown(keyboard(h.canvas, { key: "Tab" }));
  assert.equal(h.calls.length, before);
  h.helper.dispose();
}

async function testUnicodePasteAndQueueBound() {
  const h = harness();
  const unicode = "こんにちは 👋\nمرحبا";
  h.helper.paste(clipboard(h.canvas, unicode));
  await turn();
  assert.deepEqual(h.calls[0]?.body, { action: "text", text: unicode });
  assert.equal(h.calls[0]?.path, "/api/phone/input");
  h.helper.dispose();

  const gates = [];
  const q = harness({ send: () => new Promise((resolve, reject) => gates.push({ resolve, reject })) });
  let clock = 1;
  const originalNow = Date.now;
  Date.now = () => clock;
  try {
    for (let index = 0; index < 9; index += 1) {
      q.helper.pointerDown(pointer({ pointerId: index + 1 }));
      clock += 40;
      q.helper.pointerUp(pointer({ pointerId: index + 1 }));
      clock += 1;
    }
    await turn();
    assert.equal(q.calls.length, 1);
    assert.equal(q.reports.includes("Input queue full"), true);
    gates.shift()?.resolve({ ok: true });
    for (let index = 0; index < 7; index += 1) {
      await turn();
      gates.shift()?.resolve({ ok: true });
    }
    await turn();
    assert.equal(q.calls.length, 8);
  } finally {
    Date.now = originalNow;
    q.helper.dispose();
  }
}

async function testFailureCancelAndDispose() {
  const failed = harness({ send: async () => { throw new Error("offline"); } });
  failed.helper.paste(clipboard(failed.canvas, "x"));
  await turn();
  assert.equal(failed.reports.includes("Input failed"), true);
  failed.helper.dispose();

  const canceled = harness();
  canceled.helper.keydown(keyboard(canceled.canvas, { key: "x" }));
  canceled.helper.wheel(wheel({ deltaY: 20 }));
  canceled.helper.cancel();
  await pause(125);
  assert.equal(canceled.calls.length, 0);

  const inFlight = [];
  const active = harness({ send: () => new Promise((resolve) => inFlight.push(resolve)) });
  active.helper.paste(clipboard(active.canvas, "first"));
  await turn();
  assert.equal(active.calls.length, 1);
  active.helper.cancel();
  active.helper.paste(clipboard(active.canvas, "second"));
  await turn();
  assert.equal(active.calls.length, 1);
  inFlight.shift()?.({ ok: true });
  await turn();
  assert.equal(active.calls.length, 2);
  active.helper.dispose();
  active.helper.paste(clipboard(active.canvas, "third"));
  await pause(100);
  assert.equal(active.calls.length, 2);

  const beforeMicrotask = harness();
  beforeMicrotask.helper.paste(clipboard(beforeMicrotask.canvas, "drop"));
  beforeMicrotask.helper.cancel();
  await turn();
  assert.equal(beforeMicrotask.calls.length, 0);
  beforeMicrotask.helper.dispose();
}

await testGeometryAndGestures();
await testWheelCoalescingAndModes();
await testTextOrderingAndKeyboardIsolation();
await testUnicodePasteAndQueueBound();
await testFailureCancelAndDispose();
console.log("phone interaction tests passed");

async function testEdgeScrollUnsupportedKeysAndGestureOrder() {
 const h=harness();h.helper.pointerDown(pointer({pointerId:20,button:2}));h.helper.pointerUp(pointer({pointerId:20,button:2}));await turn();assert.equal(h.calls.length,0,'Right click is not a phone tap')
 for(const key of ['PageDown','PageUp','Insert','ContextMenu','F13'])h.helper.keydown(keyboard(h.canvas,{key}));await pause(100);assert.equal(h.calls.length,0,'Named browser keys must not be injected as literal text')
 h.helper.wheel(wheel({clientY:299,deltaY:1000}));await pause(125);assert.equal(h.calls.length,1);assert(h.calls[0].body.y1>h.calls[0].body.y2,'Wheel near the bottom edge must still scroll');assert(h.calls[0].body.y1<200)
 h.helper.keydown(keyboard(h.canvas,{key:'a'}));h.helper.pointerDown(pointer({pointerId:21}));h.helper.pointerUp(pointer({pointerId:21}));await turn();await turn();assert.equal(h.calls[1].body.text,'a');assert.equal(h.calls[2].path,'/api/phone/tap','Buffered text dispatches before the subsequent tap');h.helper.dispose()
}
await testEdgeScrollUnsupportedKeysAndGestureOrder();console.log('Edge scrolling, non-primary filtering, named-key safety and text/tap ordering tests passed')
