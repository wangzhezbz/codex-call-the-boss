import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch
import asyncio

import phone_agent

from service_state import record_state, recent_status, queue_needs_review


class ServiceStateTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="phone-status-test-"))
        self.job = {"turn_id":"test-turn", "thread_id":"test-source", "report":"secret report",
                    "to_number":"do-not-export", "phone_transcript":["private words"]}

    def test_failure_persists_and_notifies_once_without_task_contents(self):
        notify = Mock()
        for _ in range(2):
            row = record_state(self.state, self.job, "failed", reason="report_preparation", notify=True, notifier=notify)
        notify.assert_called_once()
        serialized = json.dumps(recent_status(self.state))
        for text in ("secret report", "private words", "do-not-export"):
            self.assertNotIn(text, serialized)
        self.assertEqual(row["notification"]["state"], "submitted_to_os")
        self.assertFalse(row["notification"]["visible_verified"])
        self.assertEqual((self.state / "service-status/test-turn.json").stat().st_mode & 0o777, 0o600)

    def test_os_failure_does_not_erase_incident_or_retry(self):
        notify = Mock(side_effect=OSError("private detail"))
        row = record_state(self.state, self.job, "failed", notify=True, notifier=notify)
        record_state(self.state, self.job, "failed", notify=True, notifier=notify)
        self.assertEqual(row["notification"]["state"], "submission_failed")
        notify.assert_called_once()
        self.assertNotIn("private detail", json.dumps(row))

    def test_notification_does_not_hold_state_lock_or_rollback_new_state(self):
        def notify(_message):
            record_state(self.state, self.job, 'queued')
        row = record_state(self.state, self.job, 'needs_review', reason='queue_age', notify=True, notifier=notify)
        self.assertEqual(row['phase'], 'queued')
        self.assertEqual(row['notification']['state'], 'submitted_to_os')

    def test_late_work_cannot_resurrect_failed_call(self):
        record_state(self.state, self.job, "failed")
        row = record_state(self.state, self.job, "connected")
        self.assertEqual(row["phase"], "failed")
        row = record_state(self.state, self.job, 'completed')
        self.assertEqual(row['phase'], 'failed')

    def test_only_requested_source_is_visible(self):
        record_state(self.state, self.job, "queued")
        record_state(self.state, {"turn_id":"other-turn","thread_id":"other-source"}, "failed")
        self.assertEqual(len(recent_status(self.state, "test-source")), 1)

    def test_status_history_is_bounded_and_path_traversal_refused(self):
        for i in range(40): record_state(self.state, self.job, "queued" if i % 2 else "preparing")
        self.assertEqual(len(recent_status(self.state)[0]["history"]), 32)
        self.assertIsNone(record_state(self.state, {"turn_id":"../outside"}, "failed"))

    def test_fresh_and_stale_queue_age(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(queue_needs_review({"created_at":now.isoformat()}, now=now))
        self.assertTrue(queue_needs_review({"created_at":(now-timedelta(minutes=16)).isoformat()}, now=now))
        self.assertTrue(queue_needs_review({"created_at":"bad"}, now=now))
        self.assertTrue(queue_needs_review({}, now=now))

    def test_explicit_confirmation_is_separate_from_original_creation(self):
        now = datetime.now(timezone.utc)
        job = {"created_at":"2000-01-01T00:00:00+00:00", "phone_queue_confirmed_at":now.isoformat()}
        self.assertFalse(queue_needs_review(job, now=now))
        job["phone_queue_review_required"] = True
        self.assertTrue(queue_needs_review(job, now=now))


class QueueReviewTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp(prefix='phone-queue-review-'))
        (root/'queue').mkdir()
        (root/'calling').mkdir()
        return root

    async def test_empty_queue_reconciles_exact_disconnected_guard_without_dial(self):
        root = self.fixture()
        guard = root/'phone-line-unconfirmed.json'
        guard.write_text(json.dumps({'call_started_at': 1234, 'job_id': 'failed-call',
                                     'system_call_uuid': 'known-call'}))
        daemon = phone_agent.PhoneDaemon({})
        daemon._place_job = AsyncMock()
        with patch.object(phone_agent, 'STATE_DIR', root), \
             patch.object(phone_agent, 'QUEUE_DIR', root/'queue'), \
             patch.object(phone_agent, 'load_config', return_value={}), \
             patch.object(phone_agent.IPhoneDialer, '_system_call_state_since', return_value='disconnected') as probe, \
             patch.object(phone_agent.asyncio, 'sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await daemon._queue_worker()
        probe.assert_called_once_with(1234, 'known-call')
        self.assertFalse(guard.exists())
        daemon._place_job.assert_not_awaited()

    async def test_idle_reconciliation_is_rate_limited_and_retains_unknown_call(self):
        root = self.fixture()
        guard = root/'phone-line-unconfirmed.json'
        guard.write_text(json.dumps({'call_started_at': 1234, 'job_id': 'failed-call'}))
        daemon = phone_agent.PhoneDaemon({})
        with patch.object(phone_agent, 'STATE_DIR', root), \
             patch.object(phone_agent, 'QUEUE_DIR', root/'queue'), \
             patch.object(phone_agent, 'load_config', return_value={}), \
             patch.object(phone_agent.IPhoneDialer, '_system_call_state_since', return_value='unknown') as probe, \
             patch.object(phone_agent.asyncio, 'sleep', new=AsyncMock(side_effect=[None, None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await daemon._queue_worker()
        probe.assert_called_once()
        self.assertTrue(guard.exists())

    async def test_hangup_result_never_releases_replacement_guard(self):
        root = self.fixture()
        guard = root/'phone-line-unconfirmed.json'
        guard.write_text(json.dumps({'call_started_at': 1234, 'job_id': 'old-call'}))
        replacement = {'call_started_at': 4567, 'job_id': 'new-call'}
        def probe(*args):
            guard.write_text(json.dumps(replacement))
            return 'disconnected'
        with patch.object(phone_agent, 'STATE_DIR', root), \
             patch.object(phone_agent.IPhoneDialer, '_system_call_state_since', side_effect=probe):
            self.assertTrue(await phone_agent.PhoneDaemon({})._phone_line_unconfirmed())
        self.assertEqual(json.loads(guard.read_text()), replacement)

    async def test_stale_job_is_retained_and_does_not_block_fresh_job(self):
        root = self.fixture()
        stale = root/'queue/old-turn.json'
        fresh = root/'queue/new-turn.json'
        stale.write_text(json.dumps({'turn_id':'old-turn','thread_id':'test-source',
                                    'created_at':'2000-01-01T00:00:00+00:00'}))
        fresh.write_text(json.dumps({'turn_id':'new-turn','thread_id':'test-source',
                                    'created_at':datetime.now(timezone.utc).isoformat()}))
        daemon = phone_agent.PhoneDaemon({}, once=True)
        daemon._place_job = AsyncMock()
        with patch.object(phone_agent, 'STATE_DIR', root), patch.object(phone_agent, 'QUEUE_DIR',root/'queue'), \
                patch.object(phone_agent,'ACTIVE_CALL_PATH',root/'active-call.json'), \
                patch.object(phone_agent,'load_config',return_value={}), \
                patch.object(daemon,'call_authorized',return_value=True):
            await asyncio.wait_for(daemon._queue_worker(), 1)
        daemon._place_job.assert_awaited_once()
        self.assertEqual(daemon._place_job.await_args.args[0], fresh)
        self.assertTrue(json.loads(stale.read_text())['phone_queue_review_required'])
        self.assertEqual(recent_status(root)[0]['phase'],'needs_review')

    async def test_explicit_release_binds_current_source_and_consumes_one_completion(self):
        root = self.fixture()
        path = root/'queue/old-turn.json'
        original = {'turn_id':'old-turn','thread_id':'test-source','created_at':'2000-01-01T00:00:00+00:00',
                    'phone_queue_review_required':True}
        path.write_text(json.dumps(original))
        with patch.object(phone_agent,'STATE_DIR',root), patch.object(phone_agent,'QUEUE_DIR',root/'queue'), \
                patch.object(phone_agent,'CALLING_DIR',root/'calling'), \
                patch.object(phone_agent,'ACTIVE_CALL_PATH',root/'active-call.json'), \
                patch.object(phone_agent,'current_thread_id',return_value='test-source'), \
                patch.object(phone_agent,'is_session_enabled',return_value=True), \
                patch.object(phone_agent.hook_stop,'current_root_turn_id',return_value='current-root'), \
                patch.object(phone_agent.hook_stop,'stage_skip_call') as guard:
            phone_agent.confirm_queued_report('old-turn')
            guard.assert_called_once_with('test-source', turn_id='current-root', manual_call=True)
            with self.assertRaises(RuntimeError): phone_agent.confirm_queued_report('old-turn')
        result = json.loads(path.read_text())
        self.assertEqual(result['created_at'], original['created_at'])
        self.assertFalse(result['phone_queue_review_required'])
        self.assertFalse(queue_needs_review(result))


class ProviderSettlementTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp(prefix='phone-provider-settlement-'))
        for name in ('queue', 'calling', 'done', 'failed'):
            (root/name).mkdir()
        return root

    def isolated(self, root):
        stack = ExitStack()
        stack.enter_context(patch.object(phone_agent, 'STATE_DIR', root))
        for name in ('QUEUE', 'CALLING', 'DONE', 'FAILED'):
            stack.enter_context(patch.object(phone_agent, name+'_DIR', root/name.lower()))
        return stack

    async def test_classifier_local_connection_failure_has_specific_private_status(self):
        root = self.fixture()
        job = {'job_id':'local-network-failure', 'thread_id':'source',
               'phone_startup_failure': {'stage':'intent_warmup',
                    'reason':'local_address_unavailable', 'dial_attempted':False},
               'phone_intent_warmup': {'local_transport_errno':49},
               'report':'PRIVATE REPORT'}
        daemon = phone_agent.PhoneDaemon({'phone_failure_notifications':True})
        target = root/'failed/local-network-failure.json'
        with self.isolated(root), patch('service_state.desktop_notice') as notice:
            daemon._record_service_result(target, job)
            daemon._record_service_result(target, job)
            row = recent_status(root, 'source')[0]
        self.assertEqual(row['reason'], 'local_address_unavailable')
        self.assertIn('本轮未拨号', row['message'])
        self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertEqual(job['phone_startup_failure']['stage'], 'intent_warmup')
        notice.assert_called_once()

    async def test_other_failure_is_not_relabelled_as_local_network(self):
        root = self.fixture()
        job = {'job_id':'ordinary-timeout', 'thread_id':'source',
               'phone_startup_failure': {'stage':'intent_warmup', 'reason':'timeout'}}
        daemon = phone_agent.PhoneDaemon({})
        with self.isolated(root):
            daemon._record_service_result(root/'failed/ordinary-timeout.json', job)
            self.assertEqual(recent_status(root, 'source')[0]['reason'], 'intent_warmup')

    async def test_account_startup_failures_have_specific_sanitized_terminal_status(self):
        for reason in ('account_read_timeout', 'workspace_routing_timeout'):
            root = self.fixture()
            job = {'job_id':'startup-failure', 'thread_id':'source',
                   'phone_startup_failure': {'stage':'intent_warmup', 'reason':reason,
                        'dial_attempted':False, 'detail':'PRIVATE'}, 'report':'PRIVATE'}
            daemon = phone_agent.PhoneDaemon({'phone_failure_notifications':True})
            with self.isolated(root), patch('service_state.desktop_notice') as notice:
                for _ in range(2):
                    daemon._record_service_result(root/'failed/startup-failure.json', job)
                row = recent_status(root, 'source')[0]
            self.assertEqual(row['reason'], reason)
            self.assertEqual(row['phase'], 'failed')
            self.assertIn('本轮未拨号', row['message'])
            self.assertNotIn('PRIVATE', json.dumps(row))
            notice.assert_called_once()

    async def exercise_iphone(self, failure_stage=''):
        root = self.fixture()
        job = {'job_id':'provider-turn', 'turn_id':'provider-turn',
               'session_id':'provider-source', 'thread_id':'provider-source', 'report':'test report'}
        source = root/'queue/provider-turn.json'
        source.write_text(json.dumps(job))
        record_state(root, job, 'queued')

        async def start():
            record_state(root, job, 'preparing')
            if failure_stage == 'report_preparation':
                job['phone_startup_failure'] = {'stage':failure_stage, 'dial_attempted':False}
                raise RuntimeError('injected preparation failure')

        async def dial():
            record_state(root, job, 'dialing')
            if failure_stage:
                job['phone_dial_failure'] = {'failure_stage':failure_stage}
                raise RuntimeError('injected disconnect before Active')
            record_state(root, job, 'connected')
            return 'completed'

        bridge = SimpleNamespace(start=AsyncMock(side_effect=start), dial_and_wait=AsyncMock(side_effect=dial),
                                 stop=AsyncMock(), dialer=SimpleNamespace(call_started_at=None))
        daemon = phone_agent.PhoneDaemon({'provider':'iphone', 'phone_failure_notifications':True}, once=True)
        with self.isolated(root), patch.object(phone_agent, 'IPhoneVoiceBridge', return_value=bridge), \
                patch('service_state.desktop_notice') as notice:
            await daemon._place_job(source, job)
            row = recent_status(root, 'provider-source')[0]
            self.assertEqual(row['phase'], 'failed' if failure_stage else 'completed')
            self.assertEqual(row['completion_scope'], 'phone_call_only')
            self.assertFalse(source.exists())
            self.assertFalse(list((root/'calling').glob('*.json')))
            target = root/('failed' if failure_stage else 'done')/source.name
            self.assertTrue(target.is_file())
            self.assertTrue(json.loads(target.read_text())['finished_at'])
            if failure_stage:
                notice.assert_called_once()
                self.assertEqual(row['reason'], failure_stage)
                self.assertFalse(row['notification']['visible_verified'])
                daemon._record_service_result(target, job)
                notice.assert_called_once()  # A late publisher cannot notify twice.
            else:
                notice.assert_not_called()
        bridge.start.assert_awaited_once()
        bridge.stop.assert_awaited_once()
        if failure_stage == 'report_preparation':
            bridge.dial_and_wait.assert_not_awaited()
        else:
            bridge.dial_and_wait.assert_awaited_once()

    async def test_iphone_dial_failure_settles_status_and_notifies_once(self):
        await self.exercise_iphone('call_failed')

    async def test_iphone_preparation_failure_settles_without_dial(self):
        await self.exercise_iphone('report_preparation')

    async def test_iphone_correlated_disconnect_keeps_its_specific_stage(self):
        await self.exercise_iphone('local_call_disconnect_request')

    async def test_iphone_completed_call_settles_only_phone_scope(self):
        await self.exercise_iphone()

    async def test_invalid_job_is_failed_and_skipped_job_is_not_a_completed_call(self):
        root = self.fixture()
        daemon = phone_agent.PhoneDaemon({'provider':'iphone'}, once=True)
        with self.isolated(root), patch.object(phone_agent, 'IPhoneVoiceBridge') as bridge:
            invalid = root/'queue/invalid-turn.json'
            invalid.write_text('{}')
            await daemon._place_job(invalid, {})
            row = recent_status(root)[0]
            self.assertEqual((row['phase'], row['reason']), ('failed', 'invalid_job'))
            paused = root/'queue/paused-turn.json'
            job = {'turn_id':'paused-turn', 'thread_id':'provider-source'}
            paused.write_text(json.dumps(job))
            await daemon._finish_without_call(paused, job, 'automatic_callbacks_paused')
            row = recent_status(root)[0]
            self.assertEqual((row['phase'], row['reason']), ('skipped', 'automatic_callbacks_paused'))
            self.assertEqual(record_state(root, job, 'connected')['phase'], 'skipped')
            bridge.assert_not_called()

    async def test_crash_recovery_settles_existing_call_without_redial(self):
        root = self.fixture()
        job = {'turn_id':'recovery-turn', 'thread_id':'provider-source'}
        (root/'calling/recovery-turn.json').write_text(json.dumps(job))
        record_state(root, job, 'dialing')
        daemon = phone_agent.PhoneDaemon({'provider':'iphone'}, once=True)
        with self.isolated(root), patch.object(phone_agent, 'IPhoneVoiceBridge') as bridge:
            daemon._recover_interrupted_calls()
            self.assertEqual(recent_status(root)[0]['phase'], 'failed')
            self.assertTrue((root/'failed/recovery-turn.json').is_file())
            bridge.assert_not_called()
