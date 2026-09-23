import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from codex_rpc import CodexRpcError
from phone_source_view import prepare_source_view


class SourceViewTests(unittest.IsolatedAsyncioTestCase):
    def setup_server(self, *, source_changes=None, created_changes=None, fork_error=None):
        source = {'id': 'source-0001', 'model': 'chosen-model', 'modelProvider': 'chosen-provider',
                  'cwd': '/source/project', 'ephemeral': False, **(source_changes or {})}
        created = {'id': 'context-0001', 'model': 'chosen-model', 'modelProvider': 'chosen-provider',
                   'forkedFromId': 'source-0001', 'ephemeral': True, **(created_changes or {})}
        async def request(method, params, **kwargs):
            if method == 'thread/read':
                return {'thread': source}
            if method == 'thread/fork' and fork_error:
                raise fork_error
            if method in {'thread/fork', 'thread/start'}:
                return {'thread': created}
            self.fail('No resume, turn start, or command dispatch is permitted')
        self.server = Mock(request=AsyncMock(side_effect=request))
        self.context = Mock(return_value='user: 项目新要求\nassistant: 完成了检查')

    async def prepare(self, **kwargs):
        return await prepare_source_view(self.server, 'source-0001', self.context, **kwargs)

    async def test_public_fork_preserves_source_and_never_executes(self):
        self.setup_server()
        view = await self.prepare()
        methods = [call.args[0] for call in self.server.request.call_args_list]
        self.assertEqual(methods, ['thread/read', 'thread/fork'])
        args = self.server.request.call_args_list[1].args[1]
        self.assertEqual(args['model'], 'chosen-model')
        self.assertEqual(args['modelProvider'], 'chosen-provider')
        self.assertEqual(args['sandbox'], 'read-only')
        self.assertIs(args['ephemeral'], True)
        self.assertIs(args['allowProviderModelFallback'], False)
        self.assertIs(args['excludeTurns'], True)
        self.assertEqual(view.context_origin, 'source_rollout')
        job = {'thread_id': 'source-0001'}
        view.bind_job(job)
        self.assertEqual(job['thread_id'], 'source-0001')
        self.assertNotIn('command_transport', job)

    async def test_source_metadata_mismatch_stops_before_fork(self):
        for change in ({'id':'other-source'}, {'model':None}, {'modelProvider':''},
                       {'cwd':'relative'}, {'ephemeral':True}):
            with self.subTest(change=change):
                self.setup_server(source_changes=change)
                with self.assertRaises(RuntimeError):
                    await self.prepare()
                self.assertEqual(self.server.request.await_count, 1)

    async def test_created_model_and_identity_must_be_observed(self):
        for change in ({'id':'source-0001'}, {'model':None}, {'modelProvider':'other'},
                       {'ephemeral':False}, {'forkedFromId':'wrong-source'}):
            with self.subTest(change=change):
                self.setup_server(created_changes=change)
                with self.assertRaisesRegex(RuntimeError, '校验失败'):
                    await self.prepare()

    async def test_empty_context_never_becomes_blank_phone_chat(self):
        self.setup_server()
        self.context.return_value = ''
        with self.assertRaisesRegex(RuntimeError, '空白语音上下文'):
            await self.prepare()
        self.assertEqual(self.server.request.await_count, 1)

    async def test_legacy_fallback_requires_exact_source(self):
        self.setup_server()
        self.context.return_value = ''
        fallback = AsyncMock(return_value={'sourceThreadId':'other-source', 'text':'wrong'})
        with self.assertRaisesRegex(RuntimeError, '空白语音上下文'):
            await self.prepare(desktop_context=fallback)
        fallback.return_value = {'sourceThreadId':'source-0001', 'text':'verified legacy context'}
        self.assertEqual((await self.prepare(desktop_context=fallback)).context_origin, 'source_desktop')

    async def test_exact_projection_failure_only_uses_snapshot(self):
        error = CodexRpcError(json.dumps({'code':-32603, 'message':
            'failed to prepare paginated fork: thread-store internal error: '
            'thread history projection for source-0001 expected ordinal 12, got 11'}))
        self.setup_server(fork_error=error)
        view = await self.prepare()
        self.assertTrue(view.recovered_projection)
        args = self.server.request.call_args_list[-1].args[1]
        self.assertIn('引用资料', args['developerInstructions'])
        self.assertEqual(args['model'], 'chosen-model')
        self.assertNotIn('threadId', args)

    async def test_other_errors_no_fallback_or_retry(self):
        for error in (CodexRpcError('quota'), CodexRpcError('unauthorized'), TimeoutError()):
            self.setup_server(fork_error=error)
            with self.assertRaises((RuntimeError, TimeoutError)):
                await self.prepare()
            self.assertEqual([c.args[0] for c in self.server.request.call_args_list],
                             ['thread/read', 'thread/fork'])

    async def test_deadline_bounds_all_requests(self):
        self.setup_server()
        async def stall(*args, **kwargs):
            await asyncio.sleep(1)
        self.server.request.side_effect = stall
        with self.assertRaises(TimeoutError):
            await self.prepare(timeout_seconds=0.01)


if __name__ == '__main__':
    unittest.main()
