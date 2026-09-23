"""End-to-end state contracts with no telephone, network, or task mutations."""
import asyncio
from array import array
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import phone_agent as pa
from audio_turn_fence import AudioTurnFence
from iphone_audio import IPhoneAudioPipe, IPhoneDialer
from native_speech import COMMAND_QUEUED, VOICE_FAILURE
from playback_ledger import PlaybackLedger
from speech_quality import speech_alignment
from test_voice_bridge import FakeDaemon, FakeAudio, FakeLocalTts


def pcm(ms, value=1000):
    return array('h', [value] * (48 * ms)).tobytes()


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self, actual_audio=False):
        daemon = FakeDaemon()
        daemon.config.update(phone_voice_renderer='realtime-unified', phone_realtime_semantic_gate=False,
                             phone_realtime_tail_min_wait_ms=0, phone_realtime_tail_max_wait_ms=0)
        b = pa.IPhoneVoiceBridge(daemon, pa.PendingCall('test', {'thread_id':'source','spoken_report':'还在检查。'},
                                  'fake', Path('unused.json')), local_tts=FakeLocalTts())
        b.audio = (IPhoneAudioPipe(input_device='fake-in',output_device='fake-out',on_input=lambda _:None,
                                  sounddevice_module=object(), prebuffer_ms=20) if actual_audio else FakeAudio())
        b.remote_speech_seen.set()
        b._announcement_delivered = True
        b._first_assistant_response_pending = False
        return b

    async def drain(self, b):
        for _ in range(5):
            tasks = tuple(b._relay_tasks | b._local_speech_tasks | b._realtime_release_tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks)

    async def test_action_does_not_depend_on_any_spoken_ack(self):
        for ack in ('收到，我现在开始。','好的，我来处理。','我来看看。',''):
            b = self.bridge()
            b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
            b._complete_transcript_turn('user','请写一个检查标记。','u1')
            if ack:
                b._complete_transcript_turn('assistant',ack,'a1')
            await self.drain(b)
            b._reconcile_task_relays()
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['请写一个检查标记。'])

    async def test_repeating_ack_is_not_a_command(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        b._complete_transcript_turn('user','请重复这句话：收到，我现在开始。','u1')
        b._complete_transcript_turn('assistant','收到，我现在开始。','a1')
        await self.drain(b)
        self.assertEqual(b.daemon.relayed, [])
        self.assertNotIn('a1', b._suppressed_assistant_turn_ids)

    async def test_long_first_greeting_does_not_suppress_short_later_question(self):
        b = self.bridge()
        b._announcement_delivered = False
        b._first_assistant_response_pending = True
        b._complete_transcript_turn('user','喂，你好，能听见我说话吗？','greeting')
        await self.drain(b)
        b._complete_transcript_turn('user','修好了吗？','question')
        b._complete_transcript_turn('assistant','还没有修好。','answer')
        await self.drain(b)
        self.assertNotIn('answer', b._suppressed_assistant_turn_ids)

    async def test_short_nonstreamed_voice_is_rejected(self):
        b = self.bridge()
        text = '这个问题还没有完全解决，我们需要继续检查。'
        b._begin_realtime_capture('a1')
        b._on_pcm_output(pcm(300))
        await b._release_realtime_after_drain('a1', text, b._speech_generation)
        self.assertEqual(b.local_tts.texts, [VOICE_FAILURE])
        self.assertFalse(b.pending.job['phone_realtime_audio_diagnostics']['last_structural_check_passed'])

    async def test_queued_receipt_is_not_claimed_output_and_resumes_after_interrupt(self):
        b = self.bridge(actual_audio=True)
        await b._render_local_speech(COMMAND_QUEUED, b._speech_generation, kind='command_receipt')
        self.assertEqual(b.pending.job['phone_transcript'][0]['playback_status'], 'queued')
        b._mark_remote_speech(interrupt=True)
        self.assertEqual(b.pending.job['phone_transcript'][0]['playback_status'], 'cancelled')
        b._last_speech_stopped_at = pa.time.monotonic() - 2
        await self.drain(b)
        self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED, COMMAND_QUEUED])
        b.audio._output_callback(bytearray(1920), 960, None, None)
        b._sync_transcript_job()
        self.assertEqual(b.pending.job['phone_transcript'][-1]['playback_status'], 'output_complete')
        self.assertEqual(b.pending.job['phone_transcript'][-1]['output_ms'], 20)

    async def test_second_speech_burst_cannot_merge_into_pending_query(self):
        b = self.bridge()
        b._on_realtime_event({'type':'input_transcript.added','text':'查询北京天气。','start_ms':100,'end_ms':900})
        b._on_realtime_event({'type':'input_audio_buffer.speech_stopped'})
        b._on_realtime_event({'type':'input_audio_buffer.speech_started'})
        b._on_realtime_event({'type':'input_transcript.added','text':'别查了，先讲笑话。','start_ms':4000,'end_ms':5000})
        b._finalize_pending_input()
        await self.drain(b)
        users = [t['text'] for t in b.pending.job['phone_transcript'] if t['role']=='user']
        self.assertEqual(users,['查询北京天气。','别查了，先讲笑话。'])
        self.assertEqual(b.daemon.relayed, [])

    async def test_missing_baseline_is_not_execution_proof(self):
        daemon = pa.PhoneDaemon({})
        daemon.app_tools = SimpleNamespace(read_thread=AsyncMock())
        result = await daemon._confirm_target_turn('source', {})
        self.assertEqual(result['status'], 'accepted_by_codex_app')
        daemon.app_tools.read_thread.assert_not_awaited()

    async def test_event_channel_error_is_terminal(self):
        b = self.bridge()
        b._on_realtime_event({'type':'error','error':{'type':'server_error','message':'test failure'}})
        self.assertIn('test failure', b._call_failure)
        self.assertEqual(await b._watch_call_health(pa.time.monotonic()), b._call_failure)


