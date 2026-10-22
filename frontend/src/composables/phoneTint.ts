export type RGB = readonly [number, number, number]

export type PhoneTintMode = 'light' | 'dark'

export type PhoneTintVars = Record<
  | '--phone-surface'
  | '--phone-cap-top'
  | '--phone-fg'
  | '--phone-muted'
  | '--phone-selected'
  | '--phone-proxy'
  | '--phone-warning'
  | '--phone-error'
  | '--phone-idle'
  | '--phone-sync'
  | '--phone-agent',
  string
>

export interface PhoneTintPalette {
  surface: string
  top: string
  mode: PhoneTintMode
  vars: PhoneTintVars
}

type ColorInput = RGB | string

const VARIABLE_NAMES = [
  '--phone-surface', '--phone-cap-top', '--phone-fg', '--phone-muted',
  '--phone-selected', '--phone-proxy', '--phone-warning', '--phone-error',
  '--phone-idle', '--phone-sync', '--phone-agent',
] as const

const MIN_CONTRAST = 4.5
const SAMPLE_WIDTH = 32
const SAMPLE_HEIGHT = 2
const SAMPLE_INTERVAL_MS = 4_000
const TICK_MS = 150
const MAX_BATCH_SAMPLES = 2

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Number.isFinite(value) ? Math.round(value) : 0))
}

