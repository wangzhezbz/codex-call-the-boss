"""Classifier lifecycle faults, with no account, tools, or real phone calls."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phone_intent import PhoneIntentRouter


class IntentServer:
    def __init__(self, mode='early'):
        self.mode = mode
        self.closed = asyncio.Event()
        self.handlers = []
        self.requests = []
        self.contexts = 0

    def add_notification_handler(self, handler):
        self.handlers.append(handler)

    def remove_notification_handler(self, handler):
        self.handlers.remove(handler)

    def emit(self, method, **params):
        for handler in tuple(self.handlers):
            handler({'method': method, 'params': {'threadId': self.thread, **params}})

    async def request(self, method, params, **kwargs):
        self.requests.append((method, params))
        if method == 'config/read':
            return {'config': {'mcp_servers': {'slow.example': {'enabled': True}}}}
        if method == 'skills/list':
            return {'data': [{'cwd': params['cwds'][0], 'errors': [], 'skills': [
                {'path': '/synthetic/skill/SKILL.md'}]}]}
        if method == 'thread/start':
            self.contexts += 1
            self.thread = 'intent-context-' + str(self.contexts)
            return {'thread': {'id': self.thread}}
        if method == 'turn/interrupt':
            return {}
        if method != 'turn/start':
            raise AssertionError('Unexpected method: ' + method)
        if self.mode == 'closed':
            asyncio.get_running_loop().call_soon(self.closed.set)
            return {'turn': {'id': 'current'}}
        if self.mode == 'hang':
            return {'turn': {'id': 'current'}}
        if self.mode == 'terminal_error':
            self.emit('error', turnId='current', willRetry=False,
                      error={'message': 'DO NOT PERSIST THIS RAW TEXT',
                             'codexErrorInfo': 'usageLimitExceeded'})
            return {'turn': {'id': 'current'}}
        observed_id = 'other-turn' if self.mode == 'wrong_turn' else 'current'
        text = '[]' if self.mode == 'invalid_json' else json.dumps(
            {'kind': 'greeting', 'clarification': ''})
        ids = {} if self.mode == 'unbound_events' else {'turnId': observed_id}
        self.emit('item/completed', **ids, item={
            'type': 'agentMessage', 'phase': 'final', 'text': text})
        turn = {'status': 'completed'}
        if self.mode != 'unbound_events':
            turn['id'] = observed_id
        self.emit('turn/completed', turn=turn)
        return {'turn': {} if self.mode == 'missing_id' else {'id': 'current'}}


async def missing_id_probe():
    server = IntentServer('missing_id')
    try:
        await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.1)
    except RuntimeError as exc:
        print(getattr(exc, 'code', str(exc)), flush=True)


class IntentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_skill_exclusion_is_read_only_exact_scope_and_once_per_context(self):
        server, trace = IntentServer(), {}
        router = PhoneIntentRouter(server, '/source/project')
        for text in ('你好', '您好'):
            await router.classify(text, [], trace=trace)
        inventory = [p for m, p in server.requests if m == 'skills/list']
        self.assertEqual(inventory, [{'cwds': ['/source/project'], 'forceReload': False}])
        context = next(p for m, p in server.requests if m == 'thread/start')
        self.assertEqual(context['config']['skills.config'], [
            {'path': '/synthetic/skill/SKILL.md', 'enabled': False}])
        self.assertEqual(trace['excluded_skills'], 1)
        self.assertEqual(context['sandbox'], 'read-only')
        self.assertNotIn('model', context)
        self.assertFalse(any(m.startswith('config/') and m != 'config/read'
                             for m, _ in server.requests))

    async def test_bad_skill_inventory_never_starts_context_or_classifies(self):
        for value in ({}, {'data': []}, {'data': [None]},
                      {'data': [{'cwd': '/other', 'skills': []}]},
                      {'data': [{'cwd': '/source', 'skills': [], 'errors': ['unavailable']}]},
                      *({'data': [{'cwd': '/source', 'skills': skills}]} for skills in (
                          [None], [{}], [{'path': 'relative'}], [{'path': '/bad\x00path'}],
                          [{'path': '/x'}, {'path': '/x'}], [{'path': '/x'}] * 1025))):
            class Server(IntentServer):
                async def request(self, method, params, **kwargs):
                    if method == 'skills/list':
                        self.requests.append((method, params))
                        return value
                    return await super().request(method, params, **kwargs)
            server = Server()
            with self.assertRaisesRegex(RuntimeError, 'classifier_skills_unavailable'):
                await PhoneIntentRouter(server, '/source').classify('你好', [])
            self.assertEqual([m for m, _ in server.requests], ['config/read', 'skills/list'])

    async def test_empty_skill_inventory_is_valid(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method == 'skills/list':
                    self.requests.append((method, params))
                    return {'data': [{'cwd': '/source', 'skills': [], 'errors': []}]}
                return await super().request(method, params, **kwargs)
        server, trace = Server(), {}
        await PhoneIntentRouter(server, '/source').classify('你好', [], trace=trace)
        self.assertEqual(trace['excluded_skills'], 0)

    async def test_skill_inventory_wait_obeys_original_deadline_without_retry(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method == 'skills/list':
                    self.requests.append((method, params))
                    await asyncio.Event().wait()
                return await super().request(method, params, **kwargs)
        server, trace = Server(), {}
        with self.assertRaises(TimeoutError):
            await PhoneIntentRouter(server, '/source').classify('你好', [], timeout=.03, trace=trace)
        self.assertEqual(trace['failure_phase'], 'skills_inventory')
        self.assertEqual([m for m, _ in server.requests], ['config/read', 'skills/list'])

    async def test_login_check_uses_remaining_budget_not_three_second_cutoff(self):
        from unittest.mock import AsyncMock
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method == 'account/read':
                    self.requests.append((method, params))
                    self.account_timeout = kwargs['timeout']
                    return await asyncio.wait_for(asyncio.sleep(3.05,
                        result={'account': {'type': 'chatgpt'}}), self.account_timeout)
                return await super().request(method, params, **kwargs)
        server, trace = Server(), {}
        server.start, server.close = AsyncMock(), AsyncMock()
        with patch('phone_intent.CodexAppServer', return_value=server):
            router = PhoneIntentRouter(None, '.')
        try:
            result = await router.classify('你好', [], timeout=4.5, trace=trace)
            self.assertEqual(result['kind'], 'greeting')
            self.assertGreater(server.account_timeout, 3)
            self.assertLessEqual(server.account_timeout, 4.5)
            self.assertEqual(sum(m == 'account/read' for m, _ in server.requests), 1)
            self.assertEqual(trace['outcome'], 'ready')
        finally:
            await router.close()

    async def test_login_wait_cannot_extend_original_deadline_or_start_model(self):
        from unittest.mock import AsyncMock
        server, trace = IntentServer(), {}
        cancelled = asyncio.Event()
        async def request(method, params, **kwargs):
            server.requests.append((method, params))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        server.start, server.close = AsyncMock(), AsyncMock()
        server.request = request
        with patch('phone_intent.CodexAppServer', return_value=server):
            router = PhoneIntentRouter(None, '.')
        try:
            with self.assertRaises(TimeoutError):
                await router.classify('你好', [], timeout=.04, trace=trace)
            self.assertTrue(cancelled.is_set())
            self.assertEqual([m for m, _ in server.requests], ['account/read'])
            self.assertFalse(router._account_checked)
            self.assertEqual(trace['failure_phase'], 'account_check')
            self.assertEqual(trace['failure_reason'], 'account_read_timeout')
        finally:
            await router.close()

    async def test_routing_timeout_is_not_login_failure_or_automatic_retry(self):
        from unittest.mock import AsyncMock
        from codex_rpc import CodexRpcError
        server, trace = IntentServer(), {}
        server.start, server.close = AsyncMock(), AsyncMock()
        server.request = AsyncMock(side_effect=CodexRpcError(json.dumps({
            'code': -32603, 'message': 'workspace routing discovery timed out'})))
        with patch('phone_intent.CodexAppServer', return_value=server):
            router = PhoneIntentRouter(None, '.')
        try:
            with self.assertRaisesRegex(RuntimeError, 'workspace_routing_timeout'):
                await router.classify('你好', [], timeout=.1, trace=trace)
            self.assertEqual(server.request.await_count, 1)
            self.assertFalse(router._account_checked)
            self.assertEqual(trace['failure_reason'], 'workspace_routing_timeout')
        finally:
            await router.close()

    async def test_classifier_does_not_load_other_project_memories_or_override_model(self):
        server = IntentServer()
        router = PhoneIntentRouter(server, '.')
        await router.classify('你好', [])
        context = next(params for method, params in server.requests if method == 'thread/start')
        self.assertFalse(context['config']['memories.use_memories'])
        self.assertFalse(context['config']['memories.generate_memories'])
        self.assertNotIn('model', context)
        self.assertNotIn('model', context['config'])
        self.assertEqual(context['sandbox'], 'read-only')
        await router.close()

    async def test_timeout_trace_separates_unprocessed_input_from_slow_model_output(self):
        for processing in (False, True):
            class Server(IntentServer):
                async def request(self, method, params, **kwargs):
                    result = await super().request(method, params, **kwargs)
                    if method == 'turn/start' and processing:
                        self.emit('item/started', turnId='current', item={'type': 'userMessage'})
                    return result
            trace = {}
            with self.assertRaises(TimeoutError):
                await PhoneIntentRouter(Server('hang'), '.').classify('你好', [], timeout=.02, trace=trace)
            self.assertEqual(trace['timeout_stage'], 'waiting_for_model_output' if processing else 'waiting_for_input_processing')

    async def test_owned_classifier_starts_isolated_and_checks_existing_login_once(self):
        class Server(IntentServer):
            running = False
            starts = closes = 0
            async def start(self):
                self.starts += 1
                self.running = True
            async def close(self):
                self.closes += 1
                self.closed.set()
                self.running = False
            async def request(self, method, params, **kwargs):
                if method == 'account/read':
                    self.requests.append((method, params))
                    return {'account': {'type': 'chatgpt'}}
                return await super().request(method, params, **kwargs)
        server, trace = Server(), {}
        with patch('phone_intent.CodexAppServer', return_value=server) as factory:
            router = PhoneIntentRouter(None, '/source/project')
        factory.assert_called_once_with(capability_profile='classifier')
        self.assertEqual(router.readiness_error(), 'classifier_not_ready')
        for words in ('你好', '您好'):
            self.assertEqual((await router.classify(words, [], trace=trace))['kind'], 'greeting')
        self.assertEqual(server.starts, 1)
        self.assertEqual(server.contexts, 1)
        self.assertEqual(sum(method == 'account/read' for method, _ in server.requests), 1)
        self.assertEqual(trace['process_profile'], 'isolated_classifier')
        self.assertIsNone(router.readiness_error())
        server.closed.set()
        self.assertEqual(router.readiness_error(), 'server_closed')
        await router.close()
        await router.close()
        self.assertEqual(server.closes, 1)
        with self.assertRaisesRegex(RuntimeError, 'server_closed'):
            await router.classify('你好', [])

    async def test_owned_classifier_rejects_non_chatgpt_before_any_model_request(self):
        from unittest.mock import AsyncMock
        for account in ({'account': {'type': 'apiKey'}}, {}, {'account': None}):
            server = IntentServer()
            server.start, server.close = AsyncMock(), AsyncMock()
            server.request = AsyncMock(return_value=account)
            with patch('phone_intent.CodexAppServer', return_value=server):
                router = PhoneIntentRouter(None, '.')
            with self.assertRaisesRegex(RuntimeError, 'chatgpt_login_required'):
                await router.classify('你好', [], timeout=.1)
            self.assertEqual(server.request.await_args.args[0], 'account/read')
            self.assertEqual(server.request.await_count, 1)
            await router.close()
            server.close.assert_awaited_once()

    async def test_owned_process_startup_is_inside_original_classification_deadline(self):
        from unittest.mock import AsyncMock
        server, trace = IntentServer(), {}
        began, cancelled = asyncio.Event(), asyncio.Event()
        async def slow_start():
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        server.start, server.close = slow_start, AsyncMock()
        with patch('phone_intent.CodexAppServer', return_value=server):
            router = PhoneIntentRouter(None, '.')
        with self.assertRaises(TimeoutError):
            await router.classify('你好', [], timeout=.03, trace=trace)
        self.assertTrue(began.is_set() and cancelled.is_set())
        self.assertEqual(server.requests, [])
        self.assertEqual(trace['failure_phase'], 'process_start')
        await router.close()
        server.close.assert_awaited_once()

    async def test_closing_a_borrowed_router_does_not_close_answer_process(self):
        from unittest.mock import AsyncMock
        server = IntentServer()
        server.close = AsyncMock()
        router = PhoneIntentRouter(server, '.')
        await router.classify('你好', [])
        await router.close()
        server.close.assert_not_awaited()
        self.assertFalse(server.closed.is_set())

    async def test_arriving_owned_decision_gets_one_bounded_completion_grace(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method != 'turn/start':
                    return await super().request(method, params, **kwargs)
                self.requests.append((method, params))
                loop = asyncio.get_running_loop()
                loop.call_later(.06, lambda: self.emit('item/agentMessage/delta', turnId='current', delta='{'))
                loop.call_later(.08, lambda: self.emit('item/agentMessage/delta', turnId='current', delta='...'))
                loop.call_later(.09, lambda: self.emit('item/completed', turnId='current',
                    item={'type':'agentMessage','phase':'final','text':'{"kind":"question","clarification":""}'}))
                loop.call_later(.095, lambda: self.emit('turn/completed', turn={'id':'current','status':'completed'}))
                return {'turn':{'id':'current'}}
        trace = {}
        server = Server()
        with patch('phone_intent.INTENT_COMPLETION_GRACE_SECONDS', .04):
            decision = await PhoneIntentRouter(server, '.').classify('讲故事', [], timeout=.08, trace=trace)
        self.assertEqual(decision['kind'], 'question')
        self.assertEqual(trace['completion_grace_ms'], 40)
        self.assertEqual(sum(method=='turn/start' for method, _ in server.requests), 1)
        self.assertEqual(server.handlers, [])

    async def test_partial_or_unrelated_output_cannot_extend_forever_or_authorize(self):
        for owned in (False, True):
            class Server(IntentServer):
                async def request(self, method, params, **kwargs):
                    if method != 'turn/start':
                        return await super().request(method, params, **kwargs)
                    self.requests.append((method, params))
                    loop = asyncio.get_running_loop()
                    for delay in (.045, .055, .065, .075, .085):
                        loop.call_later(delay, lambda: self.emit('item/agentMessage/delta',
                            turnId='current' if owned else 'other', delta='{"kind":"action"'))
                    return {'turn':{'id':'current'}}
            server, trace = Server(), {}
            began = time.monotonic()
            with patch('phone_intent.INTENT_COMPLETION_GRACE_SECONDS', .03):
                with self.assertRaises(TimeoutError):
                    await PhoneIntentRouter(server, '.').classify('原话', [], timeout=.06, trace=trace)
            self.assertLess(time.monotonic()-began, .16)
            self.assertEqual('completion_grace_ms' in trace, owned)
            self.assertEqual(sum(method=='turn/start' for method, _ in server.requests), 1)
            self.assertEqual(server.handlers, [])

    async def test_minimal_capabilities_are_ephemeral_and_do_not_override_model(self):
        server = IntentServer()
        await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.3)
        params = next(params for method, params in server.requests if method == 'thread/start')
        self.assertTrue(params['ephemeral'])
        self.assertEqual(params['environments'], [])
        self.assertEqual(params['selectedCapabilityRoots'], [])
        self.assertEqual(params['config']['skills.max_context_tokens'], 1)
        self.assertFalse(params['config']['features.apps'])
        self.assertEqual(params['sandbox'], 'read-only')
        self.assertEqual(params['approvalPolicy'], 'never')
        self.assertNotIn('model', params)
        self.assertFalse(any(key.startswith('model') for key in params['config']))
        self.assertEqual([method for method, _ in server.requests], ['config/read', 'skills/list', 'thread/start', 'turn/start'])
        self.assertIs(params['config']['mcp_servers']['slow.example']['enabled'], False)

    async def test_usage_metrics_require_exact_turn_and_never_retain_text(self):
        class Server(IntentServer):
            def emit(self, method, **params):
                if method == 'item/completed':
                    super().emit('thread/tokenUsage/updated', turnId='other', tokenUsage={
                        'last':{'inputTokens':999999, 'raw_text':'private'}})
                    super().emit('thread/tokenUsage/updated', turnId='current', tokenUsage={
                        'last':{'inputTokens':1500, 'cachedInputTokens':1200, 'outputTokens':18,
                                'reasoningOutputTokens':0, 'raw_text':'private'}})
                super().emit(method, **params)
        trace = {}
        await PhoneIntentRouter(Server(), '.').classify('模拟问候', [], timeout=.3, trace=trace)
        self.assertEqual(trace['token_usage'], {'inputTokens':1500, 'cachedInputTokens':1200,
                                               'outputTokens':18, 'reasoningOutputTokens':0})
        self.assertNotIn('private', str(trace))

    async def test_invalid_usage_is_ignored_without_affecting_decision(self):
        class Server(IntentServer):
            def emit(self, method, **params):
                if method == 'item/completed':
                    super().emit('thread/tokenUsage/updated', turnId='current', tokenUsage={
                        'last':{'inputTokens':True, 'cachedInputTokens':-1, 'outputTokens':'private',
                                'reasoningOutputTokens':100_000_001}})
                super().emit(method, **params)
        trace = {}
        decision = await PhoneIntentRouter(Server(), '.').classify('模拟问候', [], timeout=.3, trace=trace)
        self.assertEqual(decision['kind'], 'greeting')
        self.assertEqual(trace['token_usage'], {})
        self.assertNotIn('private', str(trace))

    async def test_rules_are_bound_once_but_original_words_are_not_truncated(self):
        server = IntentServer()
        router = PhoneIntentRouter(server, '.')
        words = '不要删除文件，请先解释。'
        await router.classify(words, [{'role':'user','text':'旧上下文'}], timeout=.3)
        await router.classify('下一句话', [], timeout=.3)
        starts = [params for method, params in server.requests if method == 'thread/start']
        self.assertEqual(len(starts), 1)
        self.assertIn('否定的命令不执行', starts[0]['developerInstructions'])
        turns = [params for method, params in server.requests if method == 'turn/start']
        self.assertEqual(json.loads(turns[0]['input'][0]['text'])['current_caller_words'], words)
        self.assertNotIn('model', turns[0])
    async def test_valid_early_events_are_replayed_once_and_handler_removed(self):
        server = IntentServer()
        router = PhoneIntentRouter(server, '.')
        result = await router.classify('模拟问候', [], timeout=.3)
        self.assertEqual(result['kind'], 'greeting')
        self.assertEqual(server.handlers, [])
        self.assertEqual([m for m, _ in server.requests], ['config/read', 'skills/list', 'thread/start', 'turn/start'])

    async def test_missing_config_does_not_start_a_tool_enabled_classifier(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method == 'config/read':
                    self.requests.append((method, params))
                    return {}
                return await super().request(method, params, **kwargs)
        server = Server()
        with self.assertRaisesRegex(RuntimeError, 'classifier_config_unavailable'):
            await PhoneIntentRouter(server, '.').classify('只做分类', [], timeout=.3)
        self.assertEqual([method for method, _ in server.requests], ['config/read'])

    async def test_mcp_exclusion_does_not_mutate_effective_config_or_source_settings(self):
        configured = {'mcp_servers': {'tool': {'enabled': True, 'command': 'DO NOT RUN'}}}
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                if method == 'config/read':
                    self.requests.append((method, params))
                    return {'config': configured}
                return await super().request(method, params, **kwargs)
        server = Server()
        trace = {}
        await PhoneIntentRouter(server, '.').classify('只做分类', [], timeout=.3, trace=trace)
        self.assertTrue(configured['mcp_servers']['tool']['enabled'])
        self.assertEqual(trace['excluded_mcp_servers'], 1)
        self.assertFalse(any('write' in method for method, _ in server.requests))

    async def test_missing_turn_id_exits_under_external_deadline(self):
        # Old code blocks asyncio itself. Bound it with a separate process so
        # this regression can never hang the test runner or phone daemon.
        child = await asyncio.to_thread(subprocess.run,
            [sys.executable, str(Path(__file__).resolve()), '--probe-missing-id'],
            capture_output=True, text=True, timeout=1.5)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('missing_turn_id', child.stdout)

    async def test_closed_server_fails_without_waiting_for_classification_deadline(self):
        server = IntentServer('closed')
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.4)
        self.assertLess(time.monotonic() - started, .2)
        self.assertEqual(server.handlers, [])
        self.assertNotIn('turn/interrupt', [m for m, _ in server.requests])

    async def test_already_closed_server_does_not_send_a_new_request(self):
        server = IntentServer()
        server.closed.set()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.2)
        self.assertEqual(server.requests, [])

    async def test_unrelated_and_unbound_completions_cannot_authorize_a_decision(self):
        for mode in ('wrong_turn', 'unbound_events'):
            with self.subTest(mode=mode):
                server = IntentServer(mode)
                with self.assertRaises(TimeoutError):
                    await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.025)
                self.assertEqual(server.handlers, [])

    async def test_deadline_includes_waiting_for_another_classification(self):
        server = IntentServer()
        router = PhoneIntentRouter(server, '.')
        await router.lock.acquire()
        try:
            task = asyncio.create_task(router.classify('模拟问候', [], timeout=.025))
            done, _ = await asyncio.wait([task], timeout=.2)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.fail('The classifier deadline did not include its lock wait')
            with self.assertRaises(TimeoutError):
                await task
            self.assertEqual(server.requests, [])
        finally:
            router.lock.release()

    async def test_timeout_discards_uncertain_context_before_next_distinct_input(self):
        server = IntentServer('hang')
        router = PhoneIntentRouter(server, '.')
        with self.assertRaises(TimeoutError):
            await router.classify('模拟问候', [], timeout=.025)
        self.assertEqual(server.handlers, [])
        self.assertEqual([m for m, _ in server.requests].count('turn/interrupt'), 1)
        server.mode = 'early'
        self.assertEqual((await router.classify('下一句模拟问候', [], timeout=.3))['kind'], 'greeting')
        self.assertEqual(server.contexts, 2)

    async def test_terminal_service_error_is_prompt_and_diagnostics_are_content_free(self):
        server = IntentServer('terminal_error')
        trace = {}
        with self.assertRaisesRegex(RuntimeError, 'service_error'):
            await PhoneIntentRouter(server, '.').classify('PRIVATE CALLER WORDS', [], timeout=.3, trace=trace)
        self.assertEqual(trace['failure_reason'], 'service_error')
        self.assertEqual(trace['service_error_code'], 'usageLimitExceeded')
        self.assertNotIn('PRIVATE', json.dumps(trace))
        self.assertNotIn('RAW TEXT', json.dumps(trace))
        self.assertEqual(server.handlers, [])

    async def test_local_address_failure_stops_owned_retry_before_deadline(self):
        for early in (True, False):
            class Server(IntentServer):
                async def request(self, method, params, **kwargs):
                    result = await super().request(method, params, **kwargs)
                    if method == 'turn/start':
                        def fail():
                            self.emit('error', turnId='current', willRetry=True,
                                error={'codexErrorInfo': 'responseStreamDisconnected',
                                       'message': "stream disconnected: Can't assign requested address (os error 49); PRIVATE ENDPOINT"})
                        if early:
                            fail()
                        else:
                            asyncio.get_running_loop().call_soon(fail)
                    return result
            server, trace = Server('hang'), {}
            router = PhoneIntentRouter(server, '.')
            with self.subTest(early=early):
                with self.assertRaisesRegex(RuntimeError, 'local_address_unavailable'):
                    await router.classify('PRIVATE CALLER WORDS', [], timeout=.3, trace=trace)
                self.assertEqual(trace['outcome'], 'error')
                self.assertEqual(trace['failure_reason'], 'local_address_unavailable')
                self.assertEqual(trace['local_transport_errno'], 49)
                self.assertLess(trace['elapsed_ms'], 200)
                self.assertEqual([p for m,p in server.requests if m=='turn/interrupt'],
                                 [{'threadId':'intent-context-1', 'turnId':'current'}])
                self.assertEqual([m for m,_ in server.requests].count('turn/start'), 1)
                self.assertEqual(router.thread_id, '')
                self.assertTrue(trace['interrupt_confirmed'])
                self.assertEqual(server.handlers, [])
                self.assertNotIn('PRIVATE', json.dumps(trace))

    async def test_retryable_nonlocal_disconnect_can_still_complete(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                result = await super().request(method, params, **kwargs)
                if method == 'turn/start':
                    self.emit('error', turnId='current', willRetry=True,
                        error={'codexErrorInfo': 'responseStreamDisconnected',
                               'message': 'temporary connection reset'})
                    self.emit('item/completed', turnId='current', item={
                        'type':'agentMessage', 'phase':'final',
                        'text':json.dumps({'kind':'greeting', 'clarification':''})})
                    self.emit('turn/completed', turn={'id':'current', 'status':'completed'})
                return result
        server, trace = Server('hang'), {}
        result = await PhoneIntentRouter(server, '.').classify('你好', [], timeout=.3, trace=trace)
        self.assertEqual(result['kind'], 'greeting')
        self.assertEqual(trace['outcome'], 'ready')
        self.assertNotIn('local_transport_errno', trace)
        self.assertNotIn('turn/interrupt', [m for m,_ in server.requests])

    async def test_unrelated_or_unbound_local_error_cannot_cancel_current_input(self):
        for observed_id in ('different-turn', None):
            class Server(IntentServer):
                async def request(self, method, params, **kwargs):
                    result = await super().request(method, params, **kwargs)
                    if method == 'turn/start':
                        self.emit('error', turnId=observed_id, willRetry=True,
                            error={'codexErrorInfo':'responseStreamDisconnected',
                                   'message':"Can't assign requested address (os error 49)"})
                    return result
            server, trace = Server(), {}
            result = await PhoneIntentRouter(server, '.').classify('你好', [], timeout=.3, trace=trace)
            self.assertEqual(result['kind'], 'greeting')
            self.assertNotIn('service_error_code', trace)
            self.assertNotIn('turn/interrupt', [m for m,_ in server.requests])

    async def test_local_transport_failure_wins_late_complete_action(self):
        class Server(IntentServer):
            async def request(self, method, params, **kwargs):
                result = await super().request(method, params, **kwargs)
                if method == 'turn/start':
                    self.emit('error', turnId='current', willRetry=True,
                        error={'codexErrorInfo': {'responseStreamDisconnected': {}},
                               'message':"Can't assign requested address (os error 49)"})
                    self.emit('item/completed', turnId='current', item={
                        'type':'agentMessage', 'phase':'final',
                        'text':json.dumps({'kind':'action', 'clarification':''})})
                    self.emit('turn/completed', turn={'id':'current', 'status':'completed'})
                return result
        server = Server('hang')
        with self.assertRaisesRegex(RuntimeError, 'local_address_unavailable'):
            await PhoneIntentRouter(server, '.').classify('写一个标记', [], timeout=.3)
        self.assertEqual([m for m,_ in server.requests].count('turn/start'), 1)

    async def test_trace_distinguishes_accepted_request_from_missing_completion(self):
        server = IntentServer('hang')
        trace = {}
        with self.assertRaises(TimeoutError):
            await PhoneIntentRouter(server, '.').classify('PRIVATE CALLER WORDS', [], timeout=.025, trace=trace)
        self.assertEqual(trace['failure_phase'], 'awaiting_completion')
        self.assertEqual(trace['outcome'], 'timeout')
        self.assertTrue(trace['turn_id_received'])
        self.assertIn('turn_start_accepted_ms', trace)
        self.assertNotIn('PRIVATE', json.dumps(trace))

    async def test_invalid_structured_value_fails_closed(self):
        server = IntentServer('invalid_json')
        with self.assertRaisesRegex(RuntimeError, 'invalid_decision'):
            await PhoneIntentRouter(server, '.').classify('模拟问候', [], timeout=.2)
        self.assertEqual(server.handlers, [])


if __name__ == '__main__':
    if '--probe-missing-id' in sys.argv:
        asyncio.run(missing_id_probe())
    else:
        unittest.main()
