import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);
let ts;
try {
  ts = require("typescript");
} catch {
  const globalCandidates = [
    process.env.APPDATA && path.join(process.env.APPDATA, "npm", "node_modules", "typescript"),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, "nodejs", "node_modules", "typescript"),
  ].filter(Boolean);
  for (const editor of ["Microsoft VS Code", "Trae"]) {
    const editorRoot = process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Programs", editor);
    if (!editorRoot || !fs.existsSync(editorRoot)) continue;
    for (const entry of fs.readdirSync(editorRoot, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        globalCandidates.push(path.join(editorRoot, entry.name, "resources", "app", "extensions", "node_modules", "typescript"));
      }
    }
  }
  for (const candidate of globalCandidates) {
    try {
      ts = require(candidate);
      break;
    } catch {
      // Try the next standard global installation location.
    }
  }
  if (!ts) throw new Error("TypeScript is required to run test_stream_intent.mjs");
}
const sibling = new URL("./streamIntent.ts", import.meta.url);
const sourceUrl = fs.existsSync(sibling) ? sibling : new URL("./src/composables/streamIntent.ts", import.meta.url);
const source = await (await import("node:fs/promises")).readFile(sourceUrl, "utf8");
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    strict: true,
  },
  fileName: "streamIntent.ts",
}).outputText;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`;
const {
  parseStreamIntent,
  startAllIntent,
  stopAllIntent,
  setDeviceIntent,
  wantsStream,
} = await import(moduleUrl);

const serial = "camera-01";
const other = "phone_2";

// 1: the disabled default is stable and correctly shaped.
const disabled = parseStreamIntent(null);
assert.deepEqual(disabled, { version: 1, all: false, devices: {} });

// 2: JSON-string input is not implicitly parsed.
assert.deepEqual(parseStreamIntent('{"version":1,"all":true,"devices":{}}'), disabled);

// 3: valid persisted values reload with the same behavior.
const persisted = parseStreamIntent({ version: 1, all: true, devices: { [serial]: false } });
const reloaded = parseStreamIntent(JSON.parse(JSON.stringify(persisted)));
assert.deepEqual(reloaded, persisted);
assert.equal(wantsStream(reloaded, serial), false);
assert.equal(wantsStream(reloaded, "new-camera"), true);

// 4: StopAll clears overrides and disables every device.
const stopped = stopAllIntent();
assert.deepEqual(stopped, { version: 1, all: false, devices: {} });
assert.equal(wantsStream(stopped, serial), false);

// 5: stopping one device while all are active leaves an explicit false override.
const stoppedOne = setDeviceIntent(startAllIntent(disabled), serial, false);
assert.equal(stoppedOne.all, true);
assert.equal(stoppedOne.devices[serial], false);
assert.equal(wantsStream(stoppedOne, serial), false);
assert.equal(wantsStream(stoppedOne, other), true);

// 6: an individual device can be started after StopAll without enabling others.
const startedOne = setDeviceIntent(stopped, serial, true);
assert.equal(wantsStream(startedOne, serial), true);
assert.equal(wantsStream(startedOne, other), false);

// 7: StartAll resets prior individual stops/starts.
const reset = startAllIntent(stoppedOne);
assert.deepEqual(reset, { version: 1, all: true, devices: {} });
assert.equal(wantsStream(reset, serial), true);

// 8: malformed top-level data gracefully falls back to disabled.
for (const malformed of [undefined, 0, [], {}, { version: 2, all: true, devices: {} }, { version: 1, all: "yes", devices: {} }, { version: 1, all: true, devices: [] }]) {
  assert.deepEqual(parseStreamIntent(malformed), disabled);
}

// 9: invalid and prototype-polluting serials are excluded.
const unsafe = parseStreamIntent(JSON.parse(`{
  "version": 1,
  "all": true,
  "devices": { "bad serial": false, "__proto__": false, "prototype": false, "constructor": false, "IOS": false }
}`));
assert.deepEqual(unsafe.devices, { IOS: false });
assert.equal(Object.prototype.polluted, undefined);
assert.equal(wantsStream(unsafe, "IOS"), false);

// 10: invalid device keys/values are discarded while valid entries survive.
const sanitized = parseStreamIntent({
  version: 1,
  all: false,
  devices: { good: true, nope: "true", "a/b": true, ["x".repeat(151)]: true },
});
assert.deepEqual(sanitized.devices, { good: true });

// 11: persisted devices are bounded at 256 keys deterministically.
const many = {};
for (let index = 0; index < 300; index += 1) many[`device-${index}`] = true;
const bounded = parseStreamIntent({ version: 1, all: true, devices: many });
assert.equal(Object.keys(bounded.devices).length, 256);
assert.equal(bounded.devices["device-255"], true);
assert.equal(bounded.devices["device-256"], undefined);

// 12: helpers do not mutate inputs and return fresh immutable values.
const original = parseStreamIntent({ version: 1, all: false, devices: { [serial]: true } });
const changed = setDeviceIntent(original, serial, false);
assert.notStrictEqual(changed, original);
assert.notStrictEqual(changed.devices, original.devices);
assert.equal(original.devices[serial], true);
assert.throws(() => { changed.all = false; }, TypeError);
assert.throws(() => { changed.devices[serial] = true; }, TypeError);

// 13: invalid serial updates are ignored without prototype pollution.
const unchanged = setDeviceIntent(original, "__proto__", false);
assert.deepEqual(unchanged, original);
assert.equal(Object.prototype.polluted, undefined);

console.log("stream-intent tests passed: 13 assertion groups");
