"""Private D3m0n H.264 streaming: one scrcpy encoder per Android phone.
Viewers have isolated queues and controls. No MJPEG, root, or public ports.

Fleet scale (50+ phones): each viewer declares a quality profile. A phone's
single encoder runs at the highest profile any current viewer needs ("hd" for
focused/controller views, "grid" for wall tiles) and is restarted in place when
that changes, without disconnecting viewers. Hidden viewers may ask for
keyframes only. The scrcpy control socket is kept open so the dashboard can
inject touch and key events instantly instead of spawning `adb shell input`.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import struct
import time
from fastapi import APIRouter, Body, WebSocket
from ops import control_arbiter
from ops.platform_config import get_allowed_origins

router=APIRouter(tags=['streaming'])
LOG=logging.getLogger('gitd.android_h264')
ROOT=Path(__file__).resolve().parents[1]
SERVER=ROOT/'ops'/'scrcpy-server-v5.0.1.jar'
REMOTE='/data/local/tmp/ghost-scrcpy-v5.0.1.jar'
VERSION='5.0.1'
# Quality profiles. "hd" is the historical default and stays the default for
# every viewer that does not ask otherwise (device windows, controller, agent).
# rank: the encoder runs at the highest-ranked profile any viewer requests.
# iframe: keyframe interval in seconds (static screens are keyframe-dominated).
PROFILES={
    'hd':{'max_size':720,'fps':24,'bit_rate':2000000,'iframe':1,'rank':3},
    'grid':{'max_size':480,'fps':15,'bit_rate':1000000,'iframe':2,'rank':2},
    'wall':{'max_size':360,'fps':12,'bit_rate':600000,'iframe':2,'rank':1},
}
DEFAULT_PROFILE='hd'
FPS=PROFILES['hd']['fps']
MAX_SIZE=PROFILES['hd']['max_size']
BIT_RATE=PROFILES['hd']['bit_rate']
PROFILE_DOWNGRADE_DELAY=8.0   # seconds an encoder stays HD after its last HD viewer leaves
PROFILE_MIN_RESTART_GAP=3.0   # minimum encoder age before an upgrade restart
KEY_ONLY_INTERVAL=4.0         # hidden viewers get one refresh keyframe per interval
KEEP_AWAKE_INTERVAL=20
CONTROL_ENABLED=True          # scrcpy control socket for instant input
CONTROL_RETRY_AFTER=900       # seconds a phone runs without control after repeated failures
CONTROL_FAILURES_TO_DISABLE=2
SESSIONS={}
LOCKS={}
LOCK_USERS={}
PREPARED=set()
CLIENT_EVENTS={}
VIEWERS={}
CONTROL_DISABLED={}           # device -> unix time until which control stays off
CONTROL_FAILURES={}           # device -> consecutive control-enabled startup failures

# scrcpy v5.0.1 control protocol (big-endian, see ControlMessageReader.java)
MSG_INJECT_KEYCODE=0
MSG_INJECT_TOUCH_EVENT=2
ACTION_DOWN=0
ACTION_UP=1
ACTION_MOVE=2
POINTER_ID_GENERIC_FINGER=-2

def touch_message(action,x,y,width,height,pressure=1.0):
    """Encode an INJECT_TOUCH_EVENT (32 bytes)."""
    fixed=max(0,min(0xffff,int(round(max(0.0,min(1.0,pressure))*0xffff))))
    return struct.pack('!BBqiiHHHii',MSG_INJECT_TOUCH_EVENT,action,POINTER_ID_GENERIC_FINGER,
                       int(x),int(y),int(width)&0xffff,int(height)&0xffff,fixed,0,0)

def key_message(action,keycode,repeat=0,meta=0):
    """Encode an INJECT_KEYCODE (14 bytes)."""
    return struct.pack('!BBiii',MSG_INJECT_KEYCODE,action,int(keycode),int(repeat),int(meta))

class ControlUnavailable(RuntimeError):
    """The phone has no live scrcpy control socket; callers fall back to adb."""


class ControlWriteAmbiguous(ControlUnavailable):
    """A socket accepted bytes but drain failed; replay is unsafe."""

async def adb(device,*args,timeout=15):
    p=await asyncio.create_subprocess_exec('adb','-s',device,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err=await asyncio.wait_for(p.communicate(),timeout)
    except BaseException:
        if p.returncode is None:p.kill()
        with contextlib.suppress(Exception):await p.wait()
        raise
    if p.returncode:raise RuntimeError(err.decode(errors='replace').strip() or f'adb exited {p.returncode}')
    return out.decode(errors='replace').strip()

def codec_of(config):
    for nal in re.split(b'\x00\x00\x00?\x01',config):
        if len(nal)>3 and nal[0]&31==7:return 'avc1.'+nal[1:4].hex().upper()
    return ''

def keep_awake_offset(device):
    """Return a deterministic 0..interval-1 second fleet staggering offset."""
    digest=hashlib.sha256(device.encode('utf-8')).digest()
    return int.from_bytes(digest[:4],'big')%KEEP_AWAKE_INTERVAL

def control_allowed(device):
    return CONTROL_ENABLED and CONTROL_DISABLED.get(device,0)<=time.time()

class Session:
    def __init__(self,device):
        self.device=device
        self.clients={};self.task=None;self.idle_task=None;self.keep_awake_task=None
        self.proc=None;self.writer=None;self.port=None
        self.scid=f'{secrets.randbits(31):08x}'
        self.config=b'';self.codec='';self.width=0;self.height=0
        self.frames=0;self.bytes=0;self.dropped=0
        self.started=time.time();self.last_frame=0
        self.last_key=b'';self.sent_frames=0;self.error='';self.last_wake=0
        self.keep_awake_errors=0;self.keep_awake_error=''
        self.rate_started=time.monotonic();self.rate_frames=0;self.fps=0;self.peak_fps=0
        # Fleet-scale additions
        self.profile=DEFAULT_PROFILE;self.client_profiles={};self.key_only=set()
        self.key_sent={}
        self.profile_task=None;self.restart_requested=False;self.restarts=0;self.last_restart=0
        self.encoder_started=0.0
        self.control_enabled=control_allowed(device)
        self.control_writer=None;self.control_reader_task=None;self.control_error=''
        self.control_events=0;self.control_lock=asyncio.Lock()
    # ---- viewers -------------------------------------------------------
    def subscribe(self,profile=DEFAULT_PROFILE):
        if self.idle_task:self.idle_task.cancel();self.idle_task=None
        q=asyncio.Queue(maxsize=6)
        self.clients[q]=True
        self.client_profiles[q]=profile if profile in PROFILES else DEFAULT_PROFILE
        if self.last_key:q.put_nowait(self.last_key)
        self.schedule_profile_check()
        return q
    def unsubscribe(self,q):
        self.clients.pop(q,None);self.client_profiles.pop(q,None);self.key_only.discard(q);self.key_sent.pop(q,None)
        self.schedule_profile_check()
    def set_client_profile(self,q,profile):
        if q in self.clients and profile in PROFILES and self.client_profiles.get(q)!=profile:
            self.client_profiles[q]=profile
            self.schedule_profile_check()
    def set_key_only(self,q,enabled):
        if q not in self.clients:return
        if enabled:
            if q not in self.key_only:self.key_only.add(q);self.key_sent[q]=time.monotonic()
            return
        if q in self.key_only:
            self.key_only.discard(q)
            self.key_sent.pop(q,None)
            # Show the newest picture now; deltas resume at the next keyframe.
            self.clients[q]=True
            if self.last_key and not q.full():q.put_nowait(self.last_key)
    def broadcast(self,payload,key=False):
        if key:self.last_key=payload
        now=time.monotonic()
        for q,needs_key in list(self.clients.items()):
            if q in self.key_only:
                # Hidden viewers: an occasional fresh keyframe, no deltas.
                if not key or now-self.key_sent.get(q,0)<KEY_ONLY_INTERVAL:continue
                self.key_sent[q]=now
            if q.full():
                while not q.empty():q.get_nowait()
                self.clients[q]=True;needs_key=True;self.dropped+=1
                if self.last_key:q.put_nowait(self.last_key)
            if needs_key and not key:continue
            self.clients[q]=False;q.put_nowait(payload)
    # ---- quality profile -------------------------------------------------
    def desired_profile(self):
        live=[p for q,p in self.client_profiles.items() if q in self.clients]
        if not live:return self.profile
        return max(live,key=lambda name:PROFILES.get(name,PROFILES[DEFAULT_PROFILE])['rank'])
    def schedule_profile_check(self):
        if self.profile_task is not None and not self.profile_task.done():self.profile_task.cancel()
        self.profile_task=None
        if self.task is None or self.task.done():return
        want=self.desired_profile()
        if want==self.profile:return
        if PROFILES[want]['rank']>PROFILES.get(self.profile,PROFILES[DEFAULT_PROFILE])['rank']:
            delay=max(0.0,PROFILE_MIN_RESTART_GAP-(time.monotonic()-self.encoder_started))
        else:delay=PROFILE_DOWNGRADE_DELAY
        self.profile_task=asyncio.get_running_loop().create_task(self._profile_after(delay),name='h264-profile-'+self.device)
    async def _profile_after(self,delay):
        await asyncio.sleep(delay)
        if self.desired_profile()!=self.profile:self.request_restart('profile '+self.desired_profile())
    def request_restart(self,reason=''):
        if self.restart_requested:return
        self.restart_requested=True
        LOG.info('H.264 encoder restart for %s (%s)',self.device,reason or 'requested')
        if self.writer is not None:
            with contextlib.suppress(Exception):self.writer.close()
    # ---- keep awake ------------------------------------------------------
    async def wake_for_viewer(self):
        if self.last_frame and time.time()-self.last_frame>5 and time.time()-self.last_wake>8:
            self.last_wake=time.time()
            with contextlib.suppress(Exception):
                await adb(self.device,'shell','input','keyevent','KEYCODE_WAKEUP',timeout=5)
    async def _keep_awake_loop(self):
        # The immediate connect wake remains separate; recurring wakes are staggered.
        await asyncio.sleep(keep_awake_offset(self.device))
        while True:
            if self.clients:
                try:
                    await adb(self.device,'shell','input','keyevent','KEYCODE_WAKEUP',timeout=5)
                    self.last_wake=time.time()
                    self.keep_awake_error=''
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A transient adb failure must not take down a healthy encoder.
                    self.keep_awake_errors+=1
                    self.keep_awake_error=str(exc)
                    LOG.debug('H.264 keep-awake failed for %s: %s',self.device,exc)
            await asyncio.sleep(KEEP_AWAKE_INTERVAL)
    # ---- encoder -----------------------------------------------------------
    def command(self):
        # Samsung ACodec has a 255-byte argv buffer. Keep defaults omitted.
        # Default device metadata costs a 64-byte handshake, saving argv space.
        # Control is scrcpy's default; it is only disabled explicitly.
        p=PROFILES.get(self.profile) or PROFILES[DEFAULT_PROFILE]
        args=['adb','-s',self.device,'shell','CLASSPATH='+REMOTE,'app_process','/','com.genymobile.scrcpy.Server',VERSION,
              'scid='+self.scid,'tunnel_forward=true','audio=false']
        if not self.control_enabled:args.append('control=false')
        args+=[f"max_size={p['max_size']}",f"max_fps={p['fps']}",f"video_bit_rate={p['bit_rate']}",'cleanup=false',
               f"video_codec_options=i-frame-interval={int(p['iframe'])},repeat-previous-frame-after:long=200000"]
        if len(' '.join(args[5:]).encode())>=255:raise RuntimeError('Encoder command exceeds Samsung-safe length')
        return args
    async def run(self):
        self.keep_awake_task=asyncio.create_task(self._keep_awake_loop(),name='h264-wake-'+self.device)
        try:
            while True:
                self.restart_requested=False
                self.profile=self.desired_profile()
                self.control_enabled=control_allowed(self.device)
                if await self._encode_once()!='restart':break
                self.restarts+=1;self.last_restart=time.time()
                for q in self.clients:self.clients[q]=True
        except asyncio.CancelledError:raise
        except Exception as e:
            self.error=str(e)
            LOG.warning('H.264 stream %s: %s',self.device,self.error)
            for q in list(self.clients):
                while not q.empty():q.get_nowait()
                q.put_nowait({'error':self.error,'status':'encoder error'})
        finally:
            if self.profile_task is not None and not self.profile_task.done():self.profile_task.cancel()
            if self.keep_awake_task:
                self.keep_awake_task.cancel()
                with contextlib.suppress(asyncio.CancelledError,Exception):await self.keep_awake_task
    async def _drain_control(self,reader):
        # Device->client messages (clipboard, acks) are not used; discard them.
        try:
            while await reader.read(65536):pass
        except Exception:pass
    async def _close_control(self):
        task=self.control_reader_task;self.control_reader_task=None
        writer=self.control_writer;self.control_writer=None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):await task
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):await writer.wait_closed()
    async def _encode_once(self):
        """Run one encoder process. Returns 'restart' when a restart was requested; raises on failure."""
        args=self.command()
        spawned=False;framed=False;control=self.control_enabled
        try:
            try:state=await adb(self.device,'get-state',timeout=3)
            except Exception:state='offline'
            if state!='device':
                from ops.wireless_reconnect import ensure_online
                if not await asyncio.to_thread(ensure_online,self.device):
                    raise RuntimeError('Wireless ADB is offline; enabled stream will retry automatically')
            if self.device not in PREPARED:
                await adb(self.device,'push',str(SERVER),REMOTE,timeout=30)
                PREPARED.add(self.device)
            await adb(self.device,'shell','input','keyevent','KEYCODE_WAKEUP',timeout=8)
            self.port=int(await adb(self.device,'forward','tcp:0','localabstract:scrcpy_'+self.scid,timeout=8))
            self.proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
            spawned=True;self.encoder_started=time.monotonic()
            reader=None;deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if self.proc.returncode is not None:
                    out=await self.proc.stdout.read()
                    raise RuntimeError('scrcpy startup failed: '+out.decode(errors='replace')[-1200:])
                writer=None
                try:
                    reader,writer=await asyncio.open_connection('127.0.0.1',self.port)
                    if await asyncio.wait_for(reader.readexactly(1),1)!=b'\x00':raise RuntimeError('Invalid scrcpy handshake')
                    self.writer=writer;break
                except (OSError,asyncio.IncompleteReadError,asyncio.TimeoutError):
                    if writer:writer.close()
                    await asyncio.sleep(.15)
            if self.writer is None:raise RuntimeError('Timed out connecting to Android encoder')
            if control:
                # tunnel_forward accepts video first, then control; device metadata
                # follows on the video socket only after every socket is accepted.
                creader,cwriter=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',self.port),5)
                self.control_writer=cwriter;self.control_error=''
                self.control_reader_task=asyncio.create_task(self._drain_control(creader),name='h264-control-'+self.device)
            await asyncio.wait_for(reader.readexactly(64),5) # Default device-name metadata.
            if await asyncio.wait_for(reader.readexactly(4),5)!=b'h264':raise RuntimeError('Encoder did not select H.264')
            LOG.info('H.264 encoder connected for %s (profile %s, control %s)',self.device,self.profile,'on' if control else 'off')
            self.schedule_profile_check()
            while True:
                if self.restart_requested:return 'restart'
                try:header=await asyncio.wait_for(reader.readexactly(12),20)
                except asyncio.TimeoutError:
                    try:state=await adb(self.device,'get-state',timeout=3)
                    except Exception:state='offline'
                    if state!='device':raise RuntimeError('Wireless ADB disconnected; enabled stream will reconnect')
                    continue # An unchanged, online phone is not a frozen stream.
                word,size=struct.unpack('!QI',header)
                if word & (1<<63):
                    _,self.width,self.height=struct.unpack('!III',header)
                    self.config=b'';self.last_key=b''
                    for q in self.clients:self.clients[q]=True
                    continue
                if size<1 or size>8*1024*1024:raise RuntimeError(f'Invalid encoder packet length {size}')
                data=await reader.readexactly(size)
                if word & (1<<62):self.config=data;self.codec=codec_of(data);continue
                key=bool(word & (1<<61))
                if key and self.config:data=self.config+data
                if not framed:
                    framed=True
                    if control:CONTROL_FAILURES.pop(self.device,None)
                self.frames+=1;self.bytes+=len(data);self.last_frame=time.time();self.rate_frames+=1
                now=time.monotonic();span=now-self.rate_started
                if span>=1:
                    self.fps=round(self.rate_frames/span,1);self.peak_fps=max(self.peak_fps,self.fps)
                    self.rate_started=now;self.rate_frames=0
                self.broadcast(data,key)
                await asyncio.sleep(0)
        except asyncio.CancelledError:raise
        except Exception as e:
            if self.restart_requested:return 'restart'
            if control and spawned and not framed:
                # A control-enabled encoder that never produced a frame: after
                # repeated failures, stream without control (input uses adb).
                count=CONTROL_FAILURES.get(self.device,0)+1;CONTROL_FAILURES[self.device]=count
                if count>=CONTROL_FAILURES_TO_DISABLE:
                    CONTROL_DISABLED[self.device]=time.time()+CONTROL_RETRY_AFTER;CONTROL_FAILURES.pop(self.device,None)
                    LOG.warning('H.264 control disabled for %s for %ss after startup failures',self.device,CONTROL_RETRY_AFTER)
            self.control_error=str(e) if control else self.control_error
            detail=''
            if self.proc:
                with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(self.proc.wait(),1)
                if self.proc.returncode is not None:
                    detail=(await self.proc.stdout.read()).decode(errors='replace')[-2000:]
            if detail:raise RuntimeError(str(e)+'; '+detail) from e
            raise
        finally:
            await self._close_control()
            if self.writer:
                self.writer.close()
                with contextlib.suppress(Exception):await self.writer.wait_closed()
            self.writer=None
            if self.proc and self.proc.returncode is None:
                self.proc.terminate()
                try:await asyncio.wait_for(self.proc.wait(),3)
                except asyncio.TimeoutError:self.proc.kill();await self.proc.wait()
            self.proc=None
            if self.port:
                with contextlib.suppress(Exception):await adb(self.device,'forward','--remove','tcp:'+str(self.port),timeout=3)
            self.port=None
    # ---- control channel ---------------------------------------------------
    def control_ready(self):
        w=self.control_writer
        return bool(w is not None and not w.is_closing() and self.width and self.height
                    and self.task is not None and not self.task.done() and not self.restart_requested)
    def invalidate_control(self, error='Control channel invalidated'):
        """Retire a channel after accepted bytes had unknown delivery."""
        writer,self.control_writer=self.control_writer,None
        self.control_error=str(error)[:200]
        if writer is not None:
            with contextlib.suppress(Exception): writer.close()
    def point(self,nx,ny):
        """Map normalized 0..1 coordinates to the current video frame."""
        w,h=int(self.width),int(self.height)
        if w<=0 or h<=0:raise ControlUnavailable('Video size unknown')
        nx=max(0.0,min(1.0,float(nx)));ny=max(0.0,min(1.0,float(ny)))
        return int(round(nx*(w-1))),int(round(ny*(h-1))),w,h
    async def _control_write(self,payload,arbiter_token=None):
        if not self.control_ready():raise ControlUnavailable('No live control channel')
        w=self.control_writer
        try:
            await control_arbiter.write(self.device,w,payload,owner=arbiter_token,drain_timeout=2)
        except control_arbiter.AmbiguousWrite as exc:
            self.control_error=str(exc) or exc.__class__.__name__
            raise ControlWriteAmbiguous(self.control_error) from exc
        except (OSError,asyncio.TimeoutError,RuntimeError) as exc:
            self.control_error=str(exc) or exc.__class__.__name__
            raise ControlUnavailable(self.control_error) from exc
        self.control_events+=1
    async def write_control(self,payload,arbiter_token=None):
        await self._control_write(payload,arbiter_token)
    async def write_control_now(self,payload,arbiter_token=None):
        """Write without draining; live fan-out drains all targets together."""
        if not self.control_ready():raise ControlUnavailable('No live control channel')
        w=self.control_writer
        async with control_arbiter.hold(self.device,arbiter_token):
            if w is None or w.is_closing():raise ControlUnavailable('No live control channel')
            try:w.write(payload)
            except (OSError,asyncio.TimeoutError,RuntimeError) as exc:
                self.control_error=str(exc) or exc.__class__.__name__
                raise ControlUnavailable(self.control_error) from exc
        return w
    async def inject_touch(self,action,nx,ny,pressure=1.0,arbiter_token=None):
        x,y,w,h=self.point(nx,ny)
        await self._control_write(touch_message(action,x,y,w,h,pressure),arbiter_token)
    async def inject_key(self,keycode,arbiter_token=None):
        try:
            await self._control_write(key_message(ACTION_DOWN,keycode),arbiter_token)
        except ControlWriteAmbiguous as error:
            released=False
            try:
                await self._control_write(key_message(ACTION_UP,keycode),arbiter_token)
                released=True
            except Exception:
                pass
            self.invalidate_control('Control channel delivery is ambiguous; ' +
                                    ('release confirmed' if released else 'release unconfirmed'))
            raise ControlWriteAmbiguous(self.control_error) from error
        await self._control_write(key_message(ACTION_UP,keycode),arbiter_token)
    def viewer_profiles(self):
        counts={name:0 for name in PROFILES}
        for q,p in self.client_profiles.items():
            if q in self.clients:counts[p]=counts.get(p,0)+1
        return counts
    def status(self):
        age=round(time.time()-self.last_frame,2) if self.last_frame else None
        p=PROFILES.get(self.profile) or PROFILES[DEFAULT_PROFILE]
        return {'device':self.device,'transport':'h264-websocket','clients':len(self.clients),'codec':self.codec,
                'width':self.width,'height':self.height,'frames':self.frames,'bytes':self.bytes,'dropped':self.dropped,
                'sent_frames':self.sent_frames,'fps':self.fps if age is not None and age<2 else 0,'peak_fps':self.peak_fps,
                'session_id':self.scid,'target_fps':p['fps'],'bitrate_limit':p['bit_rate'],'encoder_sessions':1,'running':self.task is not None and not self.task.done(),
                'last_frame_age':age,'error':self.error,'keep_awake_interval':KEEP_AWAKE_INTERVAL,
                'keep_awake_active':bool(self.clients),'keep_awake_task_running':self.keep_awake_task is not None and not self.keep_awake_task.done(),
                'keep_awake_last_wake':self.last_wake or None,'keep_awake_errors':self.keep_awake_errors,
                'keep_awake_error':self.keep_awake_error,
                'profile':self.profile,'max_size':p['max_size'],'viewer_profiles':self.viewer_profiles(),
                'key_only_clients':len([q for q in self.key_only if q in self.clients]),'restarts':self.restarts,
                'control':self.control_ready(),'control_enabled':self.control_enabled,'control_error':self.control_error,
                'control_events':self.control_events}

@router.get('/api/phone/android-h264/status')
async def stream_status():
    active=[v for v in VIEWERS.values() if v.get('active')]
    return {'ok':True,'brand':'D3m0n in the droid','transport':'h264-websocket','mjpeg':False,
            'streams':[s.status() for s in SESSIONS.values()],'client_events':CLIENT_EVENTS,'viewers':active,
            'settings':{'max_size':MAX_SIZE,'target_fps':FPS,'bitrate_limit':BIT_RATE,'requested_idle_refresh_fps':5,'static_frames':'on-change; idle is not a failure','queue_limit':6,'one_encoder_per_device':True,'keep_awake_interval':KEEP_AWAKE_INTERVAL,'keep_awake':'viewer-bound nonpersistent KEYCODE_WAKEUP',
                        'profiles':PROFILES,'default_profile':DEFAULT_PROFILE,'profile_downgrade_delay':PROFILE_DOWNGRADE_DELAY,'control':CONTROL_ENABLED}}

@router.post('/api/phone/android-h264/client-status')
async def client_status(data:dict=Body(...)):
    device=str(data.get('device',''))[:150];event=str(data.get('event',''))[:50]
    viewer=str(data.get('viewer',''))[:100]
    if device and event:
        record={'detail':data.get('detail'),'codec':data.get('codec'),'at':time.time()}
        CLIENT_EVENTS.setdefault(device,{})[event]=record
        if viewer:
            item=VIEWERS.setdefault(viewer,{'id':viewer,'device':device,'active':False,'events':{}})
            item['events'][event]=record;item['last_seen']=time.time()
    if len(VIEWERS)>256:
        for key,v in list(VIEWERS.items()):
            if not v.get('active'):VIEWERS.pop(key,None)
            if len(VIEWERS)<=200:break
    return {'ok':True}

async def expire_session(device,s,lock):
    await asyncio.sleep(3)
    async with lock:
        if not s.clients and SESSIONS.get(device) is s:
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):await s.task
            SESSIONS.pop(device,None)
            if LOCK_USERS.get(device,0) == 0 and LOCKS.get(device) is lock:
                LOCKS.pop(device,None)

def viewer_message(s,q,text):
    """Apply a small JSON control message from a viewer ({profile} / {mode})."""
    if not text or len(text)>256:return
    try:value=json.loads(text)
    except ValueError:return
    if not isinstance(value,dict):return
    profile=value.get('profile')
    if isinstance(profile,str) and profile in PROFILES:s.set_client_profile(q,profile)
    mode=value.get('mode')
    if mode in ('key','full'):s.set_key_only(q,mode=='key')

@router.websocket('/api/phone/android-h264/{device}')
async def stream(client:WebSocket,device:str):
    origin=client.headers.get('origin','')
    if origin and origin not in get_allowed_origins():
        await client.close(code=1008,reason='Untrusted stream origin');return
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}',device) or device.startswith('ios:'):
        await client.close(code=1008,reason='Invalid Android device');return
    await client.accept()
    viewer=client.query_params.get('viewer') or secrets.token_hex(16)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',viewer):viewer=secrets.token_hex(16)
    profile=client.query_params.get('profile') or DEFAULT_PROFILE
    if profile not in PROFILES:profile=DEFAULT_PROFILE
    VIEWERS[viewer]={'id':viewer,'device':device,'active':True,'events':{},'connected_at':time.time(),'sent_frames':0,'profile':profile}
    lock=LOCKS.setdefault(device,asyncio.Lock())
    LOCK_USERS[device]=LOCK_USERS.get(device,0)+1
    async with lock:
        s=SESSIONS.get(device)
        if s is None or s.task.done():
            s=Session(device);SESSIONS[device]=s;s.task=asyncio.create_task(s.run(),name='h264-'+device)
        q=s.subscribe(profile)
        await s.wake_for_viewer()
    await client.send_json({'status':'connected','transport':'h264-websocket','device':device,'viewer':viewer,'profile':profile})
    async def send():
        while True:
            try:
                data=await asyncio.wait_for(q.get(),10)
            except asyncio.TimeoutError:
                await asyncio.wait_for(client.send_json({'status':'alive','transport':'h264-websocket','last_frame_age':s.status()['last_frame_age']}),15)
                continue
            if isinstance(data,dict):await client.send_json(data);await client.close(code=1011);return
            await client.send_bytes(data);s.sent_frames+=1
            if viewer in VIEWERS:VIEWERS[viewer]['sent_frames']+=1
    async def receive():
        while True:
            message=await client.receive()
            if message['type']=='websocket.disconnect':return
            text=message.get('text')
            if text:
                viewer_message(s,q,text)
                if viewer in VIEWERS:
                    VIEWERS[viewer]['profile']=s.client_profiles.get(q,profile)
                    VIEWERS[viewer]['key_only']=q in s.key_only
    tasks=[asyncio.create_task(send()),asyncio.create_task(receive())]
    try:
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            with contextlib.suppress(Exception):t.result()
    finally:
        for t in tasks:t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError,Exception):await t
        if viewer in VIEWERS:VIEWERS[viewer]['active']=False;VIEWERS[viewer]['closed_at']=time.time()
        async with lock:
            s.unsubscribe(q)
            if not s.clients:s.idle_task=asyncio.create_task(expire_session(device,s,lock))
        LOCK_USERS[device]=max(0,LOCK_USERS.get(device,1)-1)
        if LOCK_USERS.get(device) == 0:
            LOCK_USERS.pop(device,None)
