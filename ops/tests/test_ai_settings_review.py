import asyncio,json,os,tempfile,unittest
from unittest.mock import patch
from ops import ai_settings as s

class Reply:
 def __init__(self,body,status=200):self.body=body;self.status_code=status;self.closed=False
 @property
 def content(self):raise AssertionError('Unbounded .content access')
 def iter_content(self,chunk_size=8192):
  for start in range(0,len(self.body),chunk_size):yield self.body[start:start+chunk_size]
 def close(self):self.closed=True

class AISettingsReviewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'D3M0N_AI_SETTINGS_STATE_DIR':self.tmp.name},clear=False);self.env.start()
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def test_public_default_model_and_optional_vllm_key_are_accurate(self):
  self.assertEqual(s.public_settings()['default_model'],'qwen3.5:4b')
  with patch.dict(os.environ,{'GITD_VLLM_API_KEY':'test-fake-private-key'}):
   p=s.public_provider('vllm');self.assertTrue(p['api_key_configured']);self.assertEqual(p['key_source'],'environment')
   s.update_provider('vllm',{'api_key':'saved-test-private-key'})
   p=s.public_provider('vllm');self.assertEqual(p['key_source'],'saved');self.assertNotIn('saved-test-private-key',json.dumps(p))
 def test_empty_placeholder_is_not_a_configured_vllm_secret(self):
  with patch.dict(os.environ,{'GITD_VLLM_API_KEY':'EMPTY'}):self.assertFalse(s.public_provider('vllm')['api_key_configured'])
 def test_dns_names_metadata_addresses_and_nonstandard_cloud_ports_rejected(self):
  for url in ['http://attacker.local:11434','http://169.254.169.254:80','http://0.0.0.0:11434','http://192.0.2.4:8000']:
   with self.assertRaises(ValueError):s.validate_endpoint('ollama',url)
  with self.assertRaises(ValueError):s.validate_endpoint('anthropic','https://api.anthropic.com:8443')
  self.assertEqual(s.validate_endpoint('vllm','http://192.168.50.10:8000/v1'),'http://192.168.50.10:8000/v1')
 def test_metadata_is_bounded_and_response_always_closed(self):
  r=Reply(b'a'*(s.MAX_BODY_BYTES+10000))
  with self.assertRaises(ValueError):s._safe_response_json(r)
  self.assertTrue(r.closed)
  r=Reply(b'{"models":[]}');self.assertEqual(s._safe_response_json(r),{'models':[]});self.assertTrue(r.closed)
 def test_anthropic_metadata_headers_and_no_redirects(self):
  s.update_provider('anthropic',{'api_key':'fake-unit-key'})
  with patch('requests.get',return_value=Reply(b'{"data":[{"id":"unit-model"}]}')) as get:
   self.assertTrue(s.test_provider('anthropic')['ok']);headers=get.call_args.kwargs['headers']
   self.assertEqual(headers['anthropic-version'],'2023-06-01');self.assertEqual(headers['x-api-key'],'fake-unit-key');self.assertFalse(get.call_args.kwargs['allow_redirects'])
 def test_openrouter_uses_auth_metadata_not_the_public_catalog(self):
  s.update_provider('openrouter',{'api_key':'fake-unit-key'})
  with patch('requests.get',return_value=Reply(b'{"data":{}}')) as get:
   self.assertTrue(s.test_provider('openrouter')['ok']);self.assertEqual(get.call_args.args[0],'https://openrouter.ai/api/v1/auth/key')
 def test_ollama_reports_missing_model_honestly(self):
  with patch('requests.get',return_value=Reply(b'{"models":[{"name":"other:4b"}]}')):
   result=s.test_provider('ollama');self.assertFalse(result['ok']);self.assertEqual(result['status'],'model_missing')
 def test_settings_json_is_bounded_before_the_route_runs(self):
  async def exercise(headers,pieces):
   reached=[];sent=[];chunks=iter(pieces)
   async def app(scope,receive,send):reached.append(await receive())
   async def receive():return next(chunks,{'type':'http.disconnect'})
   async def send(message):sent.append(message)
   scope={'type':'http','path':'/api/ai-settings/ollama','method':'PUT','headers':headers}
   await s.AISettingsBodyLimit(app)(scope,receive,send)
   return reached,sent
  reached,sent=asyncio.run(exercise([(b'content-length',str(s.MAX_BODY_BYTES+1).encode())],[]))
  self.assertFalse(reached);self.assertEqual(sent[0]['status'],413)
  reached,sent=asyncio.run(exercise([], [{'type':'http.request','body':b'a'*s.MAX_BODY_BYTES,'more_body':True},{'type':'http.request','body':b'b','more_body':False}]))
  self.assertFalse(reached);self.assertEqual(sent[0]['status'],413)
  reached,sent=asyncio.run(exercise([], [{'type':'http.request','body':b'{}','more_body':False}]))
  self.assertEqual(reached[0]['body'],b'{}');self.assertFalse(sent)

if __name__=='__main__':unittest.main()
