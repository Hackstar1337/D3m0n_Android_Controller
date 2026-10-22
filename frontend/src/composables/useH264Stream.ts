import { ref, type Ref } from 'vue'
export function h264Supported(): boolean { return typeof window !== 'undefined' && 'VideoDecoder' in window }
export interface H264Metrics {
  status: Ref<string>; recvFps: Ref<number>; renderFps: Ref<number>
  queue: Ref<number>; latencyMs: Ref<number>; kbps: Ref<number>; dropped: Ref<number>
  idle: Ref<boolean>; frameAgeSeconds: Ref<number>; viewerId: string; stop: () => void
  /** Send the current quality profile and visibility to the server now. */
  update: () => void
}
/** Encoder quality requested by a viewer; the server runs the highest one any viewer needs. */
export type H264Profile = 'hd' | 'grid' | 'wall'
function nalUnits(buf: Uint8Array): Uint8Array[] {
  const starts: { position: number; header: number }[] = []
  for (let i = 0; i + 3 < buf.length; i++) {
    if (buf[i] !== 0 || buf[i + 1] !== 0) continue
    if (buf[i + 2] === 1) { starts.push({ position: i, header: i + 3 }); i += 2 }
    else if (buf[i + 2] === 0 && buf[i + 3] === 1) { starts.push({ position: i, header: i + 4 }); i += 3 }
  }
  return starts.map((s, i) => buf.subarray(s.header, starts[i + 1]?.position ?? buf.length))
}
export function startH264Stream(wsUrl: string, canvas: HTMLCanvasElement, options: { visible?: () => boolean; kind?: string; profile?: () => H264Profile } = {}): H264Metrics {
  const status = ref('connecting'), recvFps = ref(0), renderFps = ref(0)
  const idle = ref(false), frameAgeSeconds = ref(0)
  let lastRenderedAt = 0
  const queue = ref(0), latencyMs = ref(0), kbps = ref(0), dropped = ref(0)
  const ctx = canvas.getContext('2d', { alpha: false })
  let decoder: VideoDecoder | null = null, ws: WebSocket | null = null
  let stopped = false, haveKeyframe = false, codec = ''
  let reconnectTimer: number | null = null, backoff = 500
  let lastMessageAt = performance.now()
  let recvWin = 0, renderWin = 0, byteWin = 0, metricsTick = 0
  let receivedFirst = false, renderedFirst = false, totalRendered = 0, lastErrorAt = 0
  // Viewer -> server hints: quality profile and hidden ("key") / visible ("full").
  let sentProfile = '', sentMode = 'full', lastSps = ''
  const viewerId = crypto.randomUUID()
  const parsed = new URL(wsUrl)
  const device = decodeURIComponent(parsed.pathname.split('/').pop() || '')
  const kind = options.kind || (new URLSearchParams(location.search).has('device-window') ? 'device-window' : 'dashboard')
  const visible = () => !document.hidden && (!options.visible || options.visible())
  const wantedProfile = (): H264Profile => {
    try { const value = options.profile?.(); return value === 'grid' || value === 'wall' ? value : 'hd' } catch { return 'hd' }
  }
  function update() {
    if (stopped || !ws || ws.readyState !== WebSocket.OPEN) return
    const profile = wantedProfile(), mode = visible() ? 'full' : 'key'
    const message: Record<string, string> = {}
    if (profile !== sentProfile) message.profile = profile
    if (mode !== sentMode) message.mode = mode
    if (!message.profile && !message.mode) return
    try { ws.send(JSON.stringify(message)); sentProfile = profile; sentMode = mode } catch {}
  }
  function report(event: string, detail: unknown = '') {
    if (stopped) return
    fetch('/api/phone/android-h264/client-status', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device, viewer: viewerId, kind, event, detail, codec }), keepalive: event === 'rendered' }).catch(() => {})
  }
  const metricsTimer = window.setInterval(() => {
    recvFps.value = recvWin; renderFps.value = renderWin; kbps.value = Math.round(byteWin * 8 / 1000)
    queue.value = decoder?.decodeQueueSize ?? 0
    frameAgeSeconds.value = lastRenderedAt ? Math.round((performance.now() - lastRenderedAt) / 1000) : 0
    idle.value = renderedFirst && frameAgeSeconds.value >= 2 && status.value === 'streaming'
    if (++metricsTick % 5 === 0) report('metrics', { recvFps: recvWin, renderFps: renderWin, queue: queue.value, latencyMs: latencyMs.value, kbps: kbps.value, dropped: dropped.value, rendered: totalRendered, idle: idle.value, frameAgeSeconds: frameAgeSeconds.value, visible: visible(), status: status.value, kind })
    recvWin = 0; renderWin = 0; byteWin = 0
    // The server sends a heartbeat even when the picture is unchanged.
    // Recover a dead socket automatically; never reset simply for a static screen.
    if (!stopped && ws && performance.now() - lastMessageAt > 35000) scheduleReconnect()
    update()
  }, 1000)
  function configure(nextCodec: string, force = false) {
    if (!decoder || decoder.state === 'closed') {
      decoder = new VideoDecoder({
        output(frame: VideoFrame) {
          try {
            if (ctx) {
              if (canvas.width !== frame.displayWidth) canvas.width = frame.displayWidth
              if (canvas.height !== frame.displayHeight) canvas.height = frame.displayHeight
              ctx.drawImage(frame, 0, 0)
            }
            renderWin++; totalRendered++; lastRenderedAt = performance.now(); idle.value = false; status.value = 'streaming'; backoff = 500
            latencyMs.value = Math.max(0, Math.round(performance.now() - frame.timestamp / 1000))
            if (!renderedFirst) { renderedFirst = true; report('rendered', { width: frame.displayWidth, height: frame.displayHeight, kind }) }
          } finally { frame.close() }
        },
        error(error) {
          status.value = `decoder error: ${error.message}`
          console.error('H264 decoder error', device, codec, error.name, error.message)
          report('decoder-error', { name: error.name, message: error.message }); scheduleReconnect()
        },
      })
    }
    if (force && decoder.state === 'configured') { try { decoder.reset() } catch {} }
    if (force || codec !== nextCodec || decoder.state !== 'configured') {
      codec = nextCodec; haveKeyframe = false
      decoder.configure({ codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' })
      VideoDecoder.isConfigSupported({ codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' }).then(value => report('codec-supported', value.supported)).catch(error => report('codec-support-error', String(error)))
    }
  }
  function onBinary(data: ArrayBuffer) {
    const buf = new Uint8Array(data), nals = nalUnits(buf)
    const key = nals.some(n => n.length > 0 && (n[0]! & 31) === 5)
    const sps = nals.find(n => n.length >= 4 && (n[0]! & 31) === 7)
    recvWin++; byteWin += buf.length
    if (!receivedFirst) { receivedFirst = true; report('received', { bytes: buf.length, key, sps: !!sps, kind }) }
    // Occluded/background tabs retain one fresh keyframe per second rather
    // than decoding every grid frame; visible device windows remain full-rate.
    if (!visible() && !key) { haveKeyframe = false; return }
    try {
      if (sps) {
        // A new SPS (e.g. after a quality-profile change) may carry a new
        // resolution with the same codec string; start a clean decoder for it.
        const spsKey = Array.from(sps.subarray(0, Math.min(sps.length, 32))).map(v => v.toString(16).padStart(2, '0')).join('')
        const changed = lastSps !== '' && spsKey !== lastSps
        lastSps = spsKey
        configure('avc1.' + Array.from(sps.subarray(1, 4)).map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase(), changed)
      }
      if (!decoder || decoder.state !== 'configured') return
      if (decoder.decodeQueueSize > 4) {
        decoder.reset(); decoder.configure({ codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' }); haveKeyframe = false
      }
      if (!haveKeyframe && !key) { dropped.value++; return }
      if (key) haveKeyframe = true
      decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: Math.round(performance.now() * 1000), data: buf }))
    } catch (error) {
      haveKeyframe = false; status.value = error instanceof Error ? `H.264 error: ${error.message}` : 'H.264 decoder error'
      if (performance.now() - lastErrorAt > 5000) { lastErrorAt = performance.now(); console.error('H264 decode failure', device, codec, error); report('decode-failure', String(error)); scheduleReconnect() }
    }
  }
  function connect() {
    if (stopped) return
    lastMessageAt = performance.now()
    haveKeyframe = false; codec = ''; lastSps = ''; receivedFirst = false; renderedFirst = false; idle.value = false; lastRenderedAt = 0
    if (!h264Supported()) { status.value = 'WebCodecs unavailable; H.264 requires a supported browser'; return }
    try {
      const url = new URL(wsUrl); url.searchParams.set('viewer', viewerId)
      const profile = wantedProfile()
      if (profile !== 'hd') url.searchParams.set('profile', profile)
      sentProfile = profile; sentMode = 'full'
      ws = new WebSocket(url); ws.binaryType = 'arraybuffer'
      ws.onopen = () => { lastMessageAt = performance.now(); status.value = 'connected'; update() }
      ws.onmessage = (event: MessageEvent) => {
        lastMessageAt = performance.now()
        if (typeof event.data === 'string') {
          try { const value = JSON.parse(event.data); if (value.error) status.value = value.error; else if (value.status && value.status !== 'alive' && status.value !== 'streaming') status.value = value.status } catch {}
        } else onBinary(event.data as ArrayBuffer)
      }
      ws.onclose = () => { if (!stopped) scheduleReconnect() }
      ws.onerror = () => { status.value = 'WebSocket connection error'; try { ws?.close() } catch {} }
    } catch { status.value = 'WebSocket connection error'; scheduleReconnect() }
  }
  function cleanupConnection() {
    if (ws) { ws.onclose = null; ws.onerror = null; ws.onmessage = null; ws.onopen = null; try { ws.close() } catch {} }
    ws = null
    try { if (decoder && decoder.state !== 'closed') decoder.close() } catch {}
    decoder = null; haveKeyframe = false
  }
  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) return
    if (!status.value.includes('error')) status.value = 'reconnecting'
    cleanupConnection()
    const delay = backoff + Math.round(Math.random() * 200)
    reconnectTimer = window.setTimeout(() => { reconnectTimer = null; backoff = Math.min(5000, backoff * 2); connect() }, delay)
  }
  function stop() {
    stopped = true; clearInterval(metricsTimer)
    if (reconnectTimer !== null) clearTimeout(reconnectTimer)
    cleanupConnection(); status.value = 'stopped'
  }
  connect()
  return { status, recvFps, renderFps, queue, latencyMs, kbps, dropped, idle, frameAgeSeconds, viewerId, stop, update }
}
