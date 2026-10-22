import asyncio,time,unittest
from unittest.mock import AsyncMock,patch
import httpx
from fastapi import FastAPI
from ops import control_arbiter as arbiter
from ops import fast_control as fc

class AIControlHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):arbiter.reset_for_tests();fc.reset_state_for_tests()
    async def asyncTearDown(self):arbiter.reset_for_tests();fc.reset_state_for_tests()
    async def test_ai_cannot_interrupt_an_existing_native_gesture(self):
        ai_owner=object()
        async with arbiter.acquired('dev1'):
            with self.assertRaises(arbiter.AIOwnershipError):
                arbiter.claim_ai('dev1',ai_owner,'lease','child',1,expires_at=time.monotonic()+60)
        arbiter.claim_ai('dev1',ai_owner,'lease','child',1,expires_at=time.monotonic()+60)
        with self.assertRaises(arbiter.AIOwnershipError):arbiter.assert_write_allowed('dev1')
        arbiter.release_ai('dev1',ai_owner);arbiter.assert_write_allowed('dev1')
    async def test_blocked_sync_leader_never_starts_follower_actions(self):
        ai_owner=object();arbiter.claim_ai('dev1',ai_owner,'lease','child',1,expires_at=time.monotonic()+60)
        follower=AsyncMock(return_value={})
        app=FastAPI();fc.install_fast_control(app)
        with patch.object(fc,'fan_out',follower),patch.object(fc.sync_links,'link_for',return_value=None):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                response=await client.post('/api/phone/tap',json={'device':'dev1','x':1,'y':1,'stream_w':3,'stream_h':3,'sync':{'devices':['dev2','dev3']}})
        self.assertEqual(response.status_code,200);self.assertFalse(response.json()['ok']);self.assertEqual(response.json()['sync']['ok'],0)
        follower.assert_not_awaited()

if __name__=='__main__':unittest.main()
