"""Independent regressions; all model/device/database IO is mocked."""
import asyncio,json,time,unittest
from unittest.mock import patch
from test_agent_console_backend import AgentConsoleBackendTests, PullResponse, Response


class ReviewTests(AgentConsoleBackendTests):
    def test_resumed_orphans_are_dropped_and_native_arguments_are_objects(self):
        s=self.service.create_session('android-1',provider='ollama')
        M=self.service.ChatMessage
        s.messages=[M('user','old'),M('tool_result','orphan','screenshot',{},'result-only'),
                    M('tool_call','','screenshot',{},'call-only'),
                    M('tool_call','','screenshot',{'device':'android-1'},'pair'),
                    M('tool_result','ok','screenshot',{},'pair'),M('user','current')]
        history=self.service._ollama_history(s,'system','current')
        serialized=json.dumps(history)
        self.assertNotIn('call-only',serialized);self.assertNotIn('result-only',serialized)
        calls=[c for msg in history for c in msg.get('tool_calls',[])]
        self.assertEqual(len(calls),1);self.assertIsInstance(calls[0]['function']['arguments'],dict)
        tools=[m for m in history if m['role']=='tool']
        self.assertEqual(len(tools),1);self.assertEqual(tools[0]['tool_name'],'screenshot')

    def test_oversized_history_unit_is_not_unconditionally_included(self):
        s=self.service.create_session('android-1',provider='ollama');s.context_length=1024
        s.messages=[self.service.ChatMessage('user','old'*20000),self.service.ChatMessage('user','new')]
        history=self.service._ollama_history(s,'system','new')
        self.assertEqual([m['role'] for m in history],['system','user'])

    def test_live_context_retains_complete_pairs_and_latest_image_only(self):
        messages=[{'role':'system','content':'system'},{'role':'user','content':'task','images':['old-image']}]
        for i in range(40):
            messages.append({'role':'assistant','content':'','tool_calls':[{'id':str(i),'type':'function','function':{'name':'screenshot','arguments':{}}}]})
            messages.append({'role':'tool','tool_call_id':str(i),'tool_name':'screenshot','content':'result'*200,'images':[str(i)]})
        bounded=self.service._bound_ollama_messages(messages,8192,1024,[],1)
        self.assertEqual(bounded[1]['content'],'task')
        ids={c['id'] for m in bounded for c in m.get('tool_calls',[])}
        results={m['tool_call_id'] for m in bounded if m['role']=='tool'}
        self.assertEqual(ids,results);self.assertTrue(ids)
        self.assertEqual([m['images'] for m in bounded if m.get('images')],[['39']])
        self.assertLess(sum(len(json.dumps({k:v for k,v in m.items() if k!='images'})) for m in bounded),30000)

    def test_dense_screen_context_never_drops_latest_result(self):
        messages=[{'role':'system','content':'instructions'+'x'*4000+'Target android-1'},
                  {'role':'user','content':'task: inspect '+'screen'*3000},
                  {'role':'assistant','content':'','tool_calls':[{'id':'last','function':{'name':'screenshot','arguments':{}}}]},
                  {'role':'tool','tool_call_id':'last','tool_name':'screenshot','content':'LATEST_RESULT'}]
        bounded=self.service._bound_ollama_messages(messages,8192,1024,[{'schema':'s'*24000}],1)
        self.assertTrue(bounded[1]['content'].startswith('task: inspect'))
        self.assertIn('Target android-1',bounded[0]['content'])
        self.assertEqual(bounded[-1]['content'],'LATEST_RESULT')
        self.assertLess(sum(len(json.dumps(m)) for m in bounded),5000)

    def test_legacy_json_tools_still_execute_and_use_text_results(self):
        responses=[Response(data={'message':{'content':'```tool\n{"tool":"screenshot","args":{}}\n```'}}),
                   Response(data={'message':{'content':'done'}})]
        with patch.object(self.service,'_ollama_capabilities',return_value={'completion'}),patch.object(self.requests,'request',side_effect=responses) as request:
            s=self.service.create_session('android-1',provider='ollama')
            events=list(self.service._chat_ollama(s,'inspect'))
        self.assertEqual(self.tools.TOOL_CALLS,[('screenshot',{'device':'android-1'})])
        payload=request.call_args_list[-1].kwargs['json']
        self.assertNotIn('tools',payload)
        self.assertFalse(any(m['role']=='tool' or m.get('tool_calls') for m in payload['messages']))
        self.assertTrue(any('Tool screenshot result:' in m.get('content','') for m in payload['messages']))

    def test_native_system_and_result_use_native_protocol(self):
        s=self.service.create_session('android-1',provider='ollama')
        list(self.service._chat_ollama(s,'inspect'))
        payloads=[call[2]['json'] for call in self.requests.calls if call[1].endswith('/api/chat')]
        self.assertIn('Use native function calls',payloads[0]['messages'][0]['content'])
        second=payloads[1]['messages'];call=next(m for m in second if m.get('tool_calls'))['tool_calls'][0]
        result=next(m for m in second if m['role']=='tool')
        self.assertIsInstance(call['function']['arguments'],dict)
        self.assertEqual(call['id'],result['tool_call_id']);self.assertEqual(result['tool_name'],'screenshot')
        first=self.service._native_tool_calls({'tool_calls':[{'function':{'name':'screenshot','arguments':{}}}]})
        again=self.service._native_tool_calls({'tool_calls':[{'function':{'name':'screenshot','arguments':{}}}]})
        self.assertNotEqual(first[0]['id'],again[0]['id'])

    def test_message_screenshot_flag_and_omitted_preference(self):
        observed=[]
        def fake_turn(session,message,**kw):
            observed.append((session.auto_screenshot,kw.get('_reserved')))
            yield {'type':'done'}
        async def consume(response):
            return [line async for line in response.body_iterator]
        with patch.object(self.service,'chat_turn',fake_turn),patch.object(self.service,'save_session_to_db',lambda s:None):
            response=self.router.send_message({'device':'android-1','provider':'ollama','content':'hello','auto_screenshot':False})
            asyncio.run(consume(response));s=next(iter(self.service._sessions.values()))
            response=self.router.send_message({'session_id':s.id,'device':'android-1','content':'again'})
            asyncio.run(consume(response))
        self.assertEqual(observed,[(False,True),(False,True)])
        self.assertFalse(s.run_lock.locked())

    def test_early_stop_before_first_agent_work_preserves_cancellation(self):
        async def scenario():
            with patch.object(self.service,'save_session_to_db',lambda s:None),patch.object(self.service,'get_screen_tree',side_effect=AssertionError('Phone read after Stop')):
                response=self.router.send_message({'device':'android-1','provider':'ollama','content':'hello'})
                s=next(iter(self.service._sessions.values()))
                self.assertTrue(s.run_lock.locked())
                await anext(response.body_iterator)
                self.service.stop_agent(s.id)
                remaining=[line async for line in response.body_iterator]
                self.assertTrue(any('stopped' in line for line in remaining))
                self.assertFalse(s.run_lock.locked())
        asyncio.run(scenario());self.assertEqual(self.tools.TOOL_CALLS,[])

    def test_busy_and_wrong_device_rejected_before_work(self):
        from fastapi import HTTPException
        s=self.service.create_session('android-1',provider='ollama')
        with self.assertRaises(HTTPException) as bad:
            self.router.send_message({'session_id':s.id,'device':'android-2','content':'hello'})
        self.assertEqual(bad.exception.status_code,409)
        self.service.begin_agent_run(s)
        with self.assertRaises(HTTPException) as busy:
            self.router.send_message({'session_id':s.id,'device':'android-1','content':'hello'})
        self.assertEqual(busy.exception.status_code,409)
        self.service.end_agent_run(s);self.assertEqual(self.tools.TOOL_CALLS,[])

    def test_queued_stop_before_message_acceptance_prevents_run(self):
        from fastapi import HTTPException
        s=self.service.create_session('android-1',provider='ollama')
        self.service.stop_agent(s.id,run_id='queued-message')
        with self.assertRaises(HTTPException) as stopped:
            self.router.send_message({'session_id':s.id,'device':'android-1','content':'hello','run_id':'queued-message'})
        self.assertEqual(stopped.exception.status_code,409)
        self.assertFalse(s.run_lock.locked());self.assertEqual(self.tools.TOOL_CALLS,[])

    def test_stale_stop_does_not_cancel_a_newer_run(self):
        s=self.service.create_session('android-1',provider='ollama')
        self.service.begin_agent_run(s,run_id='old')
        self.service.end_agent_run(s)
        self.service.begin_agent_run(s,run_id='new')
        self.service.stop_agent(s.id,run_id='old')
        self.assertFalse(s.cancel_event.is_set());self.assertEqual(s.current_run_id,'new')
        self.service.end_agent_run(s)

    def test_status_timeout_is_stable_and_show_failure_does_not_infer_caps(self):
        with patch.object(self.router,'_ollama_request',side_effect=TimeoutError('mock')):
            state=self.router.ollama_status()
        self.assertFalse(state['ok']);self.assertEqual(state['models'],[])
        real=self.router._ollama_request
        def mock(method,path,**kw):
            if path=='/api/show':raise TimeoutError('mock')
            return real(method,path,**kw)
        with patch.object(self.router,'_ollama_request',mock):
            state=self.router.ollama_status()
        self.assertTrue(state['ok']);self.assertEqual(state['models'][0]['capabilities'],[])

    def test_download_error_and_truncated_stream_never_claim_completion(self):
        for frames in ([{'error':'failed digest'}],[{'status':'pulling','completed':5,'total':10}],[]):
            with patch.object(self.requests,'post',return_value=PullResponse(frames)):
                self.router.ollama_pull(self.router.OllamaModelRequest(model='mock-review-model'))
                for _ in range(100):
                    state=self.router.ollama_pull_status('mock-review-model')
                    if state['status']=='error':break
                    time.sleep(.005)
            self.assertEqual(state['status'],'error');self.assertIn('error',state)

    def test_stop_does_not_wait_for_slow_response_close(self):
        s=self.service.create_session('android-1',provider='ollama')
        class Slow(Response):
            def close(self):time.sleep(.4);super().close()
        r=Slow();self.service._track_http_response(s.id,r,True)
        start=time.monotonic();self.service.stop_agent(s.id)
        self.assertLess(time.monotonic()-start,.2);self.assertTrue(s.cancel_event.is_set())


    def test_real_screen_tree_fits_without_losing_task_or_latest_pair(self):
        task='Launch com.android.settings, verify it, then press_home. Change no settings.'
        messages=[{'role':'system','content':'Target android-1. Use only safe requested navigation.'},
                  {'role':'user','content':'[Screen]\n'+'tree '*300+'\nDevice: android-1\n\n'+task},
                  {'role':'assistant','content':'','tool_calls':[{'id':'last','function':{'name':'get_screen_tree','arguments':{'device':'android-1'}}}]},
                  {'role':'tool','tool_call_id':'last','tool_name':'get_screen_tree','content':'<tree>'+'node '*6000+'</tree>'}]
        schema=[{'schema':'s'*24000}]
        bounded=self.service._bound_ollama_messages(messages,8192,768,schema,1)
        self.assertIn(task,bounded[1]['content'])
        self.assertIn('Tool output shortened',bounded[-1]['content'])
        self.assertEqual(bounded[-1]['tool_call_id'],'last')
        self.assertEqual(bounded[-2]['tool_calls'][0]['id'],'last')
        self.assertLessEqual(sum(len(json.dumps(m)) for m in bounded),8192*4-len(json.dumps(schema))-768*4)
    def test_new_session_route_uses_saved_provider_model_and_options(self):
        from ops import ai_settings
        snapshot={'default_provider':'ollama','default_model':'qwen3.5:4b','defaults':{'context_length':16384,'max_output_tokens':768,'max_turns':5}}
        with patch.object(ai_settings,'load_settings',return_value=snapshot):
            response=self.router.create_session({'device':'android-1'})
        session=self.service.get_session(response['session_id'])
        self.assertEqual((session.provider,session.model),('ollama','qwen3.5:4b'))
        self.assertEqual((session.context_length,session.max_output_tokens,session.max_turns),(16384,768,5))
    def test_corrupt_settings_never_fall_back_to_a_cloud_default(self):
        from ops import ai_settings
        with patch.object(ai_settings,'load_settings',side_effect=ai_settings.SettingsError('unavailable')):
            with self.assertRaises(RuntimeError):self.service.create_session('android-1')

if __name__=='__main__':unittest.main(verbosity=2)
