"""Startup liveness only: no network, audio devices, generation or dialing."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from webrtc_bridge import CodexWebRtcSession


class StartupServer:
    def __init__(self, *, answer=True, stalled_ack=False, delay=0):
        self.closed = asyncio.Event()
        self.answer = answer
        self.stalled_ack = stalled_ack
        self.delay = delay
        self.requests = []
        self.requested = asyncio.Event()
        self.request_cancelled = asyncio.Event()
        self.handlers = []

    def add_notification_handler(self, handler):
        self.handlers.append(handler)

    def remove_notification_handler(self, handler):
        self.handlers.remove(handler)

    def emit(self, method, **params):
        for handler in tuple(self.handlers):
            handler({'method': method, 'params': {'threadId': 'owned', **params}})

    async def request(self, method, params, **kwargs):
        self.requests.append(method)
        if method != 'thread/realtime/start':
            return {}
        self.requested.set()
        if self.stalled_ack:
            try:
                await asyncio.Event().wait()
            finally:
                self.request_cancelled.set()
        await asyncio.sleep(self.delay)
        if self.answer:
            self.emit('thread/realtime/sdp', sdp='answer')
            self.emit('thread/realtime/started')
        return {}


class StartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        self.real_peer = self.session.peer
        self.session.peer = SimpleNamespace(connectionState='connected',
            setRemoteDescription=AsyncMock(), close=AsyncMock())
        self.session.events = SimpleNamespace(readyState='open')
        self.session.create_offer = AsyncMock(return_value='offer')
        self.session.connected.set()
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        future = self.session.answer_sdp
        if future is not None and future.done() and not future.cancelled():
            future.exception()
        await self.real_peer.close()

    def begin(self, server):
        self.task = asyncio.create_task(self.session.start(
            server=server, thread_id='owned', prompt='test'))

    async def phase(self, name):
        async with asyncio.timeout(.5):
            while self.session.start_diagnostics.get('phase') != name:
                await asyncio.sleep(.001)

    async def test_server_close_wakes_sdp_wait_without_dial_or_retry(self):
        server = StartupServer(answer=False)
        self.begin(server)
        await self.phase('answer_sdp')
        server.closed.set()
        with self.assertRaisesRegex(RuntimeError, 'app-server closed'):
            await asyncio.wait_for(self.task, .25)
        self.session.peer.setRemoteDescription.assert_not_awaited()
        self.assertEqual(server.requests, ['thread/realtime/start'])
        self.assertEqual(self.session.start_diagnostics['outcome'], 'failed')
        self.assertEqual(self.session.start_diagnostics['failure_phase'], 'answer_sdp')

    async def test_error_wakes_pending_start_ack_and_cancels_only_owned_request(self):
        server = StartupServer(stalled_ack=True)
        self.begin(server)
        await server.requested.wait()
        server.emit('thread/realtime/error', message='synthetic quota failure')
        with self.assertRaisesRegex(RuntimeError, 'synthetic quota failure'):
            await asyncio.wait_for(self.task, .25)
        self.assertTrue(server.request_cancelled.is_set())
        self.assertEqual(server.requests, ['thread/realtime/start'])
        self.assertEqual(self.session.start_diagnostics['failure_phase'], 'start_request')

    async def test_server_close_wakes_peer_and_data_channel_waits(self):
        for phase in ('peer_connected', 'event_channel_open'):
            with self.subTest(phase=phase):
                if phase == 'peer_connected':
                    self.session.connected.clear()
                else:
                    self.session.connected.set()
                    self.session.channel_open.clear()
                    self.session.events.readyState = 'connecting'
                self.session.failed.clear()
                self.session.error_message = None
                server = StartupServer()
                self.begin(server)
                await self.phase(phase)
                server.closed.set()
                with self.assertRaisesRegex(RuntimeError, 'app-server closed'):
                    await asyncio.wait_for(self.task, .25)
                self.assertEqual(self.session.start_diagnostics['failure_phase'], phase)
                self.assertEqual(server.requests, ['thread/realtime/start'])

    async def test_sdp_success_racing_failure_never_claims_ready(self):
        server = StartupServer(answer=False)
        self.begin(server)
        await self.phase('answer_sdp')
        server.emit('thread/realtime/sdp', sdp='answer')
        server.closed.set()
        with self.assertRaisesRegex(RuntimeError, 'app-server closed'):
            await asyncio.wait_for(self.task, .25)
        self.assertNotEqual(self.session.start_diagnostics['phase'], 'ready')

    async def test_all_start_steps_share_one_original_deadline(self):
        async def offer():
            await asyncio.sleep(.03)
            return 'offer'
        async def remote(*args):
            await asyncio.sleep(.03)
        self.session.create_offer = offer
        self.session.peer.setRemoteDescription = AsyncMock(side_effect=remote)
        server = StartupServer(delay=.03)
        with patch('webrtc_bridge.REALTIME_STARTUP_TIMEOUT_SECONDS', .075, create=True):
            self.begin(server)
            with self.assertRaisesRegex(RuntimeError, '语音连接超时'):
                await asyncio.wait_for(self.task, .4)
        self.assertEqual(self.session.start_diagnostics['outcome'], 'timeout')
        self.assertEqual(self.session.start_diagnostics['failure_phase'], 'remote_description')
        self.assertEqual(server.requests, ['thread/realtime/start'])

    async def test_intentional_stop_wakes_startup_without_transport_failure(self):
        server = StartupServer(answer=False)
        self.begin(server)
        await self.phase('answer_sdp')
        await self.session.stop(server)
        with self.assertRaisesRegex(RuntimeError, 'stopping'):
            await asyncio.wait_for(self.task, .25)
        self.assertFalse(self.session.failed.is_set())
        self.assertEqual(server.requests, ['thread/realtime/start', 'thread/realtime/stop'])
        self.assertFalse(server.handlers)

    async def test_outer_cancellation_keeps_cancelled_semantics_and_no_helper_tasks(self):
        server = StartupServer(stalled_ack=True)
        before = set(asyncio.all_tasks())
        self.begin(server)
        await server.requested.wait()
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertTrue(server.request_cancelled.is_set())
        self.assertEqual(self.session.start_diagnostics['outcome'], 'cancelled')
        self.assertFalse(set(asyncio.all_tasks()) - before)
        self.assertEqual(server.requests, ['thread/realtime/start'])
