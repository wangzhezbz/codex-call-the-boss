from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import phone_agent
from codex_rpc import CodexRpcError
from phone_agent import PhoneDaemon


class FakeCodex:
    def __init__(self, returned_id: str | None = None) -> None:
        self.returned_id = returned_id
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> dict[str, Any]:
        del timeout
        self.requests.append((method, params))
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'model': 'source-test-model',
                               'modelProvider': 'openai', 'cwd': '/source-project'}}
        thread_id = self.returned_id or "voice-context"
        return {
            "thread": {
                "id": thread_id,
                "forkedFromId": str(params.get("threadId") or ""),
                "ephemeral": True,
                "model": params.get('model'),
                "modelProvider": params.get('modelProvider'),
            }
        }


class FailingCodex(FakeCodex):
    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> dict[str, Any]:
        del method, params, timeout
        raise CodexRpcError("missing")


class FakeRelay:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.sent: list[tuple[str, str, str]] = []
        self.reads: list[str] = []

    async def health(self) -> bool:
        return self.healthy

    async def read_context(self, thread_id: str) -> dict[str, Any]:
        return {"sourceThreadId": thread_id, "text": "原任务近期对话"}

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        self.reads.append(thread_id)
        return {
            "thread": {"id": thread_id, "updatedAt": 1},
            "turns": [{"id": "before-turn", "status": "completed"}],
        }

    async def send_message_to_thread(
        self, caller_thread_id: str, target_thread_id: str, prompt: str
    ) -> dict[str, Any]:
        self.sent.append((caller_thread_id, target_thread_id, prompt))
        return {"ok": True}


