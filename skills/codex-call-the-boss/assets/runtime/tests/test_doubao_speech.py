import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import doubao_speech as ds
import phone_agent as pa
from doubao_tts import DoubaoError, SPEAKER, RESOURCE_ID
from native_speech import NativeNoticeLibraryError, VOICE_FAILURE, COMMAND_QUEUED
import test_conversation_contract as contract

pcm = contract.pcm


class ChunkTests(unittest.TestCase):
    joke = '好啊,来一个:小明去买包子,问:“老板,有肉包吗?”老板说:“有,刚出锅。”小明说:“那来一个,不要肉的。”'

    def test_real_rejected_joke_stays_complete_in_one_context(self):
        spans = ds.live_speech_chunks(self.joke, final=True)
        self.assertEqual(spans, [(0,len(self.joke))])

    def test_every_streaming_split_preserves_words_quotes_and_no_duplicate(self):
        for split in range(len(self.joke)+1):
            before = ds.live_speech_chunks(self.joke[:split])
            start = before[-1][1] if before else 0
            after = ds.live_speech_chunks(self.joke,start,final=True)
            chunks = [self.joke[a:b] for a,b in before+after]
            self.assertEqual(''.join(chunks),self.joke)
            self.assertFalse(any(chunk.startswith('”') for chunk in chunks))
            self.assertFalse(any(chunk == '老板说:“有,刚出锅。”' for chunk in chunks))

    def test_short_final_answer_is_not_lost(self):
        self.assertEqual(ds.live_speech_chunks('在的。'),[])
        self.assertEqual(ds.live_speech_chunks('在的。',final=True),[(0,3)])

    def test_long_stream_stays_bounded_and_exact(self):
        text = '这是一段完整的长回答，我们保留每句话并且按照自然语义连续播放。' * 30
        chunks, start = [], 0
        for index in range(1,len(text)+1):
            spans = ds.live_speech_chunks(text[:index],start)
            chunks.extend(text[a:b] for a,b in spans)
            if spans: start = spans[-1][1]
        chunks.extend(text[a:b] for a,b in ds.live_speech_chunks(text,start,final=True))
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(c)<=400 for c in chunks))

    def test_unpunctuated_and_unbalanced_quotes_never_exceed_provider_limit(self):
        for text in ('字'*1001,'“'+'字'*1001,'"'+'word '*210):
            spans = ds.live_speech_chunks(text,final=True)
            self.assertEqual(''.join(text[a:b] for a,b in spans),text)
            self.assertTrue(all(b-a<=400 for a,b in spans))


