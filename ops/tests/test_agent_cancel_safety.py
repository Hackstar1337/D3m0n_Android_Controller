"""Focused cancellation regressions: no sockets, providers, ADB or private state.

Native transport tests execute production _ollama_http_request with fake
HTTPConnection/socket seams. Route tests use real FastAPI when available and
skip locally otherwise; parent owns framework-backed suite execution.
"""
import asyncio
import importlib.util
import io
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agent_console_backend import FakeRequests, Response, install_stubs

ROOT = Path(__file__).parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeSocket:
    def __init__(self):
        self.aborted = threading.Event()
        self.shutdown_calls = []
        self.closed = False
        self.timeout = None

    def shutdown(self, how):
        self.shutdown_calls.append(how)
        self.aborted.set()

    def close(self):
        self.closed = True

    def settimeout(self, value):
        self.timeout = value


class FakeHTTPResponse:
    def __init__(self, connection):
        self.connection = connection
        self.status = connection.status
        self.closed = False

    def read(self):
        conn = self.connection
        if conn.block_at == 'body':
            conn.blocked.set()
            if not conn.sock.aborted.wait(2):
                raise AssertionError('Stop did not interrupt response read')
            raise OSError('read interrupted by socket shutdown')
        return conn.body

    def getheaders(self):
        return [('Content-Type', 'application/json')]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, host, port=None, timeout=None, *, block_at='', body=None, status=200):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = FakeSocket()
        self.block_at = block_at
        self.body = body if body is not None else b'{"message":{"content":"ok"}}'
        self.status = status
        self.blocked = threading.Event()
        self.dispatched = []
        self.response = None
        self.closed = False
        self.registered_before_dispatch = False

    def connect(self):
        self.registered_before_dispatch = self._agent_handle.connection is self
        self._agent_handle.attach_socket(self.sock)

    def request(self, method, path, body=None, headers=None):
        self.dispatched.append((method, path, body, headers))
        if self.block_at == 'request':
            self.blocked.set()
            if not self.sock.aborted.wait(2):
                raise AssertionError('Stop did not interrupt request')
            raise OSError('request interrupted')

    def getresponse(self):
        if self.block_at == 'headers':
            self.blocked.set()
            if not self.sock.aborted.wait(2):
                raise AssertionError('Stop did not interrupt headers')
            raise OSError('headers interrupted')
        self.response = FakeHTTPResponse(self)
        return self.response

    def close(self):
        self.closed = True
        self.sock.close()


