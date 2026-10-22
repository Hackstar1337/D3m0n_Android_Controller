import ast,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from ops.fast_input_geometry import clear_size_cache
SOURCE=Path(__file__).resolve().parents[2]/'gitd/routers/phone.py'
def load_function(name):
 tree=ast.parse(SOURCE.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);node.decorator_list=[]
 env={'Body':lambda value:value,'get_device':lambda device:None,'is_ios_ref':lambda device:device.startswith('ios:'),'_platform':lambda device:'ios' if device.startswith('ios:') else 'android','_phone_error':lambda device,error:{'ok':False,'error':error},'_error_text':str,'re':__import__('re')}
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),env)
 return env[name],env
class FastManualRouteTests(unittest.TestCase):
 def setUp(self):
  clear_size_cache();self.dev=Mock();self.dev.adb.return_value='Physical size: 1080x2220';self.fake=types.ModuleType('gitd.bots.common.adb');self.fake.Device=Mock(return_value=self.dev)
 def run_route(self,name,body):
  function,env=load_function(name);env['get_device']=lambda device:self.dev
  with patch.dict('sys.modules',{'gitd.bots.common.adb':self.fake}):return function(body)
 def test_android_tap_has_zero_post_action_sleep(self):
  r=self.run_route('api_phone_tap',{'device':'192.168.50.37:5555','x':180,'y':370,'stream_w':360,'stream_h':740})
  self.assertTrue(r['ok']);self.dev.tap.assert_called_once_with(540,1110,delay=0)
 def test_repeated_taps_query_display_once(self):
  for _ in range(5):self.run_route('api_phone_tap',{'device':'192.168.50.37:5555','x':180,'y':370,'stream_w':360,'stream_h':740})
  self.assertEqual(self.dev.adb.call_count,1);self.assertEqual(self.dev.tap.call_count,5)
 def test_manual_swipe_uses_supplied_duration_and_no_sleep(self):
  r=self.run_route('api_phone_swipe',{'device':'192.168.50.37:5555','x1':180,'y1':370,'x2':180,'y2':370,'stream_w':360,'stream_h':740,'duration':900})
  self.assertTrue(r['ok']);self.dev.swipe.assert_called_once_with(540,1110,540,1110,ms=900,delay=0)
 def test_swipe_duration_is_bounded(self):
  self.run_route('api_phone_swipe',{'device':'192.168.50.37:5555','duration':99999});self.assertEqual(self.dev.swipe.call_args.kwargs['ms'],1500)
 def test_ios_tap_retains_existing_signature(self):
  r=self.run_route('api_phone_tap',{'device':'ios:abc','x':20,'y':30});self.assertTrue(r['ok']);self.dev.tap.assert_called_once_with(20,30)
 def test_ios_swipe_retains_existing_signature(self):
  r=self.run_route('api_phone_swipe',{'device':'ios:abc','x1':10,'y1':20,'x2':30,'y2':40});self.assertTrue(r['ok']);self.dev.swipe.assert_called_once_with(10,20,30,40)
 def test_invalid_geometry_does_not_send_tap(self):
  r=self.run_route('api_phone_tap',{'device':'192.168.50.37:5555','x':-10,'y':100,'stream_w':360,'stream_h':740});self.assertFalse(r['ok']);self.dev.tap.assert_not_called()
 def test_failed_display_probe_does_not_send_wrong_swipe(self):
  self.dev.adb.side_effect=RuntimeError('offline')
  r=self.run_route('api_phone_swipe',{'device':'192.168.50.37:5555','x1':20,'y1':100,'x2':100,'y2':200,'stream_w':360,'stream_h':740});self.assertFalse(r['ok']);self.dev.swipe.assert_not_called()
if __name__=='__main__':unittest.main()
