export interface StreamIntent {
  version: 1;
  all: boolean;
  devices: Record<string, boolean>;
}

const MAX_DEVICES = 256;
const SERIAL_PATTERN = /^[A-Za-z0-9_.:-]{1,150}$/;
const RESERVED_SERIALS = new Set(["__proto__", "prototype", "constructor"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isValidSerial(serial: unknown): serial is string {
  return (
    typeof serial === "string" &&
    SERIAL_PATTERN.test(serial) &&
    !RESERVED_SERIALS.has(serial)
  );
}

function freezeIntent(all: boolean, devices: Record<string, boolean>): StreamIntent {
  const copy: Record<string, boolean> = {};
  let count = 0;
  for (const serial of Object.keys(devices)) {
    if (count >= MAX_DEVICES || !isValidSerial(serial) || typeof devices[serial] !== "boolean") {
      continue;
    }
    copy[serial] = devices[serial]!;
    count += 1;
  }
  Object.freeze(copy);
  return Object.freeze({ version: 1 as const, all: Boolean(all), devices: copy });
}

const DISABLED_DEFAULT = freezeIntent(false, {});

function normalizedCurrent(current: StreamIntent): StreamIntent {
  if (!isRecord(current) || current.version !== 1 || typeof current.all !== "boolean" || !isRecord(current.devices)) {
    return parseStreamIntent(current);
  }
  return freezeIntent(current.all, current.devices as Record<string, boolean>);
}

/**
 * Sanitizes an already-parsed persisted JSON value. Strings are intentionally
 * not parsed here; callers must JSON.parse before calling this function.
 */
export function parseStreamIntent(value: unknown): StreamIntent {
  if (!isRecord(value) || value.version !== 1 || typeof value.all !== "boolean" || !isRecord(value.devices)) {
    return freezeIntent(DISABLED_DEFAULT.all, {});
  }
  return freezeIntent(value.all, value.devices as Record<string, boolean>);
}

export function startAllIntent(current: StreamIntent): StreamIntent {
  void current;
  return freezeIntent(true, {});
}

export function stopAllIntent(): StreamIntent {
  return freezeIntent(false, {});
}

export function setDeviceIntent(
  current: StreamIntent,
  serial: string,
  enabled: boolean,
): StreamIntent {
  const base = normalizedCurrent(current);
  if (!isValidSerial(serial) || typeof enabled !== "boolean") {
    return freezeIntent(base.all, base.devices);
  }
  const devices: Record<string, boolean> = {};
  for (const key of Object.keys(base.devices)) {
    devices[key] = base.devices[key]!;
  }
  devices[serial] = enabled;
  return freezeIntent(base.all, devices);
}

export function wantsStream(current: StreamIntent, serial: string): boolean {
  const base = normalizedCurrent(current);
  if (isValidSerial(serial) && Object.prototype.hasOwnProperty.call(base.devices, serial)) {
    return base.devices[serial]!;
  }
  return base.all;
}
