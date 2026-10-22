/**
 * Live input channel: one WebSocket per page to /api/phone/input/ws.
 *
 * Streams touch down / move / up and keys in real time; the server writes each
 * event to the leader and every Sync follower together. When the socket is not
 * ready, callers fall back to the existing HTTP tap/swipe/key requests.
 */
export type LiveAck = {
  t: 'ack'
  id: number
  phase: 'down' | 'up' | 'key'
  device?: string
  ok: boolean
  via?: string
  error?: string
  pending?: boolean
  cancelled?: boolean
  reason?: string
  live?: number
  adb?: number
  sync?: Record<string, unknown>
}
export type LiveListener = (ack: LiveAck) => void

type Timer = ReturnType<typeof setTimeout>
type SocketLike = {
  readyState: number
  bufferedAmount: number
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: unknown) => void) | null
  onerror: ((event: unknown) => void) | null
  send: (data: string) => void
  close: (code?: number, reason?: string) => void
}
export type LiveInputOptions = {
  url?: () => string
  createSocket?: (url: string) => SocketLike
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => Timer
  clearTimer?: (timer: Timer) => void
}

/** Keys the server maps to Android keycodes (ops.fast_control.KEYCODES). */
export const LIVE_KEYS = new Set([
  'HOME', 'BACK', 'CALL', 'ENDCALL', 'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT', 'DPAD_CENTER',
  'VOLUME_UP', 'VOLUME_DOWN', 'POWER', 'CAMERA', 'TAB', 'SPACE', 'ENTER', 'DEL', 'MENU', 'SEARCH',
  'MEDIA_PLAY_PAUSE', 'PAGE_UP', 'PAGE_DOWN', 'ESCAPE', 'FORWARD_DEL', 'MOVE_HOME', 'MOVE_END',
  'VOLUME_MUTE', 'APP_SWITCH', 'SLEEP', 'WAKEUP',
])

const OPEN = 1
const MOVE_MIN_MS = 8            // at most ~120 moves per second
const MAX_BUFFERED = 32 * 1024   // skip intermediate moves while the socket is congested
const HOLD_MS = 1000             // keep-alive while a finger rests (server releases after 4 s)
const PING_MS = 5000
const BACKOFF_MIN = 500
const BACKOFF_MAX = 5000
const MAX_SYNC = 100