class AudioContractTests(unittest.TestCase):
    def test_playback_ledger_only_consumption_completes_a_reply(self):
        ledger = PlaybackLedger()
        ledger.enqueue(3840,'one',text='测试')
        self.assertEqual(ledger.snapshot()[0]['status'], 'queued')
        ledger.consume(1920)
        self.assertEqual(ledger.snapshot()[0]['status'], 'playing')
        ledger.cancel()
        self.assertEqual(ledger.snapshot()[0]['status'], 'partial_cancelled')
        self.assertEqual(ledger.snapshot()[0]['output_bytes'], 1920)

    def test_late_old_pcm_cannot_enter_next_answer_but_new_phoneme_lead_survives(self):
        fence = AudioTurnFence()
        fence.begin('old', 1000)
        fence.interrupt()
        self.assertFalse(fence.receive(b'old', {'media_ms':1500}))
        self.assertFalse(fence.receive(b'new-lead', {'media_ms':2960}))
        self.assertEqual(fence.begin('new',3000), [b'new-lead'])
        self.assertFalse(fence.receive(b'late-old', {'media_ms':1600}))
        self.assertTrue(fence.receive(b'new', {'media_ms':3020}))

    def test_reversed_status_and_numbers_fail_even_with_high_similarity(self):
        self.assertFalse(speech_alignment('老板，这个问题没有修复，仍然需要继续检查。',
                                         '老板，这个问题已经修复，仍然需要继续检查。')['passed'])
        self.assertFalse(speech_alignment('本次已经处理15个文件，其余内容需要您继续确认。',
                                         '本次已经处理16个文件，其余内容需要您继续确认。')['passed'])

    def test_other_call_active_and_disconnect_do_not_override_our_sending(self):
        first, other = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        stamp = datetime.now(timezone.utc)
        def event(identity, state):
            return {'timestamp':stamp.strftime('%Y-%m-%d %H:%M:%S.%f%z'),
                    'eventMessage':f'TUCallCenterCallStatusChangedNotification uPI={identity} stat={state}'}
        events = [event(first,'Sending'),event(other,'Active'),event(other,'Disconnected')]
        state, identity = IPhoneDialer._state_from_call_events(events,stamp.timestamp()-1)
        self.assertEqual((state,identity),('sending',first))
        events.append(event(other,'Sending'))
        self.assertEqual(IPhoneDialer._state_from_call_events(events,stamp.timestamp()-1), ('unknown',''))
        self.assertEqual(IPhoneDialer._state_from_call_events(events,stamp.timestamp()-1,first), ('sending',first))
