"""Fault sequences from real failures; all external operations are fakes."""
import asyncio
import unittest
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from phone_turns import CallerTurns
from audio_turn_fence import AudioTurnFence, OutputTranscriptClock
from native_speech import COMMAND_QUEUED, COMMAND_ALREADY_SENT, QUERY_WAIT, QUERY_TIMEOUT, VOICE_FAILURE, INTENT_CLARIFY, INPUT_INCOMPLETE
import test_conversation_contract as contract

pcm = contract.pcm


class QueryWaitDecisionRegressionTests(unittest.IsolatedAsyncioTestCase):
    def pending_bridge(self, kind='question', *, timeout=1.1):
        b = contract.ContractTests().bridge(actual_audio=True)
        b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=timeout,
                              phone_query_wait_notice_enabled=True)
        b._schedule_context_update = lambda **kwargs: None
        release = asyncio.Event()

        async def classify(text, context):
            await release.wait()
            return {'kind': kind}

        b.daemon.classify_phone_intent = classify

        async def close():
            release.set()
            await b.stop()

        self.addAsyncCleanup(close)
        return b, release

    @staticmethod
    def queued_notices(b):
        return [item['text'] for item in b.audio.playback_snapshot()
                if item['kind'] == 'query_wait']

    async def test_default_question_wait_is_silent_but_real_timeout_remains(self):
        b, release = self.pending_bridge(timeout=.18)
        b.daemon.config.pop('phone_query_wait_notice_enabled')
        release.set()
        b._complete_transcript_turn('user', '给我讲个笑话。', 'question')
        await b._intent_tasks['question']
        await b._query_deadline_tasks['question']
        self.assertEqual(self.queued_notices(b), [])
        self.assertEqual(b.local_tts.texts, [QUERY_TIMEOUT])
        self.assertFalse(b.daemon.relayed)

    async def test_default_slow_answer_has_no_filler_and_no_late_timeout(self):
        b, release = self.pending_bridge(timeout=.3)
        b.daemon.config.pop('phone_query_wait_notice_enabled')
        release.set()
        b._complete_transcript_turn('user', '给我讲个笑话。', 'question')
        await b._intent_tasks['question']
        await asyncio.sleep(.09)
        self.assertEqual(b.local_tts.texts, [])
        b._assistant_user_turn_ids['answer'] = 'question'
        b._queue_audio(pcm(1000), 'answer', kind='realtime_answer', text='这是你要的笑话。')
        await asyncio.gather(b._query_deadline_tasks['question'], return_exceptions=True)
        self.assertEqual(b.local_tts.texts, [])
        self.assertIn('question', b._query_answered_ids)
        self.assertEqual(len([row for row in b.audio.playback_snapshot()
                              if row['kind'] == 'realtime_answer']), 1)

    async def test_late_question_gets_one_notice_without_restarting_its_deadline(self):
        b, release = self.pending_bridge()
        started = time.monotonic()
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.sleep(.62)  # Beyond the original 50 ms + 500 ms grace.
        self.assertEqual(b.local_tts.texts, [])
        release.set()
        await b._intent_tasks['question']
        await asyncio.sleep(.03)
        self.assertEqual(self.queued_notices(b), [QUERY_WAIT])
        self.assertNotIn('question', b._query_answered_ids)
        b._schedule_query_wait()  # Duplicate delegation must not add a timer.
        await b._query_deadline_tasks['question']
        self.assertEqual(b.local_tts.texts, [QUERY_WAIT, QUERY_TIMEOUT])
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertEqual(b.pending.job['phone_query_timeouts'], ['question'])
        self.assertFalse(b.daemon.relayed)

    async def test_late_question_wakes_timer_created_before_classifier_task(self):
        b, release = self.pending_bridge()
        b._latest_user_turn_id = 'question'
        b._schedule_query_wait()
        await asyncio.sleep(.08)  # There is no classifier task at this boundary.
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        release.set()
        await b._intent_tasks['question']
        await asyncio.sleep(.03)
        self.assertEqual(self.queued_notices(b), [QUERY_WAIT])
        self.assertFalse(b.daemon.relayed)

    async def test_already_decided_question_keeps_initial_wait_and_one_notice(self):
        b, release = self.pending_bridge()
        release.set()
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await b._intent_tasks['question']
        self.assertEqual(self.queued_notices(b), [])
        await asyncio.sleep(.08)
        self.assertEqual(self.queued_notices(b), [QUERY_WAIT])
        self.assertNotIn('question', b._query_answered_ids)
        await b.stop()
        self.assertEqual(b._query_decision_events, {})

    async def test_late_nonquestions_never_get_query_filler_or_timeout(self):
        for kind in ('action', 'cancel', 'clarify', 'error', 'greeting', 'farewell'):
            with self.subTest(kind=kind):
                b, release = self.pending_bridge(kind)
                b._complete_transcript_turn('user', '由结构化判断决定本次输入。', 'input')
                await asyncio.sleep(.62)
                release.set()
                await b._intent_tasks['input']
                await asyncio.gather(b._query_deadline_tasks['input'], return_exceptions=True)
                self.assertEqual(self.queued_notices(b), [])
                self.assertNotIn(QUERY_TIMEOUT, b.local_tts.texts)
                self.assertEqual(len(b.daemon.relayed), int(kind == 'action'))
                if kind == 'action':
                    self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
                self.assertEqual(b._query_decision_events, {})
                await b.stop()

    async def test_unresolved_deadline_does_not_cancel_classifier_or_allow_late_filler(self):
        b, release = self.pending_bridge(timeout=.18)
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.wait_for(b._query_deadline_tasks['question'], .5)
        self.assertEqual(b.local_tts.texts, [QUERY_TIMEOUT])
        self.assertFalse(b._intent_tasks['question'].done())
        self.assertEqual(b._query_decision_events, {})
        release.set()
        await b._intent_tasks['question']
        await asyncio.sleep(.03)
        self.assertEqual(b.local_tts.texts, [QUERY_TIMEOUT])
        self.assertFalse(b.daemon.relayed)

    async def test_late_decision_with_buffered_answer_does_not_insert_filler(self):
        b, release = self.pending_bridge()
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.sleep(.62)
        b._realtime_audio_buffer.extend(pcm(1000))
        release.set()
        await b._intent_tasks['question']
        await asyncio.sleep(.03)
        self.assertEqual(self.queued_notices(b), [])
        self.assertEqual(b.local_tts.texts, [])

    async def test_answer_released_with_decision_wins_before_notice(self):
        b, release = self.pending_bridge()
        release_responses = b._release_classified_responses

        def release_answer(identity):
            release_responses(identity)
            b._assistant_user_turn_ids['answer'] = identity
            b._queue_audio(pcm(1000), 'answer', kind='realtime_answer', text='还在检查。')

        b._release_classified_responses = release_answer
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.sleep(.62)
        release.set()
        await b._intent_tasks['question']
        await asyncio.gather(b._query_deadline_tasks['question'], return_exceptions=True)
        self.assertEqual(self.queued_notices(b), [])
        self.assertIn('question', b._query_answered_ids)
        self.assertEqual(b._query_decision_events, {})

    async def test_buffered_answer_during_notice_lock_or_cache_prevents_enqueue(self):
        for stage in ('lock', 'cache'):
            with self.subTest(stage=stage):
                b, release = self.pending_bridge()
                cache_entered, cache_release = asyncio.Event(), asyncio.Event()
                original_synthesize = b.local_tts.synthesize

                async def synthesize(text):
                    if text == QUERY_WAIT:
                        cache_entered.set()
                        await cache_release.wait()
                    return await original_synthesize(text)

                if stage == 'lock':
                    await b._local_speech_lock.acquire()
                else:
                    b.local_tts.synthesize = synthesize
                try:
                    release.set()
                    b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
                    if stage == 'lock':
                        await asyncio.sleep(.08)
                    else:
                        await asyncio.wait_for(cache_entered.wait(), .3)
                    b._realtime_audio_buffer.extend(pcm(1000))
                    cache_release.set()
                    if stage == 'lock':
                        b._local_speech_lock.release()
                    await asyncio.sleep(.03)
                    self.assertEqual(self.queued_notices(b), [])
                    if stage == 'lock':
                        self.assertEqual(b.local_tts.texts, [])
                finally:
                    cache_release.set()
                    if stage == 'lock' and b._local_speech_lock.locked():
                        b._local_speech_lock.release()
                    await b.stop()

    async def test_slow_notice_cache_cannot_extend_original_question_deadline(self):
        b, release = self.pending_bridge(timeout=.18)
        original_synthesize = b.local_tts.synthesize
        cache_cancelled = asyncio.Event()

        async def synthesize(text):
            if text == QUERY_WAIT:
                try:
                    await asyncio.Event().wait()
                finally:
                    cache_cancelled.set()
            return await original_synthesize(text)

        b.local_tts.synthesize = synthesize
        release.set()
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.wait_for(b._query_deadline_tasks['question'], .5)
        self.assertTrue(cache_cancelled.is_set())
        self.assertEqual(self.queued_notices(b), [])
        self.assertEqual(b.local_tts.texts, [QUERY_TIMEOUT])
        self.assertEqual(b._query_decision_events, {})

    async def test_interrupt_or_hangup_before_late_decision_cannot_resume_notice(self):
        for stop in ('interrupt', 'hangup'):
            with self.subTest(stop=stop):
                b, release = self.pending_bridge()
                b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
                await asyncio.sleep(.62)
                if stop == 'interrupt':
                    b._mark_remote_speech(interrupt=True)
                else:
                    b.disconnected = True
                release.set()
                await b._intent_tasks['question']
                await asyncio.sleep(.03)
                self.assertEqual(b.local_tts.texts, [])
                self.assertFalse(b._intent_tasks['question'].cancelled())
                await b.stop()
                self.assertEqual(b._query_decision_events, {})

    async def test_newer_input_without_generation_change_suppresses_old_notice(self):
        b, release = self.pending_bridge()
        b._complete_transcript_turn('user', '现在检查到哪里了？', 'question')
        await asyncio.sleep(.62)
        b._latest_user_turn_id = 'newer-input'
        release.set()
        await b._intent_tasks['question']
        await asyncio.sleep(.03)
        self.assertEqual(self.queued_notices(b), [])
        self.assertEqual(b.local_tts.texts, [])


class ResponseLatencyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_noise_cannot_hide_the_saved_command_wait(self):
        b = contract.ContractTests().bridge()
        b._phone_input_observed = True
        b._last_local_caller_end_at = 78.061
        with patch('phone_agent.time.monotonic', return_value=80.0):
            b._record_caller_input_timing('input-2')
        b._last_local_caller_end_at = b._last_speech_stopped_at = 87.021
        b._latest_user_turn_id = 'later-input'
        with patch('phone_agent.time.monotonic', return_value=88.251):
            b._record_audio_queue_latency('command_receipt', user_turn_id='input-2', output_id='receipt')
        row = b.pending.job['phone_response_latency_ms'][0]
        self.assertEqual(row['from_speech_stopped_to_audio_queued'], 10190)
        self.assertIsNone(row['from_speech_stopped_to_first_output'])
        b._sync_response_latencies({'receipt': {'first_output_at': 88.281,
            'status': 'partial_cancelled', 'output_bytes': 163200}})
        self.assertEqual(row['from_speech_stopped_to_first_output'], 10220)
        self.assertEqual(row['output_ms'], 1700)
        self.assertEqual(row['playback_status'], 'partial_cancelled')
        self.assertEqual(b.pending.job['phone_dialogue_timing'][-1]['user_turn_id'], 'input-2')
        await b.stop()

    async def test_unknown_input_end_is_unknown_not_latest_speech_or_zero(self):
        b = contract.ContractTests().bridge()
        b._last_speech_stopped_at = time.monotonic() - .1
        b._record_audio_queue_latency('command_receipt', user_turn_id='unbound', output_id='receipt')
        row = b.pending.job['phone_response_latency_ms'][0]
        self.assertIsNone(row['from_speech_stopped_to_audio_queued'])
        b._sync_response_latencies({'receipt': {'status': 'queued', 'output_bytes': 0}})
        self.assertIsNone(row['from_speech_stopped_to_first_output'])
        await b.stop()

    async def test_repeated_final_and_alias_do_not_move_original_end(self):
        b = contract.ContractTests().bridge()
        b._phone_input_observed = True
        b._user_turn_aliases['server-user'] = 'input-1'
        with patch('phone_agent.time.monotonic', return_value=101):
            b._last_local_caller_end_at = 100
            b._record_caller_input_timing('input-1')
        with patch('phone_agent.time.monotonic', return_value=110):
            b._last_local_caller_end_at = 109
            b._record_caller_input_timing('server-user')
            b._record_audio_queue_latency('answer', user_turn_id='server-user', output_id='a1')
        self.assertEqual(b.pending.job['phone_response_latency_ms'][0]['from_speech_stopped_to_audio_queued'], 10000)
        await b.stop()

    async def pending_bridge(self):
        b = CallerTurnTests().bound_input('好的，你放个礼花庆祝一下。')
        if b._input_settle_task:
            b._input_settle_task.cancel()
            await asyncio.gather(b._input_settle_task, return_exceptions=True)
            b._input_settle_task = None
        b._pending_server_handoff = True
        b._pending_input_ended = True
        b._pending_input_updated_at = time.monotonic()
        b._last_local_caller_end_at = time.monotonic() - .4
        b._caller_turns.voice(False)
        b._local_caller_active = b._remote_speech_active = False
        return b

    async def test_owned_decision_overlaps_grace_but_never_dispatches_early(self):
        b = await self.pending_bridge()
        entered, release = asyncio.Event(), asyncio.Event()
        async def classify(text, context):
            entered.set()
            await release.wait()
            return {'kind': 'action'}
        b.daemon.classify_phone_intent = AsyncMock(side_effect=classify)
        try:
            b._schedule_input_settle()
            await entered.wait()
            self.assertTrue(b._pending_input_id)
            self.assertFalse(b.daemon.relayed)
            release.set()
            await b._intent_prefetch['task']
            self.assertFalse(b._intent_decisions)
            self.assertFalse(b.local_tts.texts)
            await b._input_settle_task
            await contract.ContractTests().drain(b)
            self.assertEqual(b.daemon.relayed, ['好的，你放个礼花庆祝一下。'])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            trace = b.pending.job['phone_intent_decisions'][0]['classification_stages']
            self.assertGreaterEqual(trace['prefetch_lead_ms'], 1000)
            self.assertLessEqual(trace['overlapped_asr_grace_ms'], trace['bridge_decision_elapsed_ms'])
            self.assertLess(trace['decision_wait_after_input_final_ms'], 100)
        finally:
            release.set()
            await b.stop()

    async def test_late_changed_words_discard_prefetched_action(self):
        b = await self.pending_bridge()
        b.daemon.classify_phone_intent = AsyncMock(side_effect=[{'kind': 'action'}, {'kind': 'question'}])
        try:
            b._prepare_intent_prefetch()
            await b._intent_prefetch['task']
            b._pending_input_text += '是什么意思？不要执行。'
            b._pending_server_handoff = False
            b._prepare_intent_prefetch()
            self.assertIsNone(b._intent_prefetch)
            self.assertFalse(b.daemon.relayed)
            b._pending_server_done = True
            b._finalize_pending_input()
            await contract.ContractTests().drain(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 2)
            self.assertIn('不要执行', b.daemon.classify_phone_intent.await_args.args[0])
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_duplicate_boundary_reuses_only_same_candidate(self):
        b = await self.pending_bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            b._prepare_intent_prefetch()
            candidate = b._intent_prefetch
            b._prepare_intent_prefetch()
            self.assertIs(b._intent_prefetch, candidate)
            await candidate['task']
            b._finalize_pending_input()
            await contract.ContractTests().drain(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            self.assertEqual(len(b.daemon.relayed), 1)
        finally:
            await b.stop()

    async def test_abandoned_input_cancels_read_only_candidate_without_action(self):
        b = await self.pending_bridge()
        waiting = asyncio.Event()
        async def classify(text, context):
            waiting.set()
            await asyncio.Event().wait()
        b.daemon.classify_phone_intent = AsyncMock(side_effect=classify)
        b._prepare_intent_prefetch()
        candidate = b._intent_prefetch['task']
        await waiting.wait()
        b._abandon_pending_input('diagnostic_incomplete')
        await asyncio.gather(candidate, return_exceptions=True)
        self.assertTrue(candidate.cancelled())
        self.assertFalse(b.daemon.relayed)
        await b.stop()

    async def test_unowned_prefix_or_active_speech_never_prefetches(self):
        b = await self.pending_bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            b._pending_server_handoff = False
            b._prepare_intent_prefetch()
            b._pending_server_handoff = True
            b._local_caller_active = True
            b._prepare_intent_prefetch()
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertIsNone(b._intent_prefetch)
        finally:
            b._abandon_pending_input('diagnostic_incomplete')
            b._local_caller_active = False
            await b.stop()

    async def test_failed_candidate_is_not_retried_after_finalization(self):
        b = await self.pending_bridge()
        b.daemon.classify_phone_intent = AsyncMock(side_effect=TimeoutError)
        try:
            b._prepare_intent_prefetch()
            await b._intent_prefetch['task']
            b._finalize_pending_input()
            await contract.ContractTests().drain(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            self.assertEqual(b.pending.job['phone_intent_decisions'][0]['kind'], 'error')
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()


class CallerTurnTests(unittest.IsolatedAsyncioTestCase):
    async def delayed_opening_bridge(self, *, report_complete=True, second_at=106.0):
        """Retained order: two physical inputs, then the first input's ASR."""
        b = contract.ContractTests().bridge(actual_audio=True)
        self.addAsyncCleanup(b.stop)
        b._phone_input_observed = True
        b._announcement_delivered = False
        b._schedule_input_settle = lambda: None
        b._schedule_context_update = lambda **kwargs: None
        b._finish_realtime_utterance = lambda *args: None
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        with patch('phone_agent.time.monotonic', return_value=100.0):
            b._observe_local_caller(pcm(120))
        with patch('phone_agent.time.monotonic', return_value=100.55):
            b._observe_local_caller(pcm(420, 0))
        b.audio.playback_ledger.clock = lambda: 101.0
        b._announcement_pcm = pcm(20)
        await b._deliver_announcement()
        if report_complete:
            b.audio._output_callback(bytearray(1920), 960, None, None)
        with patch('phone_agent.time.monotonic', return_value=second_at):
            b._observe_local_caller(pcm(120))
        with patch('phone_agent.time.monotonic', return_value=second_at + 2.55):
            b._observe_local_caller(pcm(420, 0))
        return b

    def finish_delayed_greeting(self, b, *, final='喂', end_ms=4600, owned=True):
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '喂',
                             'start_ms': 3600, 'end_ms': 3800})
        if owned:
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'greeting-server', 'role': 'user', 'transcript': '喂',
                'start_ms': 3600, 'end_ms': 3800}})
        b._on_realtime_event({'type': 'turn.done', 'turn': {
            'id': 'greeting-server', 'role': 'user', 'transcript': final,
            'start_ms': 3600, 'end_ms': end_ms}})
        b._finalize_pending_input()

    async def test_delayed_opening_final_does_not_spend_post_report_question(self):
        b = await self.delayed_opening_bridge()
        self.finish_delayed_greeting(b)
        self.assertEqual(b._consumed_acoustic_sequence, 1)
        b._on_realtime_event({'type': 'input_transcript.added',
            'text': '你这次的声音很清晰啊，我很喜欢。', 'start_ms': 9400, 'end_ms': 12000})
        self.assertEqual(b._pending_input_id, 'input-2')
        self.assertEqual(b._consumed_acoustic_sequence, 2)
        b._on_realtime_event({'type': 'turn.created', 'turn': {
            'id': 'second-answer', 'role': 'assistant', 'start_ms': 12600,
            'transcript': '谢谢你，我会保持现在这种清楚的语气。'}})
        self.assertEqual(b._assistant_user_turn_ids.get('second-answer'), 'input-2')
        self.assertNotIn('second-answer', b._suppressed_assistant_turn_ids)
        self.assertFalse(b.pending.job.get('phone_rejected_silent_transcripts'))

    async def test_delayed_opening_cannot_reserve_a_pause_or_unconfirmed_boundary(self):
        for options, final, end_ms, owned in (
            ({'report_complete': False}, '喂', 4600, True),
            ({'second_at': 100.7}, '喂', 4600, True),
            ({}, '喂，请写文件。', 4600, True),
            ({}, '喂', 12800, True),
            ({}, '喂', 4600, False),
        ):
            with self.subTest(options=options, final=final, end_ms=end_ms, owned=owned):
                b = await self.delayed_opening_bridge(**options)
                self.finish_delayed_greeting(b, final=final, end_ms=end_ms, owned=owned)
                self.assertEqual(b._consumed_acoustic_sequence, 2)
                self.assertFalse(b._has_caller_evidence(15000, '请写文件。'))
                self.assertFalse(b.daemon.relayed)

    async def test_late_greeting_delta_cannot_spend_reserved_second_audio(self):
        b = await self.delayed_opening_bridge()
        self.finish_delayed_greeting(b)
        for start in (3600, 4400, None):
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '喂', 'start_ms': start, 'end_ms': 4600})
        self.assertFalse(b._pending_input_id)
        self.assertEqual(b._consumed_acoustic_sequence, 1)
        b._on_realtime_event({'type': 'input_transcript.added',
            'text': '修好了吗？', 'start_ms': 9400, 'end_ms': 11000})
        self.assertEqual(b._pending_input_id, 'input-2')
        self.assertFalse(b._ignore_unheard_reply)

    async def test_reserved_physical_question_cannot_authorize_a_third_silent_command(self):
        b = await self.delayed_opening_bridge()
        self.finish_delayed_greeting(b)
        b._on_realtime_event({'type': 'conversation.item.input_audio_transcription.completed',
            'transcript': '修好了吗？', 'start_ms': 9400, 'end_ms': 11000})
        self.assertEqual(b._latest_user_text, '修好了吗？')
        self.assertFalse(b._pending_input_id)
        b._on_realtime_event({'type': 'input_transcript.added',
            'text': '请写文件。', 'start_ms': 15000, 'end_ms': 17000})
        self.assertFalse(b._pending_input_id)
        await contract.ContractTests().drain(b)
        self.assertFalse(b.daemon.relayed)
        b.daemon.classify_phone_intent.assert_awaited_once()

    async def test_input_diagnostics_neither_relax_acoustic_gate_nor_rewrite_pcm(self):
        b = contract.ContractTests().bridge()
        capture = Mock()
        b.rtc = SimpleNamespace(input_track=SimpleNamespace(push_pcm48k=capture))
        try:
            payload = pcm(20, 120)
            b._on_phone_pcm(payload)
            self.assertEqual(b._phone_capture_diagnostics()['callbacks'], 0)
            capture.assert_not_called()
            b.accept_phone_audio = True
            b._on_phone_pcm(payload)
            capture.assert_called_once_with(payload)
            self.assertFalse(b._has_caller_evidence(100, '模拟识别片段'))
            evidence = b.pending.job['phone_rejected_silent_transcripts'][0]['capture']
            self.assertEqual(evidence['peak_block_rms'], 120)
            self.assertEqual(evidence['local_utterances'], 0)
            self.assertEqual(evidence['above_threshold_ms'], 0)
            self.assertEqual(b.daemon.relayed, [])
        finally:
            b.rtc = None
            await b.stop()
        self.assertEqual(b.pending.job['phone_capture_diagnostics']['callbacks'], 1)

    async def test_energy_statistics_cannot_authorize_fragmented_noise_as_command(self):
        b = contract.ContractTests().bridge()
        b._phone_input_observed = True
        try:
            for _ in range(5):
                b._observe_local_caller(pcm(20, 1000))
                b._observe_local_caller(pcm(20, 0))
            self.assertFalse(b._has_caller_evidence(500, '模拟识别片段'))
            metrics = b._phone_capture_diagnostics()
            self.assertEqual(metrics['above_threshold_ms'], 100)
            self.assertEqual(metrics['max_contiguous_above_threshold_ms'], 20)
            self.assertEqual(metrics['local_utterances'], 0)
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_server_final_cannot_commit_while_real_caller_still_speaks(self):
        b = self.bound_input('请删除文件')
        b._schedule_input_settle = lambda: None
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            b._local_caller_active = True
            b._on_realtime_event({'type': 'turn.done', 'turn': {
                'id': 'owned-input', 'role': 'user', 'transcript': '请删除文件',
                'start_ms': 100, 'end_ms': 1100}})
            b._finalize_pending_input()
            await asyncio.sleep(0)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertTrue(b._pending_input_id)
        finally:
            b._local_caller_active = False
            await b.stop()

    async def test_physical_speech_after_server_final_preserves_a_late_negated_tail(self):
        b = self.bound_input('请删除文件')
        b._schedule_input_settle = lambda: None
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        began = time.monotonic()
        try:
            b._local_caller_active = True
            b._pending_local_caller_start_at = began - .9
            b._on_realtime_event({'type': 'turn.done', 'turn': {
                'id': 'owned-input', 'role': 'user', 'transcript': '请删除文件',
                'start_ms': 100, 'end_ms': 1100}})
            with patch('phone_agent.time.monotonic', return_value=began + .5):
                b._observe_local_caller(pcm(100))
            b._local_caller_active = False
            b._caller_turns.voice(False)
            b._last_local_caller_end_at = began + 2.1
            b._finalize_pending_input()
            await asyncio.sleep(0)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertTrue(b._pending_input_id)
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '是什么意思？不要执行。', 'start_ms': 1500, 'end_ms': 3100})
            b._on_realtime_event({'type': 'turn.done', 'turn': {
                'id': 'owned-input', 'role': 'user',
                'transcript': '请删除文件是什么意思？不要执行。', 'start_ms': 100, 'end_ms': 3300}})
            b._finalize_pending_input()
            await contract.ContractTests().drain(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0],
                             '请删除文件是什么意思？不要执行。')
            self.assertFalse(b.daemon.relayed)
            self.assertFalse(b.pending.job.get('phone_rejected_silent_transcripts'))
        finally:
            b._local_caller_active = False
            await b.stop()

    async def test_early_final_without_complete_followup_fails_closed_once(self):
        b = self.bound_input('请删除文件')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            if b._input_settle_task:
                b._input_settle_task.cancel()
                await asyncio.gather(b._input_settle_task, return_exceptions=True)
            b._pending_server_done = True
            b._pending_server_final_interrupted = True
            b._local_caller_active = False
            b._caller_turns.voice(False)
            b._remote_speech_active = False
            b._last_local_caller_end_at = time.monotonic() - 9
            b._pending_input_updated_at = time.monotonic() - 9
            b._schedule_input_settle()
            await b._input_settle_task
            await contract.ContractTests().drain(b)
            b._finalize_pending_input()
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertFalse(b.daemon.relayed)
            self.assertEqual(len(b.pending.job['phone_incomplete_utterances']), 1)
            self.assertEqual(b.local_tts.texts, [INPUT_INCOMPLETE])
        finally:
            await b.stop()

    async def test_hangup_does_not_promote_final_received_during_active_speech(self):
        b = self.bound_input('请删除文件')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._pending_server_done = True
        b._local_caller_active = True
        b._pending_input_ended = True
        await b.stop()
        b.daemon.classify_phone_intent.assert_not_awaited()
        self.assertFalse(b.daemon.relayed)
        self.assertEqual(len(b.pending.job['phone_incomplete_utterances']), 1)

    def bound_input(self, prefix='请写一个标记，内容是'):
        b = contract.ContractTests().bridge()
        b._phone_input_observed = True
        b._caller_acoustic_sequence = 1
        b._on_realtime_event({'type':'input_transcript.added', 'text':prefix, 'start_ms':100, 'end_ms':900})
        b._on_realtime_event({'type':'turn.created', 'turn':{
            'role':'user', 'id':'owned-input', 'transcript':prefix, 'start_ms':100, 'end_ms':900}})
        b._last_local_caller_end_at = time.monotonic()
        b._pending_input_ended = True
        return b

    async def finish_bound_input(self, b):
        b._on_realtime_event({'type':'turn.done', 'turn':{'role':'user','id':'owned-input',
            'transcript':b._pending_input_text, 'start_ms':100, 'end_ms':3000}})
        await asyncio.sleep(.16)
        if b._relay_tasks:
            await asyncio.gather(*tuple(b._relay_tasks))

    async def test_owned_input_retains_tail_after_old_1100ms_settle_boundary(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            await asyncio.sleep(1.2)
            b._finalize_pending_input()  # Even another lifecycle path cannot force the prefix out.
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertTrue(b._pending_input_id)
            b._on_realtime_event({'type':'input_transcript.added','text':'电话指令已接收。',
                'start_ms':1100,'end_ms':2300})
            await self.finish_bound_input(b)
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收。'])
            self.assertNotIn('phone_rejected_silent_transcripts', b.pending.job)
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await b.stop()

    async def test_delayed_question_tail_does_not_become_unheard_input_or_drop_answer(self):
        b = self.bound_input('咱们现在在做什么项目')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        try:
            await asyncio.sleep(1.2)
            b._on_realtime_event({'type':'input_transcript.added','text':'？你给我正常介绍一下。',
                'start_ms':1100,'end_ms':2300})
            await self.finish_bound_input(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0],
                             '咱们现在在做什么项目？你给我正常介绍一下。')
            self.assertFalse(b._ignore_unheard_reply)
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_owned_final_snapshot_can_supply_missing_last_word_deltas(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'owned-input',
                'start_ms':100,'end_ms':2300,'transcript':'请写一个标记，内容是电话指令已接收。'}})
            await asyncio.sleep(.16)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收。'])
        finally:
            await b.stop()

    async def test_missing_owned_final_is_incomplete_with_one_notice_not_a_command(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._pending_input_updated_at = time.monotonic()-2
        b._last_local_caller_end_at = time.monotonic()-9
        b._schedule_input_settle()
        try:
            await asyncio.sleep(.15)
            self.assertFalse(b.daemon.relayed)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertEqual(b.local_tts.texts, [INPUT_INCOMPLETE])
            self.assertEqual(b._caller_turns.turns['input-1']['status'], 'incomplete')
            b._on_realtime_event({'type':'input_transcript.added','text':'迟到内容',
                'start_ms':1100,'end_ms':2300})
            self.assertFalse(b._pending_input_id)
        finally:
            await b.stop()

    async def test_owned_final_after_retained_6200ms_delay_still_completes_once(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._pending_input_updated_at = time.monotonic()-2
        b._last_local_caller_end_at = time.monotonic()-6.3
        b._schedule_input_settle()
        try:
            await asyncio.sleep(.15)
            self.assertTrue(b._pending_input_id)
            b.daemon.classify_phone_intent.assert_not_awaited()
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'owned-input',
                'start_ms':100,'end_ms':2300,'transcript':'请写一个标记，内容是电话指令已接收。'}})
            await asyncio.sleep(.16)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收。'])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await b.stop()

    async def test_owned_snapshot_does_not_duplicate_late_covered_word_deltas(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'owned-input',
                'start_ms':100,'end_ms':2300,'transcript':'请写一个标记，内容是电话指令已接收。'}})
            b._on_realtime_event({'type':'input_transcript.added','text':'电话指令已接收。',
                'start_ms':1100,'end_ms':2300})
            await asyncio.sleep(.16)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收。'])
        finally:
            await b.stop()

    async def test_unrelated_grouped_final_cannot_close_owned_input(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'old-aggregate',
                'start_ms':0,'end_ms':4000,'transcript':b._pending_input_text}})
            await asyncio.sleep(.16)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertTrue(b._awaiting_owned_input_final())
        finally:
            await b.stop()

    async def test_inconsistent_owned_final_never_authorizes_rewritten_command(self):
        b = self.bound_input('不要执行删除')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'owned-input',
                'start_ms':100,'end_ms':2300,'transcript':'执行删除'}})
            await asyncio.sleep(.16)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertFalse(b.daemon.relayed)
            self.assertEqual(b.local_tts.texts, [INPUT_INCOMPLETE])
        finally:
            await b.stop()

    async def test_hangup_cannot_force_an_owned_unfinished_input_to_execute(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        await b.stop()
        self.assertFalse(b.daemon.relayed)
        b.daemon.classify_phone_intent.assert_not_awaited()
        self.assertEqual(b.pending.job['phone_incomplete_utterances'][0]['reason'],
                         'hangup_before_confirmed_speech_end')

    def handoff(self, text, identity='owned-input', offset=900):
        return {'type':'delegation.created','offset_ms':offset,'item':{
            'user_bidi_turn_id':identity,'content':[{'type':'input_text','text':text}]}}

    async def test_exact_owned_handoff_finishes_before_backing_reply_not_as_action_permission(self):
        b = self.bound_input('请写一个标记，内容是电话指令已接收。')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event(self.handoff(b._pending_input_text))
            await asyncio.sleep(.16)
            # Read-only classification can overlap; ASR grace still forbids
            # committing its result or emitting an execution receipt.
            self.assertTrue(b._pending_input_id)
            self.assertFalse(b._intent_decisions)
            self.assertFalse(b.daemon.relayed)
            self.assertFalse(b.local_tts.texts)
            await asyncio.sleep(1.05)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收。'])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await b.stop()

    async def test_owned_question_handoff_still_uses_structured_question_decision(self):
        b = self.bound_input('电话里的任务会在哪里执行？')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        try:
            b._on_realtime_event(self.handoff(b._pending_input_text))
            await asyncio.sleep(1.2)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_duplicate_owned_handoff_keeps_the_original_grace_clock(self):
        b = self.bound_input('电话里的任务会在哪里执行？')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        try:
            event = self.handoff(b._pending_input_text)
            b._on_realtime_event(event)
            first_clock = b._pending_input_updated_at
            first_waiter = b._input_settle_task
            with patch('phone_agent.time.monotonic', return_value=first_clock + .8):
                b._on_realtime_event(event)
            self.assertEqual(b._pending_input_updated_at, first_clock)
            self.assertIs(b._input_settle_task, first_waiter)
            self.assertEqual(len(b.pending.job['phone_input_boundaries']), 1)
            b.daemon.classify_phone_intent.assert_not_awaited()
        finally:
            await b.stop()

    async def test_duplicate_handoff_does_not_postpone_the_same_complete_command(self):
        text = '请写一个标记，内容是电话指令已接收。'
        b = self.bound_input(text)
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            event = self.handoff(text)
            b._on_realtime_event(event)
            await asyncio.sleep(.65)
            b._on_realtime_event(event)
            self.assertTrue(b._pending_input_id)
            self.assertFalse(b._intent_decisions)
            self.assertFalse(b.daemon.relayed)
            await asyncio.sleep(.65)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, [text])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await b.stop()

    async def test_new_words_still_require_a_new_handoff_and_full_grace(self):
        b = self.bound_input('请写一个标记')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        try:
            old = self.handoff(b._pending_input_text)
            b._on_realtime_event(old)
            b._on_realtime_event({'type': 'input_transcript.added', 'text': '，但先不要执行。',
                                 'start_ms': 1100, 'end_ms': 2300})
            b._on_realtime_event(old)
            self.assertFalse(b._pending_server_handoff)
            self.assertTrue(b._awaiting_owned_input_final())
            complete = self.handoff(b._pending_input_text, offset=2300)
            b._on_realtime_event(complete)
            self.assertTrue(b._pending_server_handoff)
            await asyncio.sleep(.2)
            self.assertTrue(b._pending_input_id)
            self.assertFalse(b._intent_decisions)
            self.assertFalse(b.daemon.relayed)
            await asyncio.sleep(1)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0],
                             '请写一个标记，但先不要执行。')
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_nonfinite_handoff_offset_never_confirms_an_input(self):
        for offset in (float('nan'), float('inf'), float('-inf'), True, None, '900'):
            with self.subTest(offset=offset):
                b = self.bound_input()
                b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
                try:
                    b._on_realtime_event(self.handoff(b._pending_input_text, offset=offset))
                    self.assertFalse(b._pending_server_handoff)
                    self.assertTrue(b._awaiting_owned_input_final())
                    b._finalize_pending_input()
                    b.daemon.classify_phone_intent.assert_not_awaited()
                finally:
                    await b.stop()

    async def test_unrelated_rewritten_or_early_handoff_cannot_finish_owned_input(self):
        for text, identity, offset, speaking in [
                ('请写一个标记，内容是', 'other-turn', 900, False),
                ('请执行删除', 'owned-input', 900, False),
                ('请写一个标记，内容是', 'owned-input', 100, False),
                ('请写一个标记，内容是', 'owned-input', 900, True)]:
            with self.subTest(identity=identity, offset=offset, speaking=speaking):
                b = self.bound_input()
                b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
                try:
                    b._local_caller_active = speaking
                    b._on_realtime_event(self.handoff(text, identity, offset))
                    self.assertTrue(b._awaiting_owned_input_final())
                    b._finalize_pending_input()
                    b.daemon.classify_phone_intent.assert_not_awaited()
                finally:
                    b._local_caller_active = False
                    await b.stop()

    async def test_new_recognized_tail_revokes_prior_matching_handoff(self):
        b = self.bound_input()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._on_realtime_event(self.handoff(b._pending_input_text))
            self.assertFalse(b._awaiting_owned_input_final())
            b._on_realtime_event({'type':'input_transcript.added','text':'先不要执行。',
                'start_ms':1100,'end_ms':2300})
            self.assertTrue(b._awaiting_owned_input_final())
            b._finalize_pending_input()
            b.daemon.classify_phone_intent.assert_not_awaited()
        finally:
            await b.stop()

    async def test_recognized_tail_covering_real_voice_does_not_require_punctuation_or_unemitted_final(self):
        b = self.bound_input('请写一个标记，内容是电话指令已接收')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._pending_local_caller_start_at = b._last_local_caller_end_at - .9
        try:
            self.assertTrue(b._recognized_tail_covers_local_speech())
            await asyncio.sleep(1.2)
            await asyncio.gather(*tuple(b._relay_tasks))
            self.assertEqual(b.daemon.relayed, ['请写一个标记，内容是电话指令已接收'])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await b.stop()

    async def test_punctuated_prefix_missing_acoustic_tail_remains_unexecutable(self):
        b = self.bound_input('请写一个标记。')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._pending_local_caller_start_at = b._last_local_caller_end_at - 3
        try:
            self.assertFalse(b._recognized_tail_covers_local_speech())
            await asyncio.sleep(1.2)
            b._finalize_pending_input()
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertFalse(b.daemon.relayed)
        finally:
            await b.stop()

    async def test_acoustically_covered_sentence_does_not_finalize_during_new_speech(self):
        b = self.bound_input('请写一个标记。')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._pending_local_caller_start_at = b._last_local_caller_end_at - .9
        b._local_caller_active = True
        try:
            b._finalize_pending_input()
            b.daemon.classify_phone_intent.assert_not_awaited()
        finally:
            b._local_caller_active = False
            await b.stop()

    async def test_terminal_punctuation_without_acoustic_clock_proof_does_not_close_input(self):
        b = self.bound_input('请写一个标记。')
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        try:
            b._finalize_pending_input()
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertTrue(b._awaiting_owned_input_final())
        finally:
            await b.stop()

    async def test_drifting_text_clock_does_not_discard_received_short_reply(self):
        b = contract.ContractTests().bridge()
        b._latest_user_turn_id = 'question'
        b._assistant_user_turn_ids['answer'] = 'question'
        for stamp in range(42000, 43020, 20):
            b._on_pcm_frame(pcm(20, 1000 if stamp >= 42400 else 0),
                            {'media_ms': stamp, 'duration_ms': 20, 'arrival': time.monotonic()})
        b._on_realtime_event({'type': 'output_transcript.added', 'start_ms': 45800,
                              'end_ms': 46000, 'text': '可以的'})
        b._on_realtime_event({'type': 'turn.created', 'turn': {
            'id': 'answer', 'role': 'assistant', 'start_ms': 46600,
            'end_ms': 46800, 'transcript': '可以的'}})
        self.assertLessEqual(b._media_fence.minimum_ms, 42400)
        self.assertGreaterEqual(len(b._realtime_audio_buffer), 600 * 96)
        self.assertEqual(b.pending.job['phone_output_clock_alignment']['offset_ms'], 2800)

    def test_word_clock_maps_start_and_end_in_the_same_received_domain(self):
        clock = OutputTranscriptClock()
        clock.observe({'start_ms': 167200, 'end_ms': 167400, 'text': '完整的'}, media_ms=154700)
        clock.observe({'start_ms': 172000, 'end_ms': 172200, 'text': '回答。'}, media_ms=159500)
        turn = {'start_ms': 168200, 'end_ms': 174600, 'transcript': '完整的回答。'}
        self.assertEqual(clock.start_for(turn), 154700)
        self.assertEqual(clock.end_for(turn), 159700)
        self.assertEqual(clock.media_time(174600), 162100)
        clock.clear()
        self.assertEqual(clock.start_for(turn), 168200)

    def test_long_reply_does_not_lose_first_word_at_final_punctuation(self):
        clock = OutputTranscriptClock()
        for index in range(150):
            clock.observe({'start_ms': 10000+index*200, 'end_ms': 10200+index*200,
                           'text': '字'})
        clock.observe({'start_ms': 40000, 'end_ms': 40200, 'text': '。'})
        turn = {'start_ms': 10200, 'end_ms': 43000, 'transcript': '字'*150+'。'}
        self.assertEqual(clock.start_for(turn), 10000)
        self.assertEqual(clock.end_for(turn), 40000)

    async def test_stale_receive_cursor_cannot_reanchor_a_word_event(self):
        b = contract.ContractTests().bridge()
        b._latest_audio_frame_metadata = {'media_ms': 20, 'arrival': time.monotonic()-5}
        b._on_realtime_event({'type':'output_transcript.added', 'start_ms':10000,
                              'end_ms':10200, 'text':'新的回答'})
        self.assertFalse(b._output_clock.media_observations)
        self.assertNotIn('phone_output_clock_alignment', b.pending.job)

    def test_complete_word_clock_excludes_aggregate_trailing_silence(self):
        clock = OutputTranscriptClock()
        clock.observe({'type': 'output_transcript.added', 'start_ms': 146000,
                       'end_ms': 146800, 'text': '完整的'})
        clock.observe({'type': 'output_transcript.added', 'start_ms': 146800,
                       'end_ms': 147400, 'text': '回答。'})
        turn = {'start_ms': 146000, 'end_ms': 149600, 'transcript': '完整的回答。'}
        self.assertEqual(clock.end_for(turn), 147400)
        self.assertEqual(clock.end_for({**turn, 'transcript': '完整的回答。还有下半句。'}), 149600)
        self.assertEqual(clock.end_for({**turn, 'start_ms': 152000}), 149600)
        clock.clear()
        self.assertEqual(clock.end_for(turn), 149600)

    def test_distant_previous_turn_preserves_a_short_paused_intro_only_with_evidence(self):
        for previous, expected in ((129000, 141580), (142000, 142060), (None, 142060)):
            for data_first in (False, True):
                fence = AudioTurnFence()
                if data_first:
                    fence.begin('new-reply', 142800, previous_end_ms=previous)
                for stamp in range(141000, 143020, 20):
                    active = 141620 <= stamp <= 141800 or stamp >= 142100
                    fence.receive(pcm(20, 1000 if active else 0), {'media_ms': stamp})
                    fence.take_ready()
                if not data_first:
                    fence.begin('new-reply', 142800, previous_end_ms=previous)
                self.assertEqual(fence.minimum_ms, expected)

    def test_long_idle_word_clock_lag_does_not_cut_the_first_clause(self):
        for data_first in (False, True):
            fence = AudioTurnFence()
            ready = []
            if data_first:
                ready.extend(fence.begin('after-idle', 220000))
            for stamp in range(218400, 220220, 20):
                block = pcm(20, 1000 if stamp >= 219040 else 0)
                if fence.receive(block, {'media_ms': stamp}):
                    ready.append(block)
                ready.extend(fence.take_ready())
            if not data_first:
                ready.extend(fence.begin('after-idle', 220000))
            self.assertEqual(fence.minimum_ms, 219000)
            self.assertEqual(sum(len(block) for block in ready) / 96, 1220)

    def test_fence_preserves_onset_when_data_event_beats_media(self):
        fence=AudioTurnFence()
        self.assertEqual(fence.begin('reply',29600),[])
        played=[]
        for stamp in range(28600,29900,20):
            block=pcm(20,1000 if stamp>=29140 else 0)
            if fence.receive(block,{'media_ms':stamp}): played.append(block)
            played.extend(fence.take_ready())
        self.assertEqual(fence.minimum_ms,29100)
        self.assertEqual(sum(len(p) for p in played)/96,800)

    def test_fence_uses_actual_phoneme_onset_without_replaying_separate_old_tail(self):
        fence=AudioTurnFence()
        for stamp in range(28600,29900,20):
            fence.receive(pcm(20,1000 if stamp>=29140 else 0),{'media_ms':stamp})
        fence.begin('new',29600)
        self.assertEqual(fence.minimum_ms,29100)
        fence.interrupt()
        for stamp in range(28600,29900,20):
            fence.receive(pcm(20,1000 if stamp<29200 or stamp>=29600 else 0),{'media_ms':stamp})
        fence.begin('next',29600)
        self.assertEqual(fence.minimum_ms,29480)

    def test_real_late_turn_timestamp_does_not_drop_its_first_words(self):
        clock=OutputTranscriptClock()
        for stamp,text in [(28000,'现在在'),(28200,'做的是'),(28600,'会'),(28800,'话级')]:
            clock.observe({'start_ms':stamp,'item':{'text':text}})
        turn={'start_ms':28800,'transcript':'现在在做的是会'}
        self.assertEqual(clock.start_for(turn),28000)
        self.assertEqual(clock.start_for({'start_ms':30000,'transcript':'不同的下一句'}),30000)

    async def test_cancel_classified_after_action_prevents_uncommitted_send(self):
        flow = CallerTurns()
        flow.observe('action', '保存文件', complete=True)
        flow.observe('cancel', '别执行')
        flow.decide('action', 'action')
        sending = asyncio.create_task(flow.commit('action'))
        await asyncio.sleep(0)
        self.assertFalse(sending.done())
        flow.decide('cancel', 'cancel')
        self.assertFalse(await sending)

    async def test_a_later_question_does_not_cancel_the_previous_action(self):
        flow = CallerTurns()
        flow.observe('action', complete=True)
        flow.observe('question', complete=True)
        flow.decide('action', 'action')
        sending = asyncio.create_task(flow.commit('action'))
        await asyncio.sleep(0)
        self.assertFalse(sending.done())
        flow.decide('question', 'question')
        self.assertTrue(await sending)
        self.assertFalse(await flow.commit('action'))

    async def test_actions_preserve_original_order_even_if_classified_backwards(self):
        flow = CallerTurns()
        for identity in ('first', 'second'):
            flow.observe(identity, complete=True)
        flow.decide('second', 'action')
        second = asyncio.create_task(flow.commit('second'))
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        flow.decide('first', 'action')
        self.assertTrue(await flow.commit('first'))
        self.assertTrue(await second)

    async def test_cancel_after_commit_does_not_claim_an_undo(self):
        flow = CallerTurns()
        flow.decide('first', 'action')
        self.assertTrue(await flow.commit('first'))
        result = flow.decide('cancel', 'cancel')
        self.assertEqual(result['already_committed'], ['first'])
        self.assertEqual(result['cancelled'], [])
        flow.decide('later', 'action')
        self.assertTrue(await flow.commit('later'))

    async def test_incomplete_new_input_defers_instead_of_sending_old_action(self):
        flow = CallerTurns()
        flow.decide('first', 'action')
        flow.observe('partial', '不要')
        flow.abandon('partial')
        self.assertFalse(await flow.commit('first', timeout=.01))
        self.assertEqual(flow.turns['first']['status'], 'deferred')


class FlowRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_clarification_cannot_speak_for_newer_input(self):
        for kind in ('question', 'action'):
            b = self.bridge(actual_audio=True)
            entered, release = asyncio.Event(), asyncio.Event()
            async def classify(text, context):
                if text == '刚才那个问题':
                    entered.set()
                    await release.wait()
                    return {'kind': 'clarify'}
                return {'kind': kind}
            b.daemon.classify_phone_intent = classify
            try:
                b._complete_transcript_turn('user', '刚才那个问题', 'old')
                await entered.wait()
                b._mark_remote_speech(interrupt=True)
                b._complete_transcript_turn('user', '补充完整的新输入。', 'new')
                release.set()
                await self.drain(b)
                self.assertNotIn(INTENT_CLARIFY, b.local_tts.texts)
                self.assertEqual(len(b.daemon.relayed), int(kind == 'action'))
            finally:
                release.set()
                await self.close(b)

    async def test_current_clarification_is_spoken_once_with_its_original_identity(self):
        b = self.bridge(actual_audio=True)
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'clarify'})
        b._complete_transcript_turn('user', '刚才那个问题', 'current')
        await self.drain(b)
        self.assertEqual(b.local_tts.texts, [INTENT_CLARIFY])
        rows = b.audio.playback_snapshot()
        self.assertEqual(rows[0]['kind'], 'intent_clarification')
        self.assertEqual(b._assistant_user_turn_ids[rows[0]['id']], 'current')
        await self.close(b)

    async def test_interrupted_clarification_is_not_resumed_after_farewell(self):
        b = self.bridge(actual_audio=True)
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'clarify'})
        b._complete_transcript_turn('user', '刚才那个问题', 'old')
        await self.drain(b)
        b._mark_remote_speech(interrupt=True)
        b._complete_transcript_turn('user', '拜拜。', 'farewell')
        b._last_speech_stopped_at = time.monotonic() - 2
        await self.drain(b)
        self.assertEqual([row['status'] for row in b.audio.playback_snapshot()], ['cancelled'])
        self.assertEqual(b.local_tts.texts, [INTENT_CLARIFY])
        await self.close(b)

    async def test_interrupted_real_receipt_resumes_for_original_command(self):
        b = self.bridge(actual_audio=True)
        b._latest_user_turn_id = 'original-command'
        await b._render_local_speech(COMMAND_QUEUED, b._speech_generation,
                                    kind='command_receipt', user_turn_id='original-command')
        b._mark_remote_speech(interrupt=True)
        b._latest_user_turn_id = 'new-question'
        b._last_speech_stopped_at = time.monotonic() - 2
        await self.drain(b)
        rows = b.audio.playback_snapshot()
        self.assertEqual([row['status'] for row in rows], ['cancelled', 'queued'])
        self.assertEqual([b._assistant_user_turn_ids[row['id']] for row in rows],
                         ['original-command', 'original-command'])
        self.assertFalse(b.daemon.relayed)
        await self.close(b)

    async def untranscribed_opening(self):
        b = self.bridge()
        b._announcement_delivered = False
        b._first_assistant_response_pending = True
        b._announcement_pcm = pcm(4040)
        for payload in (pcm(200, 435), pcm(420, 0)):
            b._observe_local_greeting(payload)
            b._observe_local_caller(payload)
        await b._deliver_announcement()
        return b

    async def timeout_opening(self):
        b = self.bridge(actual_audio=True)
        b.remote_speech_seen.clear()
        b._announcement_delivered = False
        b._first_assistant_response_pending = True
        b._announcement_pcm = pcm(4300)
        b.pending.job['announcement_trigger'] = 'pickup_timeout'
        await b._deliver_announcement()
        return b

    @staticmethod
    def consume_output(b, milliseconds):
        for _ in range(milliseconds // 20):
            b.audio._output_callback(bytearray(1920), 960, None, None)

    async def test_first_untranscribed_greeting_during_timeout_report_does_not_reply_twice(self):
        b = await self.timeout_opening()
        try:
            # Retained call: report queued, first caller onset +720 ms,
            # no greeting transcript, live greeting after the 4300 ms report.
            self.consume_output(b, 720)
            for payload in (pcm(120, 435), pcm(420, 0)):
                b._observe_local_greeting(payload)
                b._observe_local_caller(payload)
            self.consume_output(b, 3580)
            self.assertEqual(b.audio.playback_snapshot()[0]['status'], 'output_complete')
            await self.opening_reply(b, '喂,我在,听得见。', duration=1280)
            self.assertIn('opening-reply', b._untranscribed_opening_reply_ids)
            self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertNotIn('opening-reply', b._realtime_early_allowed_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 1)
            self.assertFalse(b.local_tts.texts)
            self.assertFalse(b.daemon.relayed)
            self.assertFalse(b._caller_turns.turns)
        finally:
            await self.close(b)

    async def test_first_greeting_while_report_is_queued_is_still_opening(self):
        b = await self.timeout_opening()
        try:
            b._observe_local_caller(pcm(120, 435))
            b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '您好，我在听，您请讲。')
            self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 1)
        finally:
            await self.close(b)

    async def test_first_greeting_after_report_output_completes_still_gets_answer(self):
        b = await self.timeout_opening()
        try:
            self.consume_output(b, 4300)
            # Its onset is genuinely later, not VAD's delayed detection of
            # an utterance that began during the final output callback.
            with patch('phone_agent.time.monotonic', return_value=time.monotonic() + 1):
                b._observe_local_caller(pcm(120, 435))
                b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '喂,我在,听得见。', duration=1280)
            self.assertNotIn('opening-reply', b._untranscribed_opening_reply_ids)
            self.assertNotIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 2)
        finally:
            await self.close(b)

    async def test_first_greeting_onset_before_last_callback_keeps_opening_ownership(self):
        b = await self.timeout_opening()
        try:
            self.consume_output(b, 4300)
            finished = b.audio.playback_snapshot()[0]['output_finished_at']
            with patch('phone_agent.time.monotonic', return_value=finished + .05):
                b._observe_local_caller(pcm(120, 435))
                b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '喂,我在,听得见。', duration=1280)
            self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(b.pending.job['phone_opening_greeting_binding']
                             ['report_status_at_detection'], 'output_complete')
            self.assertEqual(len(b.audio.playback_snapshot()), 1)
        finally:
            await self.close(b)

    async def test_cancelled_report_is_not_an_opening_output_window(self):
        for consumed in (0, 720):
            with self.subTest(consumed=consumed):
                b = await self.timeout_opening()
                try:
                    self.consume_output(b, consumed)
                    b.audio.clear_output()
                    b._observe_local_caller(pcm(120, 435))
                    b._observe_local_caller(pcm(420, 0))
                    await self.opening_reply(b, '喂,我在,听得见。', duration=1280)
                    self.assertNotIn('opening-reply', b._untranscribed_opening_reply_ids)
                    self.assertNotIn('opening-reply', b._suppressed_assistant_turn_ids)
                finally:
                    await self.close(b)

    async def test_later_greeting_after_timeout_opening_keeps_its_own_reply(self):
        b = await self.timeout_opening()
        try:
            b._observe_local_caller(pcm(120, 435))
            b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '喂,我在,听得见。', duration=1280)
            self.consume_output(b, 4300)
            b._observe_local_caller(pcm(120, 435))
            b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '听得见，您说。', turn_id='later-reply')
            self.assertNotIn('later-reply', b._untranscribed_opening_reply_ids)
            self.assertNotIn('later-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 2)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_substantive_reply_to_first_utterance_during_report_is_not_dropped(self):
        b = await self.timeout_opening()
        try:
            b._observe_local_caller(pcm(120, 435))
            b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '喂，我在听。这个问题还没有完全修好。', duration=3800)
            self.assertNotIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 2)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_explicit_repeat_request_after_report_is_allowed_once(self):
        b = await self.timeout_opening()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        try:
            self.consume_output(b, 4300)
            b._complete_transcript_turn('user', '把开头的汇报再说一遍。', 'repeat-request')
            await self.drain(b)
            await self.opening_reply(b, b._announcement_text, turn_id='requested-repeat')
            self.assertNotIn('requested-repeat', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.playback_snapshot()), 2)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_command_during_timeout_report_still_dispatches_once(self):
        b = await self.timeout_opening()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            b._observe_local_caller(pcm(120, 435))
            b._observe_local_caller(pcm(420, 0))
            b._complete_transcript_turn('user', '继续排查重复开场。', 'command')
            await self.drain(b)
            b._complete_transcript_turn('assistant', '收到，我来检查。', 'speculative-ack')
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['继续排查重复开场。'])
            self.assertIn('speculative-ack', b._suppressed_assistant_turn_ids)
            self.assertEqual(sum(row['kind'] == 'command_receipt'
                                 for row in b.audio.playback_snapshot()), 1)
        finally:
            await self.close(b)

    async def test_untranscribed_initial_greeting_never_triggers_voice_alarm(self):
        b = await self.untranscribed_opening()
        b._begin_realtime_capture('opening-reply')
        b._on_pcm_output(pcm(560))
        b._complete_transcript_turn('assistant', '喂，在的，听得见。', 'opening-reply')
        b._finish_realtime_utterance('opening-reply', '喂，在的，听得见。')
        await self.drain(b)
        self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
        self.assertNotIn(VOICE_FAILURE, b.local_tts.texts)
        self.assertEqual(len(b.audio.played), 1)  # Dedicated report only.
        await self.close(b)

    async def opening_reply(self, bridge, text, *, turn_id='opening-reply', duration=2400):
        bridge._begin_realtime_capture(turn_id)
        bridge._on_pcm_output(pcm(duration))
        bridge._complete_transcript_turn('assistant', text, turn_id)
        bridge._finish_realtime_utterance(turn_id, text)
        await self.drain(bridge)

    async def test_recorded_untranscribed_greeting_with_you_speak_plays_only_report(self):
        b = await self.untranscribed_opening()
        try:
            # Retained call: 1540 ms live reply followed the dedicated report.
            await self.opening_reply(b, '喂,我在,听得见,你说。', duration=1540)
            self.assertIn('opening-reply', b._untranscribed_opening_reply_ids)
            self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
            self.assertNotIn('opening-reply', b._realtime_early_allowed_turn_ids)
            self.assertEqual(len(b.audio.played), 1)
            self.assertFalse(b.local_tts.texts)
            self.assertFalse(b.daemon.relayed)
            self.assertFalse([t for t in b._transcript_turns if t['role'] == 'user'])
        finally:
            await self.close(b)

    async def test_untranscribed_opening_suppresses_polite_turn_handover_variants(self):
        for text in ('喂，我在，您说。', '你好，听得到，你讲。',
                     '您好，我能听见您说话，您请讲。', '在的，请说。',
                     '我在听，您继续说吧。', '听得见，你请继续讲。',
                     '喂，听得到您的声音，您说吧。', '喂，我在呢，请讲。'):
            with self.subTest(text=text):
                b = await self.untranscribed_opening()
                try:
                    await self.opening_reply(b, text)
                    self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
                    self.assertEqual(len(b.audio.played), 1)
                    self.assertFalse(b.local_tts.texts)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    await self.close(b)

    async def test_recorded_initial_ei_uses_semantic_greeting_and_never_streams_reply(self):
        b = await self.untranscribed_opening()
        classified = asyncio.Event()
        async def classify(text, context):
            self.assertEqual(text, '诶')
            await classified.wait()
            return {'kind': 'greeting'}
        b.daemon.classify_phone_intent = classify
        try:
            b._complete_transcript_turn('user', '诶', 'input-1')
            b._begin_realtime_capture('reply')
            b._on_pcm_output(pcm(1800))
            b._complete_transcript_turn('assistant', '喂，在的，我听得见。', 'reply')
            b._finish_realtime_utterance('reply', '喂，在的，我听得见。')
            self.assertEqual(len(b.audio.played), 1)
            classified.set()
            await self.drain(b)
            self.assertIn('reply', b._suppressed_assistant_turn_ids)
            self.assertNotIn('reply', b._realtime_early_allowed_turn_ids)
            self.assertEqual(len(b.audio.played), 1)
            self.assertFalse(b.daemon.relayed)
            self.assertFalse(b.local_tts.texts)
        finally:
            classified.set()
            await self.close(b)

    async def test_later_semantic_greeting_is_not_suppressed(self):
        b = await self.untranscribed_opening()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'greeting'})
        try:
            b._complete_transcript_turn('user', '喂', 'initial')
            await self.drain(b)
            b._complete_transcript_turn('user', '诶，听得到不？', 'later')
            await self.opening_reply(b, '听得到，我在听。', turn_id='later-reply')
            self.assertNotIn('later-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.played), 2)
        finally:
            await self.close(b)

    async def test_untranscribed_complete_greetings_do_not_start_another_online_classification(self):
        for phrase in ('好的，我在听，您说吧。', '嗯，听得见，您请说。',
                       '您先说，我听着。', '哈喽，这边听得见您的声音，您继续说吧。'):
            with self.subTest(phrase=phrase):
                b = await self.untranscribed_opening()
                b._intent_router = SimpleNamespace(
                    classify_opening_reply=AsyncMock(side_effect=AssertionError('No extra online gate')),
                    classify=AsyncMock(side_effect=AssertionError('Not caller input')))
                try:
                    await self.opening_reply(b, phrase, duration=2600)
                    self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
                    self.assertEqual(len(b.audio.played), 1)
                    self.assertFalse(b.daemon.relayed)
                    self.assertFalse(b._caller_turns.turns)
                    b._intent_router.classify.assert_not_awaited()
                    b._intent_router.classify_opening_reply.assert_not_awaited()
                finally:
                    await self.close(b)

    async def test_untranscribed_complete_greeting_prefix_retains_the_substantive_tail(self):
        b = await self.untranscribed_opening()
        text = '好的，我在听。刚才失败是因为连接还没有就绪。'
        b._intent_router = SimpleNamespace(classify_opening_reply=AsyncMock())
        try:
            b._record_transcript_turn('reply', 'assistant', '好的，我在听。')
            b._begin_realtime_capture('reply')
            b._on_pcm_output(pcm(5000))
            b._intent_router.classify_opening_reply.assert_not_awaited()
            b._complete_transcript_turn('assistant', text, 'reply')
            b._finish_realtime_utterance('reply', text)
            await self.drain(b)
            self.assertEqual(len(b.audio.played), 2)
            self.assertNotIn('reply', b._suppressed_assistant_turn_ids)
            b._intent_router.classify_opening_reply.assert_not_awaited()
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_semantic_initial_greeting_releases_response_under_server_alias_once(self):
        b = await self.untranscribed_opening()
        ready = asyncio.Event()
        async def classify(text, context):
            await ready.wait()
            return {'kind': 'greeting'}
        b.daemon.classify_phone_intent = classify
        try:
            b._complete_transcript_turn('user', '诶', 'input-1')
            b._user_turn_aliases['server-user'] = 'input-1'
            b._assistant_user_turn_ids['reply'] = 'server-user'
            b._begin_realtime_capture('reply')
            b._on_pcm_output(pcm(1800))
            b._complete_transcript_turn('assistant', '喂，我听得见。', 'reply')
            b._finish_realtime_utterance('reply', '喂，我听得见。')
            self.assertEqual(len(b.audio.played), 1)
            ready.set()
            await self.drain(b)
            self.assertIn('reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.played), 1)
            self.assertFalse(b._deferred_intent_releases)
            self.assertFalse(b.daemon.relayed)
        finally:
            ready.set()
            await self.close(b)

    async def test_semantic_initial_greeting_decided_before_reply_still_suppresses_it(self):
        b = await self.untranscribed_opening()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'greeting'})
        try:
            b._complete_transcript_turn('user', '诶', 'initial')
            await self.drain(b)
            await self.opening_reply(b, '喂，这边可以听到您。', duration=2200)
            self.assertEqual(len(b.audio.played), 1)
            self.assertIn('opening-reply', b._suppressed_assistant_turn_ids)
        finally:
            await self.close(b)

    async def test_handover_prefix_waits_for_substantive_tail_and_plays_full_answer(self):
        b = await self.untranscribed_opening()
        try:
            prefix = '喂，听得见，您说。'
            text = prefix + '刚才的连接还没有恢复，需要继续检查。'
            b._record_transcript_turn('answer', 'assistant', prefix)
            b._begin_realtime_capture('answer')
            b._on_pcm_output(pcm(5000))
            self.assertEqual(len(b.audio.played), 1)
            self.assertNotIn('answer', b._realtime_early_allowed_turn_ids)
            b._complete_transcript_turn('assistant', text, 'answer')
            b._finish_realtime_utterance('answer', text)
            await self.drain(b)
            self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.played), 2)
            self.assertEqual(b._transcript_text_for_turn('answer'), text)
            self.assertFalse(b.local_tts.texts)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_you_speak_in_a_substantive_answer_is_not_a_greeting(self):
        for text in ('您说的那个问题还没有修好。', '听不到您说话，请检查麦克风。',
                     '我在检查文件，稍后汇报结果。'):
            with self.subTest(text=text):
                b = await self.untranscribed_opening()
                try:
                    await self.opening_reply(b, text, duration=3600)
                    self.assertNotIn('opening-reply', b._suppressed_assistant_turn_ids)
                    self.assertEqual(len(b.audio.played), 2)
                    self.assertFalse(b.local_tts.texts)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    await self.close(b)

    async def test_later_untranscribed_greeting_with_you_speak_still_gets_answer(self):
        b = await self.untranscribed_opening()
        try:
            b._observe_local_caller(pcm(220, 435))
            b._observe_local_caller(pcm(420, 0))
            await self.opening_reply(b, '喂,我在,听得见,你说。',
                                     turn_id='later-reply', duration=1540)
            self.assertNotIn('later-reply', b._untranscribed_opening_reply_ids)
            self.assertNotIn('later-reply', b._suppressed_assistant_turn_ids)
            self.assertEqual(len(b.audio.played), 2)
            self.assertFalse(b.local_tts.texts)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_untranscribed_opening_candidate_waits_for_full_reply_not_greeting_prefix(self):
        b = await self.untranscribed_opening()
        b._record_transcript_turn('answer', 'assistant', '喂，在的。')
        self.assertNotIn('answer', b._realtime_early_allowed_turn_ids)
        text = '喂，在的。刚才检查失败是因为连接尚未就绪。'
        b._complete_transcript_turn('assistant', text, 'answer')
        self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
        self.assertIn('answer', b._realtime_early_allowed_turn_ids)
        await self.close(b)

    async def test_new_acoustic_greeting_without_transcript_is_not_initial_greeting(self):
        b = await self.untranscribed_opening()
        b._observe_local_caller(pcm(120, 435))
        b._observe_local_caller(pcm(420, 0))
        b._complete_transcript_turn('assistant', '喂，在的，听得见。', 'later-reply')
        self.assertNotIn('later-reply', b._suppressed_assistant_turn_ids)
        self.assertIn('later-reply', b._realtime_early_allowed_turn_ids)
        await self.close(b)

    async def test_initial_greeting_prefix_cannot_suppress_answer_to_late_question_tail(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        b._on_realtime_event({'type':'input_transcript.added', 'text':'喂，', 'start_ms':100, 'end_ms':250})
        b._complete_transcript_turn('assistant', '还没有全部修好。', 'answer')
        self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
        b._on_realtime_event({'type':'input_transcript.added', 'text':'现在修好了没有？', 'start_ms':300, 'end_ms':900})
        b._finalize_pending_input()
        await self.drain(b)
        self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
        self.assertIn('answer', b._realtime_early_allowed_turn_ids)
        self.assertFalse(b.daemon.relayed)
        await self.close(b)

    bridge = contract.ContractTests.bridge
    drain = contract.ContractTests.drain
    async def close(self, bridge):
        await bridge.stop()

    async def test_aggregate_tail_restarts_settle_and_preserves_final_negation(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        b._on_realtime_event({'type':'input_transcript.added','text':'请先解释', 'start_ms':100,'end_ms':700})
        b._on_realtime_event({'type':'turn.created','turn':{'role':'user','id':'server-user'}})
        b._pending_input_updated_at = time.monotonic() - 2
        b._on_realtime_event({'type':'turn.delta','turn_id':'server-user',
                             'delta':'，不要修改', 'end_ms':1100})
        try:
            await asyncio.sleep(.2)
            b.daemon.classify_phone_intent.assert_not_awaited()
            b._on_realtime_event({'type':'turn.delta','turn_id':'server-user',
                                 'delta':'任何文件。','end_ms':1500})
            b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'server-user','end_ms':1500}})
            await asyncio.sleep(.2)
            await self.drain(b)
            b.daemon.classify_phone_intent.assert_awaited_once()
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0], '请先解释，不要修改任何文件。')
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_old_aggregate_done_cannot_finalize_the_next_utterance(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        b._on_realtime_event({'type':'input_transcript.added','text':'喂', 'start_ms':100,'end_ms':300})
        b._on_realtime_event({'type':'turn.created','turn':{'role':'user','id':'old-user'}})
        b._finalize_pending_input()
        b._on_realtime_event({'type':'input_transcript.added','text':'请解释', 'start_ms':2000,'end_ms':2300})
        b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'old-user','end_ms':300}})
        try:
            await asyncio.sleep(.2)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertFalse(b._pending_server_done)
            self.assertEqual(b._pending_input_text, '请解释')
        finally:
            b._pending_input_id = b._pending_input_text = ''
            await self.close(b)

    async def test_classifier_context_excludes_a_speculative_same_turn_reply(self):
        b = self.bridge(actual_audio=True)
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        b._record_transcript_turn('prior', 'assistant', '您想先了解什么？')
        b._queue_audio(pcm(20), 'prior', kind='realtime', text='您想先了解什么？')
        b.audio._output_callback(bytearray(1920), 960, None, None)
        b._on_realtime_event({'type':'input_transcript.added','text':'好的，先解释。', 'start_ms':100,'end_ms':900})
        b._on_realtime_event({'type':'turn.created','turn':{'role':'assistant',
                              'id':'speculative','transcript':'那我马上删除文件。','start_ms':1000}})
        b._finalize_pending_input()
        try:
            await self.drain(b)
            context = b.daemon.classify_phone_intent.await_args.args[1]
            self.assertIn({'role':'assistant','text':'您想先了解什么？'}, context)
            self.assertNotIn('那我马上删除文件。', str(context))
        finally:
            await self.close(b)

    async def test_classifier_uses_only_completed_output_from_earlier_replies(self):
        b = self.bridge(actual_audio=True)
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        try:
            b._record_transcript_turn('heard', 'assistant', '可以先讨论方案。')
            b._queue_audio(pcm(20), 'heard', kind='realtime', text='可以先讨论方案。')
            b.audio._output_callback(bytearray(1920), 960, None, None)
            b._record_transcript_turn('unheard', 'assistant', '那我直接删除文件。')
            b._record_transcript_turn('partial', 'assistant', '我已经完成全部修改。')
            b._queue_audio(pcm(40), 'partial', kind='realtime', text='我已经完成全部修改。')
            b.audio._output_callback(bytearray(1920), 960, None, None)
            b.audio.clear_output()
            b._record_transcript_turn('queued', 'assistant', '马上发布。')
            b._queue_audio(pcm(20), 'queued', kind='realtime', text='马上发布。')
            b._complete_transcript_turn('user', '好的，先解释。', 'next')
            await self.drain(b)
            context = b.daemon.classify_phone_intent.await_args.args[1]
            self.assertEqual(context, [{'role': 'assistant', 'text': '可以先讨论方案。'}])
            # The raw diagnostic archive still records what was generated
            # and distinguishes unplayed/partial output; it is not rewritten.
            b._sync_transcript_job()
            self.assertIn('那我直接删除文件。', str(b.pending.job['phone_transcript']))
            self.assertIn('partial_cancelled', str(b.pending.job['phone_transcript']))
        finally:
            await self.close(b)

    async def test_grouped_completion_requires_current_tail_and_media_clock(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._on_realtime_event({'type':'input_transcript.added','text':'喂', 'start_ms':100,'end_ms':300})
        b._on_realtime_event({'type':'turn.created','turn':{'role':'user','id':'grouped-user'}})
        b._finalize_pending_input()
        b._on_realtime_event({'type':'input_transcript.added','text':'请保存文件。', 'start_ms':2000,'end_ms':3000})
        b._on_realtime_event({'type':'turn.done','turn':{'role':'user','id':'grouped-user',
            'transcript':'喂，请保存文件。','end_ms':3000}})
        await self.close(b)
        self.assertEqual(b.daemon.relayed, ['请保存文件。'])

    async def test_live_completed_text_waits_for_its_late_media_tail(self):
        b = self.bridge()
        b.daemon.config.update(phone_realtime_tail_min_wait_ms=40,
                              phone_realtime_tail_quiet_ms=50, phone_realtime_tail_max_wait_ms=350)
        b._begin_realtime_capture('late-tail')
        b._media_fence.begin('late-tail', 1000)
        b._on_pcm_frame(pcm(200), {'media_ms': 1000, 'duration_ms': 200})
        b._on_realtime_event({'type': 'turn.done', 'turn': {
            'role': 'assistant', 'id': 'late-tail', 'transcript': '在的。', 'end_ms': 1800}})
        await asyncio.sleep(.15)
        b._on_pcm_frame(pcm(400), {'media_ms': 1400, 'duration_ms': 400})
        await self.drain(b)
        self.assertEqual(b.local_tts.texts, [])
        self.assertEqual(sum(map(len, b.audio.played)), len(pcm(600)))
        await self.close(b)

    async def test_live_missing_tail_is_not_accepted_just_because_prefix_is_long_enough(self):
        b = self.bridge()
        b._begin_realtime_capture('missing-tail')
        b._media_fence.begin('missing-tail', 1000)
        b._on_pcm_frame(pcm(400), {'media_ms': 1000, 'duration_ms': 400})
        b._on_realtime_event({'type': 'turn.done', 'turn': {
            'role': 'assistant', 'id': 'missing-tail', 'transcript': '在的。', 'end_ms': 1800}})
        await self.drain(b)
        self.assertEqual(b.local_tts.texts, [VOICE_FAILURE])
        self.assertFalse(b.pending.job['phone_realtime_audio_diagnostics']['last_structural_check_passed'])
        await self.close(b)

    async def test_early_assistant_cannot_finalize_a_caller_mid_sentence(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._local_caller_active = True
        b._caller_turns.voice(True)
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '请保存文件',
                              'start_ms': 100, 'end_ms': 900})
        b._on_realtime_event({'type': 'turn.created', 'turn': {
            'id': 'early-ack', 'role': 'assistant', 'transcript': '我来处理。', 'start_ms': 1000}})
        b._on_pcm_output(pcm(2000))
        await asyncio.sleep(.02)
        try:
            self.assertEqual(b._pending_input_text, '请保存文件')
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertFalse(b.audio.played)
            b._on_realtime_event({'type': 'input_transcript.added', 'text': '，但不要覆盖原文件。',
                                  'start_ms': 950, 'end_ms': 2100})
            b._observe_local_caller(pcm(420, 0))
            await asyncio.sleep(1.15)
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['请保存文件，但不要覆盖原文件。'])
        finally:
            await self.close(b)

    async def test_early_completed_reply_waits_for_full_question_classification(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'question'})
        b._local_caller_active = True
        b._caller_turns.voice(True)
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '请解释这个设置',
                              'start_ms': 100, 'end_ms': 900})
        answer = {'id': 'early-answer', 'role': 'assistant',
                  'transcript': '这是当前任务的设置。', 'start_ms': 1000}
        b._on_realtime_event({'type': 'turn.created', 'turn': answer})
        b._on_pcm_output(pcm(2000))
        b._on_realtime_event({'type': 'turn.done', 'turn': answer})
        await asyncio.sleep(.02)
        try:
            self.assertFalse(b.audio.played)
            self.assertIn('early-answer', b._deferred_intent_releases)
            b._on_realtime_event({'type': 'input_transcript.added', 'text': '，不要修改。',
                                  'start_ms': 950, 'end_ms': 2100})
            b._observe_local_caller(pcm(420, 0))
            await asyncio.sleep(1.15)
            await self.drain(b)
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0],
                             '请解释这个设置，不要修改。')
            self.assertTrue(b.audio.played)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_silent_aggregate_user_turn_cannot_dispatch_a_task(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._phone_input_observed = True
        turn = {'id': 'unheard-command', 'role': 'user', 'transcript': '请写一个文件。',
                'start_ms': 1000, 'end_ms': 2000}
        b._on_realtime_event({'type': 'turn.created', 'turn': turn})
        b._on_realtime_event({'type': 'turn.done', 'turn': turn})
        try:
            await self.drain(b)
            self.assertFalse(b.daemon.relayed)
            b.daemon.classify_phone_intent.assert_not_awaited()
            self.assertNotIn('请写一个文件。', [row['text'] for row in b._transcript_turns])
        finally:
            await self.close(b)

    async def test_confirmed_receipt_remains_in_the_phone_archive(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._complete_transcript_turn('user', '请保存文件。', 'action')
        try:
            await self.drain(b)
            b._sync_transcript_job()
            self.assertEqual(b.daemon.relayed, ['请保存文件。'])
            self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
            self.assertIn(COMMAND_QUEUED, [row['text'] for row in b.pending.job['phone_transcript']])
        finally:
            await self.close(b)

    async def test_imminent_receipt_is_not_queued_behind_a_waiting_filler(self):
        b = self.bridge()
        b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=.5)
        async def classify(text, context):
            await asyncio.sleep(.1)
            return {'kind': 'action'}
        b.daemon.classify_phone_intent = classify
        b._complete_transcript_turn('user', '请保存文件。', 'action')
        try:
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['请保存文件。'])
            self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
        finally:
            await self.close(b)

    async def test_slow_action_classification_never_plays_a_query_filler(self):
        b = self.bridge()
        b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=1.5)
        async def classify(text, context):
            await asyncio.sleep(.65)
            return {'kind': 'action'}
        b.daemon.classify_phone_intent = classify
        b._complete_transcript_turn('user', '放个礼花，这是下一次任务。', 'action')
        try:
            await asyncio.sleep(.6)
            self.assertEqual(b.local_tts.texts, [])
            await self.drain(b)
            self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
            self.assertEqual(b.daemon.relayed, ['放个礼花，这是下一次任务。'])
        finally:
            await self.close(b)

    async def test_classified_action_stops_query_timer_before_slow_delivery(self):
        b = self.bridge()
        b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=.15)
        delivered = asyncio.Event()
        async def relay(*args, **kwargs):
            await delivered.wait()
            return {'status': 'accepted_by_codex_app'}
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b.daemon.relay_phone_task = relay
        b._complete_transcript_turn('user', '请保存文件。', 'action')
        try:
            await asyncio.sleep(.25)
            self.assertEqual(b.local_tts.texts, [])
            self.assertNotIn('action', b._expired_query_ids)
            delivered.set()
            await self.drain(b)
            self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
        finally:
            delivered.set()
            await self.close(b)

    async def test_aggregate_turn_needs_fresh_audio_but_its_done_is_not_a_duplicate(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._phone_input_observed = True
        try:
            for index in (1, 2, 3):
                if index != 2:
                    b._observe_local_caller(pcm(120))
                    b._observe_local_caller(pcm(420, 0))
                turn = {'id': f'aggregate-{index}', 'role': 'user',
                        'transcript': f'请保存文件{index}。', 'start_ms': index * 3000,
                        'end_ms': index * 3000 + 500}
                b._on_realtime_event({'type': 'turn.created', 'turn': turn})
                b._on_realtime_event({'type': 'turn.done', 'turn': turn})
                await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['请保存文件1。', '请保存文件3。'])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 2)
        finally:
            await self.close(b)

    async def test_v3_final_command_is_delivered_once_when_hangup_beats_settle(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '请保存文件。',
                              'start_ms': 100, 'end_ms': 1800})
        b._on_realtime_event({'type': 'turn.done', 'turn': {
            'id': 'server-user', 'role': 'user', 'transcript': '请保存文件。'}})
        await self.close(b)
        self.assertEqual(b.daemon.relayed, ['请保存文件。'])
        self.assertEqual(b._pending_input_text, '')

    async def test_hangup_preserves_partial_without_executing_it(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '帮我把',
                              'start_ms': 100, 'end_ms': 500})
        await self.close(b)
        self.assertEqual(b.daemon.relayed, [])
        self.assertEqual(b.pending.job['phone_incomplete_utterances'][0]['text'], '帮我把')

    async def test_local_speech_end_releases_command_after_early_timer(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._local_caller_active = True
        b._on_realtime_event({'type': 'input_transcript.added', 'text': '请保存文件。',
                              'start_ms': 100, 'end_ms': 1800})
        await asyncio.sleep(.75)
        self.assertFalse(b.daemon.relayed)
        b._observe_local_caller(pcm(420, 0))
        await asyncio.sleep(.1)
        await self.close(b)
        self.assertEqual(b.daemon.relayed, ['请保存文件。'])

    async def test_cancel_behind_classifier_lock_is_not_behind_dispatch(self):
        b = self.bridge()
        lock, entered, release = asyncio.Lock(), asyncio.Event(), asyncio.Event()
        async def classify(text, context):
            async with lock:
                if text.startswith('请'):
                    entered.set()
                    await release.wait()
                    return {'kind': 'action'}
                return {'kind': 'cancel'}
        b.daemon.classify_phone_intent = classify
        b._complete_transcript_turn('user', '请保存文件。', 'first')
        await entered.wait()
        b._mark_remote_speech(interrupt=True)
        b._complete_transcript_turn('user', '别执行了，取消。', 'cancel')
        release.set()
        await self.drain(b)
        self.assertFalse(b.daemon.relayed)
        self.assertIn('first', b.pending.job['phone_cancellations'][0]['cancelled'])
        await self.close(b)

    async def test_indirect_approval_blocks_model_ack_until_desktop_receipt(self):
        b = self.bridge()
        classified = asyncio.Event()
        async def classify(text, context):
            await classified.wait()
            return {'kind': 'action'}
        b.daemon.classify_phone_intent = classify
        b._complete_transcript_turn('user', '就按刚才说的办吧。', 'approval')
        b._on_realtime_event({'type': 'turn.created', 'turn': {
            'id': 'premature-ack', 'role': 'assistant', 'transcript': '已经开始处理了。'}})
        b._on_pcm_output(pcm(2000))
        self.assertFalse(b.audio.played)
        classified.set()
        await self.drain(b)
        self.assertEqual(b.daemon.relayed, ['就按刚才说的办吧。'])
        self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
        self.assertIn('premature-ack', b._suppressed_assistant_turn_ids)
        await self.close(b)

    async def test_unrelated_receipt_does_not_satisfy_question_or_erase_timeout(self):
        b = self.bridge()
        b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=.15)
        b._latest_user_turn_id = 'question'
        b._intent_decisions['question'] = {'kind': 'question'}
        b._schedule_query_wait()
        await b._render_local_speech(COMMAND_QUEUED, b._speech_generation, kind='command_receipt')
        await b._query_deadline_tasks['question']
        self.assertEqual(b.pending.job['phone_query_timeouts'], ['question'])
        self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED, QUERY_TIMEOUT])
        self.assertIn(QUERY_TIMEOUT, [row['text'] for row in b.pending.job['phone_transcript']])
        await self.close(b)

    async def test_late_model_command_commentary_is_silent_but_next_question_is_not(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(side_effect=[{'kind': 'action'}, {'kind': 'question'}])
        try:
            b._complete_transcript_turn('user', '放一次礼花。', 'action')
            await self.drain(b)
            b._complete_transcript_turn('assistant', '已经安排好了，接下来还可以做很多事情，要不要继续？', 'unwanted')
            await self.drain(b)
            self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])
            self.assertIn('unwanted', b._suppressed_assistant_turn_ids)
            b._complete_transcript_turn('user', '为什么天是蓝色的？', 'question')
            b._complete_transcript_turn('assistant', '因为空气对蓝光的散射更明显，所以天空看起来是蓝色的。', 'answer')
            await self.drain(b)
            self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
            self.assertEqual(b.daemon.relayed, ['放一次礼花。'])
        finally:
            await self.close(b)

    async def test_complete_short_reply_is_not_replaced_by_failure_notice(self):
        b = self.bridge()
        b._begin_realtime_capture('short-answer')
        b._on_pcm_output(pcm(650))
        await b._release_realtime_after_drain('short-answer', '在的。', b._speech_generation)
        self.assertEqual(b.audio.played, [pcm(650)])
        self.assertNotIn(VOICE_FAILURE, b.local_tts.texts)
        await self.close(b)

    async def test_stream_stall_has_one_notice_and_blocks_late_tail(self):
        b=self.bridge()
        b._begin_realtime_capture('stalled')
        b._realtime_streaming_turn_id='stalled'
        b._realtime_last_pcm_at=time.monotonic()-20
        await b._check_stalled_voice()
        await b._check_stalled_voice()
        self.assertEqual(b.local_tts.texts,[VOICE_FAILURE])
        self.assertIn('stalled',b._interrupted_assistant_turn_ids)
        self.assertEqual(b.pending.job['phone_stalled_voice_turns'],['stalled'])
        before=list(b.audio.played)
        b._on_pcm_output(pcm(600))
        self.assertEqual(b.audio.played,before)
        await self.close(b)

    async def test_live_context_refresh_is_exact_source_background_data(self):
        b=self.bridge()
        b.thread_id='voice-context'
        b.daemon._recent_source_context=Mock(return_value='user: 请修改\nassistant: 已保存文件')
        b.daemon.codex.request=AsyncMock(return_value={})
        await b._refresh_live_source_context({'verification':{'status':'accepted_by_codex_app'}})
        call=b.daemon.codex.request.await_args
        self.assertEqual(call.args[0],'thread/realtime/appendText')
        self.assertEqual(call.args[1]['threadId'],'voice-context')
        self.assertEqual(call.args[1]['role'],'developer')
        self.assertIn('不等于任务完成',call.args[1]['text'])
        b.daemon._recent_source_context.assert_called_once_with('source')
        self.assertFalse(b.daemon.relayed)
        self.assertFalse(b.audio.played)
        await self.close(b)

    async def test_late_opening_greeting_stays_suppressed_but_later_greetings_answer(self):
        b=self.bridge()
        b._complete_transcript_turn('user','喂','first')
        b._complete_transcript_turn('assistant','您好，我在，您说。','late-greeting')
        self.assertIn('late-greeting',b._suppressed_assistant_turn_ids)
        b._complete_transcript_turn('user','还在吗？','later')
        b.daemon.classify_phone_intent=AsyncMock(return_value={'kind':'question'})
        await self.drain(b)
        b._complete_transcript_turn('assistant','我在的，您说。','later-answer')
        self.assertNotIn('later-answer',b._suppressed_assistant_turn_ids)
        await self.close(b)

    async def test_local_voice_end_preserves_grace_after_an_old_asr_prefix(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._phone_input_observed = True
        prefix, tail = '请写一个验收标记，', '内容是完整指令。'
        try:
            b._observe_local_caller(pcm(120))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': prefix,
                                  'start_ms': 100, 'end_ms': 500})
            b._pending_input_updated_at = time.monotonic() - 2
            b._observe_local_caller(pcm(420, 0))
            # VAD has only just confirmed silence. The old word timestamp
            # must not immediately send a prefix while its tail is in flight.
            await asyncio.sleep(.18)
            self.assertTrue(b._pending_input_id)
            b.daemon.classify_phone_intent.assert_not_awaited()
            b._on_realtime_event({'type': 'input_transcript.added', 'text': tail,
                                  'start_ms': 800, 'end_ms': 1000})
            b._on_realtime_event({'type': 'turn.done', 'turn': {'role': 'user',
                'id': 'server', 'start_ms': 100, 'end_ms': 1000, 'transcript': prefix + tail}})
            await b._input_settle_task
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, [prefix + tail])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await self.close(b)

    async def test_pending_timestamp_gap_cannot_discard_same_acoustic_command_tail(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        b._phone_input_observed = True
        prefix, tail = '请写一个验收标记，', '内容是完整指令。'
        try:
            b._observe_local_caller(pcm(120))
            b._observe_local_caller(pcm(420, 0))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': prefix,
                                  'start_ms': 245400, 'end_ms': 248000})
            identity = b._pending_input_id
            # Recorded v3 words had a 1200 ms timestamp gap even though the
            # pending utterance already owned the last physical speech burst.
            b._on_realtime_event({'type': 'input_transcript.added', 'text': tail,
                                  'start_ms': 249200, 'end_ms': 250200})
            self.assertEqual(b._pending_input_id, identity)
            self.assertEqual(b._pending_input_text, prefix + tail)
            self.assertFalse(b.pending.job.get('phone_rejected_silent_transcripts'))
            b._finalize_pending_input()
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, [prefix + tail])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
        finally:
            await self.close(b)

    async def test_fresh_acoustic_cancel_after_gap_is_still_a_separate_turn(self):
        b = self.bridge()
        b._phone_input_observed = True
        async def classify(text, context):
            return {'kind': 'cancel' if text == '不要执行。' else 'action'}
        b.daemon.classify_phone_intent = classify
        try:
            for start, text in ((100, '请写一个标记。'), (4000, '不要执行。')):
                if start == 4000:
                    # A genuinely separate server speech boundary finalizes
                    # the earlier turn. Raw ASR word spacing is insufficient.
                    b._on_realtime_event({'type': 'input_audio_buffer.speech_stopped'})
                    b._on_realtime_event({'type': 'input_audio_buffer.speech_started'})
                b._observe_local_caller(pcm(120))
                b._observe_local_caller(pcm(420, 0))
                b._on_realtime_event({'type': 'input_transcript.added', 'text': text,
                                      'start_ms': start, 'end_ms': start + 500})
            b._finalize_pending_input()
            await self.drain(b)
            self.assertEqual([row['text'] for row in b._caller_turns.snapshot()],
                             ['请写一个标记。', '不要执行。'])
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_unsettled_command_keeps_condition_after_a_new_voice_burst(self):
        b = self.bridge()
        b._phone_input_observed = True
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        prefix, tail = '请生成一份报告，', '但不要改动原文件。'
        try:
            b._observe_local_caller(pcm(120))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': prefix,
                                  'start_ms': 100, 'end_ms': 600})
            b._observe_local_caller(pcm(420, 0))
            identity = b._pending_input_id
            # A short physical pause re-arms VAD, but neither its settling
            # deadline nor a server-final event has completed the utterance.
            b._observe_local_caller(pcm(120))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': tail,
                                  'start_ms': 1800, 'end_ms': 2400})
            b._observe_local_caller(pcm(420, 0))
            b._finalize_pending_input()
            await self.drain(b)
            self.assertEqual(b.daemon.relayed, [prefix + tail])
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
            self.assertEqual(b._caller_turns.snapshot()[0]['id'], identity)
        finally:
            await self.close(b)

    async def test_unsettled_command_cancellation_is_classified_with_its_prefix(self):
        b = self.bridge()
        b._phone_input_observed = True
        async def classify(text, context):
            return {'kind': 'cancel' if '不要执行' in text else 'action'}
        b.daemon.classify_phone_intent = AsyncMock(side_effect=classify)
        try:
            for start, text in ((100, '请写一个标记，'), (1800, '不要执行，我先想想。')):
                b._observe_local_caller(pcm(120))
                b._on_realtime_event({'type': 'input_transcript.added', 'text': text,
                                      'start_ms': start, 'end_ms': start + 500})
                b._observe_local_caller(pcm(420, 0))
            b._finalize_pending_input()
            await self.drain(b)
            b.daemon.classify_phone_intent.assert_awaited_once()
            self.assertEqual(b.daemon.classify_phone_intent.await_args.args[0],
                             '请写一个标记，不要执行，我先想想。')
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_real_settle_then_fresh_cancellation_remains_separate(self):
        b = self.bridge()
        b._phone_input_observed = True
        entered, release = asyncio.Event(), asyncio.Event()
        async def classify(text, context):
            if text == '请写一个标记。':
                entered.set()
                await release.wait()
                return {'kind': 'action'}
            return {'kind': 'cancel'}
        b.daemon.classify_phone_intent = AsyncMock(side_effect=classify)
        try:
            b._observe_local_caller(pcm(120))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': '请写一个标记。',
                                  'start_ms': 100, 'end_ms': 600})
            b._observe_local_caller(pcm(420, 0))
            await asyncio.wait_for(b._input_settle_task, 2)
            await asyncio.wait_for(entered.wait(), 1)
            b._observe_local_caller(pcm(120))
            b._on_realtime_event({'type': 'input_transcript.added', 'text': '不要执行。',
                                  'start_ms': 4000, 'end_ms': 4500})
            b._observe_local_caller(pcm(420, 0))
            b._finalize_pending_input()
            release.set()
            await self.drain(b)
            self.assertEqual([call.args[0] for call in b.daemon.classify_phone_intent.await_args_list],
                             ['请写一个标记。', '不要执行。'])
            self.assertFalse(b.daemon.relayed)
        finally:
            release.set()
            await self.close(b)

    async def test_delivery_history_is_bound_to_exact_caller_identity(self):
        b = self.bridge(actual_audio=True)
        b.daemon.relay_phone_task = AsyncMock(return_value={'status': 'accepted_by_codex_app'})
        try:
            b._record_transcript_turn('heard', 'assistant', '可以先整理红色方案。')
            b._queue_audio(pcm(20), 'heard', kind='realtime', text='可以先整理红色方案。')
            b.audio._output_callback(bytearray(1920), 960, None, None)
            b._record_transcript_turn('current', 'user', '就按这个做。')
            b._record_transcript_turn('unheard', 'assistant', '另外还会发布出去。')
            b._record_transcript_turn('later', 'user', '后来新增的另一件事。')
            b._record_transcript_turn('repeated', 'user', '就按这个做。')
            await b._relay_phone_task('就按这个做。', 'current')
            call = b.daemon.relay_phone_task.await_args
            self.assertEqual(call.kwargs['phone_history'],
                             [{'role': 'assistant', 'text': '可以先整理红色方案。'}])
            self.assertEqual(call.kwargs['command_id'], 'test:current')
        finally:
            await self.close(b)

    async def test_local_vad_end_does_not_cut_delayed_asr_tail_into_commands(self):
        b=self.bridge()
        b.daemon.classify_phone_intent=AsyncMock(return_value={'kind':'question'})
        b._on_realtime_event({'type':'input_transcript.added','text':'任务会', 'start_ms':100,'end_ms':500})
        b._local_caller_active=True
        b._observe_local_caller(pcm(420,0))
        await asyncio.sleep(.45)
        b._on_realtime_event({'type':'input_transcript.added','text':'在哪里', 'start_ms':700,'end_ms':900})
        await asyncio.sleep(.75)
        b._on_realtime_event({'type':'input_transcript.added','text':'执行？', 'start_ms':1300,'end_ms':1500})
        b._on_realtime_event({'type':'turn.done','turn':{'id':'server','role':'user','transcript':'任务会在哪里执行？'}})
        await self.close(b)
        users=[item['text'] for item in b.pending.job['phone_transcript'] if item['role']=='user']
        self.assertEqual(users,['任务会在哪里执行？'])
        self.assertEqual(b.daemon.classify_phone_intent.await_count,1)
        self.assertFalse(b.daemon.relayed)

    async def test_silence_hallucination_cannot_interrupt_opening_or_dispatch(self):
        b=self.bridge()
        b.daemon.classify_phone_intent=AsyncMock(return_value={'kind':'action'})
        b._phone_input_observed=True
        b._observe_local_caller(pcm(120))
        b._observe_local_caller(pcm(420,0))
        b._on_realtime_event({'type':'input_transcript.added','text':'喂','start_ms':14600,'end_ms':14800})
        b._finalize_pending_input()
        generation=b._speech_generation
        b._on_realtime_event({'type':'input_transcript.added','text':'保存文件','start_ms':16600,'end_ms':16800})
        for event in ('turn.created','turn.done'):
            b._on_realtime_event({'type':event,'turn':{'id':'phantom','role':'user',
                'transcript':'保存文件','start_ms':16600,'end_ms':16800}})
        await self.drain(b)
        self.assertEqual(b._speech_generation,generation)
        self.assertFalse(b.daemon.relayed)
        b.daemon.classify_phone_intent.assert_not_awaited()
        self.assertEqual(b._latest_user_text,'喂')
        # A real later acoustic burst is not suppressed by the rejected ASR.
        b._observe_local_caller(pcm(120))
        b._observe_local_caller(pcm(420,0))
        b._on_realtime_event({'type':'input_transcript.added','text':'保存文件','start_ms':19000,'end_ms':19500})
        b._finalize_pending_input()
        await self.drain(b)
        self.assertEqual(b.daemon.relayed,['保存文件'])
        await self.close(b)

    async def test_silent_pickup_cannot_invent_a_first_command(self):
        b=self.bridge()
        b._phone_input_observed=True
        b._on_realtime_event({'type':'input_transcript.added','text':'保存文件','start_ms':100,'end_ms':900})
        self.assertFalse(b._pending_input_id)
        self.assertFalse(b.daemon.relayed)
        self.assertTrue(b.pending.job['phone_rejected_silent_transcripts'])
        await self.close(b)

    async def test_finalized_input_cannot_reuse_consumed_voice_at_any_timestamp(self):
        for start in (600, 500, 100, None, 2500):
            with self.subTest(start=start):
                b = self.bridge()
                b._phone_input_observed = True
                b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
                try:
                    b._observe_local_caller(pcm(120))
                    b._observe_local_caller(pcm(420, 0))
                    b._on_realtime_event({'type': 'input_transcript.added',
                        'text': '请写一个验收标记。', 'start_ms': 100, 'end_ms': 500})
                    b._finalize_pending_input()
                    await self.drain(b)
                    b._on_realtime_event({'type': 'input_transcript.added',
                        'text': '请写一个验收标记。', 'start_ms': start, 'end_ms': 900})
                    b._finalize_pending_input()
                    await self.drain(b)
                    self.assertEqual(b.daemon.relayed, ['请写一个验收标记。'])
                    self.assertEqual(b.daemon.classify_phone_intent.await_count, 1)
                    self.assertEqual(len(b.pending.job['phone_rejected_silent_transcripts']), 1)
                finally:
                    await self.close(b)

    async def test_same_words_with_fresh_voice_are_a_valid_second_instruction(self):
        b = self.bridge()
        b._phone_input_observed = True
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind': 'action'})
        try:
            for start in (100, 600):
                b._observe_local_caller(pcm(120))
                b._observe_local_caller(pcm(420, 0))
                b._on_realtime_event({'type': 'input_transcript.added',
                    'text': '请写一个验收标记。', 'start_ms': start, 'end_ms': start + 400})
                b._finalize_pending_input()
                await self.drain(b)
            self.assertEqual(b.daemon.relayed, ['请写一个验收标记。'] * 2)
            self.assertEqual(b.daemon.classify_phone_intent.await_count, 2)
        finally:
            await self.close(b)

    async def test_short_first_greeting_evidence_cannot_be_spent_twice(self):
        b = self.bridge()
        b._phone_input_observed = True
        b._local_greeting_voiced_ms = 60
        try:
            self.assertTrue(b._has_caller_evidence(None, '喂'))
            self.assertFalse(b._has_caller_evidence(None, '保存文件'))
        finally:
            await self.close(b)

    async def test_terminal_native_failure_stops_wait_and_timeout_notices(self):
        b=self.bridge()
        b.daemon.config.update(phone_query_wait_seconds=.05,phone_query_timeout_seconds=.12)
        b._latest_user_turn_id='question'
        b._assistant_user_turn_ids['broken']='question'
        b._schedule_query_wait()
        b._begin_realtime_capture('broken')
        b._on_pcm_output(pcm(80))
        await b._release_realtime_after_drain('broken','这是一段被截断的回复。',b._speech_generation)
        await asyncio.sleep(.15)
        self.assertEqual(b.local_tts.texts,[VOICE_FAILURE])
        self.assertNotIn('question',b._query_answered_ids)
        self.assertIn('question',b._expired_query_ids)
        await self.close(b)

    async def test_delegation_for_opening_greeting_does_not_start_query_deadline(self):
        b=self.bridge()
        b._complete_transcript_turn('user','喂','greeting')
        b._on_realtime_event({'type':'delegation.created'})
        self.assertFalse(b._query_deadline_tasks)
        b.delegation_seen.clear()
        await self.close(b)

    async def test_buffered_answer_does_not_get_blocked_by_waiting_filler(self):
        b=self.bridge()
        b._latest_user_turn_id='question'
        b.daemon.config.update(phone_query_wait_seconds=.05,phone_query_timeout_seconds=.12)
        b._realtime_audio_buffer.extend(pcm(1000))
        await b._query_wait_notice(b._speech_generation,identity='question')
        self.assertEqual(b.local_tts.texts,[QUERY_TIMEOUT])
        await self.close(b)

    async def test_cancellation_of_sent_task_is_forwarded_once_without_claiming_undo(self):
        b=self.bridge()
        b.daemon.classify_phone_intent=AsyncMock(side_effect=[{'kind':'action'},{'kind':'cancel'}])
        b._complete_transcript_turn('user','保存文件','original')
        await self.drain(b)
        b._complete_transcript_turn('user','取消刚才的操作','cancel')
        await self.drain(b)
        b._reconcile_task_relays()
        await self.drain(b)
        self.assertEqual(b.daemon.relayed,['保存文件','取消刚才的操作'])
        self.assertEqual(b.local_tts.texts[-1],COMMAND_ALREADY_SENT)
        await self.close(b)

    def replay_bridge(self):
        b=self.bridge()
        b.thread_id='isolated-voice'
        b.rtc=SimpleNamespace(stop=AsyncMock(),input_track=SimpleNamespace(diagnostics=lambda:{}))
        b._latest_user_turn_id='question'
        b._intent_decisions['question']={'kind':'question'}
        b._assistant_user_turn_ids['broken']='question'
        b._realtime_semantic_checker = AsyncMock(return_value=True)
        return b

    async def test_unplayed_native_failure_can_repair_once_on_the_same_connection(self):
        b=self.replay_bridge()
        b._begin_realtime_capture('broken')
        await b._release_realtime_after_drain('broken','打断成功了，我停下了。',b._speech_generation)
        methods=[row[0] for row in b.daemon.codex.requests]
        self.assertEqual(methods,['thread/realtime/appendText','thread/realtime/appendSpeech'])
        self.assertFalse(b.audio.played)
        b._on_realtime_event({'type':'turn.created','turn':{
            'id':'repaired','role':'assistant','transcript':'打断成功了，我停下了。'}})
        b._on_pcm_output(pcm(1800))
        self.assertFalse(b.audio.played)  # Whole replay checked before playback.
        await b._release_realtime_after_drain('repaired','打断成功了，我停下了。',b._speech_generation)
        self.assertTrue(b.audio.played)
        self.assertFalse(b.local_tts.texts)
        self.assertEqual(len(b.pending.job['phone_native_repairs']),1)
        await self.close(b)

    async def test_native_repair_does_not_loop_or_accept_changed_words(self):
        b=self.replay_bridge()
        b._begin_realtime_capture('broken')
        await b._release_realtime_after_drain('broken','我还没有完成。',b._speech_generation)
        b._on_realtime_event({'type':'turn.created','turn':{
            'id':'wrong-replay','role':'assistant','transcript':'我'}})
        b._on_pcm_output(pcm(1800))
        await b._release_realtime_after_drain('wrong-replay','我已经完成了。',b._speech_generation)
        self.assertEqual(b.local_tts.texts,[VOICE_FAILURE])
        self.assertEqual(len(b.pending.job['phone_native_repairs']),1)
        self.assertIn('question',b._expired_query_ids)
        await self.close(b)

    async def test_silent_asr_during_owned_repair_cannot_mute_the_repair(self):
        for prefix in ('我还没有完成。', ''):
            with self.subTest(prefix=prefix):
                b = self.replay_bridge()
                b._phone_input_observed = True
                text = '我还没有完成。'
                try:
                    b._begin_realtime_capture('broken')
                    await b._release_realtime_after_drain('broken', text, b._speech_generation)
                    b._on_realtime_event({'type': 'input_transcript.added',
                        'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
                    b._on_realtime_event({'type': 'turn.created', 'turn': {
                        'id': 'repair', 'role': 'assistant', 'transcript': prefix}})
                    self.assertNotIn('repair', b._suppressed_assistant_turn_ids)
                    self.assertEqual(b._native_replay_texts.get('repair'), text)
                    b._on_pcm_output(pcm(1800))
                    self.assertFalse(b.audio.played)
                    await b._release_realtime_after_drain('repair', text, b._speech_generation)
                    self.assertTrue(b.audio.played)
                    self.assertFalse(b.daemon.relayed)
                    self.assertEqual(b._assistant_user_turn_ids['repair'], 'question')
                    self.assertTrue(b._ignore_unheard_reply)
                finally:
                    await self.close(b)

    async def test_unheard_wrong_reply_cannot_consume_an_owned_repair(self):
        b = self.replay_bridge()
        b._phone_input_observed = True
        try:
            b._begin_realtime_capture('broken')
            await b._release_realtime_after_drain('broken', '我还没有完成。', b._speech_generation)
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'unheard', 'role': 'assistant', 'transcript': '文件保存好了。'}})
            self.assertIn('unheard', b._suppressed_assistant_turn_ids)
            self.assertNotIn('unheard', b._native_replay_texts)
            self.assertIsNotNone(b._native_replay)
            self.assertFalse(b.audio.played)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_unrelated_reply_without_silent_asr_cannot_steal_pending_repair(self):
        b = self.replay_bridge()
        text = '我还没有完成。'
        try:
            b._begin_realtime_capture('broken')
            await b._release_realtime_after_drain('broken', text, b._speech_generation)
            self.assertFalse(b._ignore_unheard_reply)
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'late-unrelated', 'role': 'assistant', 'transcript': '好的，我来看看。'}})
            self.assertIn('late-unrelated', b._suppressed_assistant_turn_ids)
            self.assertIsNotNone(b._native_replay)
            self.assertNotIn('late-unrelated', b._native_replay_texts)
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'actual-repair', 'role': 'assistant', 'transcript': text}})
            b._on_pcm_output(pcm(1800))
            await b._release_realtime_after_drain('actual-repair', text, b._speech_generation)
            self.assertTrue(b.audio.played)
            self.assertFalse(b.local_tts.texts)
            self.assertEqual(len(b.pending.job['phone_native_repairs']), 1)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_owned_repair_checks_audio_even_when_normal_answers_stream(self):
        for passed in (True, False):
            with self.subTest(passed=passed):
                b = self.replay_bridge()
                b._realtime_semantic_checker = AsyncMock(return_value=passed)
                text = '我还没有完成。'
                try:
                    self.assertFalse(b.daemon.config['phone_realtime_semantic_gate'])
                    b._begin_realtime_capture('broken')
                    await b._release_realtime_after_drain('broken', text, b._speech_generation)
                    b._on_realtime_event({'type': 'turn.created', 'turn': {
                        'id': 'repair', 'role': 'assistant', 'transcript': text}})
                    payload = pcm(1800)
                    b._on_pcm_output(payload)
                    self.assertFalse(b.audio.played)
                    await b._release_realtime_after_drain('repair', text, b._speech_generation)
                    b._realtime_semantic_checker.assert_awaited_once_with(payload, text)
                    self.assertEqual(b.local_tts.texts, [] if passed else [VOICE_FAILURE])
                    self.assertEqual('question' in b._query_answered_ids, passed)
                    self.assertEqual('question' in b._expired_query_ids, not passed)
                    self.assertEqual(len(b.pending.job['phone_native_repairs']), 1)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    await self.close(b)

    async def test_unheard_done_only_reply_cannot_bypass_the_created_guard(self):
        b = self.bridge()
        b._phone_input_observed = True
        try:
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
            b._on_realtime_event({'type': 'turn.done', 'turn': {
                'id': 'unheard', 'role': 'assistant', 'transcript': '文件保存好了。'}})
            await self.drain(b)
            self.assertIn('unheard', b._suppressed_assistant_turn_ids)
            self.assertNotIn('unheard', b._realtime_early_allowed_turn_ids)
            self.assertFalse(b.audio.played)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_interrupt_during_owned_repair_audio_check_prevents_playback(self):
        b = self.replay_bridge()
        entered, release = asyncio.Event(), asyncio.Event()
        async def check(payload, expected):
            entered.set()
            await release.wait()
            return True
        b._realtime_semantic_checker = check
        task = None
        try:
            text = '我还没有完成。'
            b._begin_realtime_capture('broken')
            await b._release_realtime_after_drain('broken', text, b._speech_generation)
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'repair', 'role': 'assistant', 'transcript': text}})
            b._on_pcm_output(pcm(1800))
            task = asyncio.create_task(b._release_realtime_after_drain('repair', text, b._speech_generation))
            await asyncio.wait_for(entered.wait(), 1)
            b._mark_remote_speech(interrupt=True)
            release.set()
            await task
            self.assertFalse(b.audio.played)
            self.assertFalse(b.local_tts.texts)
            self.assertNotIn('question', b._query_answered_ids)
            self.assertFalse(b.daemon.relayed)
        finally:
            release.set()
            if task is not None:
                await task
            await self.close(b)

    async def test_query_timeout_during_repair_qa_blocks_success_and_failure_audio(self):
        for passed in (True, False):
            with self.subTest(passed=passed):
                b = self.replay_bridge()
                b.daemon.config.update(phone_query_wait_seconds=.05, phone_query_timeout_seconds=.12)
                entered, release = asyncio.Event(), asyncio.Event()
                async def check(payload, expected):
                    entered.set()
                    await release.wait()
                    return passed
                b._realtime_semantic_checker = check
                task = None
                try:
                    text = '我还没有完成。'
                    b._begin_realtime_capture('broken')
                    await b._release_realtime_after_drain('broken', text, b._speech_generation)
                    b._on_realtime_event({'type': 'turn.created', 'turn': {
                        'id': 'repair', 'role': 'assistant', 'transcript': text}})
                    b._on_pcm_output(pcm(1800))
                    task = asyncio.create_task(b._release_realtime_after_drain('repair', text, b._speech_generation))
                    await asyncio.wait_for(entered.wait(), 1)
                    await b._query_wait_notice(b._speech_generation, identity='question')
                    self.assertIn('question', b._expired_query_ids)
                    before = list(b.audio.played)
                    release.set()
                    await task
                    self.assertTrue(b.audio.played == before, 'stale QA enqueued extra audio')
                    self.assertEqual(b.local_tts.texts, [QUERY_TIMEOUT])
                    self.assertNotIn('question', b._query_answered_ids)
                    self.assertNotIn('phone_failed_questions', b.pending.job)
                    self.assertEqual(len(b.pending.job['phone_native_repairs']), 1)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    release.set()
                    if task is not None:
                        await task
                    await self.close(b)

    async def test_stale_qa_cannot_speak_or_clear_next_turn_buffer(self):
        for cause in ('suppressed', 'expired_alias', 'service_failure', 'interrupted', 'disconnected'):
            for passed in (True, False):
                with self.subTest(cause=cause, passed=passed):
                    b = self.replay_bridge()
                    b.daemon.config['phone_realtime_semantic_gate'] = True
                    b._assistant_user_turn_ids['old'] = 'server-question'
                    b._user_turn_aliases['server-question'] = 'question'
                    entered, release = asyncio.Event(), asyncio.Event()
                    async def check(payload, expected):
                        entered.set()
                        await release.wait()
                        return passed
                    b._realtime_semantic_checker = check
                    task = None
                    try:
                        b._begin_realtime_capture('old')
                        b._on_pcm_output(pcm(1800))
                        task = asyncio.create_task(b._release_realtime_after_drain('old', '我还没有完成。', b._speech_generation))
                        await asyncio.wait_for(entered.wait(), 1)
                        if cause == 'suppressed':
                            b._suppressed_assistant_turn_ids.add('old')
                        elif cause == 'expired_alias':
                            b._expired_query_ids.add('question')
                        elif cause == 'service_failure':
                            b._call_failure = 'synthetic service failure'
                        elif cause == 'interrupted':
                            b._mark_remote_speech(interrupt=True)
                        else:
                            b.disconnected = True
                        b._begin_realtime_capture('new')
                        b._realtime_audio_buffer.extend(pcm(400, 2000))
                        next_payload = bytes(b._realtime_audio_buffer)
                        release.set()
                        await task
                        self.assertEqual(len(b.audio.played), 0)
                        self.assertFalse(b.local_tts.texts)
                        self.assertTrue(bytes(b._realtime_audio_buffer) == next_payload,
                                        'stale QA modified the next turn buffer')
                        self.assertFalse(b.daemon.relayed)
                    finally:
                        release.set()
                        if task is not None:
                            await task
                        await self.close(b)

    async def test_rejected_asr_does_not_revoke_an_already_bound_answer(self):
        b = self.replay_bridge()
        b._phone_input_observed = True
        text = '我还没有完成。'
        turn = {'id': 'answer', 'role': 'assistant', 'transcript': text}
        try:
            b._on_realtime_event({'type': 'turn.created', 'turn': turn})
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
            b._on_pcm_output(pcm(1800))
            b._on_realtime_event({'type': 'turn.done', 'turn': turn})
            await self.drain(b)
            self.assertNotIn('answer', b._suppressed_assistant_turn_ids)
            self.assertTrue(b.audio.played)
            self.assertEqual(b._assistant_user_turn_ids['answer'], 'question')
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_expiry_during_repair_request_never_sends_late_speech_or_failure(self):
        for rpc_fails in (False, True):
            with self.subTest(rpc_fails=rpc_fails):
                b = self.replay_bridge()
                entered, release = asyncio.Event(), asyncio.Event()
                original = b.daemon.codex.request
                async def request(method, params, **kwargs):
                    if method == 'thread/realtime/appendText':
                        entered.set()
                        await release.wait()
                        if rpc_fails:
                            raise RuntimeError('synthetic repair failure')
                    return await original(method, params, **kwargs)
                b.daemon.codex.request = request
                b._begin_realtime_capture('broken')
                task = asyncio.create_task(b._release_realtime_after_drain('broken', '我还没有完成。', b._speech_generation))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    b._expired_query_ids.add('question')
                    release.set()
                    await task
                    self.assertFalse(any(m == 'thread/realtime/appendSpeech' for m, _ in b.daemon.codex.requests))
                    self.assertFalse(b.local_tts.texts)
                    self.assertEqual(len(b.audio.played), 0)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    release.set()
                    await task
                    await self.close(b)

    async def test_current_repair_rpc_failure_still_announces_once(self):
        b = self.replay_bridge()
        b.daemon.codex.request = AsyncMock(side_effect=RuntimeError('synthetic repair failure'))
        try:
            b._begin_realtime_capture('broken')
            await b._release_realtime_after_drain('broken', '我还没有完成。', b._speech_generation)
            self.assertEqual(b.local_tts.texts, [VOICE_FAILURE])
            self.assertEqual(len(b.pending.job['phone_native_repairs']), 1)
            self.assertIn('question', b._expired_query_ids)
            self.assertEqual(len(b.audio.played), 1)
        finally:
            await self.close(b)

    async def test_stale_drain_discards_only_its_owned_capture_and_next_answer_works(self):
        for owner in ('old', 'new'):
            with self.subTest(owner=owner):
                b = self.replay_bridge()
                try:
                    b._suppressed_assistant_turn_ids.add('old')
                    b._begin_realtime_capture(owner)
                    b._realtime_audio_buffer.extend(pcm(400))
                    await b._release_realtime_after_drain('old', '旧回复。', b._speech_generation)
                    self.assertEqual(len(b._realtime_audio_buffer), len(pcm(400)) if owner == 'new' else 0)
                    self.assertEqual(len(b.audio.played), 0)
                    b._latest_user_turn_id = 'next-question'
                    b._intent_decisions['next-question'] = {'kind':'question'}
                    b._assistant_user_turn_ids['next-answer'] = 'next-question'
                    b._begin_realtime_capture('next-answer')
                    b._on_pcm_output(pcm(1200))
                    await b._release_realtime_after_drain('next-answer', '还在检查。', b._speech_generation)
                    self.assertIn('next-question', b._query_answered_ids)
                    self.assertEqual(len(b.audio.played), 1)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    await self.close(b)

    async def test_empty_owned_repair_prefix_cannot_bypass_full_word_check(self):
        b = self.replay_bridge()
        b._phone_input_observed = True
        try:
            b._begin_realtime_capture('broken')
            await b._release_realtime_after_drain('broken', '我还没有完成。', b._speech_generation)
            b._on_realtime_event({'type': 'input_transcript.added',
                'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
            b._on_realtime_event({'type': 'turn.created', 'turn': {
                'id': 'repair', 'role': 'assistant', 'transcript': ''}})
            b._on_pcm_output(pcm(1800))
            self.assertFalse(b.audio.played)
            await b._release_realtime_after_drain('repair', '我已经完成了。', b._speech_generation)
            self.assertEqual(b.local_tts.texts, [VOICE_FAILURE])
            self.assertNotIn('question', b._query_answered_ids)
            self.assertFalse(b.daemon.relayed)
        finally:
            await self.close(b)

    async def test_expired_or_interrupted_owned_repair_cannot_bypass_silence_guard(self):
        for cause in ('expired', 'interrupted'):
            with self.subTest(cause=cause):
                b = self.replay_bridge()
                b._phone_input_observed = True
                try:
                    b._begin_realtime_capture('broken')
                    await b._release_realtime_after_drain('broken', '我还没有完成。', b._speech_generation)
                    b._on_realtime_event({'type': 'input_transcript.added',
                        'text': '保存文件', 'start_ms': 100, 'end_ms': 900})
                    if cause == 'expired':
                        b._expired_query_ids.add('question')
                    else:
                        b._mark_remote_speech(interrupt=True)
                    b._on_realtime_event({'type': 'turn.created', 'turn': {
                        'id': 'stale', 'role': 'assistant', 'transcript': '我还没有完成。'}})
                    self.assertIn('stale', b._suppressed_assistant_turn_ids)
                    self.assertNotIn('stale', b._native_replay_texts)
                    self.assertFalse(b.audio.played)
                    self.assertFalse(b.daemon.relayed)
                finally:
                    await self.close(b)

    async def test_caller_interrupt_supersedes_an_inflight_replay(self):
        b=self.replay_bridge()
        b._begin_realtime_capture('broken')
        await b._release_realtime_after_drain('broken','我还没有完成。',b._speech_generation)
        b._mark_remote_speech(interrupt=True)
        b._on_realtime_event({'type':'turn.created','turn':{
            'id':'late-replay','role':'assistant','transcript':'我还没有完成。'}})
        self.assertIn('late-replay',b._suppressed_assistant_turn_ids)
        self.assertFalse(b.audio.played)
        await self.close(b)

    async def test_hangup_closes_readonly_rtc_without_waiting_for_lost_backing_done(self):
        b=self.replay_bridge()
        b._backing_turn_id='readonly-turn'
        b.turn_started.set()
        b.delegation_seen.set()
        await asyncio.wait_for(b.stop(),.5)
        b.rtc.stop.assert_awaited_once()
        self.assertIn(('turn/interrupt',{'threadId':'isolated-voice','turnId':'readonly-turn'}),
                      b.daemon.codex.requests)
        self.assertFalse(b.daemon.relayed)
