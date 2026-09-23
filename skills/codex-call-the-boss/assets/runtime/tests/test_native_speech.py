from __future__ import annotations
import asyncio
import json
import hashlib
import tempfile
import sys
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch
from native_speech import NativeSpeechRenderer, NOTICE_TEXTS, VOICE_FAILURE, COMMAND_QUEUED
from speech_quality import speech_alignment
import speech_quality
import phone_agent
import native_speech
from phone_agent import IPhoneVoiceBridge, PendingCall
from test_voice_bridge import FakeDaemon, FakeAudio, FakeLocalTts, FakeRtc
from types import SimpleNamespace


class NativeSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def request_lifecycle(self, mode):
        renderer = NativeSpeechRenderer(voice='cove', cache_dir='/unused-no-write',
            trim=lambda payload: payload, validate=AsyncMock())
        rtc = SimpleNamespace(failed=asyncio.Event(), error_message='', stop=AsyncMock())
        server = SimpleNamespace(closed=asyncio.Event(), close=AsyncMock())
        cancelled = asyncio.Event()

        async def request(method, params):
            self.assertEqual(method, 'test-only/speech-submission')
            self.assertEqual(params['text'], '原文保持不变')
            if mode == 'rpc_error_after_done':
                renderer._done.set()
                raise RuntimeError('synthetic RPC rejection')
            if mode == 'closed_before_ack':
                rtc.error_message = 'synthetic event channel closed'
                rtc.failed.set()
            elif mode == 'server_closed_before_ack':
                server.last_error = 'synthetic app-server closed'
                server.closed.set()
            elif mode == 'capture_error_before_ack':
                renderer._capture_error = 'synthetic unrelated turn'
                renderer._done.set()
            elif mode == 'done_without_ack':
                renderer._done.set()
            elif mode == 'ack_without_done':
                return {}
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        server.request = AsyncMock(side_effect=request)
        # Exercise submission/completion lifecycle independently of the wire
        # adapter; the real data-channel sender has its own no-RPC tests.
        renderer._submit_speech = lambda text: server.request(
            'test-only/speech-submission', {'text': text})
        async def opened():
            renderer.rtc, renderer.server = rtc, server
            if mode == 'already_failed':
                rtc.error_message = 'synthetic already failed'
                rtc.failed.set()
        renderer._open = opened
        return renderer, server, cancelled

    async def test_native_channel_closure_cancels_unacknowledged_speech_request(self):
        for mode in ('closed_before_ack', 'server_closed_before_ack', 'capture_error_before_ack'):
            with self.subTest(mode=mode):
                renderer, server, cancelled = await self.request_lifecycle(mode)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                        await asyncio.wait_for(renderer._generate('原文保持不变'), .25)
                    self.assertTrue(cancelled.is_set())
                    server.request.assert_awaited_once()
                finally:
                    await renderer.close()

    async def test_native_already_failed_channel_never_appends_speech(self):
        renderer, server, _ = await self.request_lifecycle('already_failed')
        try:
            with self.assertRaisesRegex(RuntimeError, 'already failed'):
                await asyncio.wait_for(renderer._generate('原文保持不变'), .25)
            server.request.assert_not_awaited()
        finally:
            await renderer.close()

    async def test_native_done_cannot_hide_rpc_rejection(self):
        renderer, _, _ = await self.request_lifecycle('rpc_error_after_done')
        try:
            with self.assertRaisesRegex(RuntimeError, 'RPC rejection'):
                await renderer._generate('原文保持不变')
        finally:
            await renderer.close()

    async def test_native_response_bound_requires_both_ack_and_completion(self):
        for mode in ('done_without_ack', 'ack_without_done'):
            with self.subTest(mode=mode):
                renderer, server, cancelled = await self.request_lifecycle(mode)
                try:
                    with patch.object(native_speech, 'NATIVE_SPEECH_RESPONSE_TIMEOUT_SECONDS', .02):
                        with self.assertRaises(TimeoutError):
                            await asyncio.wait_for(renderer._generate('原文保持不变'), .25)
                    server.request.assert_awaited_once()
                    if mode == 'done_without_ack':
                        self.assertTrue(cancelled.is_set())
                finally:
                    await renderer.close()

    async def test_cached_alternative_does_not_weaken_generation_exact_script_gate(self):
        with patch.object(phone_agent, '_offline_whisper_transcript_async', new=AsyncMock()) as qa:
            result = await phone_agent.validate_native_clip(b'\x00\x20' * 192000, COMMAND_QUEUED,
                native_speech.COMMAND_WAIT_RECEIPT, Path('/unused.wav'))
        self.assertFalse(result['passed'])
        self.assertLess(result['script_similarity'], 1.0)
        qa.assert_not_awaited()

    async def approved_receipt(self, directory):
        renderer = self.renderer(directory)
        pcm = b'\x00\x10' * 48000 + b'\x00\x00' * 2400 + b'\x00\x20' * 96000
        renderer._generate = AsyncMock(return_value=(pcm, native_speech.COMMAND_RECEIPT))
        await renderer.synthesize(native_speech.COMMAND_RECEIPT)
        _, path = renderer.paths(native_speech.COMMAND_RECEIPT)
        meta = json.loads(path.read_text())
        meta.update(human_listening_verified=True, acceptance={
            'kind': 'explicit_listener_acceptance', 'pcm_sha256': meta['sha256']})
        path.write_text(json.dumps(meta))
        renderer._generate.reset_mock()
        return renderer, pcm, meta['sha256']

    async def test_preserved_suffix_is_not_an_automatic_command_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer, source, digest = await self.approved_receipt(directory)
            before = renderer.paths(native_speech.COMMAND_RECEIPT)[1].read_bytes()
            result = await renderer.prepare_delivery_excerpt(source_pcm_sha256=digest, start_frame=49200)
            renderer.cache_only = True
            self.assertTrue(renderer.cached(COMMAND_QUEUED) is None,
                            'A retained cropped suffix must not be selected for a command')
            with self.assertRaisesRegex(RuntimeError, 'not prepared'):
                await renderer.synthesize(COMMAND_QUEUED)
            pcm = await renderer.synthesize(native_speech.COMMAND_WAIT_RECEIPT)
            self.assertEqual(pcm, source[49200 * 2:])
            self.assertEqual(renderer.last_spoken_text, native_speech.COMMAND_WAIT_RECEIPT)
            self.assertNotIn('开始执行', renderer.last_spoken_text)
            self.assertFalse(result['human_listening_verified'])
            self.assertEqual(renderer.paths(native_speech.COMMAND_RECEIPT)[0].read_bytes(), source)
            self.assertEqual(renderer.paths(native_speech.COMMAND_RECEIPT)[1].read_bytes(), before)
            renderer._generate.assert_not_awaited()
            self.assertIsNone(renderer._cached_exact(COMMAND_QUEUED))

    async def test_actual_delivery_branch_uses_complete_receipt_or_explicit_unconfirmed_notice(self):
        for status in ('accepted', 'target_turn_started', 'active_target_steered'):
            with tempfile.TemporaryDirectory() as directory:
                renderer, source, digest = await self.approved_receipt(directory)
                await renderer.prepare_delivery_excerpt(source_pcm_sha256=digest, start_frame=49200)
                renderer._generate = AsyncMock(return_value=(b'\x00\x30' * 96000, COMMAND_QUEUED))
                await renderer.synthesize(COMMAND_QUEUED)
                renderer._generate.reset_mock()
                renderer.cache_only = True
                daemon = FakeDaemon(); daemon.config['phone_voice_renderer'] = 'realtime-unified'
                delivered = asyncio.Event()
                async def relay(*args, **kwargs):
                    await delivered.wait()
                    return {'status': status}
                daemon.relay_phone_task = relay
                bridge = IPhoneVoiceBridge(daemon, PendingCall('test', {'thread_id': 'source'},
                    'token', Path(directory)/'job.json'), local_tts=renderer)
                bridge.audio = FakeAudio()
                task = asyncio.create_task(bridge._relay_phone_task('修复问题', 'user-one'))
                await asyncio.sleep(.01)
                self.assertNotIn('phone_transcript', bridge.pending.job)
                delivered.set(); await task
                await asyncio.gather(*tuple(bridge._local_speech_tasks))
                rows = bridge.pending.job['phone_transcript']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['text'], COMMAND_QUEUED if status == 'accepted'
                                 else native_speech.COMMAND_RECEIPT)
                self.assertNotEqual(rows[0]['text'], native_speech.COMMAND_WAIT_RECEIPT)
                renderer._generate.assert_not_awaited()

    async def test_production_suffix_command_refuses_an_active_line(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'phone-line-unconfirmed.json').write_text('{}')
            with patch.object(phone_agent, 'STATE_DIR', Path(directory)), \
                 patch.object(phone_agent, 'load_config', return_value={'phone_voice_renderer': 'realtime-unified'}), \
                 patch.object(phone_agent, 'native_speech_renderer') as factory:
                with self.assertRaisesRegex(RuntimeError, 'active or unconfirmed'):
                    await phone_agent.prepare_delivery_excerpt('0' * 64, 1000)
                factory.assert_not_called()

    async def test_suffix_rejects_unapproved_wrong_hash_or_voiced_boundary(self):
        for mode in ('unapproved', 'hash', 'voiced'):
            with tempfile.TemporaryDirectory() as directory:
                renderer, source, digest = await self.approved_receipt(directory)
                if mode == 'unapproved':
                    path = renderer.paths(native_speech.COMMAND_RECEIPT)[1]
                    meta = json.loads(path.read_text()); meta['human_listening_verified'] = False
                    path.write_text(json.dumps(meta))
                with self.assertRaises(ValueError):
                    await renderer.prepare_delivery_excerpt(
                        source_pcm_sha256='0' * 64 if mode == 'hash' else digest,
                        start_frame=4800 if mode == 'voiced' else 49200)
                renderer._generate.assert_not_awaited()
                self.assertIsNone(renderer.cached(native_speech.COMMAND_WAIT_RECEIPT))

    async def test_suffix_content_rejection_is_not_promoted_or_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer, source, digest = await self.approved_receipt(directory)
            renderer.validate = AsyncMock(return_value={'passed': False, 'script_similarity': 1.0})
            with self.assertRaises(native_speech.NativeClipRejectedError):
                await renderer.prepare_delivery_excerpt(source_pcm_sha256=digest, start_frame=49200)
            self.assertIsNone(renderer.cached(native_speech.COMMAND_WAIT_RECEIPT))
            with self.assertRaises(ValueError):
                await renderer.prepare_delivery_excerpt(source_pcm_sha256=digest, start_frame=49200)
            renderer.validate.assert_awaited_once()
            renderer._generate.assert_not_awaited()

    async def test_suffix_veto_tracks_both_parent_and_derivative(self):
        for reject_parent in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                renderer, source, digest = await self.approved_receipt(directory)
                result = await renderer.prepare_delivery_excerpt(source_pcm_sha256=digest, start_frame=49200)
                veto = renderer._listener_rejection_path(digest if reject_parent else result['sha256'])
                veto.parent.mkdir(parents=True); veto.write_text('{}')
                self.assertIsNone(renderer.cached(native_speech.COMMAND_WAIT_RECEIPT))

    async def test_readiness_does_not_substitute_started_audio_for_missing_delivery_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer, source, digest = await self.approved_receipt(directory)
            with self.assertRaises(native_speech.NativeNoticeLibraryError) as caught:
                renderer.require_cached([COMMAND_QUEUED])
            self.assertEqual(caught.exception.missing, (COMMAND_QUEUED,))
            self.assertEqual(caught.exception.code, 'native_notice_library_incomplete')
            library = renderer.notice_readiness()
            self.assertFalse(library['ready'])
            self.assertEqual(library['ready_count'], 1)
            renderer._generate.assert_not_awaited()

    async def test_missing_library_preparation_manifest_is_not_an_opening_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(Path(directory)/'audio')
            with patch.object(phone_agent, 'STATE_DIR', Path(directory)), \
                 patch.object(phone_agent, 'load_config', return_value={'phone_voice_renderer': 'realtime-unified'}), \
                 patch.object(phone_agent, 'native_speech_renderer', return_value=renderer), \
                 patch.object(phone_agent, 'output_probe_seconds', return_value=0):
                with self.assertRaises(native_speech.NativeNoticeLibraryError):
                    await phone_agent.prepare_native_audio('修复正在验证。', evidence_source='source-123', evidence_turn_id='root-123')
            record = json.loads((Path(directory)/'preparations/root-123.json').read_text())
            self.assertEqual(record['failure_code'], 'native_notice_library_incomplete')
            self.assertEqual(record['preparation_stage'], 'notice_library')
            self.assertIn(COMMAND_QUEUED, record['missing_notices'])
            self.assertFalse(record['dial_attempted'])
            renderer._generate.assert_not_awaited()

    def test_pronunciation_context_is_only_for_the_opening_address(self):
        for text in ('老板，任务已经完成。', '老板,任务已完成。'):
            context = native_speech.opening_pronunciation_context(text)
            self.assertTrue(context)
            self.assertEqual(context, native_speech.OPENING_PRONUNCIATION_CONTEXT)
        for text in (*NOTICE_TEXTS, '请老板确认。', '老板您好', '“老板，任务已完成。”'):
            self.assertEqual(native_speech.opening_pronunciation_context(text), '')

    def test_opening_cache_namespace_changes_without_invalidating_notices(self):
        renderer = self.renderer('/unused-cache-policy')
        for text in NOTICE_TEXTS:
            old_key = hashlib.sha256(json.dumps([native_speech.CACHE_VERSION, 'cove', text],
                ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(renderer.paths(text)[0].stem, old_key)
        text = '老板，任务已经完成。'
        old_key = hashlib.sha256(json.dumps([native_speech.CACHE_VERSION, 'cove', text],
            ensure_ascii=False).encode()).hexdigest()
        self.assertNotEqual(renderer.paths(text)[0].stem, old_key)

    async def test_previous_opening_is_preserved_and_cannot_bypass_new_preparation(self):
        text, pcm = '老板，任务已经完成。', b'\x01\x00' * 48000
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            key = hashlib.sha256(json.dumps([native_speech.CACHE_VERSION, 'cove', text],
                ensure_ascii=False).encode()).hexdigest()
            old_pcm, old_meta = Path(directory)/(key+'.pcm'), Path(directory)/(key+'.json')
            old_pcm.write_bytes(pcm)
            old_meta.write_text(json.dumps({'version':native_speech.CACHE_VERSION,
                'voice':'cove','text':text,'passed':True,'script_similarity':1.0,
                'sha256':hashlib.sha256(pcm).hexdigest(),
                'alignment_revision':speech_quality.ALIGNMENT_REVISION}))
            before = old_meta.read_bytes()
            renderer.cache_only = True
            with self.assertRaisesRegex(RuntimeError, 'not prepared'):
                await renderer.synthesize(text)
            renderer._generate.assert_not_awaited()
            self.assertEqual(old_meta.read_bytes(), before)
            self.assertEqual(old_pcm.read_bytes(), pcm)

    async def test_opening_proof_and_pcm_survive_cache_only_reuse(self):
        text, pcm = '老板，任务已经完成。', b'\x00\x10' * 48000
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            renderer._generate = AsyncMock(return_value=(pcm, text))
            self.assertEqual(await renderer.synthesize(text), pcm)
            _, meta_path = renderer.paths(text)
            meta = json.loads(meta_path.read_text())
            self.assertEqual(meta['opening_pronunciation'], native_speech.opening_pronunciation_proof(text))
            renderer.cache_only = True
            self.assertEqual(await renderer.synthesize(text), pcm)
            renderer._generate.assert_awaited_once_with(text)
            meta.pop('opening_pronunciation')
            meta_path.write_text(json.dumps(meta))
            self.assertIsNone(renderer.cached(text))

    async def test_connected_address_cache_cannot_satisfy_approved_natural_address(self):
        text, pcm = '老板，任务已经完成。', b'\x00\x10' * 48000
        previous_proof = {'revision': 'mandarin-connected-address-v1',
            'prompt_sha256': '894021cd25572c372c87572f90391103f92fd0f40ce13af13d995b5195c72bdd'}
        with tempfile.TemporaryDirectory() as directory:
            old_key = hashlib.sha256(json.dumps([native_speech.CACHE_VERSION, 'cove', text,
                previous_proof], ensure_ascii=False).encode()).hexdigest()
            old_pcm, old_meta = Path(directory)/(old_key+'.pcm'), Path(directory)/(old_key+'.json')
            old_pcm.write_bytes(pcm)
            old_meta.write_text(json.dumps({'version':native_speech.CACHE_VERSION,
                'voice':'cove','text':text,'passed':True,'script_similarity':1.0,
                'sha256':hashlib.sha256(pcm).hexdigest(), 'opening_pronunciation':previous_proof,
                'alignment_revision':speech_quality.ALIGNMENT_REVISION}))
            before = old_meta.read_bytes()
            renderer = self.renderer(directory)
            renderer.cache_only = True
            with self.assertRaisesRegex(RuntimeError, 'not prepared'):
                await renderer.synthesize(text)
            renderer._generate.assert_not_awaited()
            self.assertNotEqual(renderer.paths(text)[0], old_pcm)
            self.assertEqual(old_meta.read_bytes(), before)
            self.assertEqual(old_pcm.read_bytes(), pcm)

    async def capture_clock_domains(self, *, offset=4000, media_first=False,
                                    missing_tail=False, following=False, no_own_media=False):
        callbacks = {}
        original, other = b'\x00\x10'*960, b'\x00\x20'*960
        class Rtc:
            def __init__(self, **kwargs):
                callbacks.update(kwargs)
                self.failed = asyncio.Event(); self.error_message = ''
            async def start(self, **kwargs): pass
            async def stop(self, server): pass
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'config/read': return {'config': {'mcp_servers': {}}}
                if method == 'thread/start': return {'thread': {'id': 'literal-clock-test'}}
                if method != 'test-only/speech-submission': raise AssertionError(method)
                def frames():
                    if no_own_media: return
                    for stamp in range(2000, 2200 if missing_tail else 2400, 20):
                        callbacks['on_pcm_frame'](original, {'media_ms': stamp, 'duration_ms': 20,
                                                           'arrival': time.monotonic()})
                if media_first: frames()
                callbacks['on_event']({'type': 'output_transcript.added',
                    'start_ms': 2000+offset, 'end_ms': 2400+offset, 'text': params['text']})
                own = {'id': 'own-clock', 'role': 'assistant', 'start_ms': 2000+offset,
                       'end_ms': 2400+offset, 'transcript': params['text']}
                callbacks['on_event']({'type': 'turn.created', 'turn': own})
                if not media_first: frames()
                callbacks['on_event']({'type': 'turn.done', 'turn': own})
                if following:
                    # A disjoint later response may beat its data event. It
                    # cannot anchor, finish or extend the original response.
                    for stamp in range(3000, 3400, 20):
                        callbacks['on_pcm_frame'](other, {'media_ms': stamp, 'duration_ms': 20,
                                                        'arrival': time.monotonic()})
                    callbacks['on_event']({'type': 'turn.created', 'turn': {
                        'id': 'following-clock', 'role': 'assistant', 'start_ms': 3000+offset}})
                return {}
        with patch.object(native_speech, 'CodexAppServer', Server), patch.object(native_speech, 'CodexWebRtcSession', Rtc):
            renderer = NativeSpeechRenderer(voice='cove', cache_dir='/does-not-write-here',
                trim=lambda payload: payload, validate=AsyncMock())
            renderer._submit_speech = lambda text: renderer.server.request(
                'test-only/speech-submission', {'text': text})
            try:
                payload, transcript = await renderer._generate('老板您好')
                return payload, transcript, renderer
            finally:
                await renderer.close()

    async def test_literal_clock_shift_preserves_same_pcm_in_both_event_orders(self):
        for offset in (0, 4000):
            for media_first in (False, True):
                with self.subTest(offset=offset, media_first=media_first):
                    payload, text, renderer = await self.capture_clock_domains(offset=offset, media_first=media_first)
                    self.assertEqual(payload, b'\x00\x10'*960*20)
                    self.assertEqual(text, '老板您好')
                    self.assertEqual(renderer._clip_end_ms, 2400)
                    self.assertEqual(renderer._clip_aggregate_end_ms, 2400)
                    self.assertEqual(renderer._clip_text_end_ms, 2400+offset)
                    self.assertEqual(renderer._output_clock.domain_offset_ms, offset)

    async def test_literal_clock_anchor_cannot_make_missing_tail_complete(self):
        with self.assertRaisesRegex(RuntimeError, 'tail.*incomplete'):
            await self.capture_clock_domains(missing_tail=True)

    async def test_literal_following_turn_stays_in_text_domain_and_outside_owned_pcm(self):
        payload, _, renderer = await self.capture_clock_domains(following=True)
        self.assertEqual(payload, b'\x00\x10'*960*20)
        self.assertEqual(renderer._ignored_following_turns, 1)
        self.assertEqual(renderer._clip_aggregate_end_ms, 2400)
        self.assertEqual(renderer._output_clock.domain_offset_ms, 4000)

    async def test_later_media_cannot_anchor_an_already_completed_empty_clip(self):
        with self.assertRaisesRegex(RuntimeError, 'tail.*incomplete'):
            await self.capture_clock_domains(no_own_media=True, following=True)

    def test_literal_context_excludes_mcp_without_mutating_owner_config(self):
        config = {'config': {'mcp_servers': {'one': {'enabled': True, 'command': 'private'},
                                           'two': {'url': 'private'}}, 'model': 'owner-model'}}
        before = json.dumps(config, sort_keys=True)
        overrides = native_speech.literal_context_config(config)
        self.assertEqual(overrides['mcp_servers'], {'one': {'enabled': False}, 'two': {'enabled': False}})
        self.assertNotIn('model', overrides)
        self.assertNotIn('model_reasoning_effort', overrides)
        self.assertEqual(json.dumps(config, sort_keys=True), before)

    def test_literal_context_rejects_unknown_config_shape(self):
        for config in (None, {}, {'config': None}, {'config': {'mcp_servers': []}}):
            with self.subTest(config=config), self.assertRaisesRegex(RuntimeError, 'configuration unavailable'):
                native_speech.literal_context_config(config)

    async def test_literal_context_overrides_are_only_on_its_readonly_thread_start(self):
        requests = []
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                requests.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'config/read': return {'config': {'mcp_servers': {'external': {}}}}
                if method == 'thread/start': return {'thread': {'id': 'literal-only'}}
                raise AssertionError('Must not write global config or start speaking')
        rtc = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, patch.object(native_speech, 'CodexAppServer', Server), \
                patch.object(native_speech, 'CodexWebRtcSession', return_value=rtc):
            renderer = NativeSpeechRenderer(voice='cove', cache_dir=directory,
                trim=lambda p: p, validate=AsyncMock())
            try:
                await renderer._open()
                params = requests[-1][1]
                self.assertEqual([m for m, _ in requests], ['account/read', 'config/read', 'thread/start'])
                self.assertTrue(params['ephemeral'])
                self.assertEqual(params['sandbox'], 'read-only')
                self.assertEqual(params['selectedCapabilityRoots'], [])
                self.assertEqual(params['config']['mcp_servers'], {'external': {'enabled': False}})
                self.assertNotIn('model', params)
                self.assertEqual(rtc.start.await_args.kwargs['voice'], 'cove')
            finally:
                await renderer.close()

    async def capture_extra_turn(self, directory, *, completed=True, start=1400, media_first=False, end=1120):
        callbacks = {}
        original = b'\x01\x02' * 960
        following = b'\x03\x04' * 960
        class Rtc:
            def __init__(self, **kwargs):
                callbacks.update(kwargs)
                self.failed = asyncio.Event(); self.error_message = None
            async def start(self, **kwargs): pass
            async def stop(self, server): pass
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'config/read': return {'config': {'mcp_servers': {}}}
                if method == 'thread/start': return {'thread': {'id': 'only-clip'}}
                if method != 'test-only/speech-submission': return {}
                callbacks['on_event']({'type': 'turn.created', 'turn': {
                    'id': 'own', 'role': 'assistant', 'start_ms': 1000}})
                callbacks['on_pcm_frame'](original, {'media_ms': 1100, 'duration_ms': 20})
                if media_first:
                    callbacks['on_pcm_frame'](following, {'media_ms': 1400, 'duration_ms': 20})
                if completed:
                    callbacks['on_event']({'type': 'turn.done', 'turn': {
                        'id': 'own', 'role': 'assistant', 'start_ms': 1000,
                        'end_ms': end, 'transcript': params['text']}})
                callbacks['on_event']({'type': 'turn.created', 'turn': {
                    'id': 'next', 'role': 'assistant', 'start_ms': start}})
                callbacks['on_pcm_frame'](following, {'media_ms': 1400, 'duration_ms': 20})
                callbacks['on_event']({'type': 'turn.done', 'turn': {
                    'id': 'next', 'role': 'assistant', 'start_ms': start,
                    'end_ms': 1420, 'transcript': '别的一段话'}})
                return {}
        with patch.object(native_speech, 'CodexAppServer', Server), patch.object(native_speech, 'CodexWebRtcSession', Rtc):
            renderer = NativeSpeechRenderer(voice='cove', cache_dir=directory,
                trim=lambda p: p, validate=AsyncMock())
            renderer._submit_speech = lambda text: renderer.server.request(
                'test-only/speech-submission', {'text': text})
            try:
                return await renderer._generate('原本的台词')
            finally:
                await renderer.close()

    async def test_completed_clip_excludes_later_turn_on_both_event_media_orders(self):
        for media_first in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(await self.capture_extra_turn(directory, media_first=media_first),
                                 (b'\x01\x02' * 960, '原本的台词'))

    async def test_extra_turn_before_completion_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'More than one'):
                await self.capture_extra_turn(directory, completed=False)

    async def test_overlapping_or_unbound_extra_turn_still_fails_closed(self):
        for start in (1100, None):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, 'More than one'):
                    await self.capture_extra_turn(directory, start=start)

    async def test_following_turn_media_cannot_certify_missing_original_tail(self):
        for media_first in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, 'tail.*incomplete'):
                    await self.capture_extra_turn(directory, end=1320, media_first=media_first)

    async def test_each_literal_script_is_bound_before_the_media_session_starts(self):
        callbacks, starts, requests = [], [], []
        scripts = ['老板，第一项检查已完成。', '检查还没有通过，请先不要执行。',
                   '老板，第二项检查已完成。', native_speech.COMMAND_RECEIPT]
        joinable = [False, False, True, True]
        class Rtc:
            def __init__(self, **kwargs):
                callbacks.append(kwargs)
                self.failed = asyncio.Event(); self.error_message = None
            async def start(self, **kwargs):
                starts.append(kwargs)
                script = scripts[len(starts)-1]
                self_test.assertIn({'role': 'developer', 'text': native_speech.literal_script_binding(script)},
                                   kwargs['initial_items'])
                self_test.assertNotIn(native_speech.literal_speech_request(script),
                                      [item['text'] for item in kwargs['initial_items']])
                self_test.assertEqual(kwargs['voice'], 'cove')
                self_test.assertTrue(kwargs['client_managed_handoffs'])
                self_test.assertFalse(kwargs['include_startup_context'])
                pronunciation = native_speech.opening_pronunciation_context(script, joinable=joinable[len(starts)-1])
                expected = [{'role': 'developer', 'text': pronunciation}] if pronunciation else []
                actual = [item for item in kwargs['initial_items']
                          if item.get('text', '').startswith(native_speech.OPENING_PRONUNCIATION_CONTEXT)]
                self_test.assertEqual(actual, expected)
                self_test.assertEqual(kwargs['prompt'], native_speech.NATIVE_LITERAL_PROMPT)
                # Unrequested startup callbacks must not become this clip.
                callbacks[-1]['on_event']({'type': 'turn.created', 'turn': {
                    'role': 'assistant', 'id': 'startup', 'start_ms': 0}})
                callbacks[-1]['on_pcm_frame'](b'startup', {'media_ms': 0, 'duration_ms': 20})
                callbacks[-1]['on_event']({'type': 'turn.done', 'turn': {
                    'role': 'assistant', 'id': 'startup', 'transcript': '这不是台词', 'end_ms': 20}})
            async def stop(self, server): pass
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                requests.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/start': return {'thread': {'id': f'clip-{len(starts)}'}}
                if method == 'config/read': return {'config': {'mcp_servers': {}}}
                if method != 'test-only/speech-submission': return {}
                identity = f'own-turn-{len(starts)}'
                # Known pre-request turns may emit duplicated created/done
                # callbacks after readiness. They cannot capture this clip.
                callbacks[-1]['on_event']({'type': 'turn.created', 'turn': {
                    'role': 'assistant', 'id': 'startup', 'start_ms': 0}})
                callbacks[-1]['on_event']({'type': 'turn.done', 'turn': {
                    'role': 'assistant', 'id': 'startup', 'transcript': '迟到的开场', 'end_ms': 20}})
                callbacks[-1]['on_event']({'type': 'turn.created', 'turn': {
                    'role': 'assistant', 'id': identity, 'start_ms': 0}})
                callbacks[-1]['on_pcm_frame'](b'own-voice', {'media_ms': 0, 'duration_ms': 20})
                callbacks[-1]['on_event']({'type': 'turn.done', 'turn': {
                    'role': 'assistant', 'id': identity, 'transcript': params['text'], 'end_ms': 20}})
                return {}
        self_test = self
        with tempfile.TemporaryDirectory() as directory, patch.object(native_speech, 'CodexAppServer', Server), \
                patch.object(native_speech, 'CodexWebRtcSession', Rtc):
            renderer = NativeSpeechRenderer(voice='cove', cache_dir=directory,
                trim=lambda p: p, validate=AsyncMock())
            renderer._submit_speech = lambda text: renderer.server.request(
                'test-only/speech-submission', {'text': text})
            try:
                for script, with_join_pause in zip(scripts, joinable):
                    renderer.opening_join_pause = with_join_pause
                    self.assertEqual(await renderer._generate(script), (b'own-voice', script))
            finally:
                await renderer.close()
        self.assertEqual([params['text'] for method, params in requests
                          if method == 'test-only/speech-submission'], scripts)
        self.assertFalse(any(method == 'thread/realtime/appendSpeech' for method, _ in requests))
        self.assertFalse(any(method == 'thread/realtime/appendText' for method, _ in requests))
        self.assertNotIn(native_speech.literal_script_binding(scripts[0]),
                         [item['text'] for item in starts[1]['initial_items']])

    async def test_rejected_startup_binding_does_not_append_or_retry_speech(self):
        requests, starts = [], []
        class Rtc:
            def __init__(self, **kwargs):
                self.failed = asyncio.Event(); self.error_message = None
            async def start(self, **kwargs):
                starts.append(kwargs)
                raise RuntimeError('initial script rejected')
            async def stop(self, server): pass
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                requests.append(method)
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/start': return {'thread': {'id': 'clip'}}
                if method == 'config/read': return {'config': {'mcp_servers': {}}}
                raise AssertionError('No speech after a failed startup')
        with tempfile.TemporaryDirectory() as directory, patch.object(native_speech, 'CodexAppServer', Server), \
                patch.object(native_speech, 'CodexWebRtcSession', Rtc):
            renderer = NativeSpeechRenderer(voice='cove', cache_dir=directory,
                trim=lambda p: p, validate=AsyncMock())
            with self.assertRaisesRegex(RuntimeError, 'initial script rejected'):
                await renderer.prepare(['只应尝试一次的台词。'])
            self.assertEqual(len(starts), 1)
            self.assertEqual(requests, ['account/read', 'config/read', 'thread/start'])
            self.assertIsNone(renderer.server)
            self.assertIsNone(renderer.rtc)
            renderer.validate.assert_not_awaited()
            self.assertFalse(list(Path(directory).glob('*.pcm')))

    async def test_cancelled_qa_reaps_its_real_child_before_returning(self):
        spawned = asyncio.Event()
        processes = []
        original = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            child = await original(*args, **kwargs)
            processes.append(child)
            spawned.set()
            return child
        with patch.object(phone_agent.asyncio, 'create_subprocess_exec', side_effect=spawn):
            task = asyncio.create_task(phone_agent._run_offline_speech_qa(
                [sys.executable, '-c', 'import time; time.sleep(30)']))
            await spawned.wait()
            began = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertIsNotNone(processes[0].returncode)
        self.assertLess(time.monotonic()-began, 1)

    async def test_qa_timeout_does_not_leave_a_running_process(self):
        processes = []
        original = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            child = await original(*args, **kwargs)
            processes.append(child)
            return child
        with patch.object(phone_agent.asyncio, 'create_subprocess_exec', side_effect=spawn):
            with self.assertRaises(TimeoutError):
                await phone_agent._run_offline_speech_qa(
                    [sys.executable, '-c', 'import time; time.sleep(30)'], timeout=.05)
        self.assertIsNotNone(processes[0].returncode)

    async def test_async_qa_keeps_the_existing_unprompted_output_parser(self):
        result = await phone_agent._run_offline_speech_qa(
            [sys.executable, '-c', 'print("[00:00.000 --> 00:01.000] 测试完成。")'])
        self.assertEqual(result, ('测试完成。', ''))

    async def test_invalid_script_or_waveform_skips_expensive_qa_without_passing(self):
        with patch.object(phone_agent, '_offline_whisper_transcript_async', new_callable=AsyncMock) as qa:
            for payload, script in ((b'\x00\x08'*48000, '改写了台词'), (b'\0'*96000, '原来的台词')):
                result = await phone_agent.validate_native_clip(payload, '原来的台词', script, Path('unused.wav'))
                self.assertFalse(result['passed'])
                self.assertEqual(result['alignment']['method'], 'skipped_invalid_script_or_waveform')
            qa.assert_not_awaited()

    async def test_same_turn_full_script_repetition_is_not_normalized_into_success(self):
        text = '老板，开场重复接话已修正，实际通话还需验证。'
        payload = b'\x00\x08' * (48 * 9100)
        with patch.object(phone_agent, '_offline_whisper_transcript_async', new_callable=AsyncMock) as qa:
            result = await phone_agent.validate_native_clip(payload, text, text + text, Path('unused.wav'))
        self.assertFalse(result['passed'])
        self.assertAlmostEqual(result['script_similarity'], 2/3, places=3)
        qa.assert_not_awaited()

    async def test_each_rejected_attempt_keeps_its_own_completion_events(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory, False)
            async def generate(text):
                sequence = renderer._generate.await_count
                renderer._clip_turn_id = f'clip-{sequence}'
                renderer._events = [{'type': 'turn.done', 'turn': {
                    'id': renderer._clip_turn_id, 'role': 'assistant', 'transcript': text * 2}}]
                return b'\x01\x00' * 48000, text * 2
            renderer._generate = AsyncMock(side_effect=generate)
            with self.assertRaises(native_speech.NativeClipRejectedError):
                await renderer.prepare(['重复的坏样本'])
            evidence = [json.loads(p.read_text()) for p in Path(directory).glob('*-rejected-*.json')]
            self.assertEqual(len(evidence), 2)
            self.assertEqual({m['clip_turn_id'] for m in evidence}, {'clip-1', 'clip-2'})
            for meta in evidence:
                self.assertEqual(meta['events'][-1]['turn']['id'], meta['clip_turn_id'])
                self.assertFalse(meta['passed'])
            self.assertIsNone(renderer.cached('重复的坏样本'))

    async def run_tail_capture(self, directory, tail_kind):
        pending = []
        callbacks = {}
        class Rtc:
            def __init__(self, **kwargs):
                callbacks.update(kwargs)
                self.failed = asyncio.Event(); self.error_message = None
            async def start(self, **kwargs): pass
            async def stop(self, server): pass
        class Server:
            def __init__(self): self.closed = asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/start': return {'thread': {'id': 'clip-context'}}
                if method == 'config/read': return {'config': {'mcp_servers': {}}}
                if method != 'test-only/speech-submission': return {}
                callbacks['on_event']({'type': 'turn.created', 'turn': {
                    'role': 'assistant', 'id': 'clip', 'start_ms': 1000}})
                callbacks['on_pcm_frame'](b'head', {'media_ms': 1100, 'duration_ms': 20})
                callbacks['on_event']({'type': 'turn.done', 'turn': {
                    'role': 'assistant', 'id': 'clip', 'end_ms': 1320, 'transcript': params['text']}})
                async def tail():
                    await asyncio.sleep(.65 if tail_kind == 'late' else .02)
                    if tail_kind == 'late':
                        callbacks['on_pcm_frame'](b'tail', {'media_ms': 1300, 'duration_ms': 20})
                    elif tail_kind == 'reader_error':
                        speech.rtc.error_message = 'Realtime audio reader failed'
                        speech.rtc.failed.set()
                pending.append(asyncio.create_task(tail()))
                return {}
        with patch.object(native_speech, 'CodexAppServer', Server), patch.object(native_speech, 'CodexWebRtcSession', Rtc):
            speech = NativeSpeechRenderer(voice='cove', cache_dir=directory,
                trim=lambda p:p, validate=AsyncMock(return_value={'passed': True, 'script_similarity': 1.0}))
            speech._submit_speech = lambda text: speech.server.request(
                'test-only/speech-submission', {'text': text})
            try:
                return await speech.synthesize('完整台词')
            finally:
                for task in pending: task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                await speech.close()

    async def test_done_event_cannot_discard_a_late_media_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(await self.run_tail_capture(directory, 'late'), b'headtail')

    async def test_missing_media_tail_is_not_cached_even_if_text_would_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'tail.*incomplete'):
                await self.run_tail_capture(directory, 'missing')
            self.assertFalse(list(Path(directory).glob('*.pcm')))

    async def test_reader_failure_during_tail_drain_cannot_create_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'audio reader failed'):
                await self.run_tail_capture(directory, 'reader_error')
            self.assertFalse(list(Path(directory).glob('*.pcm')))

    async def test_missing_negation_in_long_report_is_not_accepted(self):
        text='这次任务的检查还没有通过，我们已经保存问题记录，接下来会继续查明原因。'
        altered=text.replace('没有通过','已经通过')
        pcm=b'\x00\x08'*480000
        with patch.object(phone_agent,'_offline_whisper_transcript_async',new_callable=AsyncMock,return_value=(altered,'')):
            result=await phone_agent.validate_native_clip(pcm,text,altered,Path('synthetic.wav'))
        self.assertFalse(result['passed'])

    def renderer(self, directory, passed=True):
        renderer = NativeSpeechRenderer(voice='cove',cache_dir=directory,trim=lambda p:p,
            validate=AsyncMock(return_value={'passed':passed,'script_similarity':1.0}))
        renderer._generate = AsyncMock(return_value=(b'\x01\x00'*48000, '测试原声'))
        return renderer

    async def test_validated_cache_survives_new_renderer_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.renderer(directory)
            expected = await first.synthesize('测试原声')
            second = self.renderer(directory)
            second.cache_only = True
            self.assertEqual(await second.synthesize('测试原声'),expected)
            second._generate.assert_not_awaited()
            self.assertEqual(next(Path(directory).glob('*.pcm')).stat().st_mode & 0o777,0o600)

    async def test_legacy_cache_checks_current_status_rules_without_overwriting_it(self):
        text = '老板，任务还没有完成，暂时不能确认全部正常。'
        for actual, allowed in (
            ('老闆，任務還沒有完成，暫時不能確認全部正常。', True),
            ('老板，任务还没有修复，暂时不能确认全部正常。', False),
            ('', False),
        ):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as directory:
                renderer = self.renderer(directory)
                renderer._generate = AsyncMock(return_value=(b'\x01\x00' * 48000, text))
                await renderer.synthesize(text)
                _, meta_path = renderer.paths(text)
                meta = json.loads(meta_path.read_text())
                meta.pop('alignment_revision')
                meta['offline_transcript'] = actual
                meta_path.write_text(json.dumps(meta, ensure_ascii=False))
                original_metadata = meta_path.read_bytes()
                self.assertEqual(renderer.cached(text) is not None, allowed)
                self.assertEqual(meta_path.read_bytes(), original_metadata)
                self.assertEqual(renderer._generate.await_count, 1)

    async def legacy_notice_fixture(self):
        directory = Path(tempfile.mkdtemp(prefix='phone-cache-recheck-test-'))
        renderer = self.renderer(directory)
        text = native_speech.SERVICE_FAILURE
        renderer._generate = AsyncMock(return_value=(b'\x01\x00' * 48000, text))
        pcm = await renderer.synthesize(text)
        pcm_path, meta_path = renderer.paths(text)
        meta = json.loads(meta_path.read_text())
        meta['alignment_revision'] = 'legacy-test-revision'
        meta['offline_transcript'] = text
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
        renderer.cache_only = True
        return renderer, text, pcm, pcm_path, meta_path

    async def test_unchanged_legacy_notice_reuses_only_its_successful_content_recheck(self):
        renderer, text, pcm, pcm_path, meta_path = await self.legacy_notice_fixture()
        original = pcm_path.read_bytes(), meta_path.read_bytes()
        with patch('native_speech.speech_alignment', return_value={'passed': True}) as align:
            renderer.require_cached([text])
            for _ in range(3):
                self.assertEqual(await renderer.synthesize(text), pcm)
            align.assert_called_once_with(text, text)
        self.assertEqual((pcm_path.read_bytes(), meta_path.read_bytes()), original)
        self.assertEqual(renderer._generate.await_count, 1)

    async def test_legacy_recheck_memo_does_not_hide_changed_metadata_pcm_or_listener_veto(self):
        for change in ('metadata', 'pcm', 'listener'):
            with self.subTest(change=change):
                renderer, text, pcm, pcm_path, meta_path = await self.legacy_notice_fixture()
                with patch('native_speech.speech_alignment', return_value={'passed': True}):
                    self.assertEqual(renderer.cached(text), pcm)
                if change == 'metadata':
                    meta = json.loads(meta_path.read_text())
                    meta['offline_transcript'] = '老板，任务已经全部完成。'
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False))
                elif change == 'pcm':
                    pcm_path.write_bytes(b'\x02\x00' * 48000)
                else:
                    marker = renderer._listener_rejection_path(hashlib.sha256(pcm).hexdigest())
                    marker.parent.mkdir(parents=True)
                    marker.write_text('{}')
                with patch('native_speech.speech_alignment', return_value={'passed': False}) as align:
                    self.assertIsNone(renderer.cached(text))
                    self.assertEqual(align.call_count, 1 if change == 'metadata' else 0)
                self.assertEqual(renderer._generate.await_count, 1)

    async def test_legacy_recheck_memo_is_bounded_per_renderer_and_alignment_revision(self):
        renderer, text, pcm, pcm_path, meta_path = await self.legacy_notice_fixture()
        original = meta_path.read_bytes()
        with patch('native_speech.speech_alignment', return_value={'passed': True}) as align:
            self.assertEqual(renderer.cached(text), pcm)
            self.assertEqual(renderer.cached(text), pcm)
            with patch('native_speech.ALIGNMENT_REVISION', 'next-test-revision'):
                self.assertEqual(renderer.cached(text), pcm)
            second = self.renderer(renderer.cache_dir)
            self.assertEqual(second.cached(text), pcm)
            self.assertEqual(align.call_count, 3)
            for index in range(70):
                meta = json.loads(original)
                meta['diagnostic_test_variant'] = index
                meta_path.write_text(json.dumps(meta, ensure_ascii=False))
                self.assertEqual(renderer.cached(text), pcm)
            meta_path.write_bytes(original)
            calls_before = align.call_count
            self.assertEqual(renderer.cached(text), pcm)
            self.assertEqual(align.call_count, calls_before + 1)
        self.assertEqual(len(renderer._legacy_alignment_passes), 64)

    async def test_failed_legacy_recheck_is_never_memoized_as_acceptance(self):
        renderer, text, pcm, _, _ = await self.legacy_notice_fixture()
        with patch('native_speech.speech_alignment', side_effect=[
                {'passed': False}, {'passed': False}, {'passed': True}]) as align:
            self.assertIsNone(renderer.cached(text))
            self.assertIsNone(renderer.cached(text))
            self.assertEqual(renderer.cached(text), pcm)
            self.assertEqual(renderer.cached(text), pcm)
            self.assertEqual(align.call_count, 3)

    async def test_rejected_audio_cannot_be_read_as_accepted_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory,False)
            with self.assertRaises(RuntimeError): await renderer.synthesize('坏语音')
            self.assertIsNone(renderer.cached('坏语音'))
            self.assertTrue(list(Path(directory).glob('*.wav')))

    async def make_listener_fixture(self):
        directory = Path(tempfile.mkdtemp(prefix='phone-listener-feedback-test-'))
        renderer = self.renderer(directory)
        text,pcm = '老板，任务已经完成。', b'\x00\x10'*48000
        renderer._generate = AsyncMock(return_value=(pcm,text))
        await renderer.synthesize(text)
        return directory,renderer,text,pcm

    def reject_fixture(self,renderer,text,pcm,**kwargs):
        return renderer.record_listener_rejection(text,pcm_sha256=hashlib.sha256(pcm).hexdigest(),
            source_thread_id='source-listener',job_id='call-listener',**kwargs)

    async def test_listener_rejection_blocks_asr_approved_pcm_without_rewriting_it(self):
        directory,renderer,text,pcm = await self.make_listener_fixture()
        paths = [*renderer.paths(text),renderer.paths(text)[0].with_suffix('.wav')]
        before = {path:path.read_bytes() for path in paths}
        self.assertEqual(renderer.cached(text),pcm)
        result = self.reject_fixture(renderer,text,pcm,evidence={'actual_feed_matched':True})
        self.assertTrue(result['created'])
        self.assertFalse(result['root_cause_verified'])
        self.assertEqual(before,{path:path.read_bytes() for path in paths})
        self.assertIsNone(renderer.cached(text))
        renderer.cache_only = True
        with self.assertRaisesRegex(RuntimeError,'not prepared'):
            await renderer.synthesize(text)
        renderer._generate.assert_awaited_once()
        marker = Path(result['path'])
        self.assertEqual(marker.stat().st_mode & 0o777,0o600)
        self.assertEqual((marker.parent/'original.pcm').read_bytes(),pcm)
        self.assertEqual((marker.parent/'original.json').read_bytes(),before[paths[1]])

    async def test_listener_rejection_is_idempotent_and_survives_new_renderer(self):
        _,renderer,text,pcm = await self.make_listener_fixture()
        result = self.reject_fixture(renderer,text,pcm)
        marker = Path(result['path'])
        before = marker.read_bytes()
        self.assertFalse(self.reject_fixture(renderer,text,pcm)['created'])
        self.assertEqual(marker.read_bytes(),before)
        self.assertIsNone(self.renderer(renderer.cache_dir).cached(text))

    async def test_listener_rejection_refuses_mismatched_identity_without_writes(self):
        directory,renderer,text,pcm = await self.make_listener_fixture()
        for override in ({'text':'不同台词'}, {'pcm_sha256':'0'*64},
                         {'source_thread_id':'../wrong'}, {'job_id':''}, {'pcm_sha256':'../wrong'}):
            args = {'text':text,'pcm_sha256':hashlib.sha256(pcm).hexdigest(),
                'source_thread_id':'source-listener','job_id':'call-listener',**override}
            with self.subTest(override=override), self.assertRaises((ValueError,FileNotFoundError)):
                renderer.record_listener_rejection(**args)
        self.assertFalse((directory/'listener-rejections').exists())
        self.assertEqual(renderer.cached(text),pcm)

    async def test_same_rejected_waveform_cannot_be_regenerated_into_passing_cache(self):
        directory,renderer,text,pcm = await self.make_listener_fixture()
        self.reject_fixture(renderer,text,pcm)
        with self.assertRaises(native_speech.NativeClipRejectedError):
            await renderer.synthesize(text)
        failures = [json.loads(path.read_text()) for path in directory.glob('*-rejected-*.json')]
        self.assertEqual(len(failures),1)
        self.assertTrue(failures[0]['listener_rejected'])
        self.assertFalse(failures[0]['passed'])
        self.assertIsNone(renderer.cached(text))

    async def test_different_new_pcm_still_requires_quality_and_keeps_negative_evidence(self):
        _,renderer,text,pcm = await self.make_listener_fixture()
        result = self.reject_fixture(renderer,text,pcm)
        marker = Path(result['path'])
        before = marker.read_bytes()
        replacement = b'\x00\x08'*48000
        renderer._generate = AsyncMock(return_value=(replacement,text))
        renderer.validate.reset_mock()
        self.assertEqual(await renderer.synthesize(text),replacement)
        renderer.validate.assert_awaited_once()
        self.assertEqual(renderer.cached(text),replacement)
        self.assertEqual(marker.read_bytes(),before)
        self.assertEqual((marker.parent/'original.pcm').read_bytes(),pcm)

    async def test_rejection_still_blocks_when_snapshot_write_fails(self):
        _,renderer,text,pcm = await self.make_listener_fixture()
        with patch.object(native_speech,'atomic_write',side_effect=OSError('snapshot unavailable')):
            with self.assertRaises(OSError):
                self.reject_fixture(renderer,text,pcm)
        self.assertIsNone(renderer.cached(text))
        result = self.reject_fixture(renderer,text,pcm)
        self.assertFalse(result['created'])
        self.assertEqual((Path(result['path']).parent/'original.pcm').read_bytes(),pcm)

    async def test_delivered_opening_records_actual_pcm_identity(self):
        root = Path(tempfile.mkdtemp(prefix='phone-opening-identity-test-'))
        pending = PendingCall('opening-test',{'spoken_report':'任务检查结束。'},'token',root/'call.json')
        bridge = IPhoneVoiceBridge(FakeDaemon(),pending,local_tts=FakeLocalTts())
        bridge.audio = FakeAudio()
        bridge._announcement_pcm = b'\x00\x08'*48000
        await bridge._deliver_announcement()
        self.assertEqual(pending.job['phone_opening_pcm_sha256'],
            hashlib.sha256(bridge._announcement_pcm).hexdigest())

    async def test_legacy_opening_cache_rechecks_address_without_rewriting_evidence(self):
        text = '老板，修好一处漏话问题，回复速度和原声稳定性还得优化。'
        for actual, allowed in ((text, True), (text[3:], False),
                                ('老爸，' + text[3:], False), ('好吧，' + text[3:], False)):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as directory:
                renderer = self.renderer(directory)
                renderer._generate = AsyncMock(return_value=(b'\x01\x00' * 48000, text))
                await renderer.synthesize(text)
                _, meta_path = renderer.paths(text)
                meta = json.loads(meta_path.read_text())
                meta['alignment_revision'] = 'orthography-and-status-v1'
                meta['offline_transcript'] = actual
                meta_path.write_text(json.dumps(meta, ensure_ascii=False))
                original = meta_path.read_bytes()
                self.assertEqual(renderer.cached(text) is not None, allowed)
                self.assertEqual(meta_path.read_bytes(), original)
                self.assertEqual(renderer._generate.await_count, 1)

    async def test_corrupt_cache_and_voice_mismatch_require_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            await renderer.synthesize('测试原声')
            renderer.paths('测试原声')[0].write_bytes(b'corrupt')
            renderer.cache_only = True
            with self.assertRaises(RuntimeError): await renderer.synthesize('测试原声')
            renderer.voice='juniper'
            self.assertIsNone(renderer.cached('测试原声'))

    async def test_prepare_covers_every_notice_then_refuses_live_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            await renderer.prepare([*NOTICE_TEXTS,'本轮开场'])
            self.assertTrue(renderer.cache_only)
            self.assertEqual(renderer._generate.await_count,len(NOTICE_TEXTS)+1)
            with self.assertRaises(RuntimeError): await renderer.synthesize('未准备的新句子')
            self.assertEqual(renderer._generate.await_count,len(NOTICE_TEXTS)+1)

    async def test_rejected_prepare_is_bounded_to_two_preserved_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory,False)
            with self.assertRaises(RuntimeError):
                await renderer.prepare(['坏语音'])
            self.assertEqual(renderer._generate.await_count,2)
            evidence = [json.loads(p.read_text()) for p in Path(directory).glob('*-rejected-*.json')]
            self.assertEqual(len(evidence), 2)
            self.assertEqual(len(list(Path(directory).glob('*-rejected-*.wav'))), 6)
            self.assertFalse(renderer.cache_only)

    async def test_rejected_audio_preserves_decode_and_ownership_before_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory, False)
            decoded, owned, trimmed = b'\x01\x00' * 100, b'\x01\x00' * 60, b'\x01\x00' * 20
            renderer._received_frames = [(decoded, 0, 100)]
            renderer._generate = AsyncMock(return_value=(owned, '坏语音'))
            renderer.trim = lambda _: trimmed
            with self.assertRaises(native_speech.NativeClipRejectedError):
                await renderer.synthesize('坏语音')
            meta_path = next(Path(directory).glob('*-rejected-*.json'))
            metadata = json.loads(meta_path.read_text())
            for label, expected in (('decoded', decoded), ('owned', owned)):
                item = metadata['rejected_audio_stages'][label]
                path = Path(directory) / item['file']
                with wave.open(str(path), 'rb') as stream:
                    self.assertEqual(stream.readframes(stream.getnframes()), expected)
                self.assertEqual(item['sha256'], hashlib.sha256(expected).hexdigest())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with wave.open(str(meta_path.with_suffix('.wav')), 'rb') as stream:
                self.assertEqual(stream.readframes(stream.getnframes()), trimmed)
            self.assertIsNone(renderer.cached('坏语音'))

    async def test_failed_attempt_keeps_private_hashed_packets_not_playable_cache(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory, False)
            renderer._record_encoded_audio(b'opus', {'ssrc': 3, 'sequence': 5,
                'timestamp': 960, 'arrival_time_ms': 42})
            with self.assertRaises(native_speech.NativeClipRejectedError):
                await renderer.synthesize('坏语音')
            metadata = json.loads((Path(directory)/renderer.rejected_evidence[-1]).read_text())
            evidence = metadata['rejected_packet_evidence']
            path = Path(directory)/evidence['file']
            self.assertEqual(evidence['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(json.loads(path.read_text()), [[3, 5, 960, base64.b64encode(b'opus').decode(), 42]])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).glob('*-packets.json'))), 1)
            failure = json.loads(renderer.paths('坏语音')[1].with_suffix('.failure.json').read_text())
            self.assertEqual(failure['packet_evidence'], evidence)
            self.assertIsNone(renderer.cached('坏语音'))

    async def test_packet_evidence_is_bounded_and_not_saved_for_passing_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            meta = {'ssrc': 3, 'sequence': 5, 'timestamp': 960, 'arrival_time_ms': 42}
            for _ in range(4100): renderer._record_encoded_audio(b'x', meta)
            self.assertEqual(len(renderer._encoded_audio), 4096)
            self.assertEqual(renderer._encoded_dropped, 4)
            renderer._encoded_audio = []
            renderer._encoded_bytes = 0
            renderer._record_encoded_audio(b'x' * (4*1024*1024), meta)
            renderer._record_encoded_audio(b'x', meta)
            self.assertEqual(renderer._encoded_bytes, 4*1024*1024)
            self.assertEqual(renderer._encoded_dropped, 5)
            await renderer.synthesize('测试原声')
            self.assertEqual(list(Path(directory).glob('*-packets.json')), [])

    async def test_opted_in_passing_clip_keeps_exact_private_packets_without_changing_pcm(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            renderer.retain_passed_packet_evidence = True
            renderer._record_encoded_audio(b'opus', {'ssrc': 3, 'sequence': 5,
                'timestamp': 960, 'arrival_time_ms': 42})
            pcm = await renderer.synthesize('测试原声')
            metadata = json.loads(renderer.paths('测试原声')[1].read_text())
            self.assertTrue(metadata['passed'])
            packet = Path(directory)/metadata['packet_evidence']['file']
            self.assertEqual(hashlib.sha256(packet.read_bytes()).hexdigest(), metadata['packet_evidence']['sha256'])
            self.assertEqual(packet.stat().st_mode & 0o777, 0o600)
            self.assertEqual(pcm, b'\x01\x00'*48000)
            self.assertEqual(renderer.cached('测试原声'), pcm)
            self.assertEqual(len(list(Path(directory).glob('*-packets.json'))), 1)
            await renderer.synthesize('测试原声')
            renderer._generate.assert_awaited_once()

    async def test_optional_passing_packet_save_error_does_not_change_content_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            renderer.retain_passed_packet_evidence = True
            with patch.object(renderer, '_save_packet_evidence', side_effect=OSError('unavailable')):
                await renderer.synthesize('测试原声')
            metadata = json.loads(renderer.paths('测试原声')[1].read_text())
            self.assertTrue(metadata['passed'])
            self.assertEqual(metadata['packet_evidence_error'], 'OSError')
            self.assertNotIn('packet_evidence', metadata)

    async def test_passing_packet_retention_is_bound_to_opted_in_preparation_source(self):
        from session_registry import enable_session, set_output_probe, disable_session
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            registry = state/'sessions.json'
            enable_session('source-a12345678', path=registry)
            enable_session('source-b12345678', path=registry)
            set_output_probe('source-a12345678', 60, path=registry)
            with patch.object(phone_agent, 'STATE_DIR', state), \
                    patch.object(phone_agent, 'load_config', return_value={'phone_voice_renderer':'realtime-unified'}):
                for source, enabled in [('source-a12345678', True), ('source-b12345678', False), ('', False)]:
                    renderer = self.renderer(temporary)
                    renderer.require_cached = lambda _: None
                    with patch.object(phone_agent, 'native_speech_renderer', return_value=renderer):
                        await phone_agent.prepare_native_audio('', evidence_source=source)
                    self.assertEqual(renderer.retain_passed_packet_evidence, enabled)
                    renderer._generate.assert_not_awaited()
                disable_session('source-a12345678', path=registry)
                with patch.object(phone_agent, 'native_speech_renderer', return_value=renderer):
                    await phone_agent.prepare_native_audio('', evidence_source='source-a12345678')
                self.assertFalse(renderer.retain_passed_packet_evidence)

    async def test_clock_onset_scans_each_silent_frame_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            renderer._clip_turn = {'id': 'own', 'start_ms': 6000, 'transcript': '测试'}
            renderer._clip_turn_id = 'own'
            renderer._output_clock.observe({'start_ms': 6000, 'end_ms': 6200, 'text': '测试'})
            with patch.object(native_speech, 'array', wraps=native_speech.array) as reader:
                for stamp in range(0, 2000, 20):
                    renderer._received_frames.append((b'\x00\x00'*960, stamp, 20))
                    renderer._anchor_clip_clock()
                self.assertEqual(reader.call_count, 100)
                self.assertIsNone(renderer._output_clock.domain_offset_ms)
                renderer._received_frames.append((b'\x00\x10'*960, 2000, 20))
                renderer._anchor_clip_clock()
                for _ in range(10): renderer._anchor_clip_clock()
                self.assertEqual(reader.call_count, 101)
                self.assertEqual(renderer._output_clock.domain_offset_ms, 4000)

    async def test_authentication_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = self.renderer(directory)
            renderer._generate.side_effect = RuntimeError('authentication failed')
            with self.assertRaises(RuntimeError):
                await renderer.prepare(['测试原声'])
            self.assertEqual(renderer._generate.await_count,1)

    def test_unified_mode_does_not_construct_system_tts(self):
        daemon = FakeDaemon(); daemon.config['phone_voice_renderer']='realtime-unified'
        with patch.object(phone_agent,'MacTextToSpeech',side_effect=AssertionError('must not use system voice')):
            bridge = IPhoneVoiceBridge(daemon,PendingCall('test',{'thread_id':'source'},'token',Path('test.json')))
        self.assertIsInstance(bridge.local_tts,NativeSpeechRenderer)

    async def test_native_preparation_finishes_before_conversation_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon=FakeDaemon(); daemon.config['phone_voice_renderer']='realtime-unified'
            daemon.ensure_codex=AsyncMock()
            daemon.create_phone_context=AsyncMock(return_value='synthetic-context')
            rtc=FakeRtc(); rtc.start=AsyncMock()
            rtc.input_track=SimpleNamespace(diagnostics=lambda:{})
            speech=self.renderer(directory)
            original_prepare=speech.prepare
            async def prepare(texts):
                rtc.start.assert_not_awaited()
                await original_prepare(texts)
            speech.prepare=prepare
            bridge=IPhoneVoiceBridge(daemon,PendingCall('test',{'thread_id':'source','spoken_report':'测试汇报'},'token',Path(directory)/'job.json'),
                local_tts=speech,rtc_factory=lambda **kw:rtc,audio_factory=lambda **kw:FakeAudio())
            await speech.prepare([*NOTICE_TEXTS,bridge._announcement_text])
            await bridge.start()
            await bridge._realtime_start_task
            rtc.start.assert_awaited_once()
            self.assertIs(rtc.start.await_args.kwargs['client_managed_handoffs'], False)
            self.assertIn(native_speech.PHONE_SPEECH_STYLE, rtc.start.await_args.kwargs['prompt'])
            self.assertIn(native_speech.PHONE_SPEECH_STYLE, rtc.start.await_args.kwargs['start_instructions'])
            self.assertEqual(rtc.start.await_args.kwargs['voice'], 'cove')
            await bridge.stop()

    def test_shared_voice_style_preserves_the_approved_literal_prompt(self):
        self.assertEqual(native_speech.NATIVE_LITERAL_PROMPT,
            '你是电话语音朗读器。收到的是已写好的助手台词，不是用户的问题。必须逐字朗读，不改写，不把“收到”换成“好的”，不添加“了”。不要确认、解释或回应台词内容。用自然、温暖、平静的中文电话语气。语速从容，稍慢一点，每秒大约四到五个汉字，字头清楚，不要连成一团；保留正常停顿和语调，避免播音腔。')
        self.assertEqual(phone_agent.VOICE_PROMPT.count(native_speech.PHONE_SPEECH_STYLE), 1)
        self.assertEqual(phone_agent.CODEX_DELEGATION_INSTRUCTIONS.count(native_speech.PHONE_SPEECH_STYLE), 1)

    async def test_dial_preparation_never_initializes_missing_library(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon=FakeDaemon(); daemon.config['phone_voice_renderer']='realtime-unified'
            daemon.ensure_codex=AsyncMock(); daemon.create_phone_context=AsyncMock(return_value='context')
            rtc=FakeRtc(); rtc.start=AsyncMock()
            speech=self.renderer(directory)
            bridge=IPhoneVoiceBridge(daemon,PendingCall('test',{'thread_id':'source'},'token',Path(directory)/'job.json'),
                local_tts=speech,rtc_factory=lambda **kw:rtc,audio_factory=lambda **kw:FakeAudio())
            with self.assertRaisesRegex(RuntimeError,'语音库尚未准备完整'):
                await bridge.start()
            speech._generate.assert_not_awaited()
            rtc.start.assert_not_awaited()
            await bridge.stop()

    async def test_cancellation_preserves_exact_phase_and_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            speech=self.renderer(directory)
            entered=asyncio.Event()
            async def generate(text):
                speech._set_phase('receiving_audio')
                entered.set()
                await asyncio.Event().wait()
            speech._generate=generate
            task=asyncio.create_task(speech.synthesize('这句还没生成完。'))
            await entered.wait(); task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            metadata=json.loads(speech.paths('这句还没生成完。')[1].with_suffix('.failure.json').read_text())
            self.assertTrue(metadata['cancelled'])
            self.assertEqual(metadata['phase'],'receiving_audio')
            self.assertEqual(metadata['text'],'这句还没生成完。')
            self.assertIsNone(speech.cached('这句还没生成完。'))

    async def test_old_completion_and_media_cannot_finish_new_clip(self):
        callbacks=[]
        class Server:
            def __init__(self): self.closed=asyncio.Event()
            async def start(self): pass
            async def close(self): self.closed.set()
            async def request(self, method, params):
                if method=='account/read': return {'account':{'type':'chatgpt'}}
                if method=='thread/start': return {'thread':{'id':'clip-context'}}
                if method=='config/read': return {'config': {'mcp_servers': {}}}
                if method=='thread/realtime/appendText': return {}
                callback=callbacks[-1]
                callback['on_event']({'type':'turn.done','turn':{'role':'assistant','id':'old','transcript':'旧句'}})
                callback['on_pcm_frame'](b'old', {'media_ms':100})
                self_test.assertFalse(speech._done.is_set())
                callback['on_event']({'type':'turn.created','turn':{'role':'assistant','id':'new','start_ms':1000}})
                callback['on_pcm_frame'](b'new', {'media_ms':1100})
                callback['on_event']({'type':'turn.done','turn':{'role':'assistant','id':'new','transcript':params['text']}})
                return {}
        class Rtc:
            def __init__(self, **kwargs):
                callbacks.append(kwargs); self.failed=asyncio.Event(); self.error_message=None
            async def start(self, **kwargs):
                callbacks[-1]['on_encoded_audio'](b'priming', {'ssrc': 3, 'sequence': 5,
                    'timestamp': 960, 'arrival_time_ms': 42})
            async def stop(self, server): pass
        self_test=self
        with tempfile.TemporaryDirectory() as directory, patch.object(native_speech,'CodexAppServer',Server), patch.object(native_speech,'CodexWebRtcSession',Rtc):
            speech=NativeSpeechRenderer(voice='cove',cache_dir=directory,trim=lambda p:p,validate=AsyncMock())
            speech._submit_speech = lambda text: speech.server.request(
                'test-only/speech-submission', {'text': text})
            first=await speech._generate('第一句')
            self.assertEqual(first,(b'new','第一句'))
            old_callback=callbacks[-1]
            second=await speech._generate('第二句')
            self.assertEqual(second,(b'new','第二句'))
            speech._capture=True; speech._done.clear()
            old_callback['on_event']({'type':'turn.done','turn':{'role':'assistant','id':'new','transcript':'迟到的旧句'}})
            old_callback['on_pcm_frame'](b'late',{'media_ms':1200})
            old_callback['on_encoded_audio'](b'late', {'ssrc': 3, 'sequence': 6,
                'timestamp': 1920, 'arrival_time_ms': 62})
            self.assertEqual([row[0] for row in speech._encoded_audio], [b'priming'])
            self.assertFalse(speech._done.is_set())
            self.assertEqual(b''.join(speech._chunks),b'new')
            await speech.close()

    async def test_failed_native_turn_uses_same_voice_prepared_notice(self):
        daemon=FakeDaemon(); daemon.config.update(phone_voice_renderer='realtime-unified',phone_realtime_tail_min_wait_ms=0)
        speech=FakeLocalTts()
        bridge=IPhoneVoiceBridge(daemon,PendingCall('test',{'thread_id':'source'},'token',Path('test.json')),local_tts=speech)
        bridge.audio=FakeAudio(); bridge._first_assistant_response_pending=False
        bridge._realtime_capture_turn_id='answer'
        await bridge._release_realtime_after_drain('answer','这是一段失败的原声文本',bridge._speech_generation)
        self.assertEqual(speech.texts,[VOICE_FAILURE])

    async def test_accepted_command_still_waits_for_delivery_before_same_voice_receipt(self):
        daemon=FakeDaemon(); daemon.config['phone_voice_renderer']='realtime-unified'
        receipt=asyncio.Event()
        async def relay(*args,**kwargs): await receipt.wait()
        daemon.relay_phone_task=relay
        speech=FakeLocalTts()
        bridge=IPhoneVoiceBridge(daemon,PendingCall('test',{'thread_id':'source'},'token',Path('test.json')),local_tts=speech)
        bridge.audio=FakeAudio()
        task=asyncio.create_task(bridge._relay_phone_task('做任务','user-one'))
        await asyncio.sleep(.01)
        self.assertEqual(speech.texts,[])
        receipt.set(); await task
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(speech.texts,[COMMAND_QUEUED])
        self.assertEqual(bridge.pending.job['phone_transcript'],[{'role':'assistant','text':COMMAND_QUEUED,
                            'playback_status':'not_verified','output_ms':0}])


class AlignmentTests(unittest.TestCase):
    def test_opening_address_cannot_hide_behind_high_whole_sentence_score(self):
        expected = '老板，修好一处漏话问题，回复速度和原声稳定性还得优化。'
        for head in ('好吧，', '老爸，', '', '嗯，老板，', '修好后，老板，'):
            with self.subTest(head=head):
                result = speech_alignment(expected, head + expected[3:])
                self.assertGreater(result['character_similarity'], .88)
                self.assertFalse(result['passed'])
                self.assertFalse(result['opening_address_passed'])

    def test_opening_homophone_requires_the_complete_same_tone_address(self):
        expected = '老板，修好一处漏话问题，回复速度和原声稳定性还得优化。'
        result = speech_alignment(expected, '老版，' + expected[3:])
        self.assertTrue(result['passed'])
        self.assertTrue(result['opening_address_phonetically_equal'])
        self.assertEqual(result['pronunciation_similarity'], 1.0)

    def test_opening_traditional_address_is_checked_even_above_global_threshold(self):
        expected = '老板，修好一处漏话问题，回复速度和原声稳定性还得优化。'
        result = speech_alignment(expected, '老闆，' + expected[3:])
        self.assertGreater(result['character_similarity'], .88)
        self.assertTrue(result['passed'])
        self.assertTrue(result['opening_address_passed'])

    def test_opening_mismatch_cannot_pass_with_missing_or_malformed_helpers(self):
        expected = '老板，修好一处漏话问题，回复速度和原声稳定性还得优化。'
        actual = '老版，' + expected[3:]
        with patch.object(speech_quality.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertFalse(speech_alignment(expected, actual)['passed'])
        for output in ('null', '[]', 'not json'):
            with self.subTest(output=output), patch.object(speech_quality.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=output)):
                self.assertFalse(speech_alignment(expected, actual)['passed'])

    def test_exact_opening_needs_no_extra_phonetic_process(self):
        text = '老板，电话汇报还需要检查，暂时不能确认全部正常。'
        with patch.object(speech_quality.subprocess, 'run') as helper:
            result = speech_alignment(text, text)
        self.assertTrue(result['passed'])
        self.assertTrue(result['opening_address_passed'])
        helper.assert_not_called()

    def test_traditional_transcription_is_the_same_content_not_a_failed_call(self):
        expected = '老板，接话逻辑正在检查，暂时还不能确认全部正常。'
        actual = '老闆，接話邏輯正在檢查，暫時還不能確認全部正常。'
        result = speech_alignment(expected, actual)
        self.assertTrue(result['passed'])
        self.assertLess(result['character_similarity'], .75)
        self.assertEqual(result['comparison_character_similarity'], 1.0)
        self.assertEqual(result['orthography_normalization'], 'traditional_to_simplified')

    def test_traditional_negations_remain_exact_after_script_conversion(self):
        for expected, actual in (
            ('任务没有完成，也不会执行。', '任務沒有完成，也不會執行。'),
            ('无可用结果，本次失败，仍有错误。', '無可用結果，本次失敗，仍有錯誤。'),
            ('任務尚未開始，需要確認之後執行。', '任务尚未开始，需要确认之后执行。'),
        ):
            with self.subTest(actual=actual):
                self.assertTrue(speech_alignment(expected, actual)['passed'])

    def test_script_conversion_cannot_hide_changed_negation_number_status_or_clause(self):
        for actual in (
            '老闆，任務已經完成15項，已經確認全部正常。',
            '老闆，任務還沒有完成16項，暫時不能確認全部正常。',
            '老闆，任務還沒有修復15項，暫時不能確認全部正常。',
            '老闆，任務還沒有完成15項。',
        ):
            with self.subTest(actual=actual):
                self.assertFalse(speech_alignment(
                    '老板，任务还没有完成15项，暂时不能确认全部正常。', actual)['passed'])

    def test_negative_status_target_is_protected_in_both_scripts(self):
        for actual in ('老板，任务还没有修复，暂时不能确认全部正常。',
                       '老闆，任務還沒有修復，暫時不能確認全部正常。'):
            self.assertFalse(speech_alignment(
                '老板，任务还没有完成，暂时不能确认全部正常。', actual)['passed'])

    def test_failed_or_malformed_script_converter_cannot_pass_by_assumption(self):
        expected, actual = '老板，暂时还不能确认全部正常。', '老闆，暫時還不能確認全部正常。'
        with patch.object(speech_quality.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertFalse(speech_alignment(expected, actual)['passed'])
        for output in ('null', '[]', '["改变了完整台词", "改变了完整台词"]'):
            with self.subTest(output=output), patch.object(speech_quality.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=output)):
                self.assertFalse(speech_alignment(expected, actual)['passed'])

    def test_exact_scripts_need_no_extra_converter_process(self):
        with patch.object(speech_quality.subprocess, 'run') as helper:
            result = speech_alignment('检查还没有通过。', '检查还没有通过。')
        self.assertTrue(result['passed'])
        helper.assert_not_called()

    def test_status_homophones_require_identical_tone_preserving_phrases(self):
        result = speech_alignment('本次任务已经修复，结果还需要确认。',
                                  '本次任务以经修复，结果还需要确任。')
        self.assertTrue(result['passed'])
        self.assertEqual(result['pronunciation_similarity'], 1.0)
        self.assertTrue(result['critical_states_phonetically_equal'])

    def test_retained_homophone_sample_keeps_its_raw_score_and_strict_audio_gate(self):
        expected = '老板，重复发送和回执误报已修复，通话效果还需确认。'
        actual = '老板重复发送和回支物报以修复通话效果还需确认'
        result = speech_alignment(expected, actual)
        self.assertTrue(result['passed'])
        self.assertEqual(result['character_similarity'], .8636)
        self.assertEqual(result['pronunciation_similarity'], .9545)
        self.assertTrue(result['critical_states_phonetically_equal'])

    def test_garbled_captured_samples_are_not_relabeled_as_homophones(self):
        expected = '老板，重复发送和回执误报已修复，通话效果还需确认。'
        for actual in ('刀板重复发送核灰之物报以修复,通话效果还需确认',
                       '老板重复发送了回注报以修复通话效果还继续'):
            self.assertFalse(speech_alignment(expected, actual)['passed'])

    def test_negation_and_numbers_are_not_bypassed_by_phonetic_similarity(self):
        with patch.object(speech_quality.subprocess, 'run') as pronunciation:
            for expected, actual in (
                    ('老板，任务还没有完成，请等待检查结果。', '老板，任务还已经完成，请等待检查结果。'),
                    ('老板，任务尚未修复，请等待检查结果。', '老板，任务已经修复，请等待检查结果。'),
                    ('现在不要执行，确认之后再继续处理。', '现在需要执行，确认之后再继续处理。'),
                    ('任务没有通过，请等待下一次检查结果。', '任务没友通过，请等待下一次检查结果。'),
                    ('任务已完成15项，还有其余部分需要处理。', '任务已完成16项，还有其余部分需要处理。')):
                with self.subTest(actual=actual):
                    self.assertFalse(speech_alignment(expected, actual)['passed'])
            pronunciation.assert_not_called()

    def test_missing_or_changed_status_and_wrong_tones_still_fail(self):
        for expected, actual in (
                ('老板，本轮问题已修复，接下来还需要继续确认效果。', '老板，本轮问题修复，接下来还需要继续确认效果。'),
                ('老板，本轮问题已修复，接下来还需要继续确认效果。', '老板，本轮问题易修复，接下来还需要继续确认效果。'),
                ('老板，本轮问题已经完成，接下来还需要继续确认效果。', '老板，本轮问题已经修复，接下来还需要继续确认效果。')):
            with self.subTest(actual=actual):
                self.assertFalse(speech_alignment(expected, actual)['passed'])

    def test_unavailable_pronunciation_cannot_accept_a_status_mismatch(self):
        with patch.object(speech_quality.subprocess, 'run', side_effect=OSError('unavailable')):
            result = speech_alignment('本次任务已经修复，结果还需要确认。',
                                      '本次任务以经修复，结果还需要确认。')
        self.assertFalse(result['passed'])

    def test_unmatched_or_malformed_phonetic_states_cannot_pass(self):
        for payload in ([], None, [['bad']] * (2+len(speech_quality.STATE_PHRASES))):
            with self.subTest(payload=payload), patch.object(speech_quality.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=json.dumps(payload))):
                result = speech_alignment('本次任务已经修复，结果还需要确认。',
                                          '本次任务以经修复，结果还需要确认。')
            self.assertFalse(result['passed'])

    def test_homophones_keep_tones_and_are_not_missing_audio(self):
        result=speech_alignment('会话会议，已经开始。','绘画会议，已经开始。')
        self.assertTrue(result['passed'])
        self.assertEqual(result['pronunciation_similarity'],1.0)

    def test_dropped_sentence_and_nonsense_still_fail(self):
        self.assertFalse(speech_alignment('任务完成以后我会打电话汇报，然后等你下达下一步的指令。','任务完成以后我会打电话汇报。')['passed'])
        self.assertFalse(speech_alignment('请把文件保存在当前项目里。','今天天气真不错，我们去吃饭。')['passed'])