class ThreadBindingTests(unittest.IsolatedAsyncioTestCase):
    def projection_daemon(self, *, source_overrides=None, context='user: 原任务近况', created_overrides=None):
        daemon = PhoneDaemon({'relay_caller_thread_id': 'relay-thread'})
        daemon.ensure_codex = AsyncMock()
        daemon.app_tools = FakeRelay()
        daemon._recent_source_context = Mock(return_value=context)
        requests = []
        source = {'id':'source-thread','model':'test-source-model','modelProvider':'test-provider',
                  'cwd':'/verified-source-project', **(source_overrides or {})}
        async def request(method, params, **kwargs):
            requests.append((method, params))
            if method == 'thread/fork':
                raise CodexRpcError(json.dumps({'code':-32603,'message':
                    'failed to prepare paginated fork: thread-store internal error: '
                    'thread history projection for original-ancestor expected ordinal 12, got 11'}))
            if method == 'thread/read': return {'thread':source}
            if method == 'thread/start':
                return {'thread':{'id':'temporary-phone-view','ephemeral':True,
                    'model':params['model'],'modelProvider':params['modelProvider'],
                    **(created_overrides or {})}}
            raise AssertionError('No other operation authorized')
        daemon.codex = Mock(request=AsyncMock(side_effect=request))
        return daemon, requests

    async def test_projection_error_uses_verified_source_view_not_another_execution_task(self):
        daemon, requests = self.projection_daemon()
        job = {'thread_id':'source-thread', 'cwd':'/untrusted-job-directory'}
        self.assertEqual(await daemon.create_phone_context(job), 'temporary-phone-view')
        self.assertEqual([name for name, _ in requests], ['thread/read','thread/fork','thread/start'])
        self.assertEqual(requests[0][1], {'threadId':'source-thread','includeTurns':False})
        args = requests[2][1]
        self.assertEqual(args['cwd'], '/verified-source-project')
        self.assertEqual(args['model'], 'test-source-model')
        self.assertFalse(args['allowProviderModelFallback'])
        self.assertTrue(args['ephemeral'])
        self.assertEqual(args['approvalPolicy'], 'never')
        self.assertEqual(args['sandbox'], 'read-only')
        self.assertEqual(args['config'], {'model_reasoning_effort':'low'})
        self.assertIn('原任务近况', args['developerInstructions'])
        self.assertEqual(job['thread_id'], 'source-thread')
        self.assertEqual(job['source_thread_id'], 'source-thread')
        self.assertEqual(job['context_binding'], 'ephemeral_snapshot_of_source')
        self.assertFalse(job['context_recovery']['source_history_modified'])
        self.assertEqual(daemon.app_tools.sent, [])
        daemon._confirm_target_turn = AsyncMock(return_value={'status':'target_turn_started','turn_id':'correlated'})
        await daemon.relay_phone_task(job, '请继续处理原项目。')
        self.assertEqual(daemon.app_tools.sent[0][0:2], ('relay-thread','source-thread'))

    async def test_projection_recovery_refuses_missing_or_mismatched_source_metadata(self):
        for change in ({'id':'other-thread'}, {'model':None}, {'modelProvider':''}, {'cwd':'relative'}):
            with self.subTest(change=change):
                daemon, requests = self.projection_daemon(source_overrides=change)
                with self.assertRaisesRegex(RuntimeError, '无法核验'):
                    await daemon.create_phone_context({'thread_id':'source-thread'})
                self.assertNotIn('thread/start', [name for name, _ in requests])

    async def test_projection_recovery_refuses_empty_or_wrong_task_context(self):
        daemon, requests = self.projection_daemon(context='')
        daemon.app_tools.read_context = AsyncMock(return_value={'sourceThreadId':'other-thread','text':'wrong history'})
        with self.assertRaisesRegex(RuntimeError, '空白语音上下文'):
            await daemon.create_phone_context({'thread_id':'source-thread'})
        self.assertNotIn('thread/start', [name for name, _ in requests])

    async def test_projection_recovery_checks_created_identity_model_and_ephemeral_flag(self):
        for change in ({'id':'source-thread'}, {'model':'different-model'}, {'ephemeral':False}, {'modelProvider':'other'}):
            with self.subTest(change=change):
                daemon, _ = self.projection_daemon(created_overrides=change)
                with self.assertRaisesRegex(RuntimeError, '校验失败'):
                    await daemon.create_phone_context({'thread_id':'source-thread'})

    def test_projection_recovery_does_not_match_other_failures(self):
        for message in ('missing', 'rate_limit_exceeded', '{"code":401,"message":"unauthorized"}',
                        '{"code":-32603,"message":"other internal failure"}'):
            self.assertFalse(PhoneDaemon._history_projection_failed(CodexRpcError(message)))

    def test_desktop_phone_delegation_is_correlated_without_trusting_other_outputs(self):
        marker = '[codex-phone-command:' + 'a' * 64 + ']'
        body = '执行这条模拟指令\n\n这是本通电话新下达的执行指令。请实际处理。\n' + marker
        def item(namespace='codex_app', name='send_message_to_thread', sender='fixed-relay', text=body):
            return {'type': 'FunctionCallOutput', 'namespace': namespace, 'name': name,
                    'output': '<codex_delegation><source_thread_id>' + sender +
                    '</source_thread_id><input>' + escape(text) + '</input></codex_delegation>'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': 'source-thread'}}
            daemon = PhoneDaemon({'relay_caller_thread_id': 'fixed-relay'})
            daemon._find_rollout_path = Mock(return_value=path)
            for candidate, expected in [
                (item(), 'actual-turn'),
                (item(namespace='other'), ''), (item(name='read_thread'), ''),
                (item(sender='other-relay'), ''),
                (item(text='引用的旧指令\n<phone_history>\n' + marker + '\n</phone_history>'), ''),
                (item(text=body.replace(marker, '[codex-phone-command:' + 'b'*64 + ']') +
                      '\n\n以下是本通电话的先前对话，仅用于理解指代：\n<phone_history>\n' + marker + '\n</phone_history>'), ''),
            ]:
                with self.subTest(candidate=candidate):
                    row = {'type': 'event_msg', 'payload': {'type': 'item_completed',
                        'thread_id': 'source-thread', 'turn_id': 'actual-turn', 'item': candidate}}
                    path.write_text('\n'.join(map(json.dumps, [header, row])) + '\n')
                    self.assertEqual(daemon._command_turn_from_rollout('source-thread', marker), expected)

    def test_phone_context_includes_new_delegation_but_not_quoted_old_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'source.jsonl'
            text = ('做新的任务\n\n这是本通电话新下达的执行指令。请实际处理。\n'
                    '[codex-phone-command:' + 'a'*64 + ']\n\n'
                    '以下是本通电话的先前对话，仅用于理解指代：\n<phone_history>\nuser: 旧请求不要重做\n</phone_history>')
            rows = [{'type': 'session_meta', 'payload': {'id': 'source-thread'}},
                    {'type': 'event_msg', 'payload': {'type': 'item_completed',
                     'thread_id': 'source-thread', 'turn_id': 'command-turn',
                     'item': {'type': 'FunctionCallOutput', 'namespace': 'codex_app',
                     'name': 'send_message_to_thread', 'output':
                     '<codex_delegation><source_thread_id>fixed-relay</source_thread_id><input>' +
                     escape(text) + '</input></codex_delegation>'}}}]
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
            daemon = PhoneDaemon({'relay_caller_thread_id': 'fixed-relay'})
            daemon._find_rollout_path = Mock(return_value=path)
            self.assertEqual(daemon._recent_source_context('source-thread'), 'user: 做新的任务')

    async def test_unrelated_new_turn_is_not_execution_confirmation(self):
        daemon=PhoneDaemon({})
        daemon.app_tools=FakeRelay()
        daemon.app_tools.read_thread=AsyncMock(return_value={'thread':{'id':'source-thread'},
            'turns':[{'id':'new-unrelated','status':'inProgress','items':[{'type':'userMessage',
                'content':[{'type':'text','text':'别的任务'}]}]}]})
        daemon._command_rollout_evidence=Mock(return_value={})
        before={'thread_id':'source-thread','turn_id':'old','turn_status':'inProgress'}
        result=await daemon._confirm_target_turn('source-thread',before,marker='[codex-phone-command:123]')
        self.assertEqual(result['status'],'accepted_by_codex_app')
        self.assertEqual(result['turn_id'], '')

    async def test_only_exact_command_user_record_confirms_its_turn(self):
        daemon=PhoneDaemon({})
        marker='[codex-phone-command:123]'
        daemon.app_tools=FakeRelay()
        daemon.app_tools.read_thread=AsyncMock(return_value={'thread':{'id':'source-thread'},
            'turns':[{'id':'correct-turn','status':'inProgress','items':[{'type':'userMessage',
                'content':[{'type':'text','text':'执行命令\n'+marker}]}]}]})
        daemon._command_rollout_evidence=Mock(side_effect=AssertionError('No local fallback needed'))
        result=await daemon._confirm_target_turn('source-thread',
            {'thread_id':'source-thread','turn_id':'old','turn_status':'completed'},marker=marker)
        self.assertEqual(result['status'],'target_turn_started')
        self.assertEqual(result['turn_id'],'correct-turn')

    def local_command_fixture(self):
        # These fixtures exercise state ownership, not wall-clock delays.
        self.enterContext(patch('phone_agent.asyncio.sleep', new_callable=AsyncMock))
        marker = '[codex-phone-command:' + 'c' * 64 + ']'
        body = '继续优化\n\n这是本通电话新下达的执行指令。请实际处理。\n' + marker
        header = {'type': 'session_meta', 'payload': {'id': 'source-thread'}}
        started = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'actual-turn'}}
        command = {'type': 'event_msg', 'payload': {'type': 'item_completed',
            'thread_id': 'source-thread', 'turn_id': 'actual-turn',
            'item': {'type': 'FunctionCallOutput', 'namespace': 'codex_app',
                     'name': 'send_message_to_thread', 'output':
                     '<codex_delegation><source_thread_id>fixed-relay</source_thread_id><input>' +
                     escape(body) + '</input></codex_delegation>'}}}
        path = Path(tempfile.mkdtemp(prefix='phone-command-state-test-')) / 'source.jsonl'
        path.write_text('\n'.join(map(json.dumps, [header, started, command])) + '\n')
        daemon = PhoneDaemon({'relay_caller_thread_id': 'fixed-relay'})
        daemon._find_rollout_path = Mock(return_value=path)
        daemon.app_tools = FakeRelay()
        daemon.app_tools.read_thread = AsyncMock(return_value={
            'thread': {'id': 'source-thread'},
            'turns': [{'id': 'stale-turn', 'status': 'inProgress', 'items': []}]})
        before = {'thread_id': 'source-thread', 'turn_id': 'stale-turn', 'turn_status': 'inProgress'}
        return daemon, path, marker, before, [header, started, command]

    async def test_stale_desktop_projection_uses_exact_local_started_command(self):
        daemon, path, marker, before, _ = self.local_command_fixture()
        original = path.read_bytes()
        result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'target_turn_started')
        self.assertEqual(result['turn_id'], 'actual-turn')
        self.assertEqual(result['verification_reason'], 'exact_command_and_local_lifecycle')
        self.assertEqual(daemon.app_tools.sent, [])
        self.assertEqual(path.read_bytes(), original)

    async def test_delayed_command_append_uses_full_receipt_status_without_repolling_desktop(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        evidence = daemon._command_rollout_evidence('source-thread', marker)
        daemon._command_rollout_evidence = Mock(side_effect=[{}, {}, evidence])
        with patch('phone_agent.asyncio.sleep', new_callable=AsyncMock):
            result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'target_turn_started')
        self.assertEqual(result['turn_id'], 'actual-turn')
        self.assertEqual(daemon._command_rollout_evidence.call_count, 3)
        self.assertEqual(daemon.app_tools.read_thread.await_count, 1)
        self.assertEqual(daemon.app_tools.sent, [])

    async def test_missing_command_never_becomes_started_after_bounded_local_checks(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        daemon._command_rollout_evidence = Mock(return_value={})
        with patch('phone_agent.asyncio.sleep', new_callable=AsyncMock):
            result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        self.assertEqual(result['turn_id'], '')
        self.assertEqual(daemon._command_rollout_evidence.call_count, 24)
        self.assertEqual(daemon.app_tools.read_thread.await_count, 1)
        self.assertEqual(daemon.app_tools.sent, [])

    async def test_late_aborted_command_stops_local_confirmation_without_started_claim(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        daemon._command_rollout_evidence = Mock(side_effect=[{},
            {'thread_id': 'source-thread', 'turn_id': 'actual-turn',
             'turn_status': 'aborted', 'lifecycle_verified': False}])
        with patch('phone_agent.asyncio.sleep', new_callable=AsyncMock):
            result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        self.assertEqual(daemon._command_rollout_evidence.call_count, 2)
        self.assertEqual(daemon.app_tools.sent, [])

    async def test_real_call_4_8_second_append_selects_started_receipt_without_extending_forever(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        evidence = daemon._command_rollout_evidence('source-thread', marker)
        clock = {'now': 100.0}
        daemon._command_rollout_evidence = Mock(side_effect=lambda *args:
            evidence if clock['now'] >= 104.8 else {})
        async def advance(delay):
            clock['now'] += delay
        with patch('phone_agent.time') as timer, \
             patch('phone_agent.asyncio.sleep', side_effect=advance):
            timer.monotonic.side_effect = lambda: clock['now']
            result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'target_turn_started')
        self.assertLess(clock['now'], 106.0)
        self.assertEqual(daemon.app_tools.read_thread.await_count, 1)
        self.assertEqual(daemon.app_tools.sent, [])

    async def test_confirmation_deadline_stays_six_seconds_when_evidence_never_arrives(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        daemon._command_rollout_evidence = Mock(return_value={})
        clock = {'now': 100.0}
        async def advance(delay):
            clock['now'] += delay
        with patch('phone_agent.time') as timer, \
             patch('phone_agent.asyncio.sleep', side_effect=advance):
            timer.monotonic.side_effect = lambda: clock['now']
            result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        self.assertEqual(result['verification_reason'], 'execution_not_confirmed')
        self.assertLessEqual(clock['now'], 106.0)
        self.assertEqual(daemon.app_tools.sent, [])

    async def test_local_execution_fallback_requires_complete_owned_lifecycle(self):
        for variant in ('missing_start', 'aborted', 'end_before_command', 'other_source_start',
                        'other_turn_start', 'other_source_command', 'other_relay',
                        'two_command_turns', 'invalid_line', 'partial_tail'):
            with self.subTest(variant=variant):
                daemon, path, marker, before, rows = self.local_command_fixture()
                if variant == 'missing_start': rows.pop(1)
                elif variant == 'aborted':
                    rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 'actual-turn'}})
                elif variant == 'end_before_command':
                    rows.insert(2, {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'actual-turn'}})
                elif variant == 'other_source_start': rows[1]['payload']['thread_id'] = 'other-thread'
                elif variant == 'other_turn_start': rows[1]['payload']['turn_id'] = 'other-turn'
                elif variant == 'other_source_command': rows[2]['payload']['thread_id'] = 'other-thread'
                elif variant == 'other_relay':
                    rows[2]['payload']['item']['output'] = rows[2]['payload']['item']['output'].replace('fixed-relay', 'other-relay')
                elif variant == 'two_command_turns':
                    duplicate = json.loads(json.dumps(rows[2]))
                    duplicate['payload']['turn_id'] = 'another-turn'
                    rows.extend([{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'another-turn'}}, duplicate])
                content = '\n'.join(map(json.dumps, rows)) + '\n'
                if variant == 'invalid_line': content += 'corrupt record\n'
                if variant == 'partial_tail': content += '{"type":"event_msg","payload":'
                path.write_text(content)
                result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
                self.assertEqual(result['status'], 'accepted_by_codex_app')
                self.assertEqual(result['turn_id'], '')
                self.assertEqual(daemon.app_tools.sent, [])

    async def test_local_completed_command_confirms_start_not_task_success(self):
        daemon, path, marker, before, rows = self.local_command_fixture()
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'actual-turn'}})
        path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'target_turn_started')
        self.assertEqual(result['turn_id'], 'actual-turn')
        self.assertNotIn('completed', result['status'])

    async def test_local_execution_fallback_preserves_desktop_source_binding(self):
        for case in ('missing_baseline', 'other_baseline', 'other_after', 'malformed_marker'):
            with self.subTest(case=case):
                daemon, path, marker, before, _ = self.local_command_fixture()
                if case == 'missing_baseline': before = {}
                if case == 'other_baseline': before['thread_id'] = 'other-thread'
                if case == 'other_after':
                    daemon.app_tools.read_thread.return_value['thread']['id'] = 'other-thread'
                if case == 'malformed_marker':
                    path.write_text(path.read_text().replace(marker, '[codex-phone-command:123]'))
                    marker = '[codex-phone-command:123]'
                result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
                self.assertEqual(result['status'], 'accepted_by_codex_app')
                self.assertEqual(result['turn_id'], '')

    async def test_local_execution_fallback_does_not_search_unbounded_old_start(self):
        daemon, path, marker, before, rows = self.local_command_fixture()
        rows.insert(2, {'type': 'diagnostic', 'payload': 'x' * (9 * 1024 * 1024)})
        path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        self.assertEqual(result['turn_id'], '')

    async def test_exact_local_command_in_baseline_turn_is_steering(self):
        daemon, _, marker, before, _ = self.local_command_fixture()
        before['turn_id'] = 'actual-turn'
        result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'active_target_steered')
        self.assertEqual(result['turn_id'], 'actual-turn')

    async def test_local_abort_cannot_be_overridden_by_matching_stale_desktop_turn(self):
        daemon, path, marker, before, rows = self.local_command_fixture()
        rows.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'turn_id': 'actual-turn'}})
        path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
        daemon.app_tools.read_thread.return_value['turns'][0]['id'] = 'actual-turn'
        result = await daemon._confirm_target_turn('source-thread', before, marker=marker)
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        self.assertEqual(result['turn_id'], '')

    def test_rollout_correlation_excludes_other_source_assistant_and_tool_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.jsonl'
            marker='[codex-phone-command:123]'
            header={'type':'session_meta','payload':{'id':'source-thread'}}
            def record(source,kind,turn):
                return {'type':'event_msg','payload':{'type':'item_completed','thread_id':source,'turn_id':turn,
                    'item':{'type':kind,'content':[{'type':'text','text':marker}]}}}
            rows=[header, record('source-thread','AgentMessage','wrong1'),
                  record('other-thread','UserMessage','wrong2'),record('source-thread','ToolCall','wrong3')]
            path.write_text('\n'.join(map(json.dumps,rows))+'\n')
            daemon=PhoneDaemon({}); daemon._find_rollout_path=Mock(return_value=path)
            self.assertEqual(daemon._command_turn_from_rollout('source-thread',marker),'')
            path.write_text('\n'.join(map(json.dumps,rows+[record('source-thread','UserMessage','right')]))+'\n')
            self.assertEqual(daemon._command_turn_from_rollout('source-thread',marker),'right')

    def test_local_context_uses_exact_source_user_and_final_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'source.jsonl'
            rows = [{'type':'session_meta','payload':{'id':'source-thread'}}]
            for source, kind, phase, text in [
                ('source-thread','UserMessage',None,'旧问题'),
                ('source-thread','AgentMessage','final_answer','旧答案'),
                ('source-thread','Reasoning',None,'不可传出的推理'),
                ('source-thread','AgentMessage','commentary','中间进度'),
                ('different-thread','UserMessage',None,'其他任务的内容'),
                ('source-thread','UserMessage',None,'最新的问题'),
            ]:
                rows.append({'type':'event_msg','payload':{'type':'item_completed','thread_id':source,
                    'item':{'type':kind,'phase':phase,'content':[{'type':'text','text':text}]}}})
            path.write_text('\n'.join(json.dumps(row,ensure_ascii=False) for row in rows)+'\n')
            daemon = PhoneDaemon({})
            daemon._find_rollout_path = Mock(return_value=path)
            self.assertEqual(daemon._recent_source_context('source-thread'),
                'user: 旧问题\n\nassistant: 旧答案\n\nuser: 最新的问题')
            self.assertEqual(daemon._recent_source_context('different-thread'), '')

    async def test_creates_ephemeral_read_only_fork_of_source_thread(self) -> None:
        daemon = PhoneDaemon({"relay_caller_thread_id": "relay-thread"})
        codex = FakeCodex()
        daemon.codex = codex  # type: ignore[assignment]
        daemon.app_tools = FakeRelay()  # type: ignore[assignment]
        daemon.ensure_codex = AsyncMock()  # type: ignore[method-assign]

        job = {"session_id": "source-session", "thread_id": "source-thread"}
        result = await daemon.create_phone_context(job)

        self.assertEqual(result, "voice-context")
        self.assertEqual(job["source_thread_id"], "source-thread")
        self.assertEqual(job["voice_context_thread_id"], "voice-context")
        self.assertEqual(job["context_binding"], "ephemeral_fork_of_source")
        self.assertEqual(job["relay_caller_thread_id"], "relay-thread")
        self.assertEqual(job["command_transport"], "codex_app_send_message")
        self.assertEqual(
            codex.requests,
            [
                ('thread/read', {'threadId': 'source-thread', 'includeTurns': False}),
                (
                    "thread/fork",
                    {
                        "threadId": "source-thread",
                        "model": "source-test-model",
                        "modelProvider": "openai",
                        "allowProviderModelFallback": False,
                        "cwd": "/source-project",
                        "ephemeral": True,
                        "excludeTurns": True,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "config": {"model_reasoning_effort": "low"},
                    },
                )
            ],
        )

    async def test_binding_failure_never_falls_back_to_new_thread(self) -> None:
        daemon = PhoneDaemon({"relay_caller_thread_id": "relay-thread"})
        daemon.codex = FailingCodex()  # type: ignore[assignment]
        daemon.app_tools = FakeRelay()  # type: ignore[assignment]
        daemon.ensure_codex = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "建立临时语音上下文"):
            await daemon.create_phone_context({"thread_id": "source-thread"})

    async def test_call_is_rejected_without_independent_message_identity(self) -> None:
        daemon = PhoneDaemon({})
        daemon.codex = FakeCodex()  # type: ignore[assignment]
        daemon.app_tools = FakeRelay()  # type: ignore[assignment]
        daemon.ensure_codex = AsyncMock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "桌面消息转送身份"):
            await daemon.create_phone_context({"thread_id": "source-thread"})

    async def test_phone_task_is_dispatched_to_exact_source_thread(self) -> None:
        daemon = PhoneDaemon({"relay_caller_thread_id": "relay-thread"})
        relay = FakeRelay()
        daemon.app_tools = relay  # type: ignore[assignment]
        daemon._confirm_target_turn = AsyncMock(  # type: ignore[method-assign]
            return_value={"status": "target_turn_started", "turn_id": "new-turn"}
        )
        job = {
            "thread_id": "source-thread",
            "voice_context_thread_id": "voice-context",
            "relay_caller_thread_id": "relay-thread",
            "cwd": "/tmp/source-project",
        }

        await daemon.relay_phone_task(job, "整理当前目录")

        self.assertEqual(len(relay.sent), 1)
        self.assertEqual(relay.sent[0][:2], ("relay-thread", "source-thread"))
        self.assertEqual(relay.sent[0][2].split('\n\n')[0], "整理当前目录")
        self.assertEqual(job["relayed_phone_tasks"][0]["text"], "整理当前目录")
        self.assertEqual(
            job["relayed_phone_tasks"][0]["delivery_status"],
            "target_turn_started",
        )
        self.assertEqual(
            job["relayed_phone_tasks"][0]["transport"],
            "codex_app_send_message",
        )

    async def test_desktop_message_preserves_original_words_and_identity(self) -> None:
        daemon = PhoneDaemon({"relay_caller_thread_id": "relay-thread"})
        relay = FakeRelay()
        daemon.app_tools = relay  # type: ignore[assignment]
        daemon._confirm_target_turn = AsyncMock(  # type: ignore[method-assign]
            return_value={"status": "accepted_by_codex_app", "turn_id": ""}
        )

        await daemon.relay_phone_task(
            {"thread_id": "source-thread"}, "不要改写这句原话"
        )

        self.assertEqual(len(relay.sent), 1)
        self.assertEqual(relay.sent[0][:2], ("relay-thread", "source-thread"))
        self.assertEqual(relay.sent[0][2].split('\n\n')[0], "不要改写这句原话")