class AgentCancelSafetyTests(unittest.TestCase):
    def setUp(self):
        self.saved_modules = dict(sys.modules)
        self.tools = install_stubs()
        self.requests = FakeRequests()
        self.requests.Timeout = type('Timeout', (RuntimeError,), {})
        self.client_options = []
        self.settings = types.ModuleType('ops.ai_settings')
        self.settings.SettingsError = type('SettingsError', (RuntimeError,), {})
        self.settings.provider_runtime = lambda provider: {'base_url': 'http://ollama.test', 'api_key': 'mock-only'}
        self.settings.load_settings = lambda: {'default_provider': 'ollama', 'default_model': 'qwen3.5:4b'}
        sys.modules['ops.ai_settings'] = self.settings
        sys.modules['requests'] = self.requests
        self.service = load_module('gitd.services.agent_chat', ROOT / 'gitd/services/agent_chat.py')
        self.addCleanup(self.restore_modules)
        deny_network = patch.object(self.service.socket, 'create_connection',
                                    side_effect=AssertionError('network forbidden in focused tests'))
        deny_network.start()
        self.addCleanup(deny_network.stop)

    def restore_modules(self):
        for name in list(sys.modules):
            if name not in self.saved_modules:
                sys.modules.pop(name, None)
        sys.modules.update(self.saved_modules)

    def session(self, run='run', provider='ollama'):
        session = self.service.ChatSession('session-' + run, 'android-offline', provider=provider)
        self.service._sessions[session.id] = session
        self.service.begin_agent_run(session, run_id=run)
        return session

    def start_request(self, session, run):
        result = {}
        def call():
            try:
                result['response'] = self.service._ollama_request(session, 'POST', '/api/chat',
                                                               run_id=run, json={'stream': False}, timeout=120)
            except BaseException as exc:
                result['error'] = exc
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        return thread, result

    def test_stop_shutdown_interrupts_blocked_dispatch_headers_and_body(self):
        for index, phase in enumerate(('request', 'headers', 'body')):
            with self.subTest(phase=phase):
                run = f'blocked-{index}'
                session = self.session(run)
                conn = FakeConnection('ollama.test', block_at=phase)
                with patch.object(self.service, '_OllamaHTTPConnection', return_value=conn):
                    thread, result = self.start_request(session, run)
                    self.assertTrue(conn.blocked.wait(1))
                    handles = list(self.service._active_http_responses[session.id])
                    self.assertEqual(len(handles), 1)
                    self.assertIs(handles[0].connection, conn)
                    self.assertTrue(conn.registered_before_dispatch)
                    start = time.monotonic()
                    self.service.stop_agent(session.id, run_id=run)
                    self.assertLess(time.monotonic() - start, .2)
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    self.assertTrue(handles[0].done.wait(1), 'native worker must terminate, not merely detach')
                self.assertIsInstance(result.get('error'), self.service._OllamaRequestCancelled)
                self.assertEqual(conn.sock.shutdown_calls[0], self.service.socket.SHUT_RDWR)
                self.assertTrue(conn.sock.closed)
                self.assertTrue(conn.closed)
                self.assertNotIn(session.id, self.service._active_http_responses)
                self.assertFalse(session.run_lock.locked())
                self.assertEqual(self.tools.TOOL_CALLS, [])

    def test_actual_stdlib_http_body_read_wakes_on_registered_socket_shutdown(self):
        session = self.session('stdlib-body')
        blocked = threading.Event()
        wire = FakeSocket()
        sent = []
        class Raw(io.RawIOBase):
            def __init__(self):
                self.header = b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n'
            def readable(self):
                return True
            def readinto(self, target):
                if self.header:
                    chunk, self.header = self.header[:len(target)], self.header[len(target):]
                    target[:len(chunk)] = chunk
                    return len(chunk)
                blocked.set()
                if not wire.aborted.wait(2):
                    raise AssertionError('real HTTPResponse.read remained blocked')
                raise OSError('fake wire recv aborted')
        wire.setsockopt = lambda *args: None
        wire.sendall = lambda data: sent.append(data)
        wire.makefile = lambda mode: io.BufferedReader(Raw())
        # Real production subclass and real stdlib parser. The sole patched
        # boundary returns a fake wire instead of ever opening a socket.
        with patch.object(self.service.socket, 'create_connection', return_value=wire):
            thread, result = self.start_request(session, 'stdlib-body')
            self.assertTrue(blocked.wait(1))
            handle = next(iter(self.service._active_http_responses[session.id]))
            self.assertIsInstance(handle.connection, self.service._OllamaHTTPConnection)
            self.assertIs(handle.sock, wire)
            self.service.stop_agent(session.id, run_id='stdlib-body')
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(handle.done.wait(1))
        self.assertIsInstance(result.get('error'), self.service._OllamaRequestCancelled)
        self.assertTrue(wire.shutdown_calls)
        self.assertTrue(wire.closed)
        self.assertTrue(any(b'POST /api/chat HTTP/1.1' in data for data in sent))
        self.assertNotIn(session.id, self.service._active_http_responses)

    def test_native_https_publishes_tls_socket_before_blocked_handshake(self):
        session = self.session('tls-handshake')
        raw, tls = FakeSocket(), FakeSocket()
        blocked = threading.Event()
        raw.setsockopt = lambda *args: None
        def handshake():
            blocked.set()
            if not tls.aborted.wait(2):
                raise AssertionError('TLS handshake not interrupted')
            raise OSError('TLS stopped')
        tls.do_handshake = handshake
        connection = self.service._OllamaHTTPSConnection('ollama.test', timeout=120)
        # Verify the native default before substituting the no-network TLS seam.
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, __import__('ssl').CERT_REQUIRED)
        # HTTPConnection captures its factory during construction; explicitly
        # replace that captured seam as well as the module-level default.
        connection._create_connection = lambda *args, **kwargs: raw
        wrapped = []
        def wrap(sock, **kwargs):
            wrapped.append((sock, kwargs))
            return tls
        connection._context = types.SimpleNamespace(wrap_socket=wrap)
        with patch.object(self.service, '_ollama_base_url', return_value='https://ollama.test'), \
             patch.object(self.service.socket, 'create_connection', return_value=raw), \
             patch.object(self.service, '_OllamaHTTPSConnection', return_value=connection):
            thread, result = self.start_request(session, 'tls-handshake')
            self.assertTrue(blocked.wait(1), repr(result))
            handle = next(iter(self.service._active_http_responses[session.id]))
            self.assertIs(handle.sock, tls)
            self.service.stop_agent(session.id, run_id='tls-handshake')
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(handle.done.wait(1))
        self.assertIsInstance(result.get('error'), self.service._OllamaRequestCancelled)
        self.assertEqual(wrapped, [(raw, {'server_hostname': 'ollama.test', 'do_handshake_on_connect': False})])
        self.assertTrue(tls.shutdown_calls)
        self.assertTrue(tls.closed)

    def test_transport_preserves_http_https_ipv6_endpoint_json_and_error_status(self):
        for base, expected_host, expected_port, tls in (
            ('http://ollama.test:11434/prefix', 'ollama.test', 11434, False),
            ('https://ollama.test:443/prefix', 'ollama.test', 443, True),
            ('http://[::1]:11434/prefix', '::1', 11434, False),
        ):
            with self.subTest(base=base):
                session = self.session(base)
                conn = FakeConnection(expected_host, status=404, body=b'{"error":"missing model"}')
                seam = '_OllamaHTTPSConnection' if tls else '_OllamaHTTPConnection'
                with patch.object(self.service, '_ollama_base_url', return_value=base), \
                     patch.object(self.service, seam, return_value=conn) as factory:
                    response = self.service._ollama_request(session, 'POST', '/api/chat?check=1',
                                                           json={'model': 'qwen3.5:4b', 'stream': False}, timeout=(3, 120))
                factory.assert_called_once_with(expected_host, port=expected_port, timeout=3.0)
                self.assertEqual(conn.dispatched[0][1], '/prefix/api/chat?check=1')
                self.assertEqual(json.loads(conn.dispatched[0][2]), {'model': 'qwen3.5:4b', 'stream': False})
                self.assertEqual(conn.sock.timeout, 120)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {'error': 'missing model'})
                self.assertIn('missing model', response.text)
                self.assertTrue(conn.response.closed)
                self.assertTrue(conn.closed)
                self.assertNotIn(session.id, self.service._active_http_responses)

    def test_native_timeout_and_connection_errors_preserve_request_exception_shape(self):
        for failure, expected in ((TimeoutError('fake read timeout'), self.requests.Timeout),
                                  (OSError('fake refused connection'), self.requests.ConnectionError)):
            with self.subTest(failure=type(failure).__name__):
                session = self.session('failure-' + type(failure).__name__)
                conn = FakeConnection('ollama.test')
                conn.getresponse = lambda: (_ for _ in ()).throw(failure)
                with patch.object(self.service, '_OllamaHTTPConnection', return_value=conn):
                    with self.assertRaises(expected):
                        self.service._ollama_request(session, 'POST', '/api/chat', json={}, timeout=120)
                self.assertTrue(conn.closed)
                self.assertNotIn(session.id, self.service._active_http_responses)

    def test_late_old_worker_cannot_cancel_or_cleanup_new_run(self):
        session = self.session('old')
        entered, release = threading.Event(), threading.Event()
        handles = []
        late = Response(data={'message': {'content': 'late', 'tool_calls': []}})
        def transport(handle, *args, **kwargs):
            handles.append(handle)
            entered.set()
            self.assertTrue(release.wait(2))
            return late  # simulate a resolver/adapter ignoring the socket abort
        with patch.object(self.service, '_ollama_http_request', side_effect=transport):
            thread, result = self.start_request(session, 'old')
            self.assertTrue(entered.wait(1))
            old_event = session.cancel_event
            self.service.stop_agent(session.id, run_id='old')
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.service.begin_agent_run(session, run_id='new')
            new_event = session.cancel_event
            new_handle = self.service._OllamaRequestHandle(session.id, 'new', new_event)
            self.service._track_http_response(session.id, new_handle, True)
            session.messages.append(self.service.ChatMessage('user', 'new turn'))
            self.service.end_agent_run(session, run_id='old')
            self.assertTrue(session.run_lock.locked())
            self.assertTrue(self.service._cancelled(session, 'old'))
            release.set()
            self.assertTrue(handles[0].done.wait(1))
            for _ in range(100):
                if late.closed:
                    break
                time.sleep(.005)
            self.assertTrue(late.closed)
            self.assertIs(handles[0].cancel_event, old_event)
            self.assertTrue(old_event.is_set())
            self.assertFalse(new_event.is_set())
            self.assertFalse(new_handle.cancelled.is_set())
            self.assertEqual(session.current_run_id, 'new')
            self.assertEqual([m.content for m in session.messages], ['new turn'])
            self.assertEqual(self.service._active_http_responses[session.id], {new_handle})
            self.assertEqual(set(session.run_events), {'new'})
            self.service._track_http_response(session.id, new_handle, False)
            self.service.end_agent_run(session, run_id='new')
        self.assertIsInstance(result['error'], self.service._OllamaRequestCancelled)

    def test_stopped_old_reserved_generator_cannot_enter_new_run(self):
        session = self.session('old')
        generator = self.service.chat_turn(session, 'old message', _reserved=True, _run_id='old', context_length=131072)
        self.service.stop_agent(session.id, run_id='old')
        self.service.begin_agent_run(session, run_id='new')
        previous_context = session.context_length
        events = list(generator)
        self.assertEqual([e['type'] for e in events], ['stopped', 'done'])
        self.assertEqual(session.context_length, previous_context)
        self.assertEqual(session.messages, [])
        self.assertTrue(session.run_lock.locked())
        self.assertFalse(session.cancel_event.is_set())

    def test_worker_cap_bounds_stalled_adapters_and_cancelled_waiters_never_dispatch(self):
        slots = threading.BoundedSemaphore(2)
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        handles = []
        def transport(handle, *args, **kwargs):
            with lock:
                handles.append(handle)
                if len(handles) == 2:
                    entered.set()
            self.assertTrue(release.wait(2))
            return Response()
        sessions = [self.session(f'bounded-{i}') for i in range(6)]
        with patch.object(self.service, '_ollama_worker_slots', slots), \
             patch.object(self.service, '_ollama_http_request', side_effect=transport):
            pairs = [self.start_request(s, s.current_run_id) for s in sessions]
            self.assertTrue(entered.wait(1))
            for session in sessions:
                self.service.stop_agent(session.id, run_id=session.current_run_id)
            for thread, result in pairs:
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(result.get('error'), self.service._OllamaRequestCancelled)
            self.assertEqual(len(handles), 2)
            release.set()
            for handle in handles:
                self.assertTrue(handle.done.wait(1))
            self.assertEqual(self.service._active_http_responses, {})
            self.assertEqual(len(handles), 2)
            self.assertTrue(slots.acquire(blocking=False))
            self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))
            slots.release(); slots.release()

    def provider_reply(self, provider):
        if provider == 'anthropic':
            return types.SimpleNamespace(content=[types.SimpleNamespace(type='tool_use', name='screenshot', input={}, id='tool-1')])
        tool = types.SimpleNamespace(id='tool-1', function=types.SimpleNamespace(name='screenshot', arguments='{}'))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='', tool_calls=[tool]))])

    def install_provider(self, provider, call):
        if provider == 'anthropic':
            module = types.ModuleType('anthropic')
            def client(**kwargs):
                self.client_options.append((provider, kwargs))
                return types.SimpleNamespace(messages=types.SimpleNamespace(create=call))
            module.Anthropic = client
            sys.modules['anthropic'] = module
        else:
            module = types.ModuleType('openai')
            def client(**kwargs):
                self.client_options.append((provider, kwargs))
                return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=call)))
            module.OpenAI = client
            sys.modules['openai'] = module

    def test_all_three_provider_late_cancelled_replies_do_not_dispatch_or_infer_again(self):
        for provider in ('anthropic', 'openrouter', 'vllm'):
            with self.subTest(provider=provider):
                session = self.session('provider-' + provider, provider)
                calls = []
                def call(**kwargs):
                    calls.append(kwargs)
                    self.service.stop_agent(session.id, run_id=session.current_run_id)
                    self.service.begin_agent_run(session, run_id='replacement-' + provider)
                    return self.provider_reply(provider)
                self.install_provider(provider, call)
                events = list(getattr(self.service, '_chat_' + provider)(session, 'inspect', run_id='provider-' + provider))
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.client_options[-1][1]['max_retries'], 0)
                self.assertEqual(self.tools.TOOL_CALLS, [])
                self.assertIn('stopped', [e['type'] for e in events])
                self.assertEqual(events[-1]['type'], 'done')
                self.assertFalse(any(e['type'] in {'tool_call', 'tool_result', 'text', 'screenshot'} for e in events))
                self.assertEqual([m.role for m in session.messages], ['user'])
                self.assertFalse(session.cancel_event.is_set())
                self.assertTrue(session.run_lock.locked())

    def test_stop_at_tool_call_yield_prevents_extra_tool_and_next_inference_all_providers(self):
        for provider in ('anthropic', 'openrouter', 'vllm', 'ollama'):
            with self.subTest(provider=provider):
                session = self.session('yield-' + provider, provider)
                calls = []
                def call(**kwargs):
                    calls.append(kwargs)
                    return self.provider_reply(provider)
                self.install_provider(provider, call)
                def ollama(handle, method, url, **kwargs):
                    calls.append(kwargs)
                    return Response(data={'message': {'content': '', 'tool_calls': [
                        {'id': '1', 'function': {'name': 'screenshot', 'arguments': {}}},
                        {'id': '2', 'function': {'name': 'screenshot', 'arguments': {}}}]}})
                with patch.object(self.service, '_ollama_capabilities', return_value={'tools', 'vision'}), \
                     patch.object(self.service, '_ollama_http_request', side_effect=ollama):
                    generator = getattr(self.service, '_chat_' + provider)(session, 'inspect', run_id=session.current_run_id)
                    events = []
                    for event in generator:
                        events.append(event)
                        if event['type'] == 'tool_call':
                            self.service.stop_agent(session.id, run_id=session.current_run_id)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.tools.TOOL_CALLS, [])
                self.assertEqual(sum(e['type'] == 'tool_call' for e in events), 1)
                self.assertIn('stopped', [e['type'] for e in events])
                self.assertFalse(any(e['type'] in {'tool_result', 'screenshot'} for e in events))

    def test_stop_during_first_tool_prevents_second_tool_screenshot_and_inference(self):
        session = self.session('tool-stop')
        replies = Response(data={'message': {'content': '', 'tool_calls': [
            {'id': '1', 'function': {'name': 'screenshot', 'arguments': {}}},
            {'id': '2', 'function': {'name': 'screenshot', 'arguments': {}}}]}})
        def tool(name, args):
            self.tools.TOOL_CALLS.append((name, args))
            self.service.stop_agent(session.id, run_id='tool-stop')
            return 'first tool finished'
        with patch.object(self.service, '_ollama_capabilities', return_value={'tools', 'vision'}), \
             patch.object(self.service, '_ollama_http_request', return_value=replies) as request, \
             patch.object(self.service, 'execute_tool', side_effect=tool), \
             patch.object(self.service, 'get_screenshot_b64', return_value='image') as screenshot:
            events = list(self.service._chat_ollama(session, 'inspect', run_id='tool-stop'))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(self.tools.TOOL_CALLS), 1)
        self.assertEqual(screenshot.call_count, 1, 'only initial vision, none after Stop')
        self.assertFalse(any(e['type'] == 'tool_result' for e in events))
        self.assertIn('stopped', [e['type'] for e in events])

    def test_provider_retry_after_stop_never_calls_adapter_again(self):
        for provider in ('anthropic', 'openrouter', 'vllm'):
            with self.subTest(provider=provider):
                session = self.session('retry-' + provider, provider)
                calls = []
                def call(**kwargs):
                    calls.append(kwargs)
                    raise RuntimeError('rate limit')
                self.install_provider(provider, call)
                # Deterministic backoff seam retries after cancellation. Its
                # guarded callable must refuse before contacting the adapter.
                def retry(fn, **kwargs):
                    try:
                        fn()
                    except RuntimeError:
                        self.service.stop_agent(session.id, run_id=session.current_run_id)
                    yield {'__result__': fn()}
                with patch.object(self.service, 'backoff_stream', side_effect=retry):
                    events = list(getattr(self.service, '_chat_' + provider)(session, 'inspect', run_id=session.current_run_id))
                self.assertEqual(len(calls), 1)
                self.assertEqual([e['type'] for e in events][-2:], ['stopped', 'done'])
                self.assertEqual(self.tools.TOOL_CALLS, [])

    def test_settings_failure_catalog_is_unavailable_and_makes_no_discovery_request(self):
        with patch.object(self.settings, 'provider_runtime', side_effect=self.settings.SettingsError('encrypted settings unavailable')), \
             patch.object(self.requests, 'get', create=True, side_effect=AssertionError('discovery after settings failure')):
            providers = self.service.get_providers()
        self.assertEqual({p['id'] for p in providers}, set(self.service.PROVIDERS))
        self.assertTrue(all(p.get('ready') is False and p['models'] == [] for p in providers))
        self.assertTrue(all('unavailable' in p.get('error', '') for p in providers))

    def test_old_run_message_and_api_commits_are_atomically_rejected(self):
        session = self.session('commit-old')
        self.service.stop_agent(session.id, run_id='commit-old')
        self.service.begin_agent_run(session, run_id='commit-new')
        self.assertFalse(self.service._append_run_message(session, 'commit-old', self.service.ChatMessage('assistant', 'late')))
        self.assertFalse(self.service._append_run_api_message(session, 'commit-old', {'role': 'assistant', 'content': 'late'}))
        self.assertEqual(session.messages, [])
        self.assertEqual(session.api_messages, [])
        self.assertTrue(self.service._append_run_message(session, 'commit-new', self.service.ChatMessage('user', 'new')))
        self.assertEqual(session.messages[-1].content, 'new')

    def test_cancelled_registry_is_bounded_and_stale_end_keeps_current_guard(self):
        session = self.session('current')
        for i in range(400):
            self.service.stop_agent(session.id, run_id=f'queued-{i}')
        self.assertLessEqual(len(session.cancelled_run_ids), 128)
        self.assertTrue(session.run_lock.locked())
        self.assertFalse(session.cancel_event.is_set())
        self.service.end_agent_run(session, run_id='old')
        self.assertTrue(session.run_lock.locked())


