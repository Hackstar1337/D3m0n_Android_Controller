import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const sourcePath = fs.existsSync(path.join(here, 'DeviceWindowView.vue')) ? path.join(here, 'DeviceWindowView.vue') : path.join(here, 'src/views/DeviceWindowView.vue')
assert.ok(fs.existsSync(sourcePath), `missing ${sourcePath}`)
const source = fs.readFileSync(sourcePath, 'utf8')

function includes(fragment, description = fragment) {
  assert.ok(source.includes(fragment), `expected ${description}`)
}
function matches(pattern, description = pattern) {
  assert.match(source, pattern, `expected ${description}`)
}

// Shared helper wiring is canvas-scoped and uses the component's bounded sender.
includes("import { createPhoneInteractions } from '@/composables/phoneInteractions'")
includes('interactions = createPhoneInteractions({ element: () => canvas.value, enabled: interactionEnabled, send, focus: focusScreen, report: reportInteraction, live: livePort })')
// Live input: real-time touch streams to this phone and its Sync followers; HTTP stays the fallback.
includes("import { liveInputChannel, type LiveAck } from '@/composables/liveInput'")
includes('@pointermove="pointerMove"')
includes('function pointerMove(event: PointerEvent) { interactions?.pointerMove(event) }')
includes('releaseLiveInput = liveInputChannel.acquire()')
includes('releaseLiveInput?.(); releaseLiveInput = null')
includes('down: (x, y) => liveInputChannel.down(props.deviceSerial, syncFollowers(), x, y, liveAck)')
includes('if (!liveInputChannel.isReady()) return null')
includes('function onWheel(event: WheelEvent) { interactions?.wheel(event) }')
includes('function onKeydown(event: KeyboardEvent) { interactions?.keydown(event) }')
includes('function onPaste(event: ClipboardEvent) { interactions?.paste(event) }')
includes('interactions?.cancel()')
includes('interactions?.dispose(); interactions = null')
includes('return alive && !paused.value && !viewOnly.value && props.visible !== false && status.value === \'streaming\' && Boolean(canvas.value)')

// The canvas remains the focus/interaction target and all three non-pointer events are template-bound.
includes('tabindex="0"')
includes('aria-label="Live device screen: tap or drag to control this phone"')
includes('@wheel="onWheel"')
includes('@keydown="onKeydown"')
includes('@paste="onPaste"')
includes('focusScreen()')
includes('Wheel to scroll · drag to swipe · focus screen to type · Esc = Back')
includes('Keyboard target: screen')

// Touch-friendly explicit scrolling computes real swipe coordinates from the live intrinsic canvas.
includes("@click=\"scroll('up')\"")
includes("@click=\"scroll('down')\"")
includes("void send('/api/phone/swipe'")
includes('stream_w: el.width, stream_h: el.height')
includes("direction === 'down' ? bottom : top")
includes("direction === 'down' ? top : bottom")
includes('@media(pointer:coarse)')
includes('min-height:44px')

// Existing key labels remain, with explicit Enter/Backspace controls added.
includes("{ name: 'Back', code: 'BACK' }")
includes("{ name: 'Home', code: 'HOME' }")
includes("{ name: 'Enter', code: 'ENTER' }")
includes("{ name: 'Backspace', code: 'DEL' }")

// Unicode and multiline input are sent as one text action; only modified Enter submits.
includes('<textarea')
includes('aria-label="Text to send to this device"')
includes("send('/api/phone/input', { action: 'text', text: value })")
includes("event.key === 'Enter' && (event.ctrlKey || event.metaKey)")
includes('event.preventDefault()')
assert.equal((source.match(/function textKeydown\(/g) || []).length, 1)
assert.equal((source.match(/navigator\.clipboard/g) || []).length, 0, 'must not read clipboard automatically')

// Legacy pointer mapping and bounded queue remain present and unchanged in shape.
includes('function pointerDown(event: PointerEvent)')
includes('function pointerUp(event: PointerEvent)')
includes('function cancelPointer(event?: PointerEvent)')
includes("send('/api/phone/tap'")
includes("send('/api/phone/swipe'")
includes('const MAX_INPUT_QUEUE = 8')
includes('Input queue full (8 actions); try again.')
includes('body: JSON.stringify({ device: props.deviceSerial, ...item.body })')

// A small pure model test documents the required scroll direction without browser dependencies.
function scrollPayload(width, height, direction) {
  const x = Math.round(width / 2)
  const top = Math.round(height * 0.25)
  const bottom = Math.round(height * 0.75)
  return { x1: x, y1: direction === 'down' ? bottom : top, x2: x, y2: direction === 'down' ? top : bottom, stream_w: width, stream_h: height }
}
assert.deepEqual(scrollPayload(1080, 1920, 'down'), { x1: 540, y1: 1440, x2: 540, y2: 480, stream_w: 1080, stream_h: 1920 })
assert.deepEqual(scrollPayload(1080, 1920, 'up'), { x1: 540, y1: 480, x2: 540, y2: 1440, stream_w: 1080, stream_h: 1920 })

console.log(`DeviceWindowView interaction checks passed: ${sourcePath}`)
