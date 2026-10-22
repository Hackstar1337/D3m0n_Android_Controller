from pathlib import Path
import shutil
root=Path(__file__).resolve().parents[1];backup=root/'ops/backups/d3m0n-workspace'
backup.mkdir(parents=True,exist_ok=True)
def replace(s,old,new):
 if s.count(old)!=1:raise RuntimeError('Workspace patch context: '+old[:100])
 return s.replace(old,new)

p=root/'frontend/src/views/PhoneAdminView.vue';s=p.read_text()
s=replace(s,"import { deviceWindowUrl, openDeviceWindow } from '@/composables/useDeviceWindows'", "import { deviceWindowUrl, openDeviceWindow } from '@/composables/useDeviceWindows'\nimport DeviceWorkspaceView from '@/views/DeviceWorkspaceView.vue'\nconst workspaceRequests = ref<string[]>([])\nconst workspaceActivated = ref(false)\nfunction openWorkspaceDevice(serial: string) {\n  if (!workspaceRequests.value.includes(serial)) workspaceRequests.value.push(serial)\n  workspaceActivated.value = true\n  subTab.value = 'windows'\n}")
s=replace(s,"const subTab = ref<'single' | 'multi'>", "const subTab = ref<'single' | 'multi' | 'windows'>")
anchor='''<a v-if="!isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--tiny"
                :href="deviceWindowUrl(d.serial)" target="_blank" rel="noopener"
                :aria-label="'Open device window ' + d.serial" :title="'Independent window: ' + d.serial"
                @click="openDeviceWindow(d.serial, $event)">Window</a>'''
replacement='''<button v-if="!isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--tiny" :aria-label="'Open device window ' + d.serial" @click="openWorkspaceDevice(d.serial)">Window</button>
              <a v-if="!isIosDevice(d.serial)" class="ctrl-btn ctrl-btn--tiny" :href="deviceWindowUrl(d.serial)" target="_blank" rel="noopener" :aria-label="'Pop out device ' + d.serial" @click="openDeviceWindow(d.serial, $event)">Pop out</a>'''
s=replace(s,anchor,replacement)
old='<button class="tab-btn" :class="{ active: subTab === \'multi\' }" @click="subTab = \'multi\'">Multi Device</button>'
new=old+'''\n        <button class="tab-btn" :class="{ active: subTab === 'windows' }" @click="workspaceActivated = true; subTab = 'windows'">Windows</button>'''
s=replace(s,old,new)
s=replace(s,'    <!-- ═══════════════ Single Device ═══════════════ -->', '''    <DeviceWorkspaceView v-if="workspaceActivated" v-show="subTab === 'windows'" :devices="devices" :requests="workspaceRequests" :visible="subTab === 'windows'" />
    <!-- ═══════════════ Single Device ═══════════════ -->''')
s=replace(s,'watch(selectedDevice, () => {', '''watch(subTab, (value, previous) => {
  if (previous === 'multi' && value !== 'multi') stopAllStreams()
  if (previous === 'single' && value !== 'single') stopStream()
})
watch(selectedDevice, () => {''')
if not (backup/'PhoneAdminView.vue').exists():shutil.copy2(p,backup/'PhoneAdminView.vue')
p.write_text(s)

p=root/'frontend/src/views/DeviceWindowView.vue';s=p.read_text()
s=replace(s,'defineProps<{ deviceSerial: string }>()','defineProps<{ deviceSerial: string; embedded?: boolean; visible?: boolean }>()')
s=replace(s,'encodeURIComponent(props.deviceSerial)}`, canvas.value)',"encodeURIComponent(props.deviceSerial)}`, canvas.value, { visible: () => props.visible !== false, kind: props.embedded ? 'workspace' : 'device-window' })")
s=replace(s,'<main class="device-window">','<main class="device-window" :class="{ embedded }">')
p.write_text(s)

p=root/'frontend/src/composables/useH264Stream.ts';s=p.read_text()
s=replace(s,'startH264Stream(wsUrl: string, canvas: HTMLCanvasElement): H264Metrics','startH264Stream(wsUrl: string, canvas: HTMLCanvasElement, options: { visible?: () => boolean; kind?: string } = {}): H264Metrics')
s=replace(s,"const kind = new URLSearchParams(location.search).has('device-window') ? 'device-window' : 'dashboard'", "const kind = options.kind || (new URLSearchParams(location.search).has('device-window') ? 'device-window' : 'dashboard')\n  const visible = () => !document.hidden && (!options.visible || options.visible())")
s=replace(s,'visible: !document.hidden, status: status.value','visible: visible(), status: status.value')
s=replace(s,'if (document.hidden && !key)', 'if (!visible() && !key)')
p.write_text(s)

p=root/'ops/android_h264.py';s=p.read_text()
s=replace(s,"self.last_key=b'';self.sent_frames=0;self.error=''", "self.last_key=b'';self.sent_frames=0;self.error='';self.last_wake=0")
s=replace(s,'    def command(self):', '''    async def wake_for_viewer(self):
        if self.last_frame and time.time()-self.last_frame>5 and time.time()-self.last_wake>8:
            self.last_wake=time.time()
            with contextlib.suppress(Exception):
                await adb(self.device,'shell','input','keyevent','KEYCODE_WAKEUP',timeout=5)
    def command(self):''')
s=replace(s,"'target_fps':FPS,'bitrate_limit':BIT_RATE,'encoder_sessions':1", "'session_id':self.scid,'target_fps':FPS,'bitrate_limit':BIT_RATE,'encoder_sessions':1")
s=replace(s,"'idle_refresh_fps':5", "'requested_idle_refresh_fps':5,'static_frames':'on-change; idle is not a failure'")
s=replace(s,'        q=s.subscribe()','        q=s.subscribe()\n        await s.wake_for_viewer()')
p.write_text(s)
print('WORKSPACE_AND_VIEWER_ISOLATION=READY')
