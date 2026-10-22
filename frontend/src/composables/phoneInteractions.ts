/**
 * Real-time input port (see liveInput.ts). Coordinates are normalized 0..1.
 * When ready and no HTTP action is queued, touches stream live (down/move/up)
 * instead of being sent as one tap/swipe after release.
 */
export type LivePort = {
  ready: () => boolean;
  down: (x: number, y: number) => number | null;
  move: (id: number, x: number, y: number) => void;
  up: (id: number, x: number, y: number) => void;
  cancel: (id: number) => void;
  key?: (key: string) => boolean;
};

export type PhoneInteractionOptions = {
  element: () => HTMLCanvasElement | null;
  enabled: () => boolean;
  send: (path: string, body: Record<string, unknown>) => Promise<unknown>;
  focus?: () => void;
  report?: (message: string) => void;
  live?: () => LivePort | null;
};

type InteractionApi = {
  pointerDown: (event: PointerEvent) => void;
  pointerMove: (event: PointerEvent) => void;
  pointerUp: (event: PointerEvent) => void;
  pointerCancel: (event?: PointerEvent) => void;
  wheel: (event: WheelEvent) => void;
  keydown: (event: KeyboardEvent) => void;
  paste: (event: ClipboardEvent) => void;
  cancel: () => void;
  dispose: () => void;
};

type Point = { x: number; y: number };
type Geometry = { width: number; height: number; left: number; top: number; scale: number };
type Action = { path: string; body: Record<string, unknown> };
type ActivePointer = {
  id: number;
  canvas: HTMLCanvasElement;
  start: Point;
  startedAt: number;
  liveId: number | null;
  port: LivePort | null;
};

const MAX_ACTIONS = 8;
const TEXT_BATCH_MS = 80;
const WHEEL_BATCH_MS = 100;
const LONG_PRESS_MS = 500;
const MAX_GESTURE_MS = 1500;
const MIN_GESTURE_MS = 30;
const SWIPE_DISTANCE = 10;
const WHEEL_LINE_PX = 16;
const WHEEL_DURATION_MS = 100;