class RendererTests(unittest.IsolatedAsyncioTestCase):
    def renderer(self, *, passed=True):
        root = Path(tempfile.mkdtemp(prefix='doubao-renderer-test-'))
        self.client = Mock(last_result={'passed': True, 'speaker': SPEAKER})
        self.client.synthesize = AsyncMock(return_value=pcm(1200))
        self.factory = Mock(return_value=self.client)
        self.validate = AsyncMock(return_value={'passed': passed})
        self.key_patch = patch.object(ds, 'load_profile', return_value={
            'api_key': 'test-key', 'model_name': 'Doubao-语音合成-2.0',
            'resource_id': RESOURCE_ID, 'speaker': SPEAKER})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        return ds.DoubaoSpeechRenderer(cache_dir=root/'cache', credential_path=root/'private.json',
            validate=self.validate, client_factory=self.factory)

    async def test_prepared_audio_is_exact_pcm_and_reused_without_network(self):
        r = self.renderer()
        await r.prepare(['老板，新的配音已经接通。'])
        r.cache_only = True
        result = await r.synthesize('老板，新的配音已经接通。')
        self.assertEqual(result, pcm(1200))
        self.client.synthesize.assert_awaited_once()
        metadata, audio, profile = r._paths('老板，新的配音已经接通。')
        proof = json.loads(metadata.read_text())
        self.assertEqual(profile['resource_id'], RESOURCE_ID)
        self.assertEqual(profile['speaker'], SPEAKER)
        self.assertFalse(proof['human_listening_verified'])
        self.assertEqual(audio.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-key', metadata.read_text())

    async def test_cache_only_report_miss_cannot_start_network(self):
        r = self.renderer()
        r.cache_only = True
        with self.assertRaisesRegex(DoubaoError, 'prepared_audio_missing'):
            await r.synthesize('老板，尚未准备的开场。')
        self.factory.assert_not_called()

    async def test_selected_profile_is_used_and_caches_do_not_cross_voices(self):
        r = self.renderer()
        text = '测试缓存隔离。'
        await r.prepare([text])
        first = r._paths(text)[0]
        self.factory.assert_called_once_with('test-key', resource_id=RESOURCE_ID, speaker=SPEAKER)
        other = {'api_key': 'test-key', 'model_name': 'Doubao-声音复刻-2.0',
                 'resource_id': 'seed-icl-2.0', 'speaker': 'chosen_voice'}
        with patch.object(ds, 'load_profile', return_value=other):
            r2 = ds.DoubaoSpeechRenderer(cache_dir=r.cache_dir, credential_path=r.credential_path,
                                       validate=self.validate, client_factory=self.factory)
            self.assertNotEqual(first, r2._paths(text)[0])
            self.assertIsNone(r2.cached(text))
            await r2.synthesize(text)
            self.factory.assert_called_with('test-key', resource_id='seed-icl-2.0', speaker='chosen_voice')
            with self.assertRaisesRegex(DoubaoError, 'profile_changed_during_render'):
                await r.synthesize('尚未缓存的新句子。')

    async def test_live_sentence_can_render_but_missing_notice_cannot(self):
        r = self.renderer()
        r.cache_only = True
        await r.synthesize_live('这个问题我来解释一下。')
        self.client.synthesize.assert_awaited_once_with('这个问题我来解释一下。', timeout=20)
        with self.assertRaises(NativeNoticeLibraryError):
            await r.synthesize_live(VOICE_FAILURE)
        self.assertEqual(self.client.synthesize.await_count, 1)

    async def test_rejected_audio_is_retained_without_retry_or_promotion(self):
        r = self.renderer(passed=False)
        with self.assertRaisesRegex(DoubaoError, 'speech_content_rejected'):
            await r.synthesize('老板，测试完整音频。')
        self.client.synthesize.assert_awaited_once()
        self.assertIsNone(r.cached('老板，测试完整音频。'))
        evidence = Path(r.rejected_evidence[0])
        self.assertFalse(json.loads(evidence.read_text())['passed'])
        self.assertTrue((evidence.parent/'audio.wav').is_file())

    async def test_tampered_cache_fails_without_regeneration(self):
        r = self.renderer()
        await r.prepare(['测试缓存完整性。'])
        _, path, _ = r._paths('测试缓存完整性。')
        path.write_bytes(pcm(500))
        with self.assertRaisesRegex(DoubaoError, 'cache_integrity_failed'):
            await r.synthesize('测试缓存完整性。')
        self.client.synthesize.assert_awaited_once()

    async def test_cancelled_generation_has_no_playable_cache(self):
        r = self.renderer()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def waiting(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.client.synthesize.side_effect = waiting
        task = asyncio.create_task(r.synthesize_live('取消中的回答。'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(r.cached('取消中的回答。'))
        self.assertEqual(len(r.rejected_evidence), 1)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def bridge(self):
        b = contract.ContractTests().bridge(actual_audio=True)
        b.daemon.config['phone_voice_renderer'] = ds.RENDERER
        b.local_tts.synthesize_live = AsyncMock(return_value=pcm(1200))
        self.addAsyncCleanup(b.stop)
        return b

    async def drain(self, b):
        await contract.ContractTests().drain(b)

    async def test_codex_text_uses_new_renderer_and_native_audio_is_suppressed(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'question'})
        b._complete_transcript_turn('user', '现在情况怎么样？', 'u1')
        b._complete_transcript_turn('assistant', '新的配音已接通。手机端还要检查。', 'a1')
        b._on_pcm_output(pcm(500))
        await self.drain(b)
        self.assertEqual([call.args[0] for call in b.local_tts.synthesize_live.await_args_list],
                         ['新的配音已接通。手机端还要检查。'])
        outputs = b.audio.playback_snapshot()
        self.assertEqual([row['text'] for row in outputs], ['新的配音已接通。手机端还要检查。'])
        self.assertTrue(all(row['kind'] == 'answer' for row in outputs))
        self.assertFalse(b.daemon.relayed)
        b._sync_transcript_job()
        spoken_rows = [row for row in b.pending.job['phone_transcript'] if row['role'] == 'assistant']
        self.assertEqual([row['text'] for row in spoken_rows], ['新的配音已接通。手机端还要检查。'])
        self.assertTrue(all(row['playback_status'] == 'queued' for row in spoken_rows))
        # Simulated device consumption, explicitly not a handset hearing test.
        b.audio.playback_ledger.consume(sum(row['queued_bytes'] for row in outputs))
        b._sync_transcript_job()
        spoken_rows = [row for row in b.pending.job['phone_transcript'] if row['role'] == 'assistant']
        self.assertTrue(all(row['playback_status'] == 'output_complete' for row in spoken_rows))
        b._record_transcript_turn('u2', 'user', '能再解释一下吗？')
        history = b._phone_history_before('u2')
        self.assertEqual([row['text'] for row in history if row['role'] == 'assistant'],
                         ['新的配音已接通。手机端还要检查。'])

    async def test_action_ack_does_not_generate_voice_and_has_one_cached_receipt(self):
        b = self.bridge()
        b.daemon.classify_phone_intent = AsyncMock(return_value={'kind':'action'})
        b._complete_transcript_turn('user', '请检查这个项目。', 'u1')
        b._complete_transcript_turn('assistant', '好的，我马上开始。', 'a1')
        await self.drain(b)
        self.assertEqual(b.daemon.relayed, ['请检查这个项目。'])
        b.local_tts.synthesize_live.assert_not_awaited()
        self.assertEqual(b.local_tts.texts, [COMMAND_QUEUED])

    async def test_question_expired_during_tts_cannot_play_late_audio(self):
        b = self.bridge()
        b._intent_decisions['u1'] = {'kind':'question'}
        b._assistant_user_turn_ids['a1'] = 'u1'
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(text):
            entered.set()
            await release.wait()
            return pcm(1200)
        b.local_tts.synthesize_live.side_effect = slow
        task = b._schedule_local_speech('a1', '较晚到达的回答。')
        await entered.wait()
        b._expired_query_ids.add('u1')
        release.set()
        await task
        self.assertEqual(b.audio.playback_snapshot(), [])

    async def test_barge_in_cancels_inflight_tts_not_only_playback(self):
        b = self.bridge()
        b._intent_decisions['u1'] = {'kind':'question'}
        b._assistant_user_turn_ids['a1'] = 'u1'
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def waiting(text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        b.local_tts.synthesize_live.side_effect = waiting
        task = b._schedule_local_speech('a1', '正在生成的回答。')
        await entered.wait()
        b._mark_remote_speech(interrupt=True)
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(b.audio.playback_snapshot(), [])
        self.assertFalse(b._doubao_answer_tasks)

    async def test_failure_plays_one_cached_same_voice_notice_not_old_native(self):
        b = self.bridge()
        b._intent_decisions['u1'] = {'kind':'question'}
        b._assistant_user_turn_ids['a1'] = 'u1'
        b.local_tts.synthesize_live.side_effect = DoubaoError('network_failed')
        await b._schedule_local_speech('a1', '第一句回答。')
        b._assistant_user_turn_ids['a2'] = 'u1'
        await b._schedule_local_speech('a2', '第二句不能继续。')
        self.assertEqual(b.local_tts.texts, [VOICE_FAILURE])
        b.local_tts.synthesize_live.assert_awaited_once()
        self.assertEqual(len(b.pending.job['phone_tts_failures']), 1)

    async def test_incomplete_notice_library_fails_before_any_call_preflight(self):
        b = self.bridge()
        root = Path(tempfile.mkdtemp(prefix='doubao-missing-library-test-'))
        with patch.object(ds, 'load_profile', return_value={
                'model_name': 'Doubao-语音合成-2.0', 'resource_id': RESOURCE_ID, 'speaker': SPEAKER}):
            b.local_tts = ds.DoubaoSpeechRenderer(cache_dir=root/'cache', credential_path=root/'key.json',
                                                validate=AsyncMock())
        b.daemon.ensure_codex = AsyncMock()
        with self.assertRaises(NativeNoticeLibraryError):
            await b._prepare_call()
        b.daemon.ensure_codex.assert_not_awaited()
        self.assertFalse(b.pending.job['phone_startup_failure']['dial_attempted'])

    async def test_no_question_decision_no_synthesis(self):
        b = self.bridge()
        b._assistant_user_turn_ids['a1'] = 'u1'
        await b._schedule_local_speech('a1', '不能擅自播出的确认。')
        b.local_tts.synthesize_live.assert_not_awaited()

    async def test_daemon_prewarm_failure_closes_only_owned_server(self):
        daemon = pa.PhoneDaemon({})
        server = Mock(start=AsyncMock(), request=AsyncMock(side_effect=RuntimeError('routing failed')),
                      close=AsyncMock())
        with patch.object(pa, 'CodexAppServer', return_value=server):
            with self.assertRaisesRegex(RuntimeError, 'routing failed'):
                await daemon.ensure_codex()
        server.close.assert_awaited_once()
        self.assertIsNone(daemon.codex)

    async def test_daemon_still_rejects_api_key_account(self):
        daemon = pa.PhoneDaemon({})
        server = Mock(start=AsyncMock(), request=AsyncMock(return_value={'account':{'type':'apiKey'}}),
                      close=AsyncMock())
        with patch.object(pa, 'CodexAppServer', return_value=server):
            with self.assertRaises(RuntimeError):
                await daemon.ensure_codex()
        server.close.assert_awaited_once()
        self.assertIsNone(daemon.codex)


if __name__ == '__main__':
    unittest.main()
