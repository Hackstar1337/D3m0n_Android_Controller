import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import httpx
from ops.ai_fleet_settings import ProfileStore, ProfileValidationError
from ops.ai_model_policy import policy_for, register_reviewed_policy, refresh_openrouter_policy
from gitd.services.cloud_gateway import CloudGateway

class ModelPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=ProfileStore(self.tmp.name,approved_generic_origins=('https://llm.example.com/v1',),resolver=lambda h,p:['8.8.8.8'])
    def tearDown(self):self.tmp.cleanup()
    def create(self,name='shared',provider='openai',model='test-model',url='https://api.openai.com/v1',cap='20'):
        return self.store.create_profile(name,provider,model,url,'fake-offline-key',roles=('planner','executor','verifier'),max_price_usd=cap)
    def test_one_profile_can_be_frozen_for_all_three_roles(self):
        self.create();freeze=self.store.freeze(dict(planner='shared',executor='shared',verifier='shared'))
        self.assertEqual(len(freeze.profiles),1);self.assertEqual(set(freeze.role_bindings.values()),{'shared'})
    def test_shared_key_cannot_multiply_spend_by_adding_profiles(self):
        self.create('a',cap='1');self.create('b',cap='1')
        self.store.reserve('a',amount_usd='0.75')
        with self.assertRaises(ProfileValidationError):self.store.reserve('b',amount_usd='0.30')
        self.store.reserve('b',amount_usd='0.25')
    async def test_openrouter_public_metadata_never_sends_a_key(self):
        self.create(provider='openrouter',model='vendor/current',url='https://openrouter.ai/api/v1')
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'data':[{'id':'vendor/current','context_length':128000,'top_provider':{'max_completion_tokens':8192},'pricing':{'prompt':'0.0000001','completion':'0.0000005'},'architecture':{'input_modalities':['text'],'output_modalities':['text']},'supported_parameters':['response_format']}]})
        policy=await refresh_openrouter_policy(self.store,'shared',transport=httpx.MockTransport(handler))
        self.assertNotIn('authorization',seen[0].headers);self.assertEqual(policy['input_usd_per_million'],0.1)
        self.assertEqual(policy_for(self.store,'shared')['token_parameter'],'max_tokens')
    async def test_generic_registered_policy_controls_payload_not_memory(self):
        self.create(provider='generic',model='current',url='https://llm.example.com/v1')
        self.assertIsNone(policy_for(self.store,'shared'))
        policy={'input_usd_per_million':1,'output_usd_per_million':2,'request_usd':0,'context_window':32768,'max_output_tokens':8192,'token_parameter':'max_tokens','reasoning_effort':None,'structured_output':True,'vision':False,'source':'https://llm.example.com/docs'}
        register_reviewed_policy(self.store,'shared',policy,valid_until=datetime.now(timezone.utc)+timedelta(hours=1))
        seen=[]
        def handler(request):
            seen.append(json.loads(request.content));return httpx.Response(200,json={'choices':[{'message':{'content':'ok'}}],'usage':{'prompt_tokens':3,'completion_tokens':2}})
        gateway=CloudGateway(self.store,transport=httpx.MockTransport(handler),resolver=lambda h,p:['8.8.8.8'])
        result=await gateway.invoke('shared',role='executor',component='fast_agent',messages=[{'role':'user','content':'offline-test'}],max_tokens=16)
        self.assertEqual(result.content,'ok');self.assertEqual(seen[0]['max_tokens'],16);self.assertNotIn('reasoning_effort',seen[0])
    def test_unbounded_or_stale_metadata_is_rejected(self):
        self.create(provider='generic',model='current',url='https://llm.example.com/v1')
        bad={'input_usd_per_million':'NaN','output_usd_per_million':2,'request_usd':0,'context_window':32768,'max_output_tokens':8192,'token_parameter':'max_tokens','reasoning_effort':None,'structured_output':True,'vision':False}
        with self.assertRaises(ValueError):register_reviewed_policy(self.store,'shared',bad,valid_until=datetime.now(timezone.utc)+timedelta(hours=1))

if __name__=='__main__':unittest.main()
