from __future__ import annotations

import json
import os
import io
import shutil
import subprocess
import sys
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hook_stop
from hook_stop import (
    DEFAULT_SPOKEN_REPORT,
    _active_call_running,
    _consume_staged_report,
    _extract_phone_copy,
    _useful_report,
    queue_completion_event,
    stage_phone_report,
    stage_skip_call,
)


class HookStopTests(unittest.TestCase):
    def test_notice_library_failure_is_not_reported_as_bad_opening(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            self.failure_state(directory, stack)
            evidence = directory/'preparations/failed-turn.json'
            evidence.parent.mkdir()
            evidence.write_text(json.dumps({'source_thread_id': 'source', 'turn_id': 'failed-turn',
                'passed': False, 'failure_code': 'native_notice_library_incomplete',
                'missing_notices': ['固定提示']}))
            hook_stop.record_preparation_failure('source', turn_id='failed-turn')
            failed = json.loads((directory/'failed/failed-turn.json').read_text())
            status = json.loads((directory/'service-status/failed-turn.json').read_text())
            self.assertEqual(failed['phone_startup_failure']['stage'], 'notice_library')
            self.assertEqual(status['reason'], 'native_notice_library_incomplete')
            self.assertNotIn('开场', status['message'])
            self.assertFalse(hook_stop.record_preparation_failure('source', turn_id='failed-turn'))

    def failure_state(self, directory, stack):
        for name, folder in [('QUEUE_DIR','queue'), ('CALLING_DIR','calling'),
                             ('DONE_DIR','done'), ('FAILED_DIR','failed'), ('STAGED_REPORT_DIR','staged')]:
            stack.enter_context(patch.object(hook_stop, name, directory/folder))
        config = directory/'config.json'
        config.write_text('{"enabled": true}')
        stack.enter_context(patch.object(hook_stop, 'CONFIG_PATH', config))
        stack.enter_context(patch.object(hook_stop, 'is_session_enabled', return_value=True))

    def test_native_preparation_failure_blocks_both_observers_not_next_turn(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            self.failure_state(directory, stack)
            self.assertTrue(hook_stop.record_preparation_failure('source', turn_id='failed-turn'))
            event = {'session_id':'source', 'thread_id':'source', 'turn_id':'failed-turn',
                     'last_assistant_message':'修复工作已结束，但语音准备失败。'}
            self.assertFalse(queue_completion_event(event, daemon_status=(True,'ready')))
            self.assertFalse(queue_completion_event(event, daemon_status=(True,'ready')))
            self.assertFalse(list((directory/'queue').glob('*.json')))
            failed = json.loads((directory/'failed/failed-turn.json').read_text())
            self.assertEqual(failed['outcome'], 'failed: native_audio_preparation')
            self.assertFalse(failed['phone_startup_failure']['dial_attempted'])
            self.assertNotIn('skipped_by_user', failed['outcome'])
            event['turn_id'] = 'next-turn'
            self.assertTrue(queue_completion_event(event, daemon_status=(True,'ready')))

    def test_rejected_evidence_binds_exact_source_and_root(self):
        for evidence_source in ('source', 'other-source'):
            with self.subTest(source=evidence_source), ExitStack() as stack:
                directory = Path(tempfile.mkdtemp(prefix='phone-evidence-test-'))
                self.failure_state(directory, stack)
                evidence = directory/'preparations/failed-turn.json'
                evidence.parent.mkdir()
                evidence.write_text(json.dumps({'source_thread_id':evidence_source,
                    'turn_id':'failed-turn','passed':False,'failure_code':'realtime_quota_exhausted'}))
                hook_stop.record_preparation_failure('source', turn_id='failed-turn')
                failed = json.loads((directory/'failed/failed-turn.json').read_text())
                if evidence_source == 'source':
                    self.assertEqual(failed['preparation_evidence'], str(evidence))
                    self.assertEqual(failed['phone_startup_failure']['reason'], 'realtime_quota_exhausted')
                else:
                    self.assertNotIn('preparation_evidence', failed)
                    self.assertEqual(failed['phone_startup_failure']['reason'], 'native_audio_preparation_failed')

    def test_failed_preparation_cannot_be_overwritten_by_later_staging(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            self.failure_state(directory, stack)
            hook_stop.record_preparation_failure('source', turn_id='failed-turn')
            self.assertFalse(hook_stop.record_preparation_failure('source', turn_id='failed-turn'))
            with self.assertRaisesRegex(ValueError, '已有记录'):
                stage_phone_report('source', '继续准备。', turn_id='failed-turn')

    def test_failed_preparation_preserves_manual_call_guard(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            self.failure_state(directory, stack)
            stage_skip_call('source', manual_call=True, turn_id='manual-turn')
            self.assertFalse(hook_stop.record_preparation_failure('source', turn_id='manual-turn'))
            self.assertFalse(list((directory/'failed').glob('*.json')))
            stored = json.loads(hook_stop._staged_report_path('source',turn_id='manual-turn').read_text())
            self.assertTrue(stored['manual_call'])
            directive = hook_stop._consume_staged_directive('source','source',turn_id='manual-turn')
            self.assertTrue(directive['skip_call'])

    def test_failed_preparation_refuses_unsubscribed_source(self):
        with patch.object(hook_stop, 'is_session_enabled', return_value=False):
            with self.assertRaises(ValueError):
                hook_stop.record_preparation_failure('source', turn_id='turn')

    def test_turn_scoped_manual_guard_survives_long_task_but_not_next_turn(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(hook_stop,'is_session_enabled',return_value=True):
            directory = Path(temporary)
            with patch.object(hook_stop.time,'time',return_value=100):
                stage_skip_call('source-thread',directory=directory,manual_call=True,turn_id='turn-one')
                stage_phone_report('source-thread','检查已完成。',directory=directory,turn_id='turn-one')
            self.assertEqual(hook_stop._consume_staged_directive('source-thread','source-thread',directory=directory,turn_id='turn-two',now=4000),{})
            self.assertTrue(hook_stop._consume_staged_directive('source-thread','source-thread',directory=directory,turn_id='turn-one',now=4000)['skip_call'])
            self.assertEqual(hook_stop._consume_staged_directive('source-thread','source-thread',directory=directory,turn_id='turn-one',now=4000),{})

    def test_root_turn_metadata_resolution_and_completed_boundary(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'CODEX_TURN_ID':''}):
            directory = Path(temporary)
            path = directory/'rollout-source-thread.jsonl'
            record = {'type':'turn_context','payload':{'turn_id':'turn-active'}}
            path.write_text(json.dumps(record)+'\n'+json.dumps({'type':'response_item','payload':{'text':'a'*100000}})+'\n')
            self.assertEqual(hook_stop.current_root_turn_id('source-thread',sessions_dir=directory),'turn-active')
            with path.open('a') as stream:
                stream.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-active'}})+'\n')
            with self.assertRaises(ValueError):
                hook_stop.current_root_turn_id('source-thread',sessions_dir=directory)

    def test_rejects_internal_json_payloads(self) -> None:
        self.assertFalse(_useful_report('{"suggestions": []}'))
        self.assertFalse(_useful_report('[{"type": "tool"}]'))
        self.assertTrue(_useful_report("任务已完成，产物已保存。"))

    def test_active_call_lock_requires_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "active-call.json"
            path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            self.assertTrue(_active_call_running(path))
            path.write_text(json.dumps({"pid": 999_999_999}), encoding="utf-8")
            self.assertFalse(_active_call_running(path))

    def test_extracts_hidden_phone_copy_without_reading_visible_reply(self) -> None:
        visible, spoken = _extract_phone_copy(
            "已完成修改，详细测试见下方。\n"
            "<!-- codex-phone-report: 延迟和截断都修好了，电话会用一句话汇报。 -->"
        )

        self.assertEqual(visible, "已完成修改，详细测试见下方。")
        self.assertEqual(spoken, "延迟和截断都修好了，电话会用一句话汇报。")

    def test_missing_or_overlong_phone_copy_uses_complete_generic_sentence(self) -> None:
        self.assertEqual(_extract_phone_copy("很长的窗口结果")[1], DEFAULT_SPOKEN_REPORT)
        _, spoken = _extract_phone_copy(
            "结果。<!-- codex-phone-report: " + "已经处理完成" * 20 + " -->"
        )
        self.assertEqual(spoken, DEFAULT_SPOKEN_REPORT)

    def test_unsubscribed_session_never_queues_a_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            queue_dir = Path(temporary) / "queue"
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-12345678",
                "last_assistant_message": (
                    "任务已完成。"
                    "<!-- codex-phone-report: 文件已经整理好，可以继续安排。 -->"
                ),
            }
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "_load_json", return_value={"enabled": True}),
                patch.object(hook_stop, "is_session_enabled", return_value=False),
                patch("sys.stdin", io.StringIO(json.dumps(event))),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(hook_stop.main(), 0)

            self.assertFalse(queue_dir.exists())

    def test_staged_report_is_invisible_and_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "staged-reports"
            with patch.object(hook_stop, "is_session_enabled", return_value=True):
                spoken = stage_phone_report(
                    "thread-12345678",
                    "误拦截问题已经修好，本轮会正常拨打。",
                    directory=directory,
                )

            self.assertEqual(spoken, "误拦截问题已经修好，本轮会正常拨打。")
            staged_text = next(directory.glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("codex-phone-report", staged_text)
            self.assertEqual(
                _consume_staged_report(
                    "thread-12345678",
                    "thread-12345678",
                    directory=directory,
                ),
                spoken,
            )
            self.assertEqual(
                _consume_staged_report(
                    "thread-12345678",
                    "thread-12345678",
                    directory=directory,
                ),
                "",
            )

    def test_one_turn_skip_is_persistent_and_does_not_disable_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            calling_dir = root / "calling"
            done_dir = root / "done"
            failed_dir = root / "failed"
            staged_dir = root / "staged-reports"
            config_path = root / "config.json"
            config_path.write_text('{"enabled": true}\n', encoding="utf-8")
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-silently-finished",
                "last_assistant_message": "本轮修复已经完成。",
            }
            with patch.object(hook_stop, "is_session_enabled", return_value=True):
                stage_skip_call(
                    "thread-12345678",
                    directory=staged_dir,
                )
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "CALLING_DIR", calling_dir),
                patch.object(hook_stop, "DONE_DIR", done_dir),
                patch.object(hook_stop, "FAILED_DIR", failed_dir),
                patch.object(hook_stop, "STAGED_REPORT_DIR", staged_dir),
                patch.object(hook_stop, "CONFIG_PATH", config_path),
                patch.object(hook_stop, "is_session_enabled", return_value=True),
            ):
                self.assertFalse(
                    queue_completion_event(event, daemon_status=(True, "stop_hook"))
                )
                self.assertFalse(
                    queue_completion_event(
                        event, daemon_status=(True, "rollout_completion_fallback")
                    )
                )

            self.assertFalse(queue_dir.exists())
            self.assertFalse(failed_dir.exists())
            self.assertFalse(list(staged_dir.glob("*.json")))
            records = list(done_dir.glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(
                record["outcome"], "skipped_by_user_for_this_completion"
            )
            status = json.loads((root / 'service-status' / records[0].name).read_text())
            self.assertEqual(status['phase'], 'skipped')
            self.assertEqual(status['reason'], 'skipped_by_user_for_this_completion')
            self.assertEqual(status['source_thread_id'], event['thread_id'])
            self.assertTrue(status['message'])

    def test_skip_status_is_durable_private_and_never_notifies_or_starts_daemon(self):
        from service_state import record_state
        with ExitStack() as stack:
            directory = Path(tempfile.mkdtemp(prefix='phone-skip-publication-test-'))
            self.failure_state(directory, stack)
            (directory/'config.json').write_text('{"enabled":true,"phone_failure_notifications":true}')
            ensure = stack.enter_context(patch.object(hook_stop, '_ensure_background_daemon'))
            notice = stack.enter_context(patch('service_state.desktop_notice'))
            event = {'session_id':'source', 'thread_id':'source', 'turn_id':'skipped-root',
                     'cwd':'/private-project', 'last_assistant_message':'private task result'}
            def durable_before_status(state, job, phase, **kwargs):
                self.assertTrue((directory/'done/skipped-root.json').is_file())
                self.assertFalse((directory/'queue/skipped-root.json').exists())
                return record_state(state, job, phase, **kwargs)
            publish = stack.enter_context(patch.object(hook_stop, 'record_state', side_effect=durable_before_status))
            stage_skip_call('source', turn_id='skipped-root')
            self.assertFalse(queue_completion_event(event))
            target = directory/'service-status/skipped-root.json'
            first = target.read_bytes()
            self.assertFalse(queue_completion_event(event))
            self.assertEqual(target.read_bytes(), first)
            publish.assert_called_once()
            ensure.assert_not_called()
            notice.assert_not_called()
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            for private in (b'private task result', b'/private-project'):
                self.assertNotIn(private, first)

    def test_manual_call_guard_is_not_reported_as_user_skip_or_connected(self):
        with ExitStack() as stack:
            directory = Path(tempfile.mkdtemp(prefix='phone-manual-status-test-'))
            self.failure_state(directory, stack)
            stage_skip_call('source', turn_id='manual-root', manual_call=True)
            event = {'session_id':'source', 'thread_id':'source', 'turn_id':'manual-root',
                     'last_assistant_message':'手动电话已安排。'}
            self.assertFalse(queue_completion_event(event, daemon_status=(True,'unused')))
            primary = json.loads((directory/'done/manual-root.json').read_text())
            status = json.loads((directory/'service-status/manual-root.json').read_text())
            self.assertEqual(primary['outcome'], 'skipped_after_manual_call_guard')
            self.assertEqual((status['phase'], status['reason']), ('skipped', 'manual_call_guard'))
            self.assertNotIn('已接通', status['message'])
            self.assertNotIn('用户要求', status['message'])
            self.assertEqual(status['completion_scope'], 'phone_call_only')

    def test_second_observer_repairs_only_status_after_io_failure_without_redial(self):
        from service_state import record_state
        with ExitStack() as stack:
            directory = Path(tempfile.mkdtemp(prefix='phone-skip-repair-test-'))
            self.failure_state(directory, stack)
            ensure = stack.enter_context(patch.object(hook_stop, '_ensure_background_daemon'))
            stage_skip_call('source', turn_id='skipped-root')
            event = {'session_id':'source', 'thread_id':'source', 'turn_id':'skipped-root',
                     'last_assistant_message':'本轮不要打电话。'}
            with patch.object(hook_stop, 'record_state', side_effect=OSError('status unavailable')):
                self.assertFalse(queue_completion_event(event))
            primary = (directory/'done/skipped-root.json').read_bytes()
            self.assertFalse((directory/'service-status/skipped-root.json').exists())
            with patch.object(hook_stop, 'record_state', wraps=record_state) as publish:
                self.assertFalse(queue_completion_event(event))
                publish.assert_called_once()
            self.assertEqual((directory/'done/skipped-root.json').read_bytes(), primary)
            self.assertEqual(json.loads((directory/'service-status/skipped-root.json').read_text())['phase'], 'skipped')
            self.assertFalse(list((directory/'queue').glob('*.json')))
            ensure.assert_not_called()
            self.assertTrue(queue_completion_event({**event,'turn_id':'next-root'}, daemon_status=(True,'fake-ready')))

    def test_status_repair_does_not_reinterpret_other_source_or_other_call_result(self):
        cases = ({'thread_id':'other'}, {'session_id':'other'}, {'turn_id':'other'},
                 {'outcome':'completed'}, {'outcome':'failed: report_preparation'},
                 {'session_subscription':False})
        for change in cases:
            with self.subTest(change=change), ExitStack() as stack:
                directory = Path(tempfile.mkdtemp(prefix='phone-skip-scope-test-'))
                self.failure_state(directory, stack)
                (directory/'done').mkdir()
                primary = {'session_id':'source','thread_id':'source','turn_id':'skipped-root',
                    'outcome':'skipped_by_user_for_this_completion','session_subscription':True, **change}
                (directory/'done/skipped-root.json').write_text(json.dumps(primary))
                with patch.object(hook_stop, 'record_state') as publish:
                    self.assertFalse(queue_completion_event({'session_id':'source','thread_id':'source',
                        'turn_id':'skipped-root','last_assistant_message':'任务检查结束。'}))
                    publish.assert_not_called()

    def test_status_repair_preserves_any_existing_result(self):
        with ExitStack() as stack:
            directory = Path(tempfile.mkdtemp(prefix='phone-skip-existing-test-'))
            self.failure_state(directory, stack)
            stage_skip_call('source', turn_id='skipped-root')
            event = {'session_id':'source','thread_id':'source','turn_id':'skipped-root',
                     'last_assistant_message':'本轮不要打电话。'}
            queue_completion_event(event, daemon_status=(True,'unused'))
            path = directory/'service-status/skipped-root.json'
            for previous in ({'phase':'failed'}, {'phase':'connected'}, {'phase':'skipped','reason':'manual_call_guard'}):
                path.write_text(json.dumps(previous))
                before = path.read_bytes()
                with patch.object(hook_stop, 'record_state') as publish:
                    self.assertFalse(queue_completion_event(event))
                    publish.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_every_later_completion_queues_after_a_failed_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            staged_dir = root / "staged-reports"
            config_path = root / "config.json"
            config_path.write_text('{"enabled": true}\n', encoding="utf-8")
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-after-failed-call",
                "last_assistant_message": "失败解释已完成，下一轮仍应呼叫。",
            }
            with patch.object(hook_stop, "is_session_enabled", return_value=True):
                stage_phone_report(
                    "thread-12345678",
                    "已纠正任务结束后漏打电话的问题。",
                    directory=staged_dir,
                )
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "STAGED_REPORT_DIR", staged_dir),
                patch.object(hook_stop, "CONFIG_PATH", config_path),
                patch.object(hook_stop, "is_session_enabled", return_value=True),
                patch.object(hook_stop, "_active_call_running", return_value=False),
                patch.object(
                    hook_stop,
                    "_ensure_background_daemon",
                    return_value=(True, "ready"),
                ),
                patch("sys.stdin", io.StringIO(json.dumps(event))),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(hook_stop.main(), 0)

            jobs = list(queue_dir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual(
                job["spoken_report"],
                "已纠正任务结束后漏打电话的问题。",
            )

    def test_stop_hook_and_rollout_fallback_deduplicate_persistently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            staged_dir = root / "staged-reports"
            config_path = root / "config.json"
            config_path.write_text('{"enabled": true}\n', encoding="utf-8")
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-deduplicated",
                "last_assistant_message": "本轮修复已经完成。",
            }
            with patch.object(hook_stop, "is_session_enabled", return_value=True):
                stage_phone_report(
                    "thread-12345678",
                    "漏拨兜底已经修复，本轮只拨一次。",
                    directory=staged_dir,
                )
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "CALLING_DIR", root / "calling"),
                patch.object(hook_stop, "DONE_DIR", root / "done"),
                patch.object(hook_stop, "FAILED_DIR", root / "failed"),
                patch.object(hook_stop, "STAGED_REPORT_DIR", staged_dir),
                patch.object(hook_stop, "CONFIG_PATH", config_path),
                patch.object(hook_stop, "is_session_enabled", return_value=True),
            ):
                self.assertTrue(
                    queue_completion_event(
                        event, daemon_status=(True, "rollout_completion_fallback")
                    )
                )
                self.assertFalse(
                    queue_completion_event(event, daemon_status=(True, "stop_hook"))
                )

            jobs = list(queue_dir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual(job["spoken_report"], "漏拨兜底已经修复，本轮只拨一次。")
            self.assertFalse(list(staged_dir.glob("*.json")))

    def test_subscribed_completion_queues_even_during_previous_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            queue_dir = Path(temporary) / "queue"
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-12345678",
                "cwd": "/tmp/project",
                "last_assistant_message": (
                    "任务已完成。"
                    "<!-- codex-phone-report: 文件已经整理好，可以继续安排。 -->"
                ),
            }
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "_load_json", return_value={"enabled": True}),
                patch.object(hook_stop, "is_session_enabled", return_value=True),
                patch.object(hook_stop, "_active_call_running", return_value=True),
                patch.object(
                    hook_stop,
                    "_ensure_background_daemon",
                    return_value=(True, "ready"),
                ),
                patch("sys.stdin", io.StringIO(json.dumps(event))),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(hook_stop.main(), 0)

            jobs = list(queue_dir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertIs(job["session_subscription"], True)
            self.assertIs(job["queued_during_call"], True)
            self.assertEqual(job["command_transport"], "codex_app_send_message")
            self.assertIs(job["daemon_ready_at_queue_time"], True)
            self.assertEqual(job["report"], "任务已完成。")
            self.assertEqual(job["spoken_report"], "文件已经整理好，可以继续安排。")

    def test_daemon_start_failure_is_recorded_without_a_delayed_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_dir = root / "queue"
            failed_dir = root / "failed"
            event = {
                "session_id": "thread-12345678",
                "thread_id": "thread-12345678",
                "turn_id": "turn-failed-daemon",
                "last_assistant_message": "任务已完成。",
            }
            with (
                patch.object(hook_stop, "QUEUE_DIR", queue_dir),
                patch.object(hook_stop, "FAILED_DIR", failed_dir),
                patch.object(hook_stop, "_load_json", return_value={"enabled": True}),
                patch.object(hook_stop, "is_session_enabled", return_value=True),
                patch.object(
                    hook_stop,
                    "_ensure_background_daemon",
                    return_value=(False, "permission missing"),
                ),
                patch("sys.stdin", io.StringIO(json.dumps(event))),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(hook_stop.main(), 0)

            self.assertFalse(queue_dir.exists())
            jobs = list(failed_dir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertIs(job["daemon_ready_at_queue_time"], False)
            self.assertEqual(job["outcome"], "failed: background_daemon_unavailable")


class VersionedHookTests(unittest.TestCase):
    def setUp(self):
        # Retain exact temporary fixtures instead of recursively deleting them.
        self.base = Path(tempfile.mkdtemp(prefix='phone-hook-activation-test-')).resolve()
        self.old = self.base / 'versions' / 'old'
        self.current = self.base / 'versions' / 'current'
        for directory in (self.old, self.current):
            directory.mkdir(parents=True)
            shutil.copy2(hook_stop.__file__, directory / 'hook_stop.py')
            (directory / 'phone_agent.py').write_text('raise AssertionError("must not start daemon")\n')
            (directory / 'session_registry.py').write_text('def is_session_enabled(*a): return False\n')
            (directory / 'service_state.py').write_text('def record_state(*a, **k): raise AssertionError("no state writes")\n')
        self.stable = self.base / 'runtime'
        self.stable.symlink_to(self.current, target_is_directory=True)

    def test_cached_entry_executes_current_version_without_daemon_or_queue(self):
        result = subprocess.run([sys.executable, '-B', str(self.old / 'hook_stop.py'), 'inspect-runtime'],
                                capture_output=True, text=True, check=True, timeout=5)
        status = json.loads(result.stdout)
        self.assertEqual(status['runtime'], str(self.current))
        self.assertEqual(status['active_runtime'], str(self.current))
        self.assertFalse(status['dial_attempted'])

    def test_forward_preserves_original_stdin_and_arguments_once(self):
        (self.current / 'hook_stop.py').write_text(
            'import json, sys\nprint(json.dumps({"args":sys.argv[1:], "event":json.load(sys.stdin)}))\n')
        event = {'thread_id':'synthetic-source', 'turn_id':'one-root', 'text':'原始汇报'}
        result = subprocess.run([sys.executable, '-B', str(self.old / 'hook_stop.py'), 'one', 'two'],
                                input=json.dumps(event), capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), {'args':['one','two'], 'event':event})
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_old_import_ensures_only_current_daemon(self):
        with patch.object(hook_stop, 'PROJECT_DIR', self.old), \
                patch.object(hook_stop.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'ready'
            self.assertEqual(hook_stop._ensure_background_daemon(), (True, 'ready'))
        self.assertEqual(run.call_args.args[0], [str(self.current / '.venv/bin/python'),
                                               str(self.current / 'phone_agent.py'), 'ensure-daemon'])
        self.assertEqual(run.call_args.kwargs['cwd'], str(self.current))

    def test_missing_or_failed_activation_never_falls_back_to_retired_version(self):
        for failed in (True, False):
            with self.subTest(failed=failed):
                if failed:
                    (self.current / 'installation-failed.json').write_text('{}')
                else:
                    self.stable.unlink()  # One named temporary test link only.
                with patch.object(hook_stop, 'PROJECT_DIR', self.old), \
                        patch.object(hook_stop.subprocess, 'run') as run:
                    ready, detail = hook_stop._ensure_background_daemon()
                self.assertFalse(ready)
                self.assertIn('activation', detail)
                run.assert_not_called()

    def test_external_activation_target_is_rejected(self):
        self.stable.unlink()  # One named temporary test link only.
        self.stable.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'invalid'):
            hook_stop.active_runtime_dir(self.old)

    def test_source_checkout_is_not_redirected_to_unrelated_installation(self):
        checkout = self.base / 'checkout'
        checkout.mkdir()
        self.assertEqual(hook_stop.active_runtime_dir(checkout), checkout)

    def test_hook_command_keeps_stable_link_across_updates(self):
        import phone_agent
        commands = []
        for directory in (self.old, self.current):
            with patch.object(phone_agent, 'PROJECT_DIR', directory):
                commands.append(phone_agent._hook_command())
        self.assertEqual(commands[0], commands[1])
        self.assertIn(str(self.stable / 'hook_stop.py'), commands[0])
        self.assertNotIn('/versions/', commands[0])


if __name__ == "__main__":
    unittest.main()
