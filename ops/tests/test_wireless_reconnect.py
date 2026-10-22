import subprocess,unittest
from unittest.mock import patch,Mock
from ops import wireless_reconnect as m
class WirelessReconnectTests(unittest.TestCase):
 def setUp(self):m.PENDING.clear();m.LAST_ATTEMPT.clear();m.ADDRESS_LOCKS.clear()
 def test_invalid_addresses_are_rejected(self):
  for addr in ['usb-device','1.2.3.4:0','256.1.1.1:5555','192.168.50.37:5555; id','127.0.0.1:70000']:
   self.assertFalse(m.valid_address(addr));self.assertFalse(m.schedule_reconnect(addr))
 def test_online_peer_is_never_disconnected(self):
  with patch.object(m,'_run',return_value=Mock(returncode=0,stdout='device\n')) as run:
   self.assertTrue(m.ensure_online('192.168.50.37:5555'))
   self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.args[0],['-s','192.168.50.37:5555','get-state'])
 def test_offline_peer_reconnect_is_targeted_and_bounded(self):
  with patch.object(m,'_run',side_effect=[Mock(returncode=1,stdout=''),Mock(),Mock(),Mock(returncode=0,stdout='device')]) as run:
   self.assertTrue(m.ensure_online('192.168.50.37:5555'))
   self.assertEqual([x.args[0][0] for x in run.call_args_list],['-s','disconnect','connect','-s'])
   self.assertTrue(all(x.args[1]<=7 for x in run.call_args_list))
 def test_timeout_is_a_nonfatal_failure(self):
  with patch.object(m,'_run',side_effect=subprocess.TimeoutExpired('adb',3)):
   self.assertFalse(m.ensure_online('192.168.50.37:5555'))
 def test_background_jobs_are_deduplicated(self):
  with patch.object(m.POOL,'submit') as submit:
   self.assertTrue(m.schedule_reconnect('192.168.50.37:5555'));self.assertFalse(m.schedule_reconnect('192.168.50.37:5555'));self.assertEqual(submit.call_count,1)
 def test_worker_releases_pending_marker(self):
  m.PENDING.add('192.168.50.37:5555')
  with patch.object(m,'ensure_online',return_value=False):m._worker('192.168.50.37:5555')
  self.assertNotIn('192.168.50.37:5555',m.PENDING)
 def test_repeated_attempts_are_rate_limited(self):
  m.LAST_ATTEMPT['192.168.50.37:5555']=m.time.monotonic()
  with patch.object(m,'_run',return_value=Mock(returncode=1,stdout='')) as run:
   self.assertFalse(m.ensure_online('192.168.50.37:5555'));self.assertEqual(run.call_count,1)
if __name__=='__main__':unittest.main()