FRAMEWORK_AVAILABLE = bool(importlib.util.find_spec('fastapi')) and bool(importlib.util.find_spec('pydantic'))


@unittest.skipUnless(FRAMEWORK_AVAILABLE, 'parent dependency-backed stage required: FastAPI/Pydantic missing')
class AgentPullSafetyTests(AgentCancelSafetyTests):
    # Do not inherit/re-run the service tests in this route-only class.
    def setUp(self):
        super().setUp()
        routers = types.ModuleType('gitd.routers'); routers.__path__ = [str(ROOT / 'gitd/routers')]
        sys.modules['gitd.routers'] = routers
        self.router = load_module('gitd.routers.agent_chat', ROOT / 'gitd/routers/agent_chat.py')

    def test_same_model_shared_pull_different_model_runs_independently(self):
        count_lock = threading.Lock()
        calls, closed = [], []
        entered = {'same': threading.Event(), 'other': threading.Event()}
        release = threading.Event()
        class Pull(Response):
            def __init__(self, model):
                super().__init__(); self.model = model
            def iter_lines(self, **kwargs):
                entered[self.model].set()
                if not release.wait(2):
                    raise AssertionError('test release timeout')
                yield json.dumps({'status': 'pulling', 'completed': 5, 'total': 10})
                yield json.dumps({'status': 'success'})
            def close(self):
                with count_lock:
                    closed.append(self.model)
                super().close()
        def upstream(url, **kwargs):
            model = kwargs['json']['name']
            with count_lock:
                calls.append(model)
            return Pull(model)
        barrier = threading.Barrier(9)
        results, errors = [], []
        def caller():
            try:
                barrier.wait(1)
                result = self.router.ollama_pull(self.router.OllamaModelRequest(model='same'))
                with count_lock:
                    results.append(result)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.requests, 'post', side_effect=upstream):
            threads = [threading.Thread(target=caller, daemon=True) for _ in range(8)]
            for thread in threads: thread.start()
            barrier.wait(1)
            for thread in threads: thread.join(1)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 8)
            self.assertTrue(all(r == {'ok': True, 'status': 'pulling', 'model': 'same'} for r in results))
            self.assertTrue(entered['same'].wait(1))
            self.router.ollama_pull(self.router.OllamaModelRequest(model='other'))
            self.assertTrue(entered['other'].wait(1), 'different model must not wait on same model I/O')
            self.assertEqual(sorted(calls), ['other', 'same'])
            snapshot = self.router.ollama_pull_status('same')
            snapshot['status'] = 'corrupt caller'
            self.assertEqual(self.router.ollama_pull_status('same')['status'], 'pulling')
            release.set()
            for _ in range(200):
                with count_lock:
                    if len(closed) == 2: break
                time.sleep(.005)
        self.assertEqual(sorted(closed), ['other', 'same'])
        for model in ('same', 'other'):
            self.assertEqual(self.router.ollama_pull_status(model)['status'], 'done')
            self.assertEqual(self.router.ollama_pull_status(model)['percent'], 100)

    def test_pull_thread_start_failure_is_not_stuck_pulling(self):
        from fastapi import HTTPException
        with patch.object(self.router.threading.Thread, 'start', side_effect=RuntimeError('offline thread failure')):
            with self.assertRaises(HTTPException) as failed:
                self.router.ollama_pull(self.router.OllamaModelRequest(model='fail-start'))
        self.assertEqual(failed.exception.status_code, 503)
        self.assertEqual(self.router.ollama_pull_status('fail-start')['status'], 'error')
        self.assertEqual(self.requests.calls, [])

    def test_old_sse_finally_never_stops_or_saves_new_run(self):
        session = self.session('old-sse')
        self.service.end_agent_run(session, run_id='old-sse')
        async def consume():
            response = self.router.send_message({'session_id': session.id, 'content': 'old', 'run_id': 'route-old'})
            await anext(response.body_iterator)
            self.service.stop_agent(session.id, run_id='route-old')
            self.service.begin_agent_run(session, run_id='route-new')
            return [line async for line in response.body_iterator]
        with patch.object(self.service, 'save_session_to_db') as save:
            events = asyncio.run(consume())
        self.assertTrue(any('stopped' in e for e in events))
        save.assert_not_called()
        self.assertEqual(session.current_run_id, 'route-new')
        self.assertFalse(session.cancel_event.is_set())
        self.assertTrue(session.run_lock.locked())
        self.assertEqual(session.messages, [])


# unittest inherits test_* methods; remove duplication explicitly without
# changing any assertions or test behavior in the service class.
for _name in list(AgentCancelSafetyTests.__dict__):
    if _name.startswith('test_'):
        setattr(AgentPullSafetyTests, _name, None)

if __name__ == '__main__':
    unittest.main(verbosity=2)
