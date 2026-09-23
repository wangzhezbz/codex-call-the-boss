import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from phone_stop_dispatch import StopDispatch
from phone_stop_mailbox import MailboxError, Scope, StopMailbox
from phone_turns import CallerTurns


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp(prefix='phone-stop-dispatch-test-'))
        self.box = StopMailbox(self.store)
        self.scope = Scope('source-0001', 'root-000001', 'cycle-00001', 'call-000001', '/project')
        self.owner = self.box.open_window(self.scope)
        self.addCleanup(self.owner.close)
        self.dispatch = StopDispatch(self.box, self.scope)
        self.job = {'thread_id':self.scope.source_thread_id, 'job_id':self.scope.call_id,
                    'source_root_turn_id':self.scope.root_turn_id, 'cwd':self.scope.cwd,
                    'stop_wait_scope':asdict(self.scope)}
        self.turns = CallerTurns()
        self.text = '放两次礼花庆祝一下'
        self.turns.observe('actual-input', self.text, complete=True)
        self.decision = {'kind':'action', 'clarification':''}
        self.event = {'hook_event_name':'Stop', 'session_id':self.scope.source_thread_id,
                      'turn_id':self.scope.root_turn_id, 'cwd':self.scope.cwd, 'stop_hook_active':False}

    async def commit(self):
        self.turns.decide('actual-input', 'action')
        self.assertTrue(await self.turns.commit('actual-input'))

    def offer(self, **kwargs):
        return self.dispatch.offer_committed(self.job, self.turns, 'actual-input', self.decision,
                                            classified_text=kwargs.get('text', self.text))

    async def test_real_commit_to_stop_output_but_not_execution(self):
        await self.commit()
        result = self.offer()
        self.assertFalse(result['delivered_to_model'])
        self.assertFalse(result['execution_confirmed'])
        output = await self.owner.wait(self.event)
        payload = json.loads(output['reason'].splitlines()[-1])
        self.assertEqual(payload['original_caller_words'], self.text)
        self.assertEqual(payload['source_thread_id'], self.scope.source_thread_id)
        self.assertEqual(payload['root_turn_id'], self.scope.root_turn_id)

    async def test_no_commit_no_offer(self):
        with self.assertRaisesRegex(MailboxError, 'input_not_committed'):
            self.offer()
        self.assertEqual(self.owner.reserve(self.event), {})

    async def test_changed_classified_words_rejected(self):
        await self.commit()
        with self.assertRaisesRegex(MailboxError, 'input_not_committed'):
            self.offer(text='识别错了')

    async def test_question_does_not_execute(self):
        self.turns.decide('actual-input', 'question')
        self.assertFalse(await self.turns.commit('actual-input'))
        with self.assertRaises(MailboxError):
            self.offer()

    async def test_cancellation_before_commit_blocks_offer(self):
        self.turns.decide('actual-input', 'action')
        self.turns.observe('cancel-input', '别执行了', complete=True)
        self.turns.decide('cancel-input', 'cancel')
        self.assertFalse(await self.turns.commit('actual-input'))
        with self.assertRaises(MailboxError):
            self.offer()

    async def test_new_speech_after_commit_still_holds_offer(self):
        await self.commit()
        self.turns.observe('later-input', '等等')
        with self.assertRaisesRegex(MailboxError, 'newer_input_unresolved'):
            self.offer()

    async def test_mismatched_job_cannot_read_or_send(self):
        await self.commit()
        for key in ('thread_id', 'job_id', 'source_root_turn_id', 'cwd', 'stop_wait_scope'):
            saved = self.job[key]
            self.job[key] = 'wrong'
            with self.assertRaisesRegex(MailboxError, 'call_binding_mismatch'):
                self.offer()
            self.job[key] = saved

    async def test_closed_owner_cannot_offer(self):
        await self.commit()
        self.owner.close()
        with self.assertRaisesRegex(MailboxError, 'source_not_waiting'):
            self.offer()

    async def test_duplicate_offer_and_output_not_double_send(self):
        await self.commit()
        first = self.offer()
        second = self.offer()
        self.assertEqual(first['command_id'], second['command_id'])
        self.assertEqual(len(self.job['stop_offers']), 1)
        self.assertEqual(second['status'], 'already_offered')
        self.assertTrue(self.owner.reserve(self.event))
        self.assertEqual(self.owner.reserve(self.event), {})

    async def test_context_prepare_rechecks_owner_after_public_read(self):
        async def request(method, params, **kwargs):
            if method == 'thread/read':
                return {'thread':{'id':self.scope.source_thread_id, 'model':'selected',
                                 'modelProvider':'openai', 'cwd':'/project'}}
            self.owner.close('caller_hangup')
            return {'thread':{'id':'temporary-context', 'model':'selected', 'modelProvider':'openai',
                              'forkedFromId':self.scope.source_thread_id, 'ephemeral':True}}
        server = Mock(request=AsyncMock(side_effect=request))
        with self.assertRaisesRegex(MailboxError, 'source_not_waiting'):
            await self.dispatch.prepare_context(self.job, server, lambda _: 'user: verified context')
        self.assertNotIn('command_transport', self.job)

    async def test_reserved_output_alone_is_never_a_started_receipt(self):
        await self.commit()
        result = self.offer()
        self.owner.reserve(self.event)
        observed = self.dispatch.inspect_delivery(self.job, result['command_id'],
            rollout_path=self.store/'absent.jsonl', hook_config='/test/hooks.json')
        self.assertEqual(observed['status'], 'pending_stop_delivery')
        self.assertFalse(observed['delivered_to_model'])
        self.assertFalse(observed['execution_confirmed'])

    async def test_receipt_matches_exact_reserved_reason_in_typed_host_record(self):
        await self.commit()
        result = self.offer()
        output = self.owner.reserve(self.event)
        path = self.store/'synthetic-source.jsonl'
        rows = [
            {'type':'session_meta','payload':{'id':self.scope.source_thread_id}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':self.scope.root_turn_id}},
            {'timestamp':datetime.now(timezone.utc).isoformat(),'type':'event_msg','payload':{
                'type':'item_completed','thread_id':self.scope.source_thread_id,'turn_id':self.scope.root_turn_id,
                'item':{'type':'HookPrompt','id':'synthetic-input','fragments':[
                    {'text':output['reason'],'hookRunId':'stop:0:/test/hooks.json'}]}}}]
        with path.open('x') as stream:
            stream.write(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
        observed = self.dispatch.inspect_delivery(self.job,result['command_id'],
            rollout_path=path,hook_config='/test/hooks.json')
        self.assertEqual(observed['status'],'delivered_execution_unconfirmed')
        self.assertFalse(observed['execution_confirmed'])
        self.assertFalse(observed['evidence']['task_completed_successfully'])


if __name__ == '__main__':
    unittest.main()
