import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const root = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.join(root, "src/composables/fleetReboot.ts");

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
assert.equal(errors.length, 0, "fleetReboot.ts transpiles cleanly");
const moduleRequire = createRequire(path.join(root, "package.json"));
const mod = { exports: {} };
new Function("exports", "require", "module", compiled.outputText)(mod.exports, moduleRequire, mod);
const R = mod.exports;

const A = "192.168.50.57:5555", B = "192.168.50.56:5555", C = "192.168.50.55:5555", D = "192.168.50.54:5555", E = "192.168.50.53:5555";
const checks = {
  [A]: { online: true, usb_connected: true, agent_busy: false, rebooting: false },
  [B]: { online: true, usb_connected: false, agent_busy: false, rebooting: false },
  [C]: { online: true, usb_connected: false, agent_busy: true, rebooting: false },
  [D]: { online: false, usb_connected: false, agent_busy: false, rebooting: false },
  [E]: { online: false, usb_connected: false, agent_busy: false, rebooting: true },
};
let tests = 0;

// 1. Skip order matches the server: already rebooting, offline, Agent running.
const plan = R.rebootPlan([A, B, C, D, E, "missing:5555"], checks);
assert.deepEqual(plan, { eligible: [A, B], withUsb: [A], withoutUsb: [B], busy: [C], offline: [D, "missing:5555"], already: [E] }); tests++;
assert.deepEqual(R.rebootPlan([A], null).eligible, []); tests++;

// 2. Confirmation gate: phones without USB need the acknowledgement; nothing eligible -> disabled.
const dialog = (devices, extra = {}) => ({ devices, checks, loading: false, busy: false, error: "", acknowledged: false, ...extra });
assert.equal(R.canConfirmReboot(dialog([A])), true, "USB-only selection needs no acknowledgement"); tests++;
assert.equal(R.canConfirmReboot(dialog([A, B])), false, "a non-USB phone requires acknowledgement"); tests++;
assert.equal(R.canConfirmReboot(dialog([A, B], { acknowledged: true })), true); tests++;
assert.equal(R.canConfirmReboot(dialog([C, D, E], { acknowledged: true })), false, "nothing eligible"); tests++;
assert.equal(R.canConfirmReboot(dialog([A], { loading: true })), false); tests++;
assert.equal(R.canConfirmReboot(dialog([A], { busy: true })), false); tests++;
assert.equal(R.canConfirmReboot(null), false); tests++;

// 3. Labels and errors.
assert.equal(R.rebootChipText({ state: "rebooting", elapsed_s: 31.6 }), "Restarting · 32 s"); tests++;
assert.equal(R.rebootChipText({ state: "online", back_after_s: 41 }), "Back in 41 s"); tests++;
assert.equal(R.rebootChipText({ state: "needs_usb" }), "Plug in USB"); tests++;
assert.equal(R.apiErrorText(new Error('API 422: {"detail":"Reboot requires confirm: true"}')), "Reboot requires confirm: true"); tests++;
assert.equal(R.apiErrorText(new Error("API 503: <html>bad gateway</html>")), "Request failed (503)"); tests++;
assert.equal(R.isRebootActive({ state: "needs_usb" }) && !R.isRebootActive({ state: "online" }) && !R.isRebootActive(null), true); tests++;

// 4. Controller flow with a fake API and fake timers.
const timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const calls = [];
let statusReply = { devices: {}, active: 0 };
async function api(pathName, opts = {}) {
  calls.push({ path: pathName, body: opts.body ? JSON.parse(opts.body) : null });
  if (pathName === "/api/phone/reboot/preflight") return { devices: checks };
  if (pathName === "/api/phone/reboot") return { started: [A, B], skipped: [], failed: [] };
  if (pathName === "/api/phone/reboot/status") return statusReply;
  throw new Error("unexpected " + pathName);
}
let backCount = 0;
const ctl = R.useFleetReboot(api, { pollMs: 3000, onBack: () => { backCount++; } });
await ctl.open([A, B, C, A]);
assert.deepEqual(calls[0], { path: "/api/phone/reboot/preflight", body: { devices: [A, B, C] } }, "duplicates removed before preflight"); tests++;
assert.equal(ctl.dialog.value.loading, false); tests++;
await ctl.confirm();
assert.equal(calls.length, 1, "confirm blocked until the non-USB phone is acknowledged"); tests++;
ctl.acknowledge(true);
statusReply = { devices: { [A]: { state: "rebooting", message: "", started_at: 1, elapsed_s: 2, usb_connected: true } }, active: 1 };
await ctl.confirm();
assert.deepEqual(calls[1], { path: "/api/phone/reboot", body: { devices: [A, B], confirm: true } }, "only eligible phones, with confirm: true"); tests++;
assert.equal(ctl.dialog.value, null, "dialog closes after the request"); tests++;
assert.equal(ctl.notice.value, "Reboot: 2 rebooting"); tests++;
assert.equal(ctl.activeCount.value, 1); tests++;
assert.equal(timers.at(-1).ms, 3000, "polls while a reboot is active"); tests++;

statusReply = { devices: { [A]: { state: "online", message: "", started_at: 1, elapsed_s: 40, usb_connected: true, back_after_s: 40 } }, active: 0 };
const pending = timers.length;
await ctl.refresh();
assert.equal(backCount, 1, "device list refreshes when a phone comes back"); tests++;
assert.equal(timers.length, pending, "polling stops when nothing is active"); tests++;
ctl.dismissFinished();
assert.equal(ctl.entries.value.length, 0, "finished results can be dismissed"); tests++;

// 5. Cancel ignores a late preflight reply; a failed request keeps the dialog with the error.
let release;
const slowApi = (p) => p === "/api/phone/reboot/preflight" ? new Promise((resolve) => { release = () => resolve({ devices: checks }); }) : Promise.reject(new Error('API 503: {"detail":"ADB inventory unavailable"}'));
const slow = R.useFleetReboot(slowApi);
const opening = slow.open([A]);
slow.cancel();
release();
await opening;
assert.equal(slow.dialog.value, null, "late preflight reply does not reopen a cancelled dialog"); tests++;
const failing = R.useFleetReboot(async (p) => p === "/api/phone/reboot/preflight" ? { devices: checks } : Promise.reject(new Error('API 503: {"detail":"ADB inventory unavailable"}')));
await failing.open([A]);
await failing.confirm();
assert.equal(failing.dialog.value.error, "ADB inventory unavailable"); tests++;
assert.equal(failing.dialog.value.busy, false); tests++;

// 6. View wiring contracts: confirmation-only entry points, never through Sync.
const view = fs.readFileSync(path.join(root, "src/views/PhoneAdminView.vue"), "utf8");
assert.ok(view.includes('@click="openReboot([d.serial])') && view.includes('@click="openReboot(selectedTiles.slice())"'), "tile menu and Reboot selected open the dialog"); tests++;
assert.ok(!/withSync\([^)]*reboot/i.test(view) && !view.includes("/api/phone/reboot'"), "the view never posts a reboot directly"); tests++;
assert.ok(view.includes("reboot.dispose()"), "polling stops on unmount"); tests++;
const dialogSource = fs.readFileSync(path.join(root, "src/components/RebootDialog.vue"), "utf8");
assert.ok(dialogSource.includes('aria-modal="true"') && dialogSource.includes('@keydown.esc="emit(\'cancel\')"') && dialogSource.includes(':disabled="!canConfirm"'), "modal dialog with Escape and gated confirm"); tests++;

console.log(`FLEET_REBOOT_TESTS=${tests} PASS`);
