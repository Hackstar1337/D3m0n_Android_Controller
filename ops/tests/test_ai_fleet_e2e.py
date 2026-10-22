from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ai_contracts import Capability, TargetRef
from ops.ai_control_guard import AIControlGuard, reset_for_tests
from ops.ai_fleet_jobs import JobStore
from ops.ai_fleet_services import ProfileStoreService, SubprocessSDKSidecar, ImmutableTargetInventory
from ops.ai_fleet_settings import ProfileStore
from ops.ai_fleet_controller import FleetController, set_controller
from ops.ai_fleet_api import install_ai_fleet
from gitd.services.cloud_gateway import CloudGateway
from gitd.services.native_bridge import NativeBridge
from gitd.services.native_observation import CanonicalUIState, CanonicalNode
from ops.tests.test_ai_fleet_api import LocalAuth

class FakeObservation:
    def __init__(self):
        self.package='com.sec.android.app.launcher'
        self.history=[]
        self.sensitive=False
    async def state(self, serial):
        self.history.append(self.package)
        return CanonicalUIState(serial,self.package,'.Home',100,200,
            (CanonicalNode('title','Home' if 'launcher' in self.package else 'Settings','','TextView','',(0,0,100,30),False,True,True),),
            self.sensitive,'Owner input required' if self.sensitive else None)
    def _device(self, serial):
        observation=self
        class Device:
            def adb(self,*args,**kwargs):
                if args[:4]==('shell','input','keyevent','KEYCODE_HOME'): observation.package='com.sec.android.app.launcher'
                elif args[:3]==('shell','monkey','-p'): observation.package=args[3]
                else: raise AssertionError(('unexpected fake native command',args))
                return 'ok'
            def back(self): observation.package='com.sec.android.app.launcher'
        return Device()

class TestNative(NativeBridge):
    async def _current_app_observation(self,serial):
        from gitd.services.native_bridge_tools import NativeResult
        return NativeResult(True,'current_app',serial,metadata={'package':self.observation.package})

