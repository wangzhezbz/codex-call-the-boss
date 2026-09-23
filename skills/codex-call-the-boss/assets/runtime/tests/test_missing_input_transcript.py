"""No-device replay of post-report speech with zero Realtime transcript."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock

import phone_agent as pa
import test_conversation_contract as contract


class MissingTranscriptTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        b = contract.ContractTests().bridge()
        self.addAsyncCleanup(b.stop)
        b.accept_phone_audio = b._phone_input_observed = True
        b._opening_acoustic_sequence = 1
        b._caller_acoustic_sequence = 2
        now = time.monotonic()
        b._current_local_caller_start_at = now - 9
        b._last_local_caller_end_at = now - 8.1
        b.audio.playback_snapshot = lambda: [{
            'id': 'announcement-test', 'kind': 'project_report',
            'status': 'output_complete', 'output_finished_at': now - 14,
            'queued_bytes': 10, 'output_bytes': 10}]
        b.audio.play_pcm48k = lambda payload, **kwargs: b.audio.played.append(payload)
        return b

    async def drain(self, b):
        if b._local_speech_tasks:
            await asyncio.gather(*tuple(b._local_speech_tasks))

    async def test_observed_no_transcript_gap_gets_one_repeat_notice_not_an_answer(self):
        b = self.bridge()
        b._check_untranscribed_input()
        b._check_untranscribed_input()
        await self.drain(b)
        b._check_untranscribed_input()
        self.assertEqual(b.local_tts.texts, [pa.INPUT_INCOMPLETE])
        self.assertEqual(len(b.audio.played), 1)
        self.assertEqual(b.daemon.relayed, [])
        self.assertEqual(b._caller_turns.turns, {})
        self.assertEqual(len(b.pending.job['phone_untranscribed_input_timeouts']), 1)
        self.assertTrue(all(t['role'] == 'assistant' for t in b._transcript_turns))

    async def test_before_eight_seconds_stays_silent(self):
        b = self.bridge()
        b._last_local_caller_end_at = time.monotonic() - 7.9
        b._check_untranscribed_input()
        self.assertFalse(b._local_speech_tasks)

    async def test_greeting_pending_text_recognized_input_or_active_speech_are_excluded(self):
        for field, value in [('_caller_acoustic_sequence', 1), ('_pending_input_id', 'owned'),
                             ('_consumed_acoustic_sequence', 2), ('_local_caller_active', True),
                             ('_remote_speech_active', True), ('disconnected', True),
                             ('_closing_input', True), ('_call_failure', 'failed'),
                             ('accept_phone_audio', False)]:
            b = self.bridge()
            setattr(b, field, value)
            b._check_untranscribed_input()
            self.assertFalse(b._local_speech_tasks, field)
            b._pending_input_id = ''

    async def test_overlapping_or_unplayed_report_does_not_authorize_notice(self):
        for state, finish in [('queued', None), ('playing', None),
                              ('output_complete', time.monotonic())]:
            b = self.bridge()
            b.audio.playback_snapshot = lambda: [{'id': 'announcement-test',
                'status': state, 'output_finished_at': finish}]
            b._check_untranscribed_input()
            self.assertFalse(b._local_speech_tasks)

    async def test_new_speech_during_cache_load_discards_stale_notice(self):
        b = self.bridge()
        async def delayed(text):
            b._caller_acoustic_sequence = 3
            return b'\x01\x00' * 960
        b.local_tts.synthesize = delayed
        b._check_untranscribed_input()
        await self.drain(b)
        self.assertEqual(b.audio.played, [])

    async def test_hangup_during_cache_load_discards_stale_notice(self):
        b = self.bridge()
        async def delayed(text):
            b.disconnected = True
            return b'\x01\x00' * 960
        b.local_tts.synthesize = delayed
        b._check_untranscribed_input()
        await self.drain(b)
        self.assertEqual(b.audio.played, [])

    async def test_expired_acoustic_credit_cannot_execute_late_words_but_next_speech_can(self):
        b = self.bridge()
        b._check_untranscribed_input()
        await self.drain(b)
        self.assertFalse(b._has_caller_evidence(1000, '下一步的任务是删除文件'))
        b._caller_acoustic_sequence = 3
        self.assertTrue(b._has_caller_evidence(2000, '现在项目怎么样'))
        self.assertEqual(b.daemon.relayed, [])

    async def test_health_watch_checks_missing_input_without_waiting_for_text(self):
        b = self.bridge()
        b._realtime_readiness_error = lambda: ''
        def check():
            b._call_failure = 'test complete'
        b._check_untranscribed_input = check
        self.assertEqual(await b._watch_call_health(time.monotonic()), 'test complete')

    async def test_input_event_counters_store_only_fixed_keys_and_counts(self):
        b = self.bridge()
        b._call_failure = 'test terminal'
        b._on_realtime_event({'type': 'input_transcript.added', 'text': 'private text'})
        b._on_realtime_event({'type': 'input_transcript.added', 'text': 'private text'})
        b._on_realtime_event({'type': 'arbitrary-private-event'})
        self.assertEqual(b.pending.job['phone_realtime_input_events'],
                         {'input_transcript.added': 2})
