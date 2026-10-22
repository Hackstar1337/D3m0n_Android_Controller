from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
p=ROOT/'frontend/src/views/PhoneAdminView.vue'
s=p.read_text()
backup=ROOT/'ops/backups/h264-websocket-fix'
backup.mkdir(parents=True,exist_ok=True)
if not (backup/'PhoneAdminView.vue').exists():shutil.copy2(p,backup/'PhoneAdminView.vue')

def replace_once(old,new):
    global s
    if s.count(old)!=1:raise RuntimeError('Expected unique patch context: '+old[:100])
    s=s.replace(old,new)

replace_once("import { api } from '@/composables/useApi'", "import { api } from '@/composables/useApi'\nimport { startH264Stream as startAndroidDecoder, type H264Metrics } from '@/composables/useH264Stream'\nconst androidHandles = new Map<string, H264Metrics>()\nconst androidWatchers = new Map<string, (() => void)[]>()\nconst androidRenderFps = ref<Record<string, number>>({})")
a=s.index('async function rtcStart(serial: string, _reconnectAttempt = 0) {')
b=s.index('function rtcStop(serial: string) {',a)
s=s[:a]+'''async function rtcStart(serial: string, _reconnectAttempt = 0) {
  if (isIosDevice(serial)) { applyIosStreamFallback(serial); return }
  rtcStop(serial)
  rtcStatus.value[serial] = 'H.264 connecting...'
  await nextTick()
  const canvas = document.getElementById(`rtc-video-${serial}`) as HTMLCanvasElement | null
  if (!canvas || (!multiStreaming.value[serial] && !streaming.value)) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const handle = startAndroidDecoder(`${proto}://${location.host}/api/phone/android-h264/${encodeURIComponent(serial)}`, canvas)
  androidHandles.set(serial, handle)
  androidWatchers.set(serial, [
    watch(handle.status, value => { rtcStatus.value[serial] = value === 'streaming' ? 'Streaming' : `H.264 ${value}` }, { immediate: true }),
    watch(handle.renderFps, value => { androidRenderFps.value[serial] = value }),
  ])
}
''' + s[b:]
replace_once('function rtcStop(serial: string) {\n  clearBlackCheck(serial)', '''function rtcStop(serial: string) {
  androidWatchers.get(serial)?.forEach(stop => stop())
  androidWatchers.delete(serial)
  androidHandles.get(serial)?.stop()
  androidHandles.delete(serial)
  androidRenderFps.value[serial] = 0
  if (!isIosDevice(serial)) { rtcStatus.value[serial] = ''; return }
  clearBlackCheck(serial)''')
replace_once("if (mode === 'rtc') return rtcStatus.value[serial] || 'RTC'", "if (mode === 'rtc') return androidRenderFps.value[serial] ? `H.264 ${androidRenderFps.value[serial]}fps` : (rtcStatus.value[serial] || 'H.264')")
s=s.replace("if (mode === 'rtc') return 'WebRTC'", "if (mode === 'rtc') return 'H.264 WebSocket'")
s=s.replace("'Android Portal WebRTC stream'", "'Android H.264 WebSocket stream over ADB and SSH'")
s=s.replace("'FLAG_SECURE - switch to MJPEG'", "'Screen capture protected or stream stalled — reconnect H.264'")
s=s.replace("'Press WebRTC or MJPEG'", "'Press H.264 to start live video'")
# Remove the upstream unconditional five-second switch to MJPEG.
a=s.index('    // Auto-fallback: if RTC doesn\'t connect within 5s, switch to MJPEG')
b=s.index('    }, 5000)',a)+len('    }, 5000)')
s=s[:a]+'    // H.264 reconnects on its own; never switch Android to MJPEG.'+s[b:]

def canvas_markup(condition,serial,css):
    return f'''<canvas v-if="{condition}" :id="`rtc-video-${{{serial}}}`" class="{css}"
              style="cursor: pointer; touch-action: none"
              @mousedown="mjpegMouseDown({serial}, $event)"
              @mousemove="mjpegMove({serial})"
              @mouseup="mjpegMouseUp({serial}, $event)"
              @mouseleave="videoMouseLeave({serial})"
              @touchstart="mjpegTouchStart({serial}, $event)"
              @touchmove="mjpegTouchMove({serial}, $event)"
              @touchend="mjpegTouchEnd({serial}, $event)"
              @touchcancel="mjpegTouchCancel({serial})"
              @dragstart.prevent />'''

s,n=re.subn(r'<video v-if="streaming && singleStreamMode === \'rtc\'".*?/>',lambda _:canvas_markup("streaming && singleStreamMode === 'rtc'",'selectedDevice','stream-media'),s,flags=re.S)
if n!=1:raise RuntimeError('Single-device video patch did not match')
s,n=re.subn(r'<video v-if="multiStreaming\[d.serial\] && multiStreamMode\[d.serial\] === \'rtc\'".*?/>',lambda _:canvas_markup("multiStreaming[d.serial] && multiStreamMode[d.serial] === 'rtc'",'d.serial','multi-stream-media'),s,flags=re.S)
if n!=1:raise RuntimeError('Multi-device video patch did not match')
s=s.replace('title="Android devices use Portal WebRTC; iOS devices use WDA MJPEG automatically"','title="Native Android H.264 over WebSocket; no Portal, ICE, or MJPEG"')
s=s.replace('Start All (Auto)','Start All (H.264)')
s=s.replace('title="WebRTC"','title="H.264 WebSocket"')
# Keep legacy MJPEG only for iOS; Android controls remain native H.264.
s=s.replace('<button class="ctrl-btn ctrl-btn--mjpeg" @click="startAllStreams(\'mjpeg\')"','<button v-if="hasIosDevices" class="ctrl-btn ctrl-btn--mjpeg" @click="startAllStreams(\'mjpeg\')"')
s=s.replace('<button class="ctrl-btn ctrl-btn--mjpeg ctrl-btn--tiny" @click="startMultiStream(d.serial, \'mjpeg\')"','<button v-if="isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--mjpeg ctrl-btn--tiny" @click="startMultiStream(d.serial, \'mjpeg\')"')
a=s.index('function toggleMultiMode(serial: string) {')
b=s.index('/* ── UI grid overlay toggle',a)
s=s[:a]+'''function toggleMultiMode(serial: string) {
  if (!multiStreaming.value[serial] || isIosDevice(serial)) return
  stopMultiStream(serial)
  void startMultiStream(serial, 'rtc')
}
function toggleSingleMode() {
  if (!streaming.value || !selectedDevice.value || selectedIsIos.value) return
  stopStream()
  singleStreamMode.value = 'rtc'
  void startStream()
}
''' + s[b:]
s=s.replace(":title=\"'Switch to ' + (multiStreamMode[d.serial] === 'rtc' ? 'MJPEG' : 'WebRTC')\"",'title="Reconnect H.264"')
# Stop any invisible decoder when the component is unmounted.
s=s.replace("onUnmounted(() => {", "onUnmounted(() => {\n  for (const serial of androidHandles.keys()) rtcStop(serial)")
p.write_text(s)
print('PATCHED_FRONTEND='+str(p))