class FleetConcreteE2E(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        reset_for_tests()
        self.store=JobStore(self.root/'jobs.sqlite3',lease_seconds=30)
        self.profiles=ProfileStore(self.root/'profiles',resolver=lambda h,p:['93.184.216.34'])
        self.profiles.create_profile('shared','openai','test-model','https://api.openai.com/v1','fake-unit-credential',roles=['planner','executor','verifier'],capabilities=['text'],max_calls=100)
        self.calls=0
        self.reasoning=False
        self.stop_on_call=False
        self.observation=FakeObservation()
        self.native=TestNative(AIControlGuard(self.store),observation=self.observation,package_allowlist=frozenset({'com.sec.android.app.launcher','com.android.settings'}))
        def provider(request):
            self.calls+=1
            request_data=json.loads(request.content)
            assert request_data['reasoning_effort']=='none'
            if self.stop_on_call:
                from ai_contracts import JobAction
                self.store.control_job(self.current_job_id,JobAction.STOP)
            responses=[ '<function_calls><invoke name="home"></invoke></function_calls>',
              '<function_calls><invoke name="open_app"><parameter name="package">com.android.settings</parameter></invoke></function_calls>',
              '<function_calls><invoke name="home"></invoke></function_calls>',
              '<function_calls><invoke name="complete"><parameter name="success">true</parameter><parameter name="message">done</parameter></invoke></function_calls>' ]
            if self.reasoning:
                responses=[
                    '<thought>Use approved Home navigation.</thought><plan>Go Home</plan>',
                    '### Thought\nHome\n### Action\n{"action":"home"}\n### Description\nGo Home',
                    '<thought>Open the allowlisted Settings.</thought><plan>Open Settings</plan>',
                    '### Thought\nSettings\n### Action\n{"action":"open_app","package":"com.android.settings"}\n### Description\nOpen Settings',
                    '<thought>Return Home.</thought><plan>Go Home</plan>',
                    '### Thought\nHome\n### Action\n{"action":"home"}\n### Description\nGo Home',
                    '<thought>Native state shows Home.</thought><request_accomplished success="true">Verified the safe navigation</request_accomplished>']
            return httpx.Response(200,json={'id':'fake-response','choices':[{'message':{'content':responses[min(self.calls-1,len(responses)-1)]}}], 'usage':{'prompt_tokens':10,'completion_tokens':10}})
        self.gateway=CloudGateway(self.profiles,transport=httpx.MockTransport(provider),resolver=lambda h,p:['93.184.216.34'])
        python=os.environ.get('D3M0N_TEST_SDK_PYTHON')
        if not python: self.skipTest('actual isolated SDK interpreter required')
        self.sidecar=SubprocessSDKSidecar(python)
        target=TargetRef(target_id='fake-serial',serial='fake-serial',label='Fake phone',online=True)
        self.controller=FleetController(store=self.store,profiles=ProfileStoreService(self.profiles),runtime=self.sidecar,
            sidecar=self.sidecar,bridge=self.native,gateway=self.gateway,target_source=ImmutableTargetInventory((target,)))
        set_controller(self.controller)
        app=FastAPI();install_ai_fleet(app);app.add_middleware(LocalAuth)
        # Not a context-managed client: avoid autostart; dispatch is explicit.
        self.client=TestClient(app)
    def tearDown(self):
        if hasattr(self,'controller'): self.controller.stop_worker()
        set_controller(None);reset_for_tests();self.tmp.cleanup()
    def plan(self,mode="direct"):
        body={'intent':{'text':'Go Home, open Settings, then return Home','metadata':{'runtime_mode':mode}},
            'targets':[{'target_id':'fake-serial','serial':'fake-serial','label':'Fake phone','online':True}],
            'profiles':[{'profile_id':'shared','provider':'openai','model':'test-model','endpoint_origin':'https://api.openai.com/v1','configured':True}],
            'role_bindings':dict.fromkeys(['planner','executor','verifier'],'shared'),
            'policy':{'scope':'navigation','capabilities':['observe','navigate','input','app_state'],'max_active_workers':3,'budget_usd':20,'allow_vision':False},
            'idempotency_key':'e2e-plan-key-00001'}
        response=self.client.post('/api/ai-fleet/plans',json=body)
        self.assertEqual(response.status_code,201,response.text)
        created=response.json()
        response=self.client.post('/api/ai-fleet/plans/'+created['plan']['plan_id']+'/approve',json={'approval_token':created['approval_token'],'idempotency_key':'e2e-approval-key-00001'})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def test_actual_api_sdk_native_and_usage_settlement(self):
        self.assertTrue(self.sidecar.status().get('ready'))
        job=self.plan();self.controller.activation_enabled=True
        result=self.controller.dispatch_once()
        self.assertEqual(result['state'],'succeeded',result)
        final=self.client.get('/api/ai-fleet/jobs/'+job['job_id']).json()
        self.assertEqual(final['state'],'succeeded')
        self.assertEqual(self.calls,4)
        self.assertIn('com.android.settings',self.observation.history)
        self.assertEqual(self.observation.package,'com.sec.android.app.launcher')
        self.assertGreater(final['children'][0]['latest_step'],4)
        self.assertNotIn('fake-unit-credential',json.dumps(final))
        self.assertGreater(float(final['budget_settled_usd']),0)
    def test_sensitive_ui_pauses_before_any_cloud_call(self):
        job=self.plan();self.controller.activation_enabled=True;self.observation.sensitive=True
        result=self.controller.dispatch_once()
        self.assertEqual(result['state'],'waiting_for_owner',result)
        self.assertEqual(self.calls,0)
        final=self.store.get_job(job['job_id'])
        self.assertTrue(final.checkpoints)
    def test_profile_version_changes_cannot_retarget_approved_model(self):
        job=self.plan()
        self.profiles.create_profile('shared','openai','test-model','https://api.openai.com/v1','different-fake-unit',roles=['planner','executor','verifier'])
        self.controller.activation_enabled=True
        result=self.controller.dispatch_once()
        self.assertEqual(result['state'],'uncertain')
        self.assertEqual(self.calls,0)
    def test_reasoning_mode_reaches_real_native_bridge(self):
        self.reasoning=True
        job=self.plan(mode="reasoning");self.controller.activation_enabled=True
        result=self.controller.dispatch_once()
        self.assertEqual(result['state'],'succeeded',result)
        self.assertEqual(self.calls,7)
        self.assertIn('com.android.settings',self.observation.history)
        self.assertEqual(self.store.get_job(job['job_id']).state.value,'succeeded')
    def test_stop_during_model_call_fences_next_native_action(self):
        job=self.plan();self.current_job_id=job['job_id'];self.stop_on_call=True
        self.controller.activation_enabled=True
        result=self.controller.dispatch_once()
        final=self.store.get_job(job['job_id'])
        self.assertEqual(final.state.value,'stopped',result)
        self.assertEqual(self.calls,1)
        self.assertNotIn('com.android.settings',self.observation.history)
        self.assertFalse(any(child.state.value=='running' for child in final.children))
    def test_invalid_secret_is_not_echoed_in_422(self):
        response=self.client.post('/api/ai-fleet/profiles',json={'profile_id':'shared','provider':'openai','model':'test-model','endpoint_origin':'https://api.openai.com/v1','secret':'fake-secret-never-echo','unexpected':True})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('fake-secret-never-echo',response.text)

if __name__=='__main__': unittest.main()
