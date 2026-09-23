from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from phone_stop_mailbox import Scope, _new
from phone_stop_receipt import observe_receipt


class StopReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='stop-receipt-test-'))
        self.scope = Scope('source-0001','root-000001','cycle-00001','call-000001','/project')
        self.hook_config = '/private/test/hooks.json'
        self.marker = 'codex-phone-wait-command:' + 'a'*64
        self.reason = '已分类的测试指令\n' + self.marker + '\n原始话语'
        self.emission = {'scope':asdict(self.scope),'marker':self.marker,
                         'reason_sha256':hashlib.sha256(self.reason.encode()).hexdigest(),
                         'reserved_at':1000.0}
        self.item = {'type':'HookPrompt','id':'actual-input-item', 'fragments':[
            {'text':self.reason,'hookRunId':'stop:0:' + self.hook_config}]}
        self.rows = [
            {'type':'session_meta','payload':{'id':self.scope.source_thread_id}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':self.scope.root_turn_id}},
            {'timestamp':datetime.fromtimestamp(1001,timezone.utc).isoformat(), 'type':'event_msg',
             'payload':{'type':'item_completed','thread_id':self.scope.source_thread_id,
                        'turn_id':self.scope.root_turn_id,'item':self.item}},
        ]

    def inspect(self, rows=None):
        # Append-only fixture creation; no recursive cleanup or old-file overwrite.
        path = self.directory / ('rollout-' + str(len(list(self.directory.iterdir()))) + '.jsonl')
        with path.open('x', encoding='utf-8') as stream:
            for row in self.rows if rows is None else rows:
                stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        return observe_receipt(path,self.scope,self.emission,hook_config=self.hook_config)

    def test_exact_hook_prompt_and_old_root_lifecycle_only_proves_delivery(self):
        result = self.inspect()
        self.assertTrue(result['received_by_codex'])
        self.assertFalse(result['processing_verified'])
        self.assertFalse(result['task_completed_successfully'])

    def test_plain_user_message_tool_output_and_response_copy_not_receipts(self):
        for kind in ('UserMessage','FunctionCallOutput','CommandExecution','AgentMessage'):
            rows = deepcopy(self.rows)
            rows[2]['payload']['item']['type'] = kind
            self.assertFalse(self.inspect(rows)['received_by_codex'])
        rows = deepcopy(self.rows)
        rows[2]['type']='response_item'
        self.assertFalse(self.inspect(rows)['received_by_codex'])

    def test_quoted_or_modified_reason_rejected(self):
        for text in ('引用资料\n'+self.reason, self.reason+'\n多余命令', self.reason.replace('原始话语','不同话语')):
            rows=deepcopy(self.rows)
            rows[2]['payload']['item']['fragments'][0]['text']=text
            self.assertFalse(self.inspect(rows)['received_by_codex'])

    def test_foreign_source_root_or_hook_rejected(self):
        for field in ('thread_id','turn_id'):
            rows=deepcopy(self.rows)
            rows[2]['payload'][field]='another-id'
            self.assertFalse(self.inspect(rows)['received_by_codex'])
        for run in ('postToolUse:0:'+self.hook_config,'stop:0:/another/hooks.json'):
            rows=deepcopy(self.rows)
            rows[2]['payload']['item']['fragments'][0]['hookRunId']=run
            self.assertFalse(self.inspect(rows)['received_by_codex'])

    def test_stale_input_does_not_confirm_new_offer(self):
        self.rows[2]['timestamp']=datetime.fromtimestamp(999,timezone.utc).isoformat()
        self.assertFalse(self.inspect()['received_by_codex'])

    def test_duplicate_matching_input_is_ambiguous(self):
        self.rows.append(deepcopy(self.rows[2]))
        self.assertFalse(self.inspect()['received_by_codex'])

    def test_received_without_started_lifecycle_is_not_processing(self):
        result=self.inspect([self.rows[0],self.rows[2]])
        self.assertTrue(result['received_by_codex'])
        self.assertFalse(result['processing_verified'])

    def test_aborted_source_never_claims_started(self):
        self.rows.append({'type':'event_msg','payload':{'type':'turn_aborted','turn_id':self.scope.root_turn_id}})
        self.assertFalse(self.inspect()['processing_verified'])

    def test_completed_root_not_successful_task_claim(self):
        self.rows.append({'type':'event_msg','payload':{'type':'task_complete','turn_id':self.scope.root_turn_id}})
        result=self.inspect()
        self.assertFalse(result['processing_verified'])
        self.assertFalse(result['task_completed_successfully'])

    def test_header_mismatch_blocks_receipt(self):
        self.rows[0]['payload']['id']='other-source'
        self.assertFalse(self.inspect()['received_by_codex'])

    def test_uncertain_emission_fields_rejected(self):
        for field in ('scope','reserved_at','reason_sha256','marker'):
            saved=self.emission.pop(field)
            self.assertFalse(self.inspect()['received_by_codex'])
            self.emission[field]=saved

    def activity(self, kind='CommandExecution', event='item_started'):
        return {'timestamp':datetime.fromtimestamp(1002,timezone.utc).isoformat(), 'type':'event_msg',
                'payload':{'type':event,'thread_id':self.scope.source_thread_id,
                           'turn_id':self.scope.root_turn_id,'item':{'type':kind,'id':'new-model-action'}}}

    def test_new_processing_after_exact_input(self):
        self.rows.append(self.activity())
        self.assertTrue(self.inspect()['processing_verified'])

    def test_old_tool_completion_is_not_new_processing(self):
        self.rows.append(self.activity(event='item_completed'))
        self.assertFalse(self.inspect()['processing_verified'])

    def completed_activity(self, kind='AgentMessage'):
        row = self.activity(kind, event='item_completed')
        row['payload'].update(started_at_ms=1001200, completed_at_ms=1001900)
        return row

    def test_current_host_completion_with_new_start_certifies_processing(self):
        for kind in ('AgentMessage', 'CommandExecution', 'FileChange'):
            with self.subTest(kind=kind):
                result = self.inspect([*self.rows, self.completed_activity(kind)])
                self.assertTrue(result['received_by_codex'])
                self.assertTrue(result['processing_verified'])
                self.assertFalse(result['task_completed_successfully'])

    def test_completed_old_work_invalid_times_and_missing_times_fail_closed(self):
        for start, end in ((1000000,1001900), (True,1001900), (1001200,False),
                           (float('nan'),1001900), (1001200,float('inf')),
                           (1001900,1001200), (1001200,1003000),
                           ('1001200',1001900), (None,1001900), (1001200,None)):
            with self.subTest(start=start,end=end):
                row = self.completed_activity()
                row['payload'].update(started_at_ms=start, completed_at_ms=end)
                self.assertFalse(self.inspect([*self.rows,row])['processing_verified'])

    def test_reused_item_id_cannot_claim_new_processing(self):
        earlier = self.activity()
        earlier['timestamp'] = datetime.fromtimestamp(1000.5,timezone.utc).isoformat()
        rows = [*self.rows[:2],earlier,self.rows[2],self.completed_activity()]
        self.assertFalse(self.inspect(rows)['processing_verified'])

    def test_completed_work_after_other_user_input_is_not_this_command(self):
        rows = [*self.rows,self.activity('UserMessage','item_completed'),self.completed_activity()]
        self.assertFalse(self.inspect(rows)['processing_verified'])

    def test_completed_work_with_wrong_source_or_root_is_ineligible(self):
        for field in ('thread_id','turn_id'):
            row = self.completed_activity()
            row['payload'][field] = 'another-task'
            self.assertFalse(self.inspect([*self.rows,row])['processing_verified'])

    def test_completion_times_inside_item_are_not_trusted_envelope(self):
        row = self.completed_activity()
        for field in ('started_at_ms','completed_at_ms'):
            row['payload']['item'][field] = row['payload'].pop(field)
        self.assertFalse(self.inspect([*self.rows,row])['processing_verified'])

    def test_out_of_order_timestamp_cannot_certify_processing_before_input(self):
        activity = self.activity()
        activity['timestamp'] = datetime.fromtimestamp(1000.5, timezone.utc).isoformat()
        self.rows.append(activity)
        self.assertFalse(self.inspect()['processing_verified'])

    def test_intervening_new_input_cannot_certify_old_command_started(self):
        self.rows.extend([self.activity('UserMessage','item_completed'),self.activity()])
        self.assertFalse(self.inspect()['processing_verified'])


if __name__=='__main__':
    unittest.main()
