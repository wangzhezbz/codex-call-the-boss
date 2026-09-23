from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import time
import subprocess
import unittest
from datetime import datetime
from types import SimpleNamespace
from array import array
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hook_stop
import phone_agent
from app_tools_relay import AppToolsRelayError
from phone_agent import PhoneDaemon, IPhoneVoiceBridge, PendingCall
from phone_reports import generate_spoken_report
from test_thread_binding import FakeRelay
from test_voice_bridge import FakeAudio, FakeDaemon, FakeLocalTts
from test_voice_bridge import FakeDialer, mark_realtime_ready
from test_iphone_audio import FakeSoundDevice
from iphone_audio import IPhoneAudioPipe, IPhoneDialer


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_confirmation_failures_reach_greeting_and_real_pcm_queue(self):
        # Anonymous replay of the two retained incidents. Relative event times
        # are preserved at 20x test speed; no real calls, logs, devices or speech.
        incidents = [
            ('2026-09-08T14:05:23.893818+08:00', .187623, 6.136020, 9.252481, 'AX'),
            ('2026-09-08T07:47:27.589731+08:00', 1.195114, 9.162660, 12.023311, 'timeout'),
        ]
        for start_text, sending_at, failed_at, active_at, failure in incidents:
            for caller_greets in (True, False):
                with self.subTest(incident=failure, caller_greets=caller_greets), \
                     tempfile.TemporaryDirectory() as temporary, \
                     patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
                    _, bridge = self.bridge()
                    bridge.remote_speech_seen.clear()
                    bridge.daemon.config['announcement_wait_for_greeting_seconds'] = .2
                    bridge._announcement_pcm = array('h', [1500] * 48000).tobytes()
                    mark_realtime_ready(bridge)
                    received = []
                    bridge.rtc.input_track.push_pcm48k = received.append
                    pipe = IPhoneAudioPipe('BlackHole 2ch', 'BlackHole 16ch',
                        bridge._on_phone_pcm, prebuffer_ms=0, fade_ms=0, adaptive_rate_percent=0,
                        sounddevice_module=FakeSoundDevice())
                    bridge.audio = pipe
                    pipe.start()
                    origin = datetime.fromisoformat(start_text).timestamp()
                    began = time.monotonic()
                    ended = [None]
                    ui_failed = asyncio.Event()
                    identity = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
                    def snapshot(started_at, call_uuid=''):
                        elapsed = (time.monotonic() - began) * 20
                        steps = [(sending_at, 'Sending'), (active_at, 'Active')]
                        if ended[0] is not None:
                            steps.append((ended[0], 'Disconnected'))
                        events = [{'timestamp': datetime.fromtimestamp(origin+at).astimezone().strftime('%Y-%m-%d %H:%M:%S.%f%z'),
                                   'eventMessage': f'TUCallCenterCallStatusChangedNotification uPI={identity} stat={state}'}
                                  for at, state in steps if at <= elapsed]
                        return IPhoneDialer._state_from_call_events(events, started_at, call_uuid)
                    async def failed_confirmation(*_):
                        await asyncio.sleep(max(0, failed_at / 20 - (time.monotonic() - began)))
                        ui_failed.set()
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired('read-only fixture', 8)
                        raise RuntimeError('AX inspection unavailable (-25200)')
                    dialer = IPhoneDialer(
                        opener=lambda *a, **kw: SimpleNamespace(returncode=0),
                        process_finder=lambda _: [],
                        helper_runner=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('real UI forbidden')),
                        on_call_observation=bridge._record_call_observation,
                        on_dial_request=bridge._record_dial_request, active_timeout=2)
                    bridge.dialer = dialer
                    with patch('iphone_audio.time.time', return_value=origin), \
                         patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=123)), \
                         patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
                         patch.object(dialer, '_press_recent_once', new=AsyncMock()) as recent, \
                         patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock(side_effect=failed_confirmation)), \
                         patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock()) as confirm, \
                         patch.object(IPhoneDialer, '_system_call_snapshot_since', side_effect=snapshot):
                        call = asyncio.create_task(bridge.dial_and_wait())
                        try:
                            await asyncio.wait_for(ui_failed.wait(), 1)
                            self.assertFalse(call.done())
                            self.assertFalse(bridge.accept_phone_audio)
                            for _ in range(150):
                                if bridge.accept_phone_audio or call.done(): break
                                await asyncio.sleep(.01)
                            self.assertTrue(bridge.accept_phone_audio)
                            self.assertEqual(dialer.system_call_uuid, identity)
                            if caller_greets:
                                for frame in [array('h', [1000] * 960).tobytes()] * 3 + [bytes(1920)] * 9:
                                    bridge._on_phone_pcm(frame)
                                self.assertEqual(len(received), 12)
                            await asyncio.wait_for(bridge._announcement_task, .6)
                            self.assertEqual(bridge.pending.job['announcement_trigger'],
                                             'greeting' if caller_greets else 'pickup_timeout')
                            output = bytearray(len(bridge._announcement_pcm))
                            pipe._output_callback(output, len(output)//2, None, None)
                            self.assertEqual(bytes(output), bridge._announcement_pcm)
                            ledger = pipe.playback_ledger.snapshot()
                            self.assertEqual(len(ledger), 1)
                            self.assertEqual(ledger[0]['status'], 'output_complete')
                            self.assertEqual(ledger[0]['kind'], 'project_report')
                            self.assertEqual(bridge.pending.job['phone_dial_diagnostics']['ui_errors'][0]['stage'],
                                             'confirmation_read')
                            ended[0] = (time.monotonic()-began)*20
                            self.assertEqual(await asyncio.wait_for(call, 1.2), 'completed')
                            self.assertTrue(bridge.pending.job['phone_call_end_confirmed'])
                            recent.assert_awaited_once()
                            confirm.assert_not_awaited()
                        finally:
                            call.cancel()
                            await asyncio.gather(call, return_exceptions=True)
                            await bridge.stop()

    async def test_preconfirmation_sending_unlocks_greeting_and_audio_output(self):
        # The real dialer/parser, bridge, and PCM queue are connected, but
        # all OS/phone/network/device boundaries are replaced with fixtures.
        for caller_greets in (True, False):
            with self.subTest(caller_greets=caller_greets), \
                 tempfile.TemporaryDirectory() as temporary, \
                 patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
                _, bridge = self.bridge()
                bridge.remote_speech_seen.clear()
                bridge.daemon.config['announcement_wait_for_greeting_seconds'] = .2
                bridge._announcement_pcm = array('h', [1500] * 48000).tobytes()
                mark_realtime_ready(bridge)
                received = []
                bridge.rtc.input_track.push_pcm48k = received.append
                pipe = IPhoneAudioPipe('BlackHole 2ch', 'BlackHole 16ch',
                    bridge._on_phone_pcm, prebuffer_ms=0, fade_ms=0,
                    adaptive_rate_percent=0, sounddevice_module=FakeSoundDevice())
                bridge.audio = pipe
                pipe.start()
                clock = [1788628851.908000]
                identity = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
                events = [
                    {'timestamp': '2026-09-06 01:20:52.073635+0800', 'eventMessage':
                     f'TUCallCenterCallStatusChangedNotification uPI={identity} stat=Sending'},
                ]
                async def confirmation_ready(*_):
                    clock[0] = 1788628852.431519
                async def confirm_call(*_):
                    events.append({'timestamp': '2026-09-06 01:21:00.027515+0800', 'eventMessage':
                        f'TUCallCenterCallStatusChangedNotification uPI={identity} stat=Active'})
                def snapshot(started_at, call_uuid=''):
                    return IPhoneDialer._state_from_call_events(events, started_at, call_uuid)
                dialer = IPhoneDialer(
                    opener=lambda *a, **kw: SimpleNamespace(returncode=0),
                    process_finder=lambda name: [456] if name == IPhoneDialer.PHONE_PROCESS else [],
                    helper_runner=lambda command, **kw: SimpleNamespace(
                        returncode=65 if 'has-phone-confirmation' in command else 3, stdout='pending'),
                    on_call_observation=bridge._record_call_observation,
                    on_dial_request=bridge._record_dial_request, active_timeout=.5)
                bridge.dialer = dialer
                with patch('iphone_audio.time.time', side_effect=lambda: clock[0]), \
                     patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
                     patch.object(dialer, '_press_recent_once', new=AsyncMock()) as recent, \
                     patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock(side_effect=confirmation_ready)), \
                     patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock(side_effect=confirm_call)) as confirm, \
                     patch.object(IPhoneDialer, '_system_call_snapshot_since', side_effect=snapshot):
                    voice = array('h', [1000] * 960).tobytes()
                    silence = bytes(1920)
                    bridge._on_phone_pcm(voice)
                    self.assertEqual(received, [])  # Never listen during ringing.
                    call = asyncio.create_task(bridge.dial_and_wait())
                    try:
                        for _ in range(100):
                            if bridge.accept_phone_audio or call.done(): break
                            await asyncio.sleep(.01)
                        self.assertTrue(bridge.accept_phone_audio)
                        if caller_greets:
                            for frame in [voice] * 3 + [silence] * 9:
                                bridge._on_phone_pcm(frame)
                            self.assertEqual(len(received), 12)
                        await asyncio.wait_for(bridge._announcement_task, .6)
                        expected_trigger = 'greeting' if caller_greets else 'pickup_timeout'
                        self.assertEqual(bridge.pending.job['announcement_trigger'], expected_trigger)
                        output = bytearray(len(bridge._announcement_pcm))
                        pipe._output_callback(output, len(output) // 2, None, None)
                        self.assertEqual(bytes(output), bridge._announcement_pcm)
                        ledger = pipe.playback_ledger.snapshot()
                        self.assertEqual(len(ledger), 1)
                        self.assertEqual(ledger[0]['kind'], 'project_report')
                        self.assertEqual(ledger[0]['status'], 'output_complete')
                        events.append({'timestamp': '2026-09-06 01:21:17.497095+0800', 'eventMessage':
                            f'TUCallCenterCallStatusChangedNotification uPI={identity} stat=Disconnected'})
                        self.assertEqual(await asyncio.wait_for(call, 1), 'completed')
                        recent.assert_awaited_once()
                        confirm.assert_awaited_once()
                    finally:
                        if not call.done(): call.cancel()
                        await asyncio.gather(call, return_exceptions=True)
                        await bridge.stop()

    async def test_system_dial_intent_is_durable_before_confirmation_click(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent,'STATE_DIR',Path(temporary)):
            _, bridge = self.bridge()
            mark_realtime_ready(bridge)
            bridge._record_dial_request(1234)
            guard = json.loads((Path(temporary)/'phone-line-unconfirmed.json').read_text())
            self.assertEqual(guard['call_started_at'],1234)
            self.assertEqual(guard['job_id'],'test')

    async def test_spoken_greeting_is_not_proof_phone_has_ended(self):
        _, bridge = self.bridge()
        mark_realtime_ready(bridge)
        bridge.dialer = FakeDialer(disconnected=False)
        outcome = await bridge.dial_and_wait()
        self.assertIn('end_unconfirmed', outcome)
        self.assertFalse(bridge.pending.job['phone_call_end_confirmed'])
        await bridge.stop()

    async def test_dead_voice_channel_announces_failure_once_and_waits_for_hangup(self):
        _, bridge = self.bridge()
        mark_realtime_ready(bridge)
        hangup = asyncio.Event()
        class Dialer(FakeDialer):
            async def wait_for_disconnect(self, timeout):
                bridge.rtc.error_message = 'event channel closed'
                await hangup.wait()
                return True
        bridge.dialer = Dialer()
        task = asyncio.create_task(bridge.dial_and_wait())
        try:
            for _ in range(30):
                if bridge.pending.job.get('phone_failure_notice_queued'): break
                await asyncio.sleep(.01)
            self.assertTrue(bridge.pending.job.get('phone_failure_notice_queued'))
            self.assertFalse(task.done())
            self.assertEqual(len(bridge.local_tts.texts), 1)
            self.assertIn('连接断了', bridge.local_tts.texts[0])
            self.assertEqual(len(bridge.dialer.dialed), 1)
            hangup.set()
            self.assertIn('service_disconnected', await asyncio.wait_for(task, 1))
            self.assertTrue(bridge.pending.job['phone_call_end_confirmed'])
        finally:
            hangup.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_uncertain_line_blocks_even_after_process_restart(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            guard = Path(temporary)/'phone-line-unconfirmed.json'
            guard.write_text(json.dumps({'call_started_at':1234,'job_id':'one-call'}))
            with patch.object(phone_agent.IPhoneDialer, '_system_call_state_since', return_value='active'):
                self.assertTrue(await PhoneDaemon({})._phone_line_unconfirmed())
                self.assertTrue(guard.exists())
            with patch.object(phone_agent.IPhoneDialer, '_system_call_state_since', return_value='disconnected'):
                self.assertFalse(await PhoneDaemon({})._phone_line_unconfirmed())
                self.assertFalse(guard.exists())

    def bridge(self):
        daemon = FakeDaemon()
        bridge = IPhoneVoiceBridge(daemon, PendingCall('test', {'thread_id': 'source', 'spoken_report': '验收中。'}, 'token', Path('test.json')), local_tts=FakeLocalTts())
        bridge.audio = FakeAudio()
        bridge.remote_speech_seen.set()
        bridge._first_assistant_response_pending = False
        return daemon, bridge

    async def test_long_answer_keeps_every_sentence(self):
        _, bridge = self.bridge()
        text = '这一句是完整的项目说明，不应该被省略。' * 15
        bridge._complete_transcript_turn('assistant', text, 'answer')
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(''.join(bridge.local_tts.texts), text)

    async def test_completed_clause_can_speak_before_whole_sentence(self):
        _, bridge = self.bridge()
        first = '我们正在验证电话中的问答以及指令回传功能，'
        self.assertTrue(bridge._looks_complete_for_speech(first))
        bridge._record_transcript_turn('answer', 'assistant', first)
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(bridge.local_tts.texts, [first])
        bridge._complete_transcript_turn('assistant', first + '随后做真机验收。', 'answer')
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(''.join(bridge.local_tts.texts), first + '随后做真机验收。')

    def test_quoting_ack_is_not_an_action(self):
        self.assertFalse(phone_agent.is_task_accepted('我之前说收到，我现在开始，但这不是执行证明。'))

    async def test_worker_crash_cannot_stay_green(self):
        daemon = PhoneDaemon({})
        async def fail(): raise RuntimeError('worker failure')
        task = asyncio.create_task(fail())
        await asyncio.gather(task, return_exceptions=True)
        state = Path(tempfile.mkdtemp(prefix='phone-worker-failure-test-'))
        with patch.object(phone_agent, 'STATE_DIR', state):
            daemon._background_worker_done(task)
        self.assertTrue(daemon.stop_event.is_set())
        self.assertIn('worker failure', daemon.worker_error)
        incidents = list((state/'service-status').glob('*.json'))
        self.assertEqual(len(incidents), 1)
        self.assertEqual(json.loads(incidents[0].read_text())['reason'], 'worker_error')

    def test_restart_does_not_redial_uncertain_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calling, failed = root/'calling', root/'failed'
            calling.mkdir(); failed.mkdir()
            (calling/'exact-call.json').write_text('{"thread_id":"source"}')
            with patch.object(phone_agent, 'CALLING_DIR', calling), patch.object(phone_agent, 'FAILED_DIR', failed):
                PhoneDaemon({})._recover_interrupted_calls()
            self.assertEqual(list(calling.glob('*.json')), [])
            self.assertIn('not_retried', json.loads((failed/'exact-call.json').read_text())['outcome'])

    async def test_late_old_delta_does_not_resume_after_barge_in(self):
        _, bridge = self.bridge()
        bridge._record_transcript_turn('old-answer', 'assistant', '第一句。')
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        bridge._mark_remote_speech(interrupt=True)
        bridge._append_transcript_delta('old-answer', '这句必须取消。')
        bridge._complete_transcript_turn('assistant', '第一句。这句必须取消。', 'old-answer')
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(bridge.local_tts.texts, ['第一句。'])

    async def test_raw_noise_does_not_interrupt_assistant_output(self):
        _, bridge = self.bridge()
        voiced = array('h', [800] * 960).tobytes()
        for _ in range(10):
            bridge._observe_local_caller(voiced)
        self.assertEqual(bridge.audio.clear_calls, 0)
        for _ in range(25):
            bridge._observe_local_caller(b'\x00' * 1920)
        for _ in range(10):
            bridge._observe_local_caller(voiced)
        self.assertEqual(bridge.audio.clear_calls, 0)

    async def test_v3_recognized_new_utterance_interrupts_once(self):
        _, bridge = self.bridge()
        bridge._on_realtime_event({'type':'input_transcript.added','text':'喂'})
        self.assertEqual(bridge.audio.clear_calls, 0)
        bridge._complete_transcript_turn('user', '喂', 'first-user')
        bridge._on_realtime_event({'type':'input_transcript.added','text':'等一下'})
        bridge._on_realtime_event({'type':'input_transcript.added','text':'我想换个方案'})
        self.assertEqual(bridge.audio.clear_calls, 1)

    async def test_input_and_user_turn_are_one_command(self):
        daemon, bridge = self.bridge()
        bridge._on_realtime_event({'type': 'input_transcript.added', 'text': '写一个标记。'})
        bridge._schedule_latest_phone_task('early')
        bridge._complete_transcript_turn('user', '写一个标记。', 'user-real-id')
        bridge._schedule_latest_phone_task('late')
        await asyncio.gather(*tuple(bridge._relay_tasks))
        self.assertEqual(daemon.relayed, ['写一个标记。'])

    async def test_incremental_transcript_is_not_a_series_of_commands(self):
        daemon, bridge = self.bridge()
        bridge._on_realtime_event({'type':'input_transcript.added','text':'请在'})
        bridge._record_transcript_turn('user-id', 'user', '请在')
        for fragment in ['当前项目', '写一个', '验收标记。']:
            bridge._on_realtime_event({'type':'input_transcript.added','text':fragment})
            bridge._append_transcript_delta('user-id',fragment)
        bridge._on_realtime_event({'type':'delegation.created'})
        await asyncio.sleep(0)
        self.assertEqual(daemon.relayed, [])
        bridge._complete_transcript_turn('user','请在当前项目写一个验收标记。','user-id')
        bridge._complete_transcript_turn('assistant','收到，我现在开始。','ack')
        await asyncio.gather(*tuple(bridge._relay_tasks))
        self.assertEqual(daemon.relayed, ['请在当前项目写一个验收标记。'])

    async def test_complete_sentence_before_partial_next_word_speaks_now(self):
        _, bridge = self.bridge()
        bridge._record_transcript_turn('answer','assistant','可以，没问题。后')
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(bridge.local_tts.texts,['可以，没问题。'])

    async def test_delegated_question_never_dispatches_project_task(self):
        daemon, bridge = self.bridge()
        bridge._on_realtime_event({'type':'input_transcript.added','text':'今天天气怎么样？'})
        for _ in range(5):
            bridge._on_realtime_event({'type':'delegation.created'})
        bridge._complete_transcript_turn('user','今天天气怎么样？','weather-user')
        bridge._on_realtime_event({'type':'delegation.created'})
        bridge._complete_transcript_turn('assistant','您想问哪个城市的天气？','weather-answer')
        await asyncio.gather(*tuple(bridge._relay_tasks), *tuple(bridge._local_speech_tasks))
        self.assertEqual(daemon.relayed, [])
        self.assertEqual(bridge.local_tts.texts, ['您想问哪个城市的天气？'])

    async def test_action_ack_after_delegation_dispatches_exactly_once(self):
        daemon, bridge = self.bridge()
        bridge._complete_transcript_turn('user','帮我放两次礼花。','action-user')
        for _ in range(4):
            bridge._on_realtime_event({'type':'delegation.created'})
        await asyncio.sleep(0)
        self.assertEqual(daemon.relayed, ['帮我放两次礼花。'])
        bridge._complete_transcript_turn('assistant','收到，我现在开始。','action-ack')
        bridge._reconcile_task_relays()
        await asyncio.gather(*tuple(bridge._relay_tasks))
        self.assertEqual(daemon.relayed, ['帮我放两次礼花。'])

    async def test_action_ack_waits_for_complete_original_user_words(self):
        daemon, bridge = self.bridge()
        bridge._record_transcript_turn('user', 'user', '请写一个')
        bridge._complete_transcript_turn('assistant', '收到，我现在开始。', 'ack')
        await asyncio.sleep(0)
        self.assertEqual(daemon.relayed, [])
        bridge._complete_transcript_turn('user', '请写一个验收标记，不要更改配置。', 'user')
        await asyncio.gather(*tuple(bridge._relay_tasks))
        self.assertEqual(daemon.relayed, ['请写一个验收标记，不要更改配置。'])

    async def test_incomplete_input_does_not_trigger_opening_report(self):
        _, bridge = self.bridge()
        bridge._first_assistant_response_pending = True
        bridge._on_realtime_event({'type':'input_transcript.added','text':'我想问'})
        bridge._on_realtime_event({'type':'turn.created','turn':{'id':'user','role':'user','transcript':'我想问'}})
        self.assertFalse(bridge.greeting_finished.is_set())
        self.assertFalse(bridge._announcement_scheduled)

    async def test_no_success_ack_before_desktop_receipt(self):
        daemon, bridge = self.bridge()
        received = asyncio.Event()
        daemon.relay_phone_task = AsyncMock(side_effect=lambda *a, **kw: None)
        async def slow(*a, **kw):
            await received.wait()
        daemon.relay_phone_task.side_effect = slow
        bridge._complete_transcript_turn('user', '写一个标记。', 'user')
        bridge._complete_transcript_turn('assistant', '收到，我现在开始。', 'ack')
        await asyncio.sleep(0)
        self.assertEqual(bridge.local_tts.texts, [])
        received.set()
        await asyncio.gather(*tuple(bridge._relay_tasks))
        self.assertEqual(len(bridge.local_tts.texts), 1)

    async def test_stop_rpc_late_ack_is_delivered_before_return(self):
        daemon, bridge = self.bridge()
        class LastEventRtc:
            input_track = SimpleNamespace(diagnostics=lambda: {})
            async def stop(self, server):
                bridge._complete_transcript_turn('user', '写一个最后的标记。', 'tail-user')
                bridge._complete_transcript_turn('assistant', '收到，我现在开始。', 'tail-ack')
        bridge.rtc = LastEventRtc()
        await bridge.stop()
        self.assertEqual(daemon.relayed, ['写一个最后的标记。'])
        self.assertFalse(bridge._relay_tasks)

    async def test_accepted_message_is_not_spoken_as_execution_started(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(return_value={'status':'accepted_by_codex_app'})
        await bridge._relay_phone_task('写标记', 'u')
        self.assertEqual(bridge.local_tts.texts, [phone_agent.COMMAND_QUEUED])

    async def test_new_target_turn_gets_started_not_completed_receipt(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(return_value={'status':'target_turn_started'})
        await bridge._relay_phone_task('写标记', 'u')
        self.assertEqual(bridge.local_tts.texts, [phone_agent.COMMAND_RECEIPT])

    async def test_verified_steered_command_uses_the_same_single_started_receipt(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(return_value={
            'status': 'active_target_steered', 'verification_reason': 'exact_command_in_existing_turn'})
        await bridge._relay_phone_task('放一次礼花', 'u')
        self.assertEqual(bridge.local_tts.texts, [
            '收到指令，已经发送并且开始执行，请您耐心等待，执行完成后会电话通知您。'])

    async def test_unverified_receipt_is_short_and_does_not_claim_started(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        await bridge._relay_phone_task('放一次礼花', 'u')
        self.assertEqual(bridge.local_tts.texts, [
            '指令已经送达，执行状态暂未确认。'])

    async def test_stale_wait_notice_does_not_interrupt_an_answer(self):
        _, bridge = self.bridge()
        original = bridge._audio_queue_serial
        bridge._record_audio_queue_latency('realtime_buffered_answer')
        with patch.object(phone_agent.asyncio, 'sleep', new=AsyncMock()):
            await bridge._query_wait_notice(bridge._speech_generation, original)
        self.assertEqual(bridge.local_tts.texts, [])

    async def test_slow_query_has_only_terminal_timeout_not_filler_or_task_receipt(self):
        daemon, bridge = self.bridge()
        bridge._latest_user_turn_id = 'question'
        bridge._intent_gated_ids.add('question')
        bridge._intent_decisions['question'] = {'kind': 'question'}
        with patch.object(phone_agent.asyncio, 'sleep', new=AsyncMock()):
            for _ in range(5):
                bridge._on_realtime_event({'type':'delegation.created'})
            await asyncio.gather(*tuple(bridge._query_deadline_tasks.values()))
        self.assertEqual(bridge.local_tts.texts, [phone_agent.QUERY_TIMEOUT])
        self.assertEqual(bridge.pending.job['phone_query_timeouts'], ['question'])
        self.assertEqual(daemon.relayed, [])

    def test_next_utterance_cannot_erase_previous_wait_timeline(self):
        _, bridge = self.bridge()
        bridge._record_phone_timing('caller_voice_ended', at=bridge._timing_origin+1)
        bridge._record_phone_timing('caller_voice_ended', at=bridge._timing_origin+24)
        bridge._record_audio_queue_latency('realtime_buffered_answer')
        timeline = bridge.pending.job['phone_dialogue_timing']
        self.assertEqual([e['at_ms'] for e in timeline[:2]], [1000, 24000])
        self.assertEqual(timeline[-1]['kind'], 'realtime_buffered_answer')

    async def test_receipt_journal_deduplicates_across_daemons(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            relay = FakeRelay()
            for _ in range(2):
                daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
                daemon.app_tools = relay
                daemon._confirm_target_turn = AsyncMock(return_value={'status': 'target_turn_started', 'turn_id': 'new'})
                await daemon.relay_phone_task({'thread_id': 'source'}, '执行一次', command_id='call:user')
            self.assertEqual(len(relay.sent), 1)
            saved = json.loads(next((Path(temporary)/'command-deliveries').glob('*.json')).read_text())
            self.assertEqual(saved['status'], 'verified')

    async def test_concurrent_command_is_reserved_before_context_read(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            entered, release = asyncio.Event(), asyncio.Event()
            relay = FakeRelay()
            async def slow_read(_):
                entered.set()
                await release.wait()
                return {}
            relay.read_thread = slow_read
            daemons = [PhoneDaemon({'relay_caller_thread_id': 'relay'}) for _ in range(2)]
            for daemon in daemons:
                daemon.app_tools = relay
                daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
            first = asyncio.create_task(daemons[0].relay_phone_task(
                {'thread_id': 'source'}, '只执行一次', command_id='call:user'))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(daemons[1].relay_phone_task(
                {'thread_id': 'source'}, '只执行一次', command_id='call:user'))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(len(relay.sent), 1)
            self.assertEqual(sum(isinstance(result, AppToolsRelayError) for result in results), 1)

    async def test_existing_damaged_journal_cannot_reauthorize_delivery(self):
        unknown = json.dumps({'command_id': 'call:user', 'source_thread_id': 'source',
                              'text': '执行一次', 'status': 'future-unknown-state'})
        for content in ('{broken', '[]', '{}', 'null', 'false', '42', unknown):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary, \
                 patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
                path = Path(temporary)/'command-deliveries'/(
                    hashlib.sha256(b'source:call:user').hexdigest()+'.json')
                path.parent.mkdir()
                path.write_text(content)
                daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
                daemon.app_tools = FakeRelay()
                daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
                with self.assertRaises(AppToolsRelayError):
                    await daemon.relay_phone_task({'thread_id': 'source'}, '执行一次', command_id='call:user')
                self.assertEqual(daemon.app_tools.sent, [])
                self.assertEqual(path.read_text(), content)

    async def test_command_reservation_is_shared_with_another_process(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            path = Path(temporary)/'command-deliveries'/(
                hashlib.sha256(b'source:call:user').hexdigest()+'.lock')
            path.parent.mkdir()
            process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c',
                'import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); '
                'fcntl.flock(fd,fcntl.LOCK_EX); print("reserved"); sys.stdin.readline(); os.close(fd)',
                str(path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            try:
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 2), b'reserved\n')
                daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
                daemon.app_tools = FakeRelay()
                with self.assertRaisesRegex(AppToolsRelayError, '正在投递'):
                    await daemon.relay_phone_task({'thread_id': 'source'}, '执行一次', command_id='call:user')
                self.assertEqual(daemon.app_tools.sent, [])
                self.assertEqual(daemon.app_tools.reads, [])
            finally:
                if process.returncode is None:
                    process.stdin.write(b'\n')
                    await process.stdin.drain()
                await asyncio.wait_for(process.wait(), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_distinct_commands_do_not_share_a_reservation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            entered, release = asyncio.Event(), asyncio.Event()
            first, second = [PhoneDaemon({'relay_caller_thread_id': 'relay'}) for _ in range(2)]
            for daemon in (first, second):
                daemon.app_tools = FakeRelay()
                daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
            async def slow_read(_):
                entered.set()
                await release.wait()
                return {}
            first.app_tools.read_thread = slow_read
            pending = asyncio.create_task(first.relay_phone_task(
                {'thread_id': 'source'}, '第一条', command_id='call:one'))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                result = await asyncio.wait_for(second.relay_phone_task(
                    {'thread_id': 'source'}, '第二条', command_id='call:two'), 1)
                self.assertEqual(result['status'], 'accepted_by_codex_app')
                self.assertFalse(pending.done())
            finally:
                release.set()
                await pending
            self.assertEqual(len(first.app_tools.sent), 1)
            self.assertEqual(len(second.app_tools.sent), 1)

    async def test_cancel_before_send_releases_reservation_without_sending(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            entered = asyncio.Event()
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            relay = FakeRelay()
            normal_read = relay.read_thread
            async def slow_read(_):
                entered.set()
                await asyncio.Event().wait()
            relay.read_thread = slow_read
            daemon.app_tools = relay
            daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
            pending = asyncio.create_task(daemon.relay_phone_task(
                {'thread_id': 'source'}, '执行一次', command_id='call:user'))
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(relay.sent, [])
            relay.read_thread = normal_read
            await daemon.relay_phone_task({'thread_id': 'source'}, '执行一次', command_id='call:user')
            self.assertEqual(len(relay.sent), 1)

    async def test_cancel_after_accept_preserves_receipt_and_never_resends(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            entered = asyncio.Event()
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            daemon.app_tools = FakeRelay()
            async def slow_confirmation(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            daemon._confirm_target_turn = slow_confirmation
            job = {'thread_id': 'source'}
            pending = asyncio.create_task(daemon.relay_phone_task(job, '执行一次', command_id='call:user'))
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(job['relayed_phone_tasks'][0]['delivery_status'], 'accepted_by_codex_app')
            result = await daemon.relay_phone_task(job, '执行一次', command_id='call:user')
            self.assertEqual(result['status'], 'accepted_by_codex_app')
            self.assertEqual(len(daemon.app_tools.sent), 1)
            self.assertEqual(len(job['relayed_phone_tasks']), 1)

    async def test_reused_command_identity_rejects_changed_words(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            daemon.app_tools = FakeRelay()
            daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
            await daemon.relay_phone_task({'thread_id': 'source'}, '第一条原话', command_id='call:user')
            with self.assertRaises(AppToolsRelayError):
                await daemon.relay_phone_task({'thread_id': 'source'}, '另一条原话', command_id='call:user')
            self.assertEqual(len(daemon.app_tools.sent), 1)

    async def test_confirmation_failure_keeps_the_accepted_delivery(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            daemon.app_tools = FakeRelay()
            daemon._confirm_target_turn = AsyncMock(side_effect=ValueError('bad confirmation shape'))
            job = {'thread_id': 'source'}
            result = await daemon.relay_phone_task(job, '执行一次', command_id='call:user')
            self.assertEqual(result['status'], 'accepted_by_codex_app')
            self.assertEqual(job['relayed_phone_tasks'][0]['delivery_status'], 'accepted_by_codex_app')
            self.assertTrue(job['phone_command_warnings'])
            saved = json.loads(next((Path(temporary)/'command-deliveries').glob('*.json')).read_text())
            self.assertEqual(saved['status'], 'accepted')
            again = await daemon.relay_phone_task(job, '执行一次', command_id='call:user')
            self.assertEqual(again['status'], 'accepted_by_codex_app')
            self.assertEqual(len(daemon.app_tools.sent), 1)
            self.assertEqual(len(job['relayed_phone_tasks']), 1)

    async def test_post_accept_disk_failure_cannot_erase_the_receipt_or_resend(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            daemon.app_tools = FakeRelay()
            daemon._confirm_target_turn = AsyncMock(return_value={'status': 'target_turn_started', 'turn_id': 'new'})
            write_json = phone_agent._atomic_write_json
            def disk_failure(path, value, *args, **kwargs):
                if path.parent.name == 'command-deliveries' and value.get('status') in {'accepted', 'verified'}:
                    raise OSError('disk full after desktop acknowledgement')
                return write_json(path, value, *args, **kwargs)
            job = {'thread_id': 'source'}
            with patch.object(phone_agent, '_atomic_write_json', side_effect=disk_failure):
                result = await daemon.relay_phone_task(job, '执行一次', command_id='call:user')
            self.assertEqual(result['status'], 'target_turn_started')
            self.assertEqual(job['relayed_phone_tasks'][0]['delivery_status'], 'target_turn_started')
            self.assertTrue(job['phone_command_warnings'])
            with self.assertRaises(AppToolsRelayError):
                await daemon.relay_phone_task(job, '执行一次', command_id='call:user')
            self.assertEqual(len(daemon.app_tools.sent), 1)

    async def test_receipt_voice_failure_is_not_a_delivery_failure(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(return_value={'status': 'target_turn_started', 'turn_id': 'new'})
        bridge.local_tts.synthesize = AsyncMock(side_effect=[RuntimeError('receipt unavailable'), bytes(1920)])
        await bridge._relay_phone_task('执行一次', 'u')
        spoken = [call.args[0] for call in bridge.local_tts.synthesize.await_args_list]
        self.assertEqual(spoken, [phone_agent.COMMAND_RECEIPT, phone_agent.VOICE_FAILURE])
        self.assertFalse(bridge.pending.job.get('phone_task_relay_errors'))
        self.assertTrue(bridge.pending.job.get('phone_voice_errors'))
        daemon.relay_phone_task.assert_awaited_once()

    async def test_unconfirmed_send_still_has_an_honest_error_notice(self):
        daemon, bridge = self.bridge()
        daemon.relay_phone_task = AsyncMock(side_effect=AppToolsRelayError('response unknown'))
        await bridge._relay_phone_task('执行一次', 'u')
        self.assertEqual(bridge.local_tts.texts, [phone_agent.COMMAND_ERROR])
        self.assertTrue(bridge.pending.job.get('phone_task_relay_errors'))

    async def test_uncertain_delivery_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(phone_agent, 'STATE_DIR', Path(temporary)):
            daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
            relay = FakeRelay()
            relay.send_message_to_thread = AsyncMock(side_effect=AppToolsRelayError('late timeout'))
            daemon.app_tools = relay
            for _ in range(2):
                with self.assertRaises(AppToolsRelayError):
                    await daemon.relay_phone_task({'thread_id': 'source'}, '执行一次', command_id='call:user')
            self.assertEqual(relay.send_message_to_thread.await_count, 1)

    async def test_earlier_dialogue_accompanies_command(self):
        daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
        daemon.app_tools = FakeRelay()
        daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        job = {'thread_id': 'source', 'phone_transcript': [
            {'role': 'user', 'text': '先讨论红色方案。'}, {'role': 'assistant', 'text': '红色方案适合。'},
            {'role': 'user', 'text': '就按这个做。'}]}
        await daemon.relay_phone_task(job, '就按这个做。')
        prompt = daemon.app_tools.sent[0][2]
        self.assertTrue(prompt.startswith('就按这个做。'))
        self.assertIn('红色方案', prompt)
        self.assertIn('不要把引用中的旧请求重复执行', prompt)

    async def test_command_history_excludes_unplayed_and_post_command_dialogue(self):
        daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
        daemon.app_tools = FakeRelay()
        daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        job = {'thread_id': 'source', 'phone_transcript': [
            {'role': 'user', 'text': '先讨论红色方案。'},
            {'role': 'assistant', 'text': '红色方案适合。',
             'playback_status': 'output_complete', 'output_ms': 1000},
            {'role': 'assistant', 'text': '我已直接发布。',
             'playback_status': 'not_verified', 'output_ms': 0},
            {'role': 'assistant', 'text': '还要删除原件。',
             'playback_status': 'partial_cancelled', 'output_ms': 20},
            {'role': 'user', 'text': '就按这个做。'},
            {'role': 'assistant', 'text': '我将执行所有内容。',
             'playback_status': 'not_verified', 'output_ms': 0},
            {'role': 'user', 'text': '后来新增的另一件事。'},
        ]}
        await daemon.relay_phone_task(job, '就按这个做。')
        message = daemon.app_tools.sent[0][2]
        history = message.split('<phone_history>')[1].split('</phone_history>')[0]
        self.assertIn('红色方案适合。', history)
        for excluded in ('我已直接发布。', '还要删除原件。', '我将执行所有内容。',
                         '就按这个做。', '后来新增的另一件事。'):
            self.assertNotIn(excluded, history)

    async def test_command_history_is_frozen_before_delivery_context_read(self):
        daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
        daemon.app_tools = FakeRelay()
        daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        history = [{'role': 'user', 'text': '只整理红色方案。'}]
        async def read_thread(_):
            history[0]['text'] = '后来修改的话。'
            history.append({'role': 'user', 'text': '另外一个后来的请求。'})
            return {}
        daemon.app_tools.read_thread = read_thread
        await daemon.relay_phone_task({'thread_id': 'source'}, '就按这个做。', phone_history=history)
        message = daemon.app_tools.sent[0][2]
        self.assertIn('只整理红色方案。', message)
        self.assertNotIn('后来', message)

    async def test_ambiguous_legacy_command_does_not_guess_a_history_boundary(self):
        daemon = PhoneDaemon({'relay_caller_thread_id': 'relay'})
        daemon.app_tools = FakeRelay()
        daemon._confirm_target_turn = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        job = {'thread_id': 'source', 'phone_transcript': [
            {'role': 'user', 'text': '就按这个做。'},
            {'role': 'user', 'text': '另一个方案。'},
            {'role': 'user', 'text': '就按这个做。'},
        ]}
        await daemon.relay_phone_task(job, '就按这个做。')
        self.assertNotIn('<phone_history>', daemon.app_tools.sent[0][2])

    def test_manual_call_consumes_only_its_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.object(phone_agent, 'QUEUE_DIR', root/'queue'),
                  patch.object(phone_agent, 'CALLING_DIR', root/'calling'),
                  patch.object(phone_agent, 'load_config', return_value={'provider': 'iphone', 'to_number': '+8613800138000'}),
                  patch.object(phone_agent, 'is_session_enabled', return_value=True),
                  patch.object(hook_stop, 'is_session_enabled', return_value=True),
                  patch.object(hook_stop, 'current_root_turn_id', return_value=''),
                  patch.object(hook_stop, 'STAGED_REPORT_DIR', root/'staged')):
                phone_agent.queue_test_call(thread_id='source', spoken_report='正在验收。')
                self.assertEqual(len(list((root/'queue').glob('*.json'))), 1)
                hook_stop.stage_phone_report('source', '测试电话已完成，等待反馈。')
                self.assertTrue(hook_stop._consume_staged_directive('source', 'source')['skip_call'])
                self.assertEqual(hook_stop._consume_staged_directive('source', 'source'), {})


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    class SummaryServer:
        def __init__(self, start):
            self.start = start
            self.handler = None
            self.closed = asyncio.Event()
            self.interrupted = []

        def add_notification_handler(self, handler): self.handler = handler
        def remove_notification_handler(self, handler): self.handler = None

        def message(self, identity, text):
            if self.handler:
                self.handler({'method': 'item/completed', 'params': {
                    'threadId': 'ephemeral-fork', 'turnId': identity,
                    'item': {'id': 'message-' + str(identity), 'type': 'agentMessage',
                             'text': json.dumps({'spoken_report': text}, ensure_ascii=False)}}})

        def finish(self, identity, status='completed'):
            if self.handler:
                self.handler({'method': 'turn/completed', 'params': {
                    'threadId': 'ephemeral-fork', 'turn': {'id': identity, 'status': status}}})

        async def request(self, method, params, **kwargs):
            if method == 'turn/interrupt':
                self.interrupted.append(params['turnId'])
                return {}
            if method != 'turn/start': raise AssertionError(method)
            return await self.start(self)

    async def test_old_summary_cannot_complete_a_new_summary(self):
        async def start(server):
            server.message('previous', '全部修复完了，已经通过验收。')
            server.finish('previous')
            asyncio.get_running_loop().call_later(.01, server.message,
                'current', '声音还没修好，需要继续检查。')
            asyncio.get_running_loop().call_later(.02, server.finish, 'current')
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        self.assertEqual(await generate_spoken_report(server, 'ephemeral-fork', '仍未修好', timeout=.2),
                         '声音还没修好，需要继续检查。')
        self.assertIsNone(server.handler)

    async def test_current_words_cannot_be_finished_by_an_old_completion(self):
        async def start(server):
            server.message('current', '当前检查还没有完成，请稍等。')
            server.finish('previous')
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        with self.assertRaises(TimeoutError):
            await generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=.025)
        self.assertEqual(server.interrupted, ['current'])

    async def test_untagged_messages_are_not_trusted_as_current(self):
        async def start(server):
            server.message(None, '旧任务全部修好了，可以使用了。')
            server.finish(None)
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        with self.assertRaises(TimeoutError):
            await generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=.025)
        self.assertEqual(server.interrupted, ['current'])

    async def test_missing_summary_identity_fails_before_using_early_events(self):
        async def start(server):
            server.message('previous', '旧任务全部修好了，可以使用了。')
            server.finish('previous')
            return {'turn': {}}
        server = self.SummaryServer(start)
        with self.assertRaisesRegex(RuntimeError, '标识'):
            await generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=.1)
        self.assertEqual(server.interrupted, [])

    async def test_summary_request_and_result_share_one_deadline(self):
        async def start(server):
            await asyncio.sleep(.04)
            asyncio.get_running_loop().call_later(.04, server.message,
                'current', '任务还没有完成，需要继续处理。')
            asyncio.get_running_loop().call_later(.04, server.finish, 'current')
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        with self.assertRaises(TimeoutError):
            await generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=.065)
        self.assertEqual(server.interrupted, ['current'])

    async def test_closed_summary_connection_wakes_immediately(self):
        async def start(server):
            server.closed.set()
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        with self.assertRaisesRegex(RuntimeError, '连接'):
            await asyncio.wait_for(generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=1), .1)
        self.assertIsNone(server.handler)

    async def test_summary_schema_does_not_coerce_non_strings(self):
        async def start(server):
            server.message('current', ['任务已完成', '可以使用了'])
            server.finish('current')
            return {'turn': {'id': 'current'}}
        with self.assertRaisesRegex(RuntimeError, '合格'):
            await generate_spoken_report(self.SummaryServer(start), 'ephemeral-fork', '检查中', timeout=.1)

    async def test_summary_early_overflow_is_bounded_and_never_uses_stale_text(self):
        async def start(server):
            for index in range(129):
                server.message('old-' + str(index), '旧任务全部修好了，可以使用了。')
            return {'turn': {'id': 'current'}}
        server = self.SummaryServer(start)
        with self.assertRaisesRegex(RuntimeError, '事件过多'):
            await generate_spoken_report(server, 'ephemeral-fork', '检查中', timeout=.1)
        self.assertEqual(server.interrupted, ['current'])
        self.assertIsNone(server.handler)

    async def test_contradictory_summary_completion_id_is_ignored(self):
        async def start(server):
            server.message('current', '声音还没修好，需要继续检查。')
            server.handler({'method': 'turn/completed', 'params': {
                'threadId': 'ephemeral-fork', 'turnId': 'current',
                'turn': {'id': 'previous', 'status': 'completed'}}})
            return {'turn': {'id': 'current'}}
        with self.assertRaises(TimeoutError):
            await generate_spoken_report(self.SummaryServer(start), 'ephemeral-fork', '检查中', timeout=.025)

    async def test_separate_summary_uses_readonly_fork_and_waits_for_completed(self):
        class Server:
            def add_notification_handler(self, handler): self.handler = handler
            def remove_notification_handler(self, handler): self.handler = None
            async def request(inner, method, params, **kwargs):
                self.assertEqual(params['threadId'], 'ephemeral-fork')
                self.assertEqual(params['environments'], [])
                inner.handler({'method': 'item/completed', 'params': {'threadId': 'ephemeral-fork', 'turnId': 'summary-turn', 'item': {'type': 'agentMessage', 'text': '{"spoken_report":"指令回传已修复，真实通话还待验收。"}'}}})
                inner.handler({'method': 'turn/completed', 'params': {'threadId': 'ephemeral-fork', 'turn': {'id': 'summary-turn', 'status': 'completed'}}})
                return {'turn': {'id': 'summary-turn'}}
        text = await generate_spoken_report(Server(), 'ephemeral-fork', '很长的屏幕报告')
        self.assertEqual(text, '指令回传已修复，真实通话还待验收。')
