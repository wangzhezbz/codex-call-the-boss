from __future__ import annotations
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from native_speech import NativeSpeechRenderer
from webrtc_bridge import CodexWebRtcSession
import native_speech


class NativeSpeechControlTests(unittest.IsolatedAsyncioTestCase):
    def peer(self):
        frames = []
        rtc = object.__new__(CodexWebRtcSession)
        rtc._stopping = False
        rtc._startup_abort = asyncio.Event()
        rtc.failed = asyncio.Event()
        rtc.started = asyncio.Event()
        rtc.started.set()
        rtc.error_message = ''
        rtc.peer = SimpleNamespace(connectionState='connected')
        rtc.events = SimpleNamespace(readyState='open', bufferedAmount=0, send=frames.append)
        rtc._server = SimpleNamespace(closed=asyncio.Event(), request=AsyncMock())
        return rtc, frames

    def test_exact_payload_on_existing_channel_without_rpc(self):
        rtc, frames = self.peer()
        result = rtc.append_speech('老板，声音还在检查。')
        self.assertEqual([json.loads(frame) for frame in frames], [{
            'type': 'session.context.append', 'channel': 'speakable',
            'content': [{'type': 'input_text', 'text': '老板，声音还在检查。'}]}])
        rtc._server.request.assert_not_called()
        self.assertEqual(result['state'], 'locally_queued')
        self.assertFalse(result['service_acknowledged'])

    def test_utf8_chunks_preserve_complete_original_without_duplication(self):
        rtc, frames = self.peer()
        text = '中文和 emoji 🍀。'*120
        result = rtc.append_speech(text)
        chunks = [json.loads(frame)['content'][0]['text'] for frame in frames]
        self.assertEqual(''.join(chunks), text)
        self.assertEqual(result['frames'], len(chunks))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk.encode()) <= 500 for chunk in chunks))
        rtc._server.request.assert_not_called()

    def test_not_ready_or_closed_never_sends(self):
        for state in ('stopped', 'aborted', 'failed', 'error', 'not_started', 'peer_closed',
                      'channel_closed', 'server_closed', 'no_server'):
            with self.subTest(state=state):
                rtc, frames = self.peer()
                if state == 'stopped': rtc._stopping = True
                elif state == 'aborted': rtc._startup_abort.set()
                elif state == 'failed': rtc.failed.set()
                elif state == 'error': rtc.error_message = 'synthetic error'
                elif state == 'not_started': rtc.started.clear()
                elif state == 'peer_closed': rtc.peer.connectionState = 'closed'
                elif state == 'channel_closed': rtc.events.readyState = 'closed'
                elif state == 'server_closed': rtc._server.closed.set()
                elif state == 'no_server': rtc._server = None
                with self.assertRaises(RuntimeError): rtc.append_speech('原话')
                self.assertEqual(frames, [])

    def test_invalid_or_unencodable_text_never_partially_sends(self):
        for text in (None, 42, '', '  ', '开头'*200+'\ud800'):
            with self.subTest(text_type=type(text).__name__):
                rtc, frames = self.peer()
                with self.assertRaises((ValueError, UnicodeError)): rtc.append_speech(text)
                self.assertEqual(frames, [])

    def test_backpressure_refuses_before_first_frame(self):
        rtc, frames = self.peer()
        rtc.events.bufferedAmount = 256*1024
        with self.assertRaisesRegex(RuntimeError, 'buffer'): rtc.append_speech('原话')
        self.assertEqual(frames, [])
        rtc._server.request.assert_not_called()

    def test_uncertain_partial_send_has_no_rpc_fallback(self):
        rtc, frames = self.peer()
        def send(frame):
            frames.append(frame)
            if len(frames) == 2: raise RuntimeError('synthetic uncertain send')
        rtc.events.send = send
        with self.assertRaisesRegex(RuntimeError, 'uncertain'): rtc.append_speech('原话'*300)
        self.assertEqual(len(frames), 2)
        rtc._server.request.assert_not_called()

    async def renderer(self):
        rtc, frames = self.peer()
        renderer = NativeSpeechRenderer(voice='cove', cache_dir=Path('/no-write-diagnostic'),
                                       trim=lambda p: p, validate=AsyncMock())
        renderer.rtc, renderer.server = rtc, rtc._server
        return renderer, rtc, frames

    async def test_real_renderer_submits_once_and_requires_own_completion(self):
        renderer, rtc, frames = await self.renderer()
        def send(frame):
            frames.append(frame)
            renderer._done.set()
        rtc.events.send = send
        await renderer._request_and_wait_for_clip('原话')
        self.assertEqual(len(frames), 1)
        self.assertEqual(renderer._speech_control['transport'], 'webrtc_data_channel')
        rtc._server.request.assert_not_called()

    async def test_local_enqueue_without_completion_times_out_without_rpc_retry(self):
        renderer, rtc, frames = await self.renderer()
        with patch.object(native_speech, 'NATIVE_SPEECH_RESPONSE_TIMEOUT_SECONDS', .02):
            with self.assertRaises(TimeoutError): await renderer._request_and_wait_for_clip('原话')
        self.assertEqual(len(frames), 1)
        rtc._server.request.assert_not_called()

    async def test_completion_cannot_hide_control_send_failure(self):
        renderer, rtc, frames = await self.renderer()
        def send(frame):
            frames.append(frame)
            renderer._done.set()
            raise RuntimeError('synthetic uncertain send')
        rtc.events.send = send
        with self.assertRaisesRegex(RuntimeError, 'uncertain'):
            await renderer._request_and_wait_for_clip('原话')
        self.assertEqual(len(frames), 1)
        rtc._server.request.assert_not_called()

    async def test_completion_cannot_hide_simultaneous_channel_failure(self):
        renderer, rtc, frames = await self.renderer()
        def send(frame):
            frames.append(frame)
            renderer._done.set()
            rtc.failed.set()
        rtc.events.send = send
        with self.assertRaises(RuntimeError): await renderer._request_and_wait_for_clip('原话')
        self.assertEqual(len(frames), 1)
        rtc._server.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