function asRGB(color: ColorInput): RGB {
  if (Array.isArray(color)) return [clampChannel(Number(color[0])), clampChannel(Number(color[1])), clampChannel(Number(color[2]))]
  const value = String(color).trim().toLowerCase()
  const hex = value.replace(/^#/, '')
  if (/^[\da-f]{3}$/i.test(hex)) return [parseInt(hex[0]! + hex[0]!, 16), parseInt(hex[1]! + hex[1]!, 16), parseInt(hex[2]! + hex[2]!, 16)]
  if (/^[\da-f]{6,8}$/i.test(hex)) return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]
  const match = value.match(/rgba?\(\s*([+-]?[\d.]+)(?:\s*,\s*|\s+)([+-]?[\d.]+)(?:\s*,\s*|\s+)([+-]?[\d.]+)/)
  return match ? [clampChannel(Number(match[1])), clampChannel(Number(match[2])), clampChannel(Number(match[3]))] : [0, 0, 0]
}

function cssRGB(color: RGB): string {
  return `rgb(${color[0]} ${color[1]} ${color[2]})`
}

function luminance(color: RGB): number {
  return color.reduce((total, channel, index) => {
    const value = channel / 255
    const linear = value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
    return total + linear * [0.2126, 0.7152, 0.0722][index]!
  }, 0)
}

/** W3C relative-luminance contrast ratio. Inputs may be RGB tuples or CSS rgb/hex strings. */
export function contrastRatio(a: ColorInput, b: ColorInput): number {
  const first = luminance(asRGB(a))
  const second = luminance(asRGB(b))
  const lighter = Math.max(first, second)
  const darker = Math.min(first, second)
  return (lighter + 0.05) / (darker + 0.05)
}

function blend(from: RGB, to: RGB, amount: number): RGB {
  return [
    clampChannel(from[0] + (to[0] - from[0]) * amount),
    clampChannel(from[1] + (to[1] - from[1]) * amount),
    clampChannel(from[2] + (to[2] - from[2]) * amount),
  ]
}

function minimumContrast(color: RGB, backgrounds: readonly RGB[]): number {
  return Math.min(...backgrounds.map(background => contrastRatio(color, background)))
}

function readableColor(preferred: RGB, backgrounds: readonly RGB[], ink: RGB): RGB {
  if (minimumContrast(preferred, backgrounds) >= MIN_CONTRAST) return preferred
  let low = 0
  let high = 1
  for (let step = 0; step < 24; step += 1) {
    const middle = (low + high) / 2
    if (minimumContrast(blend(preferred, ink, middle), backgrounds) >= MIN_CONTRAST) high = middle
    else low = middle
  }
  return blend(preferred, ink, high)
}

function paletteColor(value: RGB, surface: RGB, top: RGB, ink: RGB): string {
  return cssRGB(readableColor(value, [surface, top], ink))
}

/** Build the complete CSS-variable palette for one sampled upper-screen color. */
export function paletteForColor(color: ColorInput): PhoneTintPalette {
  const surfaceRGB = asRGB(color)
  const white: RGB = [255, 255, 255]
  const black: RGB = [0, 0, 0]
  // Pick the ink with the best contrast, not merely by a hard RGB threshold. This keeps
  // neutral mid-gray samples readable at both ends of the subtle cap gradient.
  const ink = contrastRatio(surfaceRGB, white) >= contrastRatio(surfaceRGB, black) ? white : black
  const mode: PhoneTintMode = ink === white ? 'dark' : 'light'
  // Move the cap away from the chosen ink: darken when ink is white, lighten when
  // it is black. Contrast can only improve, including the gray transition range.
  const topRGB = blend(surfaceRGB, ink === white ? black : white, 0.06)
  const surface = cssRGB(surfaceRGB)
  const top = cssRGB(topRGB)
  const vars: PhoneTintVars = {
    '--phone-surface': surface,
    '--phone-cap-top': top,
    '--phone-fg': cssRGB(readableColor(ink, [surfaceRGB, topRGB], ink)),
    '--phone-muted': paletteColor(ink === white ? [184, 184, 184] : [82, 82, 82], surfaceRGB, topRGB, ink),
    '--phone-selected': paletteColor(ink === white ? [147, 197, 253] : [29, 78, 216], surfaceRGB, topRGB, ink),
    '--phone-proxy': paletteColor(ink === white ? [110, 231, 183] : [4, 120, 87], surfaceRGB, topRGB, ink),
    '--phone-warning': paletteColor(ink === white ? [251, 191, 36] : [146, 64, 14], surfaceRGB, topRGB, ink),
    '--phone-error': paletteColor(ink === white ? [248, 113, 113] : [185, 28, 28], surfaceRGB, topRGB, ink),
    '--phone-idle': paletteColor(ink === white ? [163, 163, 163] : [82, 82, 82], surfaceRGB, topRGB, ink),
    '--phone-sync': paletteColor(ink === white ? [103, 232, 249] : [14, 116, 144], surfaceRGB, topRGB, ink),
    '--phone-agent': paletteColor(ink === white ? [196, 181, 253] : [109, 40, 217], surfaceRGB, topRGB, ink),
  }
  return { surface, top, mode, vars }
}

type TintRoot = HTMLElement
interface TintEntry { node: TintRoot; lastSampleAt: number; retryAfter: number; lastColor: RGB | null }
type IdleCallback = (deadline: { timeRemaining?: () => number }) => void

type GlobalWithIdle = typeof globalThis & {
  requestIdleCallback?: (callback: IdleCallback, options?: { timeout: number }) => number
  cancelIdleCallback?: (handle: number) => void
}

function ownerDocument(node: TintRoot): Document | null {
  if (node.ownerDocument) return node.ownerDocument
  return typeof document !== 'undefined' ? document : null
}

function sourceFor(node: TintRoot): CanvasImageSource | null {
  const candidate = node as TintRoot & { canvas?: unknown; video?: unknown; __phoneTintSource?: unknown }
  const direct = candidate.__phoneTintSource || candidate.canvas || candidate.video
  if (direct && typeof direct === 'object') return direct as CanvasImageSource
  if (typeof node.querySelector !== 'function') return null
  try {
    return (node.querySelector('canvas') || node.querySelector('video')) as CanvasImageSource | null
  } catch {
    return null
  }
}

function sourceSize(source: CanvasImageSource): { width: number; height: number } | null {
  const item = source as CanvasImageSource & { videoWidth?: number; videoHeight?: number; width?: number; height?: number }
  const readyState = (item as CanvasImageSource & { readyState?: number }).readyState
  if (typeof readyState === 'number' && readyState < 2) return null
  const width = Number(item.videoWidth || item.width || 0)
  const height = Number(item.videoHeight || item.height || 0)
  return width > 0 && height > 0 ? { width, height } : null
}

function rootIsVisible(node: TintRoot): boolean {
  if (node.isConnected === false) return false
  const doc = ownerDocument(node)
  if (doc?.hidden === true) return false
  try {
    const view = doc?.defaultView
    const style = view?.getComputedStyle?.(node)
    if (style && (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse')) return false
    if (typeof node.getBoundingClientRect === 'function') {
      const rect = node.getBoundingClientRect()
      if (rect.width === 0 || rect.height === 0) return false
      const width = Number(view?.innerWidth || 0)
      const height = Number(view?.innerHeight || 0)
      if (width > 0 && height > 0 && (rect.right <= 0 || rect.bottom <= 0 || rect.left >= width || rect.top >= height)) return false
    }
  } catch {
    // A detached or test-double root is treated conservatively as eligible.
  }
  return true
}

function significantChange(previous: RGB | null, next: RGB): boolean {
  return !previous || Math.max(Math.abs(previous[0] - next[0]), Math.abs(previous[1] - next[1]), Math.abs(previous[2] - next[2])) >= 6
}

function sampledColor(data: Uint8ClampedArray): RGB | null {
  const pixels: RGB[] = []
  for (let offset = 0; offset + 3 < data.length; offset += 4) {
    const alpha = data[offset + 3]!
    if (alpha >= 128) pixels.push([data[offset]!, data[offset + 1]!, data[offset + 2]!])
  }
  if (!pixels.length) return null
  // Quantized dominant bucket rejects a small icon/noise cluster; the channel median
  // within that bucket prevents one remaining outlier from steering the bezel hue.
  const buckets = new Map<string, RGB[]>()
  for (const pixel of pixels) {
    const key = pixel.map(channel => Math.floor(channel / 32)).join(':')
    const bucket = buckets.get(key) || []
    bucket.push(pixel)
    buckets.set(key, bucket)
  }
  const dominant = [...buckets.values()].sort((a, b) => b.length - a.length)[0] || pixels
  const values: number[][] = [0, 1, 2].map(channel => dominant.map(pixel => pixel[channel]!).sort((a, b) => a - b))
  const red = values[0]!
  const green = values[1]!
  const blue = values[2]!
  return [
    red[Math.floor(red.length / 2)]!,
    green[Math.floor(green.length / 2)]!,
    blue[Math.floor(blue.length / 2)]!,
  ]
}

export interface PhoneTintManager {
  attach(serial: string, node: HTMLElement | null): void
  start(): void
  stop(): void
}

/**
 * Owns only tint sampling and CSS properties. Sampling is deliberately idle/bounded:
 * source canvases are never read or mutated, and no network/decoder/Vue state is touched.
 */
export function createPhoneTintManager(): PhoneTintManager {
  const entries = new Map<string, TintEntry>()
  let running = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let idleHandle: number | null = null
  let cursor = 0
  let scratch: HTMLCanvasElement | OffscreenCanvas | null = null
  let scratchContext: CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null = null
  let visibilityDocument: Document | null = null

  const globals = (): GlobalWithIdle => globalThis as GlobalWithIdle

  function clearSchedule(): void {
    if (timer !== null) { clearTimeout(timer); timer = null }
    if (idleHandle !== null) {
      globals().cancelIdleCallback?.(idleHandle)
      idleHandle = null
    }
  }

  function resetScratch(): void {
    scratch = null
    scratchContext = null
  }

  function ensureScratch(entry: TintEntry): boolean {
    if (scratch && scratchContext) return true
    try {
      const doc = ownerDocument(entry.node)
      if (doc?.createElement) scratch = doc.createElement('canvas')
      else if (typeof OffscreenCanvas !== 'undefined') scratch = new OffscreenCanvas(SAMPLE_WIDTH, SAMPLE_HEIGHT)
      else return false
      scratch.width = SAMPLE_WIDTH
      scratch.height = SAMPLE_HEIGHT
      scratchContext = scratch.getContext('2d', { willReadFrequently: true }) as CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D | null
      return Boolean(scratchContext)
    } catch {
      resetScratch()
      return false
    }
  }

  function sample(entry: TintEntry, now: number): RGB | null {
    if (now < entry.retryAfter || !ensureScratch(entry)) return null
    const source = sourceFor(entry.node)
    const size = source ? sourceSize(source) : null
    if (!source || !size || !scratchContext || !scratch) return null
    try {
      // Only the tiny scratch canvas is read. The source canvas/video and decoder state
      // are not queried through a 2D context and are never resized or otherwise changed.
      scratchContext.clearRect(0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT)
      scratchContext.drawImage(source, 0, 0, size.width, Math.min(2, size.height), 0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT)
      return sampledColor(scratchContext.getImageData(0, 0, SAMPLE_WIDTH, SAMPLE_HEIGHT).data)
    } catch {
      // SecurityError/taint and transient readiness failures are isolated to this root.
      resetScratch()
      entry.retryAfter = now + SAMPLE_INTERVAL_MS
      return null
    }
  }

  function writePalette(entry: TintEntry, color: RGB): void {
    if (!significantChange(entry.lastColor, color)) return
    const palette = paletteForColor(color)
    try {
      for (const name of VARIABLE_NAMES) entry.node.style.setProperty(name, palette.vars[name])
      entry.lastColor = color
    } catch {
      // A root removed during a batch must not break the other tiles.
    }
  }

  function runBatch(): void {
    timer = null
    idleHandle = null
    if (!running || entries.size === 0 || visibilityDocument?.hidden === true) return
    const list = [...entries.values()]
    const now = Date.now()
    let sampled = 0
    let inspected = 0
    while (sampled < MAX_BATCH_SAMPLES && inspected < list.length) {
      const entry = list[cursor % list.length]!
      cursor = (cursor + 1) % Math.max(1, list.length)
      inspected += 1
      if (!rootIsVisible(entry.node) || now - entry.lastSampleAt < SAMPLE_INTERVAL_MS) continue
      entry.lastSampleAt = now
      sampled += 1
      const color = sample(entry, now)
      if (color) writePalette(entry, color)
    }
    schedule()
  }

  function schedule(): void {
    if (!running || entries.size === 0 || visibilityDocument?.hidden === true || timer !== null || idleHandle !== null) return
    // Pace even idle-capable browsers. Re-arming idle work directly would scan
    // all visible roots on every animation frame despite the per-root cache.
    timer = setTimeout(() => {
      timer = null
      if (!running || entries.size === 0 || visibilityDocument?.hidden === true) return
      const g = globals()
      if (typeof g.requestIdleCallback === 'function') idleHandle = g.requestIdleCallback(runBatch, { timeout: TICK_MS })
      else runBatch()
    }, TICK_MS)
  }

  function wake(): void {
    if (!running) return
    clearSchedule()
    schedule()
  }

  return {
    attach(serial: string, node: HTMLElement | null): void {
      const key = String(serial)
      if (!node) {
        entries.delete(key)
        if (entries.size === 0) clearSchedule()
        return
      }
      const existing = entries.get(key)
      if (existing?.node === node) return
      entries.set(key, { node, lastSampleAt: -SAMPLE_INTERVAL_MS, retryAfter: 0, lastColor: null })
      schedule()
    },
    start(): void {
      if (running) return
      running = true
      visibilityDocument = typeof document !== 'undefined' ? document : null
      visibilityDocument?.addEventListener('visibilitychange', wake, { passive: true })
      schedule()
    },
    stop(): void {
      running = false
      clearSchedule()
      visibilityDocument?.removeEventListener('visibilitychange', wake)
      visibilityDocument = null
      entries.clear()
      resetScratch()
      cursor = 0
    },
  }
}
