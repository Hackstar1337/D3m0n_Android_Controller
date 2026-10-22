"""Bounded reconnection of known wireless ADB peers; no scans or global resets."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress,re,subprocess,threading,time
POOL=ThreadPoolExecutor(max_workers=3,thread_name_prefix='droid-adb-reconnect')
LIMIT=threading.BoundedSemaphore(3)
GUARD=threading.Lock()
PENDING=set()
ADDRESS_LOCKS={}
LAST_ATTEMPT={}
_RFC1918_NETWORKS=(ipaddress.IPv4Network('10.0.0.0/8'),ipaddress.IPv4Network('172.16.0.0/12'),ipaddress.IPv4Network('192.168.0.0/16'))
def valid_address(address):
 try:
  host,port=address.rsplit(':',1)
  peer=ipaddress.IPv4Address(host)
  return (str(peer)==host and any(peer in network for network in _RFC1918_NETWORKS)
          and not peer.is_loopback and not peer.is_link_local and not peer.is_unspecified
          and not peer.is_multicast and port.isascii() and port.isdigit() and 1<=int(port)<=65535)
 except (ValueError,AttributeError):return False
def _run(args,timeout):
 return subprocess.run(['adb',*args],capture_output=True,text=True,timeout=timeout)
def ensure_online(address):
 if not valid_address(address):return False
 with GUARD:lock=ADDRESS_LOCKS.setdefault(address,threading.Lock())
 with lock,LIMIT:
  try:
   state=_run(['-s',address,'get-state'],3)
   if state.returncode==0 and state.stdout.strip()=='device':return True
  except subprocess.TimeoutExpired:pass
  now=time.monotonic()
  with GUARD:
   if now-LAST_ATTEMPT.get(address,-100)<10:return False
   LAST_ATTEMPT[address]=now
  try:
   # Only the already-offline peer is disconnected. Healthy peers are untouched.
   _run(['disconnect',address],3)
   _run(['connect',address],7)
   state=_run(['-s',address,'get-state'],3)
   return state.returncode==0 and state.stdout.strip()=='device'
  except (OSError,subprocess.TimeoutExpired):return False
def _worker(address):
 try:ensure_online(address)
 finally:
  with GUARD:PENDING.discard(address)
def schedule_reconnect(address):
 if not valid_address(address):return False
 with GUARD:
  if address in PENDING:return False
  PENDING.add(address)
 try:POOL.submit(_worker,address)
 except Exception:
  with GUARD:PENDING.discard(address)
  raise
 return True
