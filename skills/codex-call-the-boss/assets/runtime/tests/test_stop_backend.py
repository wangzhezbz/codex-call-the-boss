"""Actual bridge/worker integration with synthetic host, audio and queue only."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from phone_agent import PhoneDaemon
from phone_stop_backend import StopBackend
from phone_stop_dispatch import StopDispatch
from phone_stop_completion import handle_completion
from phone_stop_cycles import StopCycles
from phone_stop_mailbox import MailboxError, Scope, StopMailbox
from phone_stop_queue import StopQueue
from phone_stop_service import process_hook
from native_speech import COMMAND_RECEIPT, COMMAND_ERROR, COMMAND_QUEUED, COMMAND_CANCELLED
import test_conversation_contract as contract


class HostFixture:
    def __init__(self):
        self.store = Path(tempfile.mkdtemp(prefix='phone-stop-integration-'))
        self.box = StopMailbox(self.store)
        self.path = self.store / 'synthetic-rollout.jsonl'
        self.source, self.root = 'source-0001', 'root-000001'
        self.hook = '/test/hooks.json'
        self.event = {'hook_event_name': 'Stop', 'session_id': self.source,
                      'turn_id': self.root, 'cwd': '/project', 'stop_hook_active': False,
                      'last_assistant_message': '首次任务处理结果。'}
        self.append({'type': 'session_meta', 'payload': {'id': self.source}})
        self.event_row('task_started')
        self.backend = StopBackend(self.box, rollout_path=lambda _: self.path,
                                   hook_config=self.hook, confirmation_seconds=.15)
        self.cycles = StopCycles(self.box, rollout_path=lambda _: self.path,
                                 hook_config=self.hook, is_enabled=lambda source: source == self.source)

    def append(self, row):
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    def event_row(self, kind, item=None):
        self.append({'timestamp': datetime.now(timezone.utc).isoformat(), 'type': 'event_msg',
                     'payload': {'type': kind, 'thread_id': self.source,
                                 'turn_id': self.root, **({'item': item} if item else {})}})

    def receive(self, output, *, processing=True, suffix='first'):
        self.event_row('item_completed', {'type': 'HookPrompt', 'id': 'input-' + suffix,
            'fragments': [{'text': output['reason'], 'hookRunId': 'stop:0:' + self.hook}]})
        if processing:
            self.event_row('item_started', {'type': 'AgentMessage', 'id': 'answer-' + suffix})

    def finish(self, text):
        self.event_row('item_completed', {'type': 'AgentMessage', 'id': 'final-' + text,
            'phase': 'final_answer', 'content': [{'type': 'text', 'text': text}]})
        return {**self.event, 'stop_hook_active': True, 'last_assistant_message': text}

    def job(self, scope):
        return {'thread_id': self.source, 'session_id': self.source, 'job_id': scope.call_id,
                'source_root_turn_id': self.root, 'cwd': '/project',
                'stop_wait_scope': asdict(scope), 'spoken_report': '本轮检查已经完成。'}


class StopHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = HostFixture()
        self.scope = self.h.cycles.claim(self.h.event)
        self.owner = self.h.box.open_window(self.scope)
        self.addCleanup(self.owner.close)
        self.job = self.h.job(self.scope)

    async def test_other_process_brief_lock_does_not_disconnect_live_owner(self):
        script = ("import fcntl,sys; f=open(sys.argv[1],'r+b'); "
                  "fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); "
                  "sys.stdin.readline(); f.close()")
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
            str(self.h.store / 'transaction.lock'), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE)
        task = None
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 3), b'locked\n')
            task = asyncio.create_task(self.h.backend.health_error(self.job))
            await asyncio.sleep(.06)
            self.assertFalse(task.done())
            self.assertIsNotNone(self.owner.fd)
            self.assertFalse((self.h.box.path(self.scope) / 'closed.json').exists())
            await asyncio.wait_for(process.communicate(b'release\n'), 3)
            self.assertIsNone(await task)
            self.assertGreater(self.job['stop_transport_health']['busy_retries'], 0)
            self.assertEqual(self.job['stop_transport_health']['last_result'], 'ready')
            self.assertTrue(self.h.box.is_waiting(self.scope))
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
                await process.wait()

    async def test_persistent_contention_is_bounded_and_does_not_block_event_loop(self):
        ticks = []
        async def ticker():
            for _ in range(10):
                await asyncio.sleep(.02)
                ticks.append(True)
        heartbeat = asyncio.create_task(ticker())
        with self.h.box.transaction():
            result = await self.h.backend.health_error(self.job)
        await heartbeat
        self.assertEqual(len(ticks), 10)
        self.assertIn('state check timed out', result)
        trace = self.job['stop_transport_health']
        self.assertEqual(trace['last_error_code'], 'transaction_busy_timeout')
        self.assertGreaterEqual(trace['last_check_ms'], 700)
        self.assertLess(trace['last_check_ms'], 1500)
        self.assertIsNone(await self.h.backend.health_error(self.job))

    async def test_expiry_during_contention_cannot_become_ready(self):
        task = None
        with self.h.box.transaction():
            task = asyncio.create_task(self.h.backend.health_error(self.job))
            await asyncio.sleep(.03)
            self.h.box.wall_clock = lambda: float('inf')
        self.assertIn('window_expired', await task)

    async def test_hangup_cancels_busy_read_without_failure_notice(self):
        with self.h.box.transaction():
            task = asyncio.create_task(self.h.backend.health_error(self.job))
            await asyncio.sleep(.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotEqual(self.job['stop_transport_health'].get('last_result'), 'failed')

    async def test_real_closed_owner_fails_without_grace(self):
        self.owner.close('caller_hangup')
        self.assertIn('window_closed', await self.h.backend.health_error(self.job))
        self.assertNotIn('busy_retries', self.job['stop_transport_health'])

    async def test_owner_loss_without_closed_record_is_not_hidden(self):
        os.close(self.owner.fd)
        os.close(self.owner.source_fd)
        self.owner.fd = self.owner.source_fd = None
        self.assertIn('owner_not_waiting', await self.h.backend.health_error(self.job))

    async def test_wrong_binding_and_corrupt_record_remain_fail_closed(self):
        wrong = {**self.job, 'thread_id': 'wrong-source'}
        self.assertIn('call_binding_mismatch', await self.h.backend.health_error(wrong))
        path = self.h.box.path(self.scope) / 'offered.json'
        with path.open('x') as stream:
            stream.write('{}')
        path.chmod(0o600)
        self.assertIn('invalid_offer', await self.h.backend.health_error(self.job))

    async def test_known_emitted_command_does_not_require_exited_owner(self):
        self.h.box.offer(self.scope, command_id='command-health', input_id='input-health',
            text='继续检查', classified_text='继续检查',
            decision={'kind': 'action', 'clarification': ''}, input_finalized=True)
        self.owner.reserve(self.h.event)
        self.owner.close()
        self.assertIn('unknown_emission', await self.h.backend.health_error(self.job))
        self.job['stop_offers'] = [{'command_id': 'command-health'}]
        self.assertIsNone(await self.h.backend.health_error(self.job))
        self.assertEqual(len(list(self.h.store.glob('command-*.json'))), 1)

    async def test_actual_bridge_monitor_survives_lock_then_detects_real_close(self):
        bridge = contract.ContractTests().bridge()
        bridge.daemon = PhoneDaemon({'provider': 'iphone'}, stop_backend=self.h.backend)
        bridge.pending.job = self.job
        bridge._realtime_readiness_error = lambda: None
        bridge._check_stalled_voice = AsyncMock()
        task = None
        try:
            with self.h.box.transaction():
                task = asyncio.create_task(bridge._watch_call_health(asyncio.get_running_loop().time()))
                await asyncio.sleep(.04)
                self.assertFalse(task.done())
            await asyncio.sleep(.04)
            self.assertFalse(task.done())
            self.assertEqual(bridge.local_tts.texts, [])
            bridge._check_stalled_voice.assert_awaited()
            self.owner.close('caller_hangup')
            self.assertIn('window_closed', await asyncio.wait_for(task, 1))
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await bridge.stop()


class BridgeStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = HostFixture()
        self.scope = self.host.cycles.claim(self.host.event)
        self.owner = self.host.box.open_window(self.scope)
        self.addCleanup(self.owner.close)
        self.bridge = contract.ContractTests().bridge()
        self.bridge.pending.job = self.host.job(self.scope)
        self.bridge.pending.job_id = self.scope.call_id
        self.bridge.daemon.stop_backend = self.host.backend
        self.bridge._schedule_context_update = lambda **kwargs: None
        self.addAsyncCleanup(self.bridge.stop)

    async def route(self, text='放两次礼花庆祝一下', identity='input-one', kind='action'):
        b = self.bridge
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': kind, 'clarification': ''})
        b._caller_turns.observe(identity, text, complete=True)
        await b._classify_and_route(identity, text, [], b._speech_generation)

    async def host_output(self, *, processing=True):
        output = await self.owner.wait(self.host.event)
        self.host.receive(output, processing=processing)
        return output

    async def test_real_bridge_commit_to_exact_source_then_complete_selected_receipt(self):
        delivery = asyncio.create_task(self.host_output())
        await self.route()
        output = await delivery
        self.assertEqual(json.loads(output['reason'].splitlines()[-1])['original_caller_words'], '放两次礼花庆祝一下')
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_RECEIPT])
        self.assertEqual(self.bridge.daemon.relayed, [])
        self.assertEqual(len(self.bridge.pending.job['relayed_phone_tasks']), 1)

    async def test_completed_only_host_event_selects_full_approved_receipt(self):
        async def host_output():
            output = await self.owner.wait(self.host.event)
            self.host.receive(output, processing=False)
            now = datetime.now(timezone.utc)
            self.host.append({'timestamp':now.isoformat(), 'type':'event_msg', 'payload':{
                'type':'item_completed', 'thread_id':self.host.source,
                'turn_id':self.host.root, 'item':{'type':'AgentMessage','id':'new-commentary'},
                'started_at_ms':now.timestamp()*1000, 'completed_at_ms':now.timestamp()*1000}})
        delivery = asyncio.create_task(host_output())
        await self.route()
        await delivery
        self.assertEqual(self.bridge.local_tts.texts,[COMMAND_RECEIPT])
        self.assertTrue(self.bridge.pending.job['stop_delivery_results'][0]['verification']['execution_confirmed'])
        self.assertEqual(len(self.bridge.pending.job['stop_offers']),1)

    async def test_unobserved_offer_gets_uncertain_notice_not_delivery_or_started(self):
        await self.route()
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_ERROR])
        self.assertNotIn('relayed_phone_tasks', self.bridge.pending.job)

    async def test_receipt_read_lock_contention_recovers_full_receipt_without_resending(self):
        self.host.backend.confirmation_seconds = .5
        async def host_output():
            output = await self.owner.wait(self.host.event)
            self.host.receive(output)
            # Real flock contention with the worker's to_thread reader.
            with self.host.box.transaction():
                await asyncio.sleep(.12)
        delivery = asyncio.create_task(host_output())
        await self.route()
        await delivery
        result = self.bridge.pending.job['stop_delivery_results'][0]['verification']
        self.assertGreater(result['verification_busy_retries'], 0)
        self.assertTrue(result['execution_confirmed'])
        self.assertNotIn('verification_warning', result)
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_RECEIPT])
        self.assertEqual(len(self.bridge.pending.job['stop_offers']), 1)
        self.assertEqual(len(list(self.host.store.glob('command-*.json'))), 1)

    async def test_persistent_busy_receipt_read_keeps_original_bound_and_single_offer(self):
        start = asyncio.get_running_loop().time()
        with patch.object(StopDispatch, 'inspect_delivery', side_effect=MailboxError('transaction_busy')) as read:
            await self.route()
        elapsed = asyncio.get_running_loop().time() - start
        result = self.bridge.pending.job['stop_delivery_results'][0]['verification']
        self.assertGreaterEqual(read.call_count, 2)
        self.assertLess(elapsed, .5)
        self.assertEqual(result['verification_warning'], 'transaction_busy_timeout')
        self.assertFalse(result['delivered_to_model'])
        self.assertFalse(result['execution_confirmed'])
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_ERROR])
        self.assertEqual(len(self.bridge.pending.job['stop_offers']), 1)

    async def test_busy_read_cannot_erase_confirmed_delivery_or_invent_execution(self):
        delivered = {'status': 'delivered_execution_unconfirmed',
                     'delivered_to_model': True, 'execution_confirmed': False}
        count = 0
        def inspect(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return delivered
            raise MailboxError('transaction_busy')
        with patch.object(StopDispatch, 'inspect_delivery', side_effect=inspect):
            await self.route()
        result = self.bridge.pending.job['stop_delivery_results'][0]['verification']
        self.assertTrue(result['delivered_to_model'])
        self.assertFalse(result['execution_confirmed'])
        self.assertEqual(result['verification_warning'], 'transaction_busy_timeout')
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_QUEUED])
        self.assertEqual(len(self.bridge.pending.job['stop_offers']), 1)

    async def test_receipt_validation_errors_are_not_retried_or_promoted(self):
        with patch.object(StopDispatch, 'inspect_delivery',
                          side_effect=MailboxError('emission_binding_mismatch')) as read:
            await self.route()
        read.assert_called_once()
        result = self.bridge.pending.job['stop_delivery_results'][0]['verification']
        self.assertEqual(result['verification_warning'], 'emission_binding_mismatch')
        self.assertFalse(result['execution_confirmed'])
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_ERROR])

    async def test_receipt_without_new_processing_gets_delivery_only(self):
        delivery = asyncio.create_task(self.host_output(processing=False))
        await self.route()
        await delivery
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_QUEUED])
        result = self.bridge.pending.job['stop_delivery_results'][0]['verification']
        self.assertEqual(result['verification_warning'], 'processing_not_observed_before_deadline')

    async def test_actual_question_does_not_offer_a_command(self):
        await self.route('现在进展如何？', kind='question')
        self.assertFalse((self.host.box.path(self.scope) / 'offered.json').exists())
        self.assertFalse(self.bridge.daemon.relayed)

    async def test_original_command_carries_only_prior_audible_quoted_context(self):
        history = [{'role': 'user', 'text': '庆祝动画能放两次吗？'},
                   {'role': 'assistant', 'text': '可以，按两次处理。'}]
        self.bridge._phone_history_before = Mock(return_value=history)
        delivery = asyncio.create_task(self.host_output())
        await self.route('就按这个做。')
        output = await delivery
        payload = json.loads(output['reason'].splitlines()[-1])
        self.assertEqual(payload['original_caller_words'], '就按这个做。')
        self.assertEqual(payload['quoted_phone_history'], history)
        self.bridge._phone_history_before.assert_called_once_with('input-one')

    async def test_private_archive_preserves_uncertain_command_records(self):
        await self.route()
        job = self.bridge.pending.job
        job['phone_transcript'] = [{'role': 'user', 'text': '放两次礼花庆祝一下'}]
        with patch('phone_agent.STATE_DIR', self.host.store):
            path = await PhoneDaemon.archive_phone_conversation_async(self.scope.call_id, job)
        record = json.loads(Path(path).read_text())
        self.assertEqual(record['synchronous_stop']['offers'], job['stop_offers'])
        self.assertEqual(record['synchronous_stop']['delivery_results'], job['stop_delivery_results'])
        self.assertEqual(record['commands'], [])

    async def test_alias_keeps_original_committed_ledger_identity(self):
        self.bridge._user_turn_aliases['input-one'] = 'canonical-alias'
        delivery = asyncio.create_task(self.host_output())
        await self.route()
        await delivery
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_RECEIPT])

    async def test_second_action_cannot_fall_back_to_rejected_desktop_transport(self):
        delivery = asyncio.create_task(self.host_output())
        await self.route()
        await delivery
        await self.route('再放一次', identity='input-two')
        self.assertEqual(self.bridge.local_tts.texts, [COMMAND_RECEIPT, COMMAND_ERROR])
        self.assertEqual(len(self.bridge.pending.job['stop_offers']), 1)
        self.assertFalse(self.bridge.daemon.relayed)

    async def test_cancellation_before_emission_stops_offered_command(self):
        await self.route()
        await self.route('别执行了', identity='cancel-input', kind='cancel')
        self.assertEqual(self.bridge.local_tts.texts[-1], COMMAND_CANCELLED)
        self.assertEqual(self.owner.reserve(self.host.event), {})

    async def test_cancellation_after_emission_does_not_claim_undo_or_delivery(self):
        delivery = asyncio.create_task(self.host_output())
        await self.route()
        await delivery
        await self.route('别执行了', identity='cancel-input', kind='cancel')
        self.assertEqual(self.bridge.local_tts.texts[-1], COMMAND_ERROR)
        self.assertEqual(len(self.bridge.pending.job['relayed_phone_tasks']), 1)

    async def test_expired_owner_blocks_before_any_dial_action(self):
        self.owner.close()
        self.bridge._realtime_readiness_error = lambda: None
        with self.assertRaises(MailboxError):
            self.bridge._assert_realtime_ready_before_dial()

    async def test_actual_worker_public_context_never_checks_private_relay(self):
        daemon = PhoneDaemon({'provider': 'iphone'}, stop_backend=self.host.backend)
        daemon.ensure_codex = AsyncMock()
        daemon.app_tools.health = AsyncMock(side_effect=AssertionError('private IPC forbidden'))
        daemon._recent_source_context = lambda _: 'user: 项目近况'
        async def request(method, args, **kwargs):
            if method == 'thread/read':
                return {'thread': {'id': self.host.source, 'cwd': '/project',
                                  'model': 'source-model', 'modelProvider': 'openai'}}
            self.assertEqual(method, 'thread/fork')
            return {'thread': {'id': 'ephemeral-view', 'forkedFromId': self.host.source,
                              'ephemeral': True, 'model': 'source-model', 'modelProvider': 'openai'}}
        daemon.codex = Mock(request=AsyncMock(side_effect=request))
        self.assertEqual(await daemon.create_phone_context(self.host.job(self.scope)), 'ephemeral-view')
        daemon.app_tools.health.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            await daemon.relay_phone_task(self.host.job(self.scope), '未通过语音提交')


class CompletionCycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = HostFixture()

    async def completion_with_command(self, event, *, suffix):
        queued = []
        async def queue(job):
            queued.append(job)
            scope = Scope(**job['stop_wait_scope'])
            self.assertTrue(self.h.box.is_waiting(scope))
            self.h.box.offer(scope, command_id='command-' + suffix, input_id='input-' + suffix,
                text='检查当前项目', classified_text='检查当前项目',
                decision={'kind': 'action', 'clarification': ''}, input_finalized=True)
        result = await handle_completion(event, cycles=self.h.cycles, queue_job=queue,
            call_settled=AsyncMock(return_value=False), spoken_report='本轮检查已经完成。', timeout_seconds=1)
        self.assertEqual(len(queued), 1)
        return queued[0], result

    async def test_three_logical_cycles_keep_same_source_root_and_distinct_one_call_claims(self):
        event, jobs = self.h.event, []
        for index in range(3):
            job, output = await self.completion_with_command(event, suffix=f'{index:08d}')
            jobs.append(job)
            self.assertIsNone(self.h.cycles.claim(event))
            self.h.receive(output, suffix=str(index))
            event = self.h.finish('本轮完成结果' + str(index))
        self.assertEqual(len({j['job_id'] for j in jobs}), 3)
        self.assertEqual({j['source_root_turn_id'] for j in jobs}, {self.h.root})
        self.assertEqual({j['thread_id'] for j in jobs}, {self.h.source})

    async def test_no_received_input_or_new_final_cannot_create_callback(self):
        _, output = await self.completion_with_command(self.h.event, suffix='00000001')
        event = {**self.h.event, 'stop_hook_active': True}
        self.assertIsNone(self.h.cycles.claim(event))
        self.h.receive(output)
        self.assertIsNone(self.h.cycles.claim(event))
        self.h.finish('不同于当前Stop的结果')
        self.assertIsNone(self.h.cycles.claim(event))

    async def test_settled_failed_call_closes_window_without_replay(self):
        jobs = []
        async def queue(job): jobs.append(job)
        result = await handle_completion(self.h.event, cycles=self.h.cycles, queue_job=queue,
                                         call_settled=AsyncMock(return_value=True))
        self.assertEqual(result, {})
        self.assertFalse(self.h.box.is_waiting(Scope(**jobs[0]['stop_wait_scope'])))
        self.assertIsNone(self.h.cycles.claim(self.h.event))

    async def test_queue_exception_is_terminal_and_releases_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'uncertain queue'):
            await handle_completion(self.h.event, cycles=self.h.cycles,
                queue_job=AsyncMock(side_effect=RuntimeError('uncertain queue')),
                call_settled=AsyncMock(return_value=False))
        self.assertIsNone(self.h.cycles.claim(self.h.event))

    async def test_wait_has_one_original_deadline(self):
        with self.assertRaises(TimeoutError):
            await handle_completion(self.h.event, cycles=self.h.cycles,
                queue_job=AsyncMock(), call_settled=AsyncMock(return_value=False), timeout_seconds=.02)
        self.assertIsNone(self.h.cycles.claim(self.h.event))

    async def test_unsubscribed_foreign_subagent_or_unbound_continuation_cannot_queue(self):
        for change in ({'session_id': 'other-source'}, {'thread_id': 'other-source'},
                       {'agent_id': 'child-agent'}, {'stop_hook_active': True}):
            with self.subTest(change=change):
                queue = AsyncMock()
                with self.assertRaises(MailboxError):
                    await handle_completion({**self.h.event, **change}, cycles=self.h.cycles,
                        queue_job=queue, call_settled=AsyncMock(return_value=False))
                queue.assert_not_awaited()


class PersistentQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = HostFixture()
        self.scope = self.h.cycles.claim(self.h.event)
        self.owner = self.h.box.open_window(self.scope)
        self.addCleanup(self.owner.close)
        self.job = self.h.job(self.scope)
        self.queue = StopQueue(self.h.store, self.h.backend,
            daemon_ready=lambda: (True, 'synchronous_stop'), is_enabled=lambda _: True)

    async def test_persistent_queue_duplicate_guard_and_bound_settlement(self):
        await self.queue.queue(self.job)
        path = self.h.store / 'queue' / (self.job['job_id'] + '.json')
        self.assertEqual(json.loads(path.read_text()), self.job)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(await self.queue.settled(self.job))
        with self.assertRaisesRegex(MailboxError, 'call_already_recorded'):
            await self.queue.queue(self.job)
        done = self.h.store / 'failed'
        done.mkdir()
        # Simulated actual worker settlement, not a physical call.
        record = {**self.job, 'outcome': 'failed: preparation', 'finished_at': 'now'}
        with (done / path.name).open('x') as stream:
            stream.write(json.dumps(record))
        self.assertTrue(await self.queue.settled(self.job))

    async def test_legacy_worker_or_disabled_subscription_refuses_queue(self):
        self.queue.daemon_ready = lambda: (True, 'codex_app_send_message')
        with self.assertRaisesRegex(MailboxError, 'synchronous_worker_not_ready'):
            await self.queue.queue(self.job)
        self.queue.daemon_ready = lambda: (True, 'synchronous_stop')
        self.queue.is_enabled = lambda _: False
        with self.assertRaisesRegex(MailboxError, 'source_not_enabled'):
            await self.queue.queue(self.job)
        self.assertFalse((self.h.store / 'queue').exists())

    async def test_missing_queue_file_is_not_phone_settlement(self):
        self.assertFalse(await self.queue.settled(self.job))

    async def test_queue_does_not_remove_an_unconfirmed_line_guard(self):
        guard = self.h.store / 'phone-line-unconfirmed.json'
        with guard.open('x') as stream: stream.write('{"call":"older-call"}')
        before = guard.read_bytes()
        await self.queue.queue(self.job)
        self.assertEqual(guard.read_bytes(), before)

    async def test_closed_hook_cannot_enqueue_a_late_call(self):
        self.owner.close()
        with self.assertRaises(MailboxError):
            await self.queue.queue(self.job)
        self.assertFalse((self.h.store / 'queue').exists())


class ServiceEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_hook_entry_queues_actual_scoped_job_and_returns_one_block(self):
        h = HostFixture()
        with (patch('phone_agent.load_config', return_value={'enabled': True, 'provider': 'iphone'}),
              patch('session_registry.is_session_enabled', return_value=True),
              patch('phone_stop_service.create_backend', return_value=h.backend),
              patch('hook_stop._completion_already_recorded', return_value=False),
              patch('hook_stop._consume_staged_directive', return_value={'spoken_report': '本轮检查已经完成。'}),
              patch('phone_agent.STATE_DIR', h.store),
              patch('phone_agent.background_daemon_pid', return_value=123),
              patch('phone_agent._daemon_status_ready', return_value=(True, 'ready')),
              patch('phone_agent._load_json', return_value={'command_transport': 'synchronous_stop'})):
            task = asyncio.create_task(process_hook(h.event))
            try:
                async with asyncio.timeout(1):
                    while not list((h.store / 'queue').glob('*.json')):
                        await asyncio.sleep(.01)
                    path = next((h.store / 'queue').glob('*.json'))
                    job = json.loads(path.read_text())
                    scope = Scope(**job['stop_wait_scope'])
                    self.assertTrue(h.box.is_waiting(scope))
                    self.assertEqual(job['spoken_report'], '本轮检查已经完成。')
                    h.box.offer(scope, command_id='command-source-entry', input_id='input-source-entry',
                        text='继续检查项目', classified_text='继续检查项目',
                        decision={'kind': 'action', 'clarification': ''}, input_finalized=True)
                    result = await task
                self.assertEqual(result['decision'], 'block')
                self.assertFalse(h.box.is_waiting(scope))
                self.assertEqual(await process_hook(h.event), {})
                self.assertEqual(len(list((h.store / 'queue').glob('*.json'))), 1)
            finally:
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_unsubscribed_hook_does_not_create_a_backend(self):
        with (patch('phone_agent.load_config', return_value={'enabled': True, 'provider': 'iphone'}),
              patch('session_registry.is_session_enabled', return_value=False),
              patch('phone_stop_service.create_backend') as create):
            self.assertEqual(await process_hook(HostFixture().event), {})
            create.assert_not_called()

if __name__ == '__main__':
    unittest.main()
