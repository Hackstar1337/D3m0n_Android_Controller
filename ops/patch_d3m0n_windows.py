from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1]
backup=root/'ops/backups/d3m0n-multiwindow';backup.mkdir(parents=True,exist_ok=True)

def change(s,old,new):
    if s.count(old)!=1:raise RuntimeError('Non-unique patch context: '+old[:100])
    return s.replace(old,new)

app=root/'frontend/src/App.vue';s=app.read_text()
s=change(s,"import PhoneAdminView from '@/views/PhoneAdminView.vue'", "import PhoneAdminView from '@/views/PhoneAdminView.vue'\nimport DeviceWindowView from '@/views/DeviceWindowView.vue'\nconst windowDevice = new URLSearchParams(window.location.search).get('device-window') || ''")
s=change(s,'onMounted(() => {\n  pollGhostState()', 'onMounted(() => {\n  if (windowDevice) return\n  pollGhostState()')
s=change(s,'<template>\n  <div class="min-h-screen"','<template>\n  <DeviceWindowView v-if="windowDevice" :device-serial="windowDevice" />\n  <div v-else class="min-h-screen"')
app_changed=s

view=root/'frontend/src/views/PhoneAdminView.vue';s=view.read_text()
s=change(s,"import { api } from '@/composables/useApi'", "import { api } from '@/composables/useApi'\nimport { deviceWindowUrl, openDeviceWindow } from '@/composables/useDeviceWindows'")
s=change(s,"const subTab = ref<'single' | 'multi'>('single')", "const subTab = ref<'single' | 'multi'>(new URLSearchParams(location.search).has('multi') ? 'multi' : 'single')")
s=change(s,"  if (actualMode === 'rtc') {\n    rtcStart(serial)","  if (actualMode === 'rtc') {\n    await rtcStart(serial)")
s=change(s,"function startAllStreams(mode: 'rtc' | 'mjpeg' = 'rtc') {\n  for (const d of devices.value) { if (!multiStreaming.value[d.serial]) startMultiStream(d.serial, mode) }\n}","""let startBatch = 0
async function startAllStreams(mode: 'rtc' | 'mjpeg' = 'rtc') {
  const batch = ++startBatch
  for (const d of devices.value) {
    if (batch !== startBatch) return
    if (!multiStreaming.value[d.serial]) await startMultiStream(d.serial, mode)
    await new Promise(resolve => setTimeout(resolve, 80))
  }
}""")
s=change(s,'function stopAllStreams() { for (const d of devices.value) stopMultiStream(d.serial) }','function stopAllStreams() { startBatch++; for (const d of devices.value) stopMultiStream(d.serial) }')
old='''async function pollMultiLogs() {
  for (const d of devices.value) {
    try {
      const resp = await api(`/api/logs?device=${d.serial}&limit=30`)
      const lines = (resp.lines || []).map((l: any) => `[${l.level}] ${l.msg}`)
      multiLogs.value[d.serial] = lines
    } catch { if (!multiLogs.value[d.serial]) multiLogs.value[d.serial] = [] }
  }
}'''
new='''let multiLogsBusy = false
async function pollMultiLogs() {
  if (multiLogsBusy || document.hidden || subTab.value !== 'multi') return
  multiLogsBusy = true
  try {
    for (let offset = 0; offset < devices.value.length; offset += 4) {
      await Promise.all(devices.value.slice(offset, offset + 4).map(async d => {
        try {
          const resp = await api(`/api/logs?device=${encodeURIComponent(d.serial)}&limit=12`)
          multiLogs.value[d.serial] = (resp.lines || []).map((l: any) => `[${l.level}] ${l.msg}`)
        } catch { if (!multiLogs.value[d.serial]) multiLogs.value[d.serial] = [] }
      }))
    }
  } finally { multiLogsBusy = false }
}'''
s=change(s,old,new)
s=change(s,'async function pollLogs() {\n  try {','async function pollLogs() {\n  if (document.hidden || !logsOpen.value) return\n  try {')
s=change(s,'  await loadDevices()\n  loadAllHealth()',"  await loadDevices()\n  if (subTab.value === 'multi' && new URLSearchParams(location.search).has('autostart')) void startAllStreams('rtc')\n  loadAllHealth()")
s=change(s,'window.setInterval(pollMultiLogs, 4000)','window.setInterval(pollMultiLogs, 8000)')
s=change(s,'onUnmounted(() => {\n  for (const serial of androidHandles.keys()) rtcStop(serial)','onUnmounted(() => {\n  startBatch++\n  for (const serial of androidHandles.keys()) rtcStop(serial)')
old='<button class="ctrl-btn ctrl-btn--tiny" @click="selectedDevice = d.serial; subTab = \'single\'; startStream()" title="Expand">&#x2197;</button>'
new='''<a v-if="!isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--tiny"
                :href="deviceWindowUrl(d.serial)" target="_blank" rel="noopener"
                :aria-label="'Open device window ' + d.serial" :title="'Independent window: ' + d.serial"
                @click="openDeviceWindow(d.serial, $event)">Window</a>
              <button class="ctrl-btn ctrl-btn--tiny" @click="selectedDevice = d.serial; subTab = 'single'; startStream()" title="Expand">&#x2197;</button>'''
s=change(s,old,new)
s=change(s,'<span class="device-count">{{ devices.length }} device(s)</span>', '<span class="device-count">{{ devices.length }} device(s) · shared H.264 encoders · 24 fps cap · 2 Mbps/device</span>')
# Accurate pointer mapping when a portrait stream is letterboxed in a wide card.
a=s.index('function mjpegImagePoint(');b=s.index('function mjpegMouseDown(',a)
s=s[:a]+'''function mjpegImagePoint(img: HTMLImageElement | HTMLCanvasElement, clientX: number, clientY: number): { x: number; y: number; width: number; height: number } | null {
  const rect = img.getBoundingClientRect()
  const sw = (img as HTMLImageElement).naturalWidth || (img as HTMLCanvasElement).width
  const sh = (img as HTMLImageElement).naturalHeight || (img as HTMLCanvasElement).height
  if (!sw || !sh || !rect.width || !rect.height) return null
  const scale = Math.min(rect.width / sw, rect.height / sh)
  const x = (clientX - rect.left - (rect.width - sw * scale) / 2) / scale
  const y = (clientY - rect.top - (rect.height - sh * scale) / 2) / scale
  if (x < 0 || y < 0 || x > sw || y > sh) return null
  return { x: Math.round(x), y: Math.round(y), width: sw, height: sh }
}
''' + s[b:]

server=root/'ops/server.py';server_s=server.read_text()
server_s=change(server_s,'from gitd.app import app','from gitd.app import app\napp.title = "D3m0n in the droid"')
for p,content in [(app,app_changed),(view,s),(server,server_s)]:
    name=str(p.relative_to(root)).replace('/','__')
    if not (backup/name).exists():shutil.copy2(p,backup/name)
    p.write_text(content)
    print('PATCHED='+str(p))
unit=Path.home()/'.config/systemd/user/android-agent.service'
unit_s=unit.read_text().replace('Ghost in the Droid dashboard and Android device API','D3m0n in the droid dashboard and Android device API')
if '--ws-per-message-deflate' not in unit_s:
    unit_s=change(unit_s,'--host 127.0.0.1 --port 5055','--host 127.0.0.1 --port 5055 --ws-per-message-deflate false')
if not (backup/'android-agent.service').exists():shutil.copy2(unit,backup/'android-agent.service')
unit.write_text(unit_s)
print('WEBSOCKET_RECOMPRESSION=DISABLED')