function defaultUrl(): string {
  const url = new URL('/api/phone/input/ws', location.href)
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

function round(value: number): number {
  return Math.round(Math.max(0, Math.min(1, value)) * 100000) / 100000
}

export function createLiveInputChannel(options: LiveInputOptions = {}) {
  const now = options.now ?? (() => (typeof performance !== 'undefined' ? performance.now() : Date.now()))
  const setTimer = options.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  const clearTimer = options.clearTimer ?? ((timer: Timer) => clearTimeout(timer))
  const createSocket = options.createSocket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike)
  const url = options.url ?? defaultUrl

  let refs = 0
  let socket: SocketLike | null = null
  let ready = false
  let backoff = BACKOFF_MIN
  let reconnectTimer: Timer | null = null
  let pingTimer: Timer | null = null
  let holdTimer: Timer | null = null
  let moveTimer: Timer | null = null
  let nextId = 1
  let rtt: number | null = null
  let reconnects = 0
  const pings = new Map<number, number>()
  const listeners = new Map<number, LiveListener>()
  let active: { id: number; lastSentAt: number; pending: { x: number; y: number } | null } | null = null

  function makeId(): number {
    const id = nextId
    nextId = nextId >= 2 ** 31 - 2 ? 1 : nextId + 1
    return id
  }

  function sendRaw(message: Record<string, unknown>): boolean {
    const ws = socket
    if (!ws || ws.readyState !== OPEN) return false
    try {
      ws.send(JSON.stringify(message))
      return true
    } catch {
      return false
    }
  }

  function notify(id: number, ack: LiveAck, keep = false): void {
    const listener = listeners.get(id)
    if (!keep) listeners.delete(id)
    if (!listener) return
    try {
      listener(ack)
    } catch {
      // A view callback must never break the channel.
    }
  }

  function stopTimer(timer: Timer | null): null {
    if (timer !== null) clearTimer(timer)
    return null
  }

  function endGesture(): void {
    holdTimer = stopTimer(holdTimer)
    moveTimer = stopTimer(moveTimer)
    active = null
  }

  function scheduleHold(): void {
    holdTimer = stopTimer(holdTimer)
    if (!active) return
    holdTimer = setTimer(() => {
      holdTimer = null
      if (!active) return
      if (now() - active.lastSentAt >= HOLD_MS - 50) {
        sendRaw({ t: 'hold', id: active.id })
        active.lastSentAt = now()
      }
      scheduleHold()
    }, HOLD_MS)
  }

  function schedulePing(): void {
    pingTimer = stopTimer(pingTimer)
    if (!ready) return
    pingTimer = setTimer(() => {
      pingTimer = null
      if (!ready) return
      const id = makeId()
      pings.set(id, now())
      if (pings.size > 8) pings.delete(pings.keys().next().value as number)
      sendRaw({ t: 'ping', id })
      schedulePing()
    }, PING_MS)
  }

  function failAll(error: string): void {
    const ids = [...listeners.keys()]
    for (const id of ids) notify(id, { t: 'ack', id, phase: 'up', ok: false, error })
    listeners.clear()
  }

  function onMessage(event: { data: unknown }): void {
    if (typeof event.data !== 'string') return
    let message: any
    try {
      message = JSON.parse(event.data)
    } catch {
      return
    }
    if (!message || typeof message !== 'object') return
    switch (message.t) {
      case 'hello':
        ready = true
        backoff = BACKOFF_MIN
        schedulePing()
        break
      case 'pong': {
        const sentAt = pings.get(message.id)
        if (sentAt !== undefined) {
          pings.delete(message.id)
          rtt = Math.max(0, Math.round(now() - sentAt))
        }
        break
      }
      case 'ack': {
        const id = Number(message.id)
        if (message.phase === 'down') {
          notify(id, message as LiveAck, Boolean(message.ok))
          if (!message.ok && active?.id === id) endGesture()
        } else {
          if (active?.id === id) endGesture()
          notify(id, message as LiveAck)
        }
        break
      }
      case 'released': {
        const id = Number(message.id)
        if (active?.id === id) endGesture()
        listeners.delete(id)
        break
      }
      case 'error': {
        const id = Number(message.id)
        if (Number.isFinite(id) && listeners.has(id)) {
          if (active?.id === id) endGesture()
          notify(id, { t: 'ack', id, phase: 'up', ok: false, error: String(message.error || 'Live input error') })
        }
        break
      }
      default:
        break
    }
  }

  function connect(): void {
    reconnectTimer = stopTimer(reconnectTimer)
    if (refs <= 0 || socket) return
    let ws: SocketLike
    try {
      ws = createSocket(url())
    } catch {
      scheduleReconnect()
      return
    }
    socket = ws
    ws.onmessage = onMessage
    ws.onerror = () => {}
    ws.onclose = () => {
      if (socket !== ws) return
      socket = null
      ready = false
      pingTimer = stopTimer(pingTimer)
      pings.clear()
      endGesture()
      failAll('Live input connection lost')
      scheduleReconnect()
    }
  }

  function scheduleReconnect(): void {
    if (refs <= 0 || reconnectTimer !== null) return
    const delay = backoff
    backoff = Math.min(BACKOFF_MAX, backoff * 2)
    reconnects += 1
    reconnectTimer = setTimer(() => {
      reconnectTimer = null
      connect()
    }, delay)
  }

  function shutdown(): void {
    reconnectTimer = stopTimer(reconnectTimer)
    pingTimer = stopTimer(pingTimer)
    endGesture()
    const ws = socket
    socket = null
    ready = false
    listeners.clear()
    pings.clear()
    if (ws) {
      try {
        ws.close(1000, 'released')
      } catch {
        // Already closed.
      }
    }
  }

  function isReady(): boolean {
    return ready && socket !== null && socket.readyState === OPEN
  }

  function cleanFollowers(followers: string[]): string[] {
    return followers.filter(serial => typeof serial === 'string' && serial).slice(0, MAX_SYNC)
  }

  function cancel(id: number): void {
    listeners.delete(id)
    if (active?.id === id) endGesture()
    sendRaw({ t: 'cancel', id })
  }

  return {
    acquire(): () => void {
      refs += 1
      connect()
      let released = false
      return () => {
        if (released) return
        released = true
        refs = Math.max(0, refs - 1)
        if (refs === 0) shutdown()
      }
    },
    isReady,
    rttMs(): number | null {
      return rtt
    },
    stats() {
      return { ready: isReady(), rttMs: rtt, reconnects, refs, active: active?.id ?? null }
    },
    down(device: string, followers: string[], x: number, y: number, onAck?: LiveListener): number | null {
      if (!isReady() || !device) return null
      if (active) cancel(active.id)
      const id = makeId()
      if (!sendRaw({ t: 'down', id, d: device, s: cleanFollowers(followers), x: round(x), y: round(y) })) return null
      if (onAck) listeners.set(id, onAck)
      active = { id, lastSentAt: now(), pending: null }
      scheduleHold()
      return id
    },
    move(id: number, x: number, y: number): void {
      const gesture = active
      if (!gesture || gesture.id !== id) return
      gesture.pending = { x: round(x), y: round(y) }
      const flush = () => {
        moveTimer = null
        const current = active
        if (!current || current.id !== id || !current.pending) return
        if ((socket?.bufferedAmount ?? 0) > MAX_BUFFERED) {
          moveTimer = setTimer(flush, MOVE_MIN_MS)
          return
        }
        const point = current.pending
        current.pending = null
        if (sendRaw({ t: 'move', id, x: point.x, y: point.y })) current.lastSentAt = now()
      }
      if (moveTimer !== null) return
      const wait = MOVE_MIN_MS - (now() - gesture.lastSentAt)
      if (wait <= 0) flush()
      else moveTimer = setTimer(flush, wait)
    },
    up(id: number, x: number, y: number): void {
      const gesture = active
      if (!gesture || gesture.id !== id) return
      if (gesture.pending) sendRaw({ t: 'move', id, x: gesture.pending.x, y: gesture.pending.y })
      endGesture()
      if (!sendRaw({ t: 'up', id, x: round(x), y: round(y) })) {
        notify(id, { t: 'ack', id, phase: 'up', ok: false, error: 'Live input connection lost' })
      }
    },
    cancel,
    key(device: string, followers: string[], key: string, onAck?: LiveListener): boolean {
      if (!isReady() || !device || !LIVE_KEYS.has(key)) return false
      const id = makeId()
      if (!sendRaw({ t: 'key', id, d: device, s: cleanFollowers(followers), k: key })) return false
      if (onAck) listeners.set(id, onAck)
      return true
    },
  }
}

export type LiveInputChannel = ReturnType<typeof createLiveInputChannel>

/** Page-wide channel shared by the Live Grid and Control desk windows. */
export const liveInputChannel: LiveInputChannel = createLiveInputChannel()