class QueueBindingTests(unittest.TestCase):
    def test_manual_call_without_actual_report_does_not_use_a_canned_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(phone_agent, 'QUEUE_DIR', root / 'queue'), \
                 patch.object(phone_agent, 'CALLING_DIR', root / 'calling'), \
                 patch.object(phone_agent, 'load_config', return_value={
                     'provider': 'iphone', 'to_number': '+8613800138000'}), \
                 patch.object(phone_agent, 'is_session_enabled', return_value=False):
                for report in (None, '', '   '):
                    with self.subTest(report=report), self.assertRaisesRegex(SystemExit, '本次任务摘要'):
                        phone_agent.queue_test_call(thread_id='source-thread', spoken_report=report)
                self.assertEqual(list((root / 'queue').glob('*.json')), [])

    def test_manual_call_uses_current_codex_thread_and_no_short_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            queue_dir = Path(temporary)
            calling_dir = queue_dir / "calling"
            with (
                patch.object(phone_agent, "QUEUE_DIR", queue_dir),
                patch.object(phone_agent, "CALLING_DIR", calling_dir),
                patch.object(
                    phone_agent,
                    "load_config",
                    return_value={
                        "enabled": False,
                        "provider": "iphone",
                        "to_number": "+8613800138000",
                    },
                ),
                patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "current-thread"},
                    clear=True,
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(
                    phone_agent.queue_test_call(
                        spoken_report="拨号超时已经修复，正在验证电话对话。"
                    ),
                    0,
                )

            jobs = list(queue_dir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual(job["session_id"], "current-thread")
            self.assertEqual(job["thread_id"], "current-thread")
            self.assertIs(job["manual_call"], True)
            self.assertEqual(
                job["spoken_report"],
                "拨号超时已经修复，正在验证电话对话。",
            )
            self.assertEqual(job["report"], job["spoken_report"])
            self.assertNotIn("当前窗口", job["spoken_report"])
            self.assertNotIn("max_call_seconds", job)

    def test_realtime_self_test_rejects_implausibly_short_chinese_audio(self) -> None:
        text = (
            "现在进行连续语音测试。第一句话应该平稳自然，"
            "第二句话中间也不应该出现颤音或突然停顿。"
        )

        self.assertGreater(phone_agent._minimum_realtime_reading_ms(text), 3_460)

    def test_realtime_self_test_rejects_semantically_garbled_audio(self) -> None:
        garbled = (
            "现在进行序运测试第一句话应该问自然"
            "第一句话中间也不应该出现颤影虎突然停顿"
        )

        self.assertLess(
            phone_agent._speech_text_similarity(
                phone_agent.REALTIME_SELF_TEST_TEXT,
                garbled,
            ),
            phone_agent.MIN_REALTIME_ASR_SIMILARITY,
        )

    def test_manual_call_without_thread_context_is_rejected(self) -> None:
        with (
            patch.object(
                phone_agent,
                "load_config",
                return_value={
                    "enabled": False,
                    "provider": "iphone",
                    "to_number": "+8613800138000",
                },
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(SystemExit, "已拒绝创建无上下文"):
                phone_agent.queue_test_call()

    def test_only_manual_or_subscribed_jobs_are_authorized(self) -> None:
        config = {"enabled": False}

        self.assertTrue(
            PhoneDaemon.call_authorized(config, {"manual_call": True})
        )
        self.assertFalse(PhoneDaemon.call_authorized(config, {"thread_id": "auto"}))
        self.assertFalse(
            PhoneDaemon.call_authorized(
                {"enabled": True},
                {"thread_id": "automatic", "session_subscription": True},
            )
        )
        with patch.object(phone_agent, "is_session_enabled", return_value=True):
            self.assertTrue(
                PhoneDaemon.call_authorized(
                    {"enabled": True},
                    {
                        "thread_id": "subscribed-thread",
                        "session_subscription": True,
                    },
                )
            )

    def test_manual_call_rejects_an_existing_pending_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            calling_dir = root / "calling"
            queue_dir.mkdir()
            (queue_dir / "existing.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(phone_agent, "QUEUE_DIR", queue_dir),
                patch.object(phone_agent, "CALLING_DIR", calling_dir),
                patch.object(
                    phone_agent,
                    "load_config",
                    return_value={
                        "enabled": False,
                        "provider": "iphone",
                        "to_number": "+8613800138000",
                    },
                ),
                patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "current-thread"},
                    clear=True,
                ),
            ):
                with self.assertRaisesRegex(SystemExit, "拒绝重复排队"):
                    phone_agent.queue_test_call()


class PhoneVoiceConfigTests(unittest.TestCase):
    def test_set_phone_voice_preserves_existing_audio_tuning(self) -> None:
        selected = "com.apple.ttsbundle.premium"
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": "iphone",
                        "to_number": "+8613800138000",
                        "phone_system_voice": "old-voice",
                        "phone_system_voice_av_rate": 0.5,
                        "phone_system_voice_pitch": 1.04,
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(phone_agent, "CONFIG_PATH", config_path),
                patch.object(
                    phone_agent,
                    "installed_chinese_voices",
                    return_value=[
                        {
                            "name": "声音4",
                            "identifier": selected,
                            "quality": 2,
                        }
                    ],
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(phone_agent.set_phone_voice(selected), 0)

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["phone_system_voice"], selected)
            self.assertEqual(updated["phone_voice_renderer"], "macos")
            self.assertEqual(updated["phone_system_voice_av_rate"], 0.5)
            self.assertEqual(updated["phone_system_voice_pitch"], 1.04)

    def test_set_phone_voice_rejects_a_voice_that_is_not_installed(self) -> None:
        with (
            patch.object(
                phone_agent,
                "installed_chinese_voices",
                return_value=[
                    {
                        "name": "声音4",
                        "identifier": "installed-voice",
                        "quality": 2,
                    }
                ],
            ),
            self.assertRaisesRegex(SystemExit, "尚未安装"),
        ):
            phone_agent.set_phone_voice("missing-voice")

    def test_set_realtime_voice_uses_only_live_v3_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": "iphone",
                        "to_number": "+8613800138000",
                        "phone_system_voice": "local-fallback",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(phone_agent, "CONFIG_PATH", config_path),
                patch.object(
                    phone_agent,
                    "realtime_voice_catalog",
                    AsyncMock(
                        return_value={
                            "transport_version": "v3",
                            "voices": ["cove", "juniper"],
                            "default": "cove",
                        }
                    ),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(asyncio.run(phone_agent.set_realtime_voice("cove")), 0)

            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["voice"], "cove")
            self.assertEqual(updated["phone_voice_renderer"], "realtime-unified")
            self.assertFalse(updated['phone_realtime_semantic_gate'])
            self.assertEqual(updated["phone_system_voice"], "local-fallback")

    def test_set_realtime_voice_rejects_v2_only_voice(self) -> None:
        with (
            patch.object(
                phone_agent,
                "realtime_voice_catalog",
                AsyncMock(
                    return_value={
                        "transport_version": "v3",
                        "voices": ["cove", "juniper"],
                        "default": "cove",
                    }
                ),
            ),
            self.assertRaisesRegex(SystemExit, "不支持当前电话 v3"),
        ):
            asyncio.run(phone_agent.set_realtime_voice("marin"))


class BackgroundDaemonTests(unittest.TestCase):
    def test_install_uses_codex_descended_daemon(self) -> None:
        with (
            patch.object(phone_agent, "install_completion_hook") as install_hook,
            patch.object(
                phone_agent, "trust_completion_hook", Mock(return_value=None)
            ),
            patch.object(phone_agent.asyncio, "run") as run_async,
            patch.object(phone_agent, "start_background_daemon") as start_daemon,
        ):
            phone_agent.install_automation()

        install_hook.assert_called_once_with()
        run_async.assert_called_once()
        start_daemon.assert_called_once_with()

    def test_accessibility_probe_requires_explicit_trusted_result(self) -> None:
        completed = Mock(returncode=0, stdout="trusted\n", stderr="")
        with patch.object(phone_agent.subprocess, "run", return_value=completed):
            self.assertEqual(
                phone_agent._probe_accessibility_permission(), (True, "trusted")
            )

        denied = Mock(
            returncode=77,
            stdout="",
            stderr="accessibility permission is not available\n",
        )
        with patch.object(phone_agent.subprocess, "run", return_value=denied):
            self.assertEqual(
                phone_agent._probe_accessibility_permission(),
                (False, "accessibility permission is not available"),
            )

    def test_daemon_instance_lock_rejects_a_second_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "daemon-instance.lock"
            with (
                patch.object(phone_agent, "DAEMON_INSTANCE_LOCK_PATH", lock_path),
                patch.object(
                    phone_agent.fcntl,
                    "flock",
                    side_effect=BlockingIOError,
                ),
                patch.object(
                    phone_agent, "_run_daemon_locked", new_callable=AsyncMock
                ) as run_locked,
            ):
                self.assertEqual(asyncio.run(phone_agent.run_daemon()), 75)

            run_locked.assert_not_awaited()

    def test_daemon_pid_rejects_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "daemon.pid.json"
            pid_path.write_text(json.dumps({"pid": 321}), encoding="utf-8")
            with (
                patch.object(phone_agent, "DAEMON_PID_PATH", pid_path),
                patch.object(phone_agent.os, "kill"),
                patch.object(
                    phone_agent,
                    "_process_command",
                    return_value="/usr/bin/python unrelated.py daemon",
                ),
            ):
                self.assertIsNone(phone_agent.background_daemon_pid())

    def test_legacy_launch_agent_is_unloaded_and_file_removed(self) -> None:
        completed = Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as temporary:
            plist = Path(temporary) / "legacy.plist"
            plist.write_text("legacy", encoding="utf-8")
            with (
                patch.object(phone_agent, "LAUNCH_AGENT_PATH", plist),
                patch.object(
                    phone_agent,
                    "launch_agent_loaded",
                    side_effect=(True, False, False),
                ),
                patch.object(phone_agent.subprocess, "run", return_value=completed) as run,
            ):
                phone_agent.remove_legacy_launch_agent()

            self.assertFalse(plist.exists())
            self.assertEqual(run.call_args.args[0][1], "bootout")


class CompletionHookInstallTests(unittest.TestCase):
    def test_phone_hooks_are_deduplicated_without_removing_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hooks_path = Path(temporary) / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/usr/bin/python3 /tmp/codex-phone/hook_stop.py",
                                        },
                                        {
                                            "type": "command",
                                            "command": "/usr/bin/other-hook",
                                        },
                                    ]
                                },
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/usr/bin/python3 /tmp/codex-call-the-boss/hook_stop.py",
                                        }
                                    ]
                                },
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(phone_agent, "GLOBAL_HOOK_PATH", hooks_path):
                phone_agent.install_completion_hook()

            payload = json.loads(hooks_path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for group in payload["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            self.assertIn("/usr/bin/other-hook", commands)
            self.assertEqual(commands.count(phone_agent._hook_command()), 1)
            self.assertEqual(sum("hook_stop.py" in command for command in commands), 1)


class QueueWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_shot_shutdown_lets_task_relay_finish(self) -> None:
        daemon = PhoneDaemon({"enabled": False}, once=True)
        finished = asyncio.Event()

        async def relay() -> None:
            await asyncio.sleep(0)
            finished.set()

        daemon.track_detached_session(asyncio.create_task(relay()))
        await daemon._shutdown_detached_sessions()

        self.assertTrue(finished.is_set())
        self.assertFalse(daemon.detached_sessions)

    async def test_one_shot_worker_processes_manual_job_while_auto_is_paused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            queue_dir.mkdir()
            job_path = queue_dir / "manual-test.json"
            job_path.write_text(
                json.dumps(
                    {
                        "session_id": "source-thread",
                        "thread_id": "source-thread",
                        "report": "done",
                        "manual_call": True,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            daemon = PhoneDaemon({"enabled": False}, once=True)
            daemon._place_job = AsyncMock()  # type: ignore[method-assign]

            with (
                # The real line guard must not affect or be cleared by this
                # isolated queue test, even after a user's failed call.
                patch.object(phone_agent, "STATE_DIR", root),
                patch.object(phone_agent, "QUEUE_DIR", queue_dir),
                patch.object(phone_agent, "ACTIVE_CALL_PATH", root / "active.json"),
                patch.object(
                    phone_agent, "load_config", return_value={"enabled": False}
                ),
            ):
                await asyncio.wait_for(daemon._queue_worker(), timeout=1)

            daemon._place_job.assert_awaited_once()
            self.assertEqual(
                daemon._place_job.await_args.args[0],
                job_path,
            )
            self.assertTrue(daemon.stop_event.is_set())
            self.assertFalse((root / "active.json").exists())


class RolloutCompletionFallbackTests(unittest.TestCase):
    def test_watcher_starts_at_eof_then_queues_only_new_completion(self) -> None:
        thread_id = "thread-12345678"
        with tempfile.TemporaryDirectory() as temporary:
            sessions_root = Path(temporary) / "sessions"
            rollout_dir = sessions_root / "2026" / "09" / "04"
            rollout_dir.mkdir(parents=True)
            rollout = rollout_dir / f"rollout-test-{thread_id}.jsonl"
            historical = {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "old-turn",
                },
            }
            rollout.write_text(json.dumps(historical) + "\n", encoding="utf-8")
            daemon = PhoneDaemon({"enabled": True})

            with (
                patch.object(phone_agent, "CODEX_SESSIONS_DIR", sessions_root),
                patch.object(
                    phone_agent,
                    "active_sessions",
                    return_value=[{"thread_id": thread_id, "enabled": True}],
                ),
                patch.object(
                    phone_agent.hook_stop, "queue_completion_event"
                ) as queue_event,
            ):
                daemon._poll_rollout_updates()
                queue_event.assert_not_called()

                records = [
                    {
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "new-turn",
                            "cwd": "/tmp/source-project",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": "new-turn",
                            "item": {
                                "type": "AgentMessage",
                                "phase": "final_answer",
                                "content": [
                                    {"type": "Text", "text": "新任务已经完成。"}
                                ],
                            },
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "new-turn",
                        },
                    },
                ]
                with rollout.open("a", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

                queue_event.return_value = True
                daemon._poll_rollout_updates()

            queue_event.assert_called_once_with(
                {
                    "session_id": thread_id,
                    "thread_id": thread_id,
                    "turn_id": "new-turn",
                    "cwd": "/tmp/source-project",
                    "last_assistant_message": "新任务已经完成。",
                },
                daemon_status=(True, "rollout_completion_fallback"),
            )


if __name__ == "__main__":
    unittest.main()