export function createPhoneInteractions(options: PhoneInteractionOptions): InteractionApi {
  let disposed = false;
  let inFlight = false;
  let dispatchEpoch = 0;
  const queue: Action[] = [];
  let activePointer: ActivePointer | null = null;

  let pendingText: string | null = null;
  let textTimer: ReturnType<typeof setTimeout> | null = null;

  let wheelTimer: ReturnType<typeof setTimeout> | null = null;
  let wheelX = 0;
  let wheelY = 0;
  let wheelAnchor: Point | null = null;
  let wheelCanvas: HTMLCanvasElement | null = null;

  function report(message: string): void {
    try {
      options.report?.(message);
    } catch {
      // Reporting must never turn device input into an unhandled exception.
    }
  }

  function enabled(): boolean {
    if (disposed) return false;
    try {
      return options.enabled();
    } catch {
      return false;
    }
  }

  function currentCanvas(): HTMLCanvasElement | null {
    try {
      return options.element();
    } catch {
      return null;
    }
  }

  function canvasSize(canvas: HTMLCanvasElement): { width: number; height: number } | null {
    const width = Number(canvas.width);
    const height = Number(canvas.height);
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
      return null;
    }
    return { width, height };
  }

  function geometry(canvas: HTMLCanvasElement): Geometry | null {
    const size = canvasSize(canvas);
    if (!size) return null;
    let rect: DOMRect;
    try {
      rect = canvas.getBoundingClientRect();
    } catch {
      return null;
    }
    const rectWidth = Number(rect.width);
    const rectHeight = Number(rect.height);
    if (!Number.isFinite(rectWidth) || !Number.isFinite(rectHeight) || rectWidth <= 0 || rectHeight <= 0) {
      return null;
    }
    const scale = Math.min(rectWidth / size.width, rectHeight / size.height);
    if (!Number.isFinite(scale) || scale <= 0) return null;
    const displayedWidth = size.width * scale;
    const displayedHeight = size.height * scale;
    return {
      width: size.width,
      height: size.height,
      left: Number(rect.left) + (rectWidth - displayedWidth) / 2,
      top: Number(rect.top) + (rectHeight - displayedHeight) / 2,
      scale,
    };
  }

  function pointForEvent(canvas: HTMLCanvasElement, event: { clientX: number; clientY: number }, clamp: boolean): Point | null {
    const box = geometry(canvas);
    if (!box) return null;
    const rawX = (Number(event.clientX) - box.left) / box.scale;
    const rawY = (Number(event.clientY) - box.top) / box.scale;
    if (!Number.isFinite(rawX) || !Number.isFinite(rawY)) return null;
    if (!clamp && (rawX < 0 || rawY < 0 || rawX > box.width || rawY > box.height)) return null;
    return {
      x: Math.max(0, Math.min(box.width - 1, rawX)),
      y: Math.max(0, Math.min(box.height - 1, rawY)),
    };
  }

  function integerPoint(point: Point): Point {
    return { x: Math.round(point.x), y: Math.round(point.y) };
  }

  function unit(point: Point, size: { width: number; height: number }): Point {
    const x = size.width > 1 ? point.x / (size.width - 1) : 0;
    const y = size.height > 1 ? point.y / (size.height - 1) : 0;
    return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
  }

  function livePort(): LivePort | null {
    if (!options.live || disposed) return null;
    try {
      const port = options.live();
      return port && port.ready() ? port : null;
    } catch {
      return null;
    }
  }

  function cancelLive(pointer: ActivePointer | null): void {
    if (!pointer || pointer.liveId === null || !pointer.port) return;
    const { port, liveId } = pointer;
    pointer.liveId = null;
    pointer.port = null;
    try {
      port.cancel(liveId);
    } catch {
      // The server also lifts the finger on disconnect or idle.
    }
  }

  function releasePointer(pointer: ActivePointer | null): void {
    if (!pointer) return;
    try {
      if (typeof pointer.canvas.releasePointerCapture === "function") {
        pointer.canvas.releasePointerCapture(pointer.id);
      }
    } catch {
      // Pointer capture can already have been released by the browser.
    }
  }

  function actionCount(): number {
    return (inFlight ? 1 : 0) + queue.length + (pendingText !== null ? 1 : 0);
  }

  function pump(): void {
    if (inFlight || disposed) return;
    const action = queue.shift();
    if (!action) return;
    inFlight = true;
    const epoch = dispatchEpoch;
    Promise.resolve()
      .then(() => {
        // Re-check both lifecycle and enabled state immediately before dispatch.
        if (disposed || epoch !== dispatchEpoch || !enabled()) return undefined;
        return options.send(action.path, action.body);
      })
      .catch(() => {
        report("Input failed");
      })
      .then(() => {
        inFlight = false;
        if (!disposed) pump();
      });
  }

  function enqueue(action: Action): boolean {
    if (disposed || actionCount() >= MAX_ACTIONS) {
      if (!disposed) report("Input queue full");
      return false;
    }
    queue.push(action);
    pump();
    return true;
  }

  function clearTextTimer(): void {
    if (textTimer !== null) {
      clearTimeout(textTimer);
      textTimer = null;
    }
  }

  function flushText(): void {
    clearTextTimer();
    const text = pendingText;
    pendingText = null;
    if (text === null || disposed) return;
    // pendingText counted as one reserved action, so moving it to queue cannot overflow.
    if ((inFlight ? 1 : 0) + queue.length >= MAX_ACTIONS) {
      report("Input queue full");
      return;
    }
    queue.push({ path: "/api/phone/input", body: { action: "text", text } });
    pump();
  }

  function queueText(text: string): void {
    if (text.length === 0 || disposed) return;
    if (pendingText === null && actionCount() >= MAX_ACTIONS) {
      report("Input queue full");
      return;
    }
    pendingText = (pendingText ?? "") + text;
    if (textTimer === null) {
      textTimer = setTimeout(() => {
        textTimer = null;
        flushText();
      }, TEXT_BATCH_MS);
    }
  }

  function clearWheel(): void {
    if (wheelTimer !== null) {
      clearTimeout(wheelTimer);
      wheelTimer = null;
    }
    wheelX = 0;
    wheelY = 0;
    wheelAnchor = null;
    wheelCanvas = null;
  }

  function flushWheel(): void {
    wheelTimer = null;
    const anchor = wheelAnchor;
    const canvas = wheelCanvas;
    const dx = wheelX;
    const dy = wheelY;
    wheelX = 0;
    wheelY = 0;
    wheelAnchor = null;
    wheelCanvas = null;
    if (!anchor || !canvas || disposed || (dx === 0 && dy === 0)) return;
    const size = canvasSize(canvas);
    if (!size) return;
    // Shift the gesture inward at screen edges rather than consuming a no-op wheel.
    const moveX = Math.max(-size.width * 0.9, Math.min(size.width * 0.9, dx));
    const moveY = Math.max(-size.height * 0.9, Math.min(size.height * 0.9, dy));
    const end = integerPoint({
      x: Math.max(Math.max(0, -moveX), Math.min(size.width - 1 - Math.max(0, moveX), anchor.x)),
      y: Math.max(Math.max(0, -moveY), Math.min(size.height - 1 - Math.max(0, moveY), anchor.y)),
    });
    const start = integerPoint({ x: Math.max(0, Math.min(size.width - 1, end.x + moveX)), y: Math.max(0, Math.min(size.height - 1, end.y + moveY)) });
    enqueue({
      path: "/api/phone/swipe",
      body: {
        x1: start.x,
        y1: start.y,
        x2: end.x,
        y2: end.y,
        duration: WHEEL_DURATION_MS,
        stream_w: size.width,
        stream_h: size.height,
      },
    });
  }

  function cancel(): void {
    dispatchEpoch += 1;
    clearTextTimer();
    clearWheel();
    pendingText = null;
    queue.length = 0;
    cancelLive(activePointer);
    releasePointer(activePointer);
    activePointer = null;
  }

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    cancel();
  }

  function pointerDown(event: PointerEvent): void {
    if (disposed || !enabled() || (event.button != null && event.button !== 0) || event.isPrimary === false || activePointer !== null) return;
    const canvas = currentCanvas();
    if (!canvas) return;
    const point = pointForEvent(canvas, event, false);
    if (!point) return; // Letterbox margins are not device input.
    flushText();
    if (wheelTimer !== null) { clearTimeout(wheelTimer); flushWheel(); }
    try { event.preventDefault(); } catch {}
    const pointer: ActivePointer = {
      id: event.pointerId,
      canvas,
      start: integerPoint(point),
      startedAt: Date.now(),
      liveId: null,
      port: null,
    };
    activePointer = pointer;
    try {
      if (typeof canvas.setPointerCapture === "function") canvas.setPointerCapture(event.pointerId);
    } catch {
      // Capture is best effort; the active-pointer guard still prevents multi-touch.
    }
    try {
      options.focus?.();
    } catch {
      // Focus is an aid for keyboard targeting, not a reason to fail input.
    }
    // Live path only when nothing is queued, so input order is preserved.
    const port = actionCount() === 0 ? livePort() : null;
    const size = canvasSize(canvas);
    if (port && size) {
      const at = unit(point, size);
      let id: number | null = null;
      try {
        id = port.down(at.x, at.y);
      } catch {
        id = null;
      }
      if (id !== null) {
        pointer.liveId = id;
        pointer.port = port;
      }
    }
  }

  function pointerMove(event: PointerEvent): void {
    const pointer = activePointer;
    if (!pointer || event.pointerId !== pointer.id || pointer.liveId === null || !pointer.port) return;
    if (!enabled()) {
      pointerCancel();
      return;
    }
    const point = pointForEvent(pointer.canvas, event, true);
    const size = canvasSize(pointer.canvas);
    if (!point || !size) return;
    const at = unit(point, size);
    try {
      pointer.port.move(pointer.liveId, at.x, at.y);
    } catch {
      // A dropped move is superseded by the next one or by release.
    }
  }

  function pointerUp(event: PointerEvent): void {
    const pointer = activePointer;
    if (!pointer || event.pointerId !== pointer.id) return;
    activePointer = null;
    releasePointer(pointer);
    if (pointer.liveId !== null && pointer.port) {
      const livePoint = pointForEvent(pointer.canvas, event, true);
      const liveSize = canvasSize(pointer.canvas);
      const { port, liveId } = pointer;
      pointer.liveId = null;
      pointer.port = null;
      try {
        if (livePoint && liveSize) {
          const at = unit(livePoint, liveSize);
          port.up(liveId, at.x, at.y);
        } else {
          port.cancel(liveId);
        }
      } catch {
        // The server lifts the finger if the release cannot be delivered.
      }
      return;
    }
    const point = pointForEvent(pointer.canvas, event, true);
    if (!point) return;
    const end = integerPoint(point);
    const elapsed = Math.max(0, Date.now() - pointer.startedAt);
    const distance = Math.hypot(end.x - pointer.start.x, end.y - pointer.start.y);
    const duration = Math.max(MIN_GESTURE_MS, Math.min(MAX_GESTURE_MS, elapsed));
    const size = canvasSize(pointer.canvas);
    if (!size) return;
    if (distance < SWIPE_DISTANCE && elapsed < LONG_PRESS_MS) {
      enqueue({
        path: "/api/phone/tap",
        body: { x: end.x, y: end.y, stream_w: size.width, stream_h: size.height },
      });
      return;
    }
    const longPress = distance < SWIPE_DISTANCE && elapsed >= LONG_PRESS_MS;
    enqueue({
      path: "/api/phone/swipe",
      body: {
        x1: pointer.start.x,
        y1: pointer.start.y,
        x2: end.x,
        y2: end.y,
        duration: longPress ? Math.max(LONG_PRESS_MS, duration) : duration,
        stream_w: size.width,
        stream_h: size.height,
      },
    });
  }

  function pointerCancel(event?: PointerEvent): void {
    if (event && activePointer && event.pointerId !== activePointer.id) return;
    cancelLive(activePointer);
    releasePointer(activePointer);
    activePointer = null;
  }

  function wheel(event: WheelEvent): void {
    if (disposed || !enabled() || event.ctrlKey || event.metaKey) return;
    const canvas = currentCanvas();
    if (!canvas) return;
    const point = pointForEvent(canvas, event, false);
    if (!point) return;
    const box = geometry(canvas);
    if (!box) return;
    let scale = 1;
    if (event.deltaMode === 1) scale = WHEEL_LINE_PX;
    else if (event.deltaMode === 2) scale = Math.max(1, box.height * box.scale);
    const normalizedX = Number(event.deltaX) * scale / box.scale;
    const normalizedY = Number(event.deltaY) * scale / box.scale;
    if (!Number.isFinite(normalizedX) || !Number.isFinite(normalizedY) || (normalizedX === 0 && normalizedY === 0)) {
      return;
    }
    if (wheelAnchor === null || wheelCanvas !== canvas) {
      if (wheelTimer !== null) {
        clearTimeout(wheelTimer);
        wheelTimer = null;
      }
      wheelAnchor = integerPoint(point);
      wheelCanvas = canvas;
      wheelX = 0;
      wheelY = 0;
    }
    wheelX += normalizedX;
    wheelY += normalizedY;
    if (wheelTimer === null) wheelTimer = setTimeout(flushWheel, WHEEL_BATCH_MS);
    try {
      event.preventDefault();
    } catch {
      // A synthetic/non-cancelable wheel event need not break device input.
    }
  }

  function focusedCanvas(event: Event, canvas: HTMLCanvasElement): boolean {
    if (event.target !== canvas) return false;
    const documentLike = canvas.ownerDocument;
    if (documentLike && documentLike.activeElement && documentLike.activeElement !== canvas) return false;
    return true;
  }

  function keydown(event: KeyboardEvent): void {
    if (disposed || event.defaultPrevented || event.isComposing || !enabled()) return;
    const canvas = currentCanvas();
    if (!canvas || !focusedCanvas(event, canvas)) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    const keys: Record<string, string> = {
      ArrowUp: "DPAD_UP",
      ArrowDown: "DPAD_DOWN",
      ArrowLeft: "DPAD_LEFT",
      ArrowRight: "DPAD_RIGHT",
      Backspace: "DEL",
      Delete: "FORWARD_DEL",
      Enter: "ENTER",
      Escape: "BACK",
      Home: "MOVE_HOME",
      End: "MOVE_END",
    };
    const key = keys[event.key];
    if (key !== undefined) {
      flushText();
      if (wheelTimer !== null) { clearTimeout(wheelTimer); flushWheel(); }
      try {
        event.preventDefault();
      } catch {
        // Ignore non-cancelable synthetic keyboard events.
      }
      const port = actionCount() === 0 ? livePort() : null;
      if (port && port.key) {
        let sent = false;
        try {
          sent = port.key(key);
        } catch {
          sent = false;
        }
        if (sent) return;
      }
      enqueue({ path: "/api/phone/key", body: { key } });
      return;
    }
    if (event.key === "Tab" || event.key === "Dead" || event.key === "Unidentified" || event.key === "Compose" || event.key === "Process" || event.key === "AltGraph") return;
    if (event.key === "Shift" || event.key === "Control" || event.key === "Alt" || event.key === "Meta" || event.key === "CapsLock" || event.key === "NumLock" || event.key === "ScrollLock") return;
    if (/^F(?:[1-9]|1[0-2])$/.test(event.key)) return;
    if (/^\p{C}/u.test(event.key) || [...event.key].length !== 1) return;
    try {
      event.preventDefault();
    } catch {
      // Ignore non-cancelable synthetic keyboard events.
    }
    queueText(event.key);
  }

  function paste(event: ClipboardEvent): void {
    if (disposed || event.defaultPrevented || !enabled()) return;
    const canvas = currentCanvas();
    if (!canvas || !focusedCanvas(event, canvas) || !event.clipboardData) return;
    let text: string;
    try {
      text = event.clipboardData.getData("text/plain");
    } catch {
      report("Paste failed");
      return;
    }
    try {
      event.preventDefault();
    } catch {
      // Ignore non-cancelable synthetic paste events.
    }
    if (text.length === 0) return;
    flushText();
    enqueue({ path: "/api/phone/input", body: { action: "text", text } });
  }

  return { pointerDown, pointerMove, pointerUp, pointerCancel, wheel, keydown, paste, cancel, dispose };
}
