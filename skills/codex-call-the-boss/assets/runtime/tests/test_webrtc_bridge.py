from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from array import array
from fractions import Fraction
from typing import Any
from unittest.mock import AsyncMock, patch

from aiortc import RTCPeerConnection
from aiortc.rtcrtpreceiver import RTCRtpReceiver
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

from webrtc_bridge import CodexWebRtcSession, PhoneInputTrack


def make_frame(value: int, pts: int) -> AudioFrame:
    frame = AudioFrame(format="s16", layout="mono", samples=960)
    frame.planes[0].update(bytes((value, 0)) * 960)
    frame.sample_rate = 48_000
    frame.pts = pts
    frame.time_base = Fraction(1, 48_000)
    return frame


class FakeTrack:
    def __init__(self, frames: list[AudioFrame]) -> None:
        self.frames = iter(frames)

    async def recv(self) -> AudioFrame:
        try:
            return next(self.frames)
        except StopIteration as exc:
            raise MediaStreamError from exc


class WebRtcTimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_encoded_observer_is_local_transparent_and_covers_first_packet(self):
        seen, handled = [], []
        original = RTCRtpReceiver._handle_rtp_packet
        async def handler(owner, packet, arrival_time_ms):
            handled.append((owner, packet, arrival_time_ms))
        with patch.object(RTCRtpReceiver, '_handle_rtp_packet', handler):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None,
                on_encoded_audio=lambda data, meta: seen.append((data, meta)))
        self.assertIs(RTCRtpReceiver._handle_rtp_packet, original)
        receiver = session.peer.getTransceivers()[0].receiver
        receiver._RTCRtpReceiver__codecs[111] = SimpleNamespace(mimeType='audio/opus', clockRate=48000)
        packet = SimpleNamespace(payload_type=111, payload=b'opus', ssrc=1, sequence_number=8, timestamp=960)
        try:
            await receiver._handle_rtp_packet(packet, 1234)
            self.assertEqual(handled, [(receiver, packet, 1234)])
            self.assertEqual(seen, [(b'opus', {'ssrc': 1, 'sequence': 8, 'timestamp': 960,
                'arrival_time_ms': 1234, 'codec': 'audio/opus', 'clock_rate': 48000})])
            self.assertEqual(session.encoded_audio_diagnostics,
                {'observed_packets': 1, 'observer_errors': 0})
        finally:
            await session.stop()

    async def test_broken_encoded_observer_never_drops_original_packet(self):
        handled = []
        async def handler(owner, packet, arrival_time_ms): handled.append(packet)
        def broken(*args): raise ValueError('private diagnostic error')
        with patch.object(RTCRtpReceiver, '_handle_rtp_packet', handler):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None,
                                        on_encoded_audio=broken)
        receiver = session.peer.getTransceivers()[0].receiver
        receiver._RTCRtpReceiver__codecs[111] = SimpleNamespace(mimeType='audio/opus', clockRate=48000)
        packet = SimpleNamespace(payload_type=111, payload=b'opus', ssrc=1, sequence_number=8, timestamp=960)
        try:
            await receiver._handle_rtp_packet(packet, 1234)
            self.assertEqual(handled, [packet])
            self.assertEqual(session.encoded_audio_diagnostics['observer_errors'], 1)
            self.assertFalse(session.failed.is_set())
        finally:
            await session.stop()

    async def test_stop_cancels_only_this_peers_pending_connector_before_close(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def connect(peer):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                # Cancellation occurs before ICE/peer closure, not after it.
                self.assertNotEqual(peer.connectionState, 'closed')
                cancelled.set()
        original = RTCPeerConnection._RTCPeerConnection__connect
        with patch.object(RTCPeerConnection, '_RTCPeerConnection__connect', connect):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        self.assertIs(RTCPeerConnection._RTCPeerConnection__connect, original)
        task = asyncio.create_task(session.peer._RTCPeerConnection__connect())
        await entered.wait()
        await session.stop()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(task.cancelled())
        self.assertEqual(session.peer.connectionState, 'closed')
        self.assertFalse(session._peer_connect_tasks)
        self.assertFalse(session.failed.is_set())

    async def test_connector_failure_is_observed_not_an_unretrieved_background_error(self):
        async def connect(peer):
            raise RuntimeError('synthetic connector failure')
        with patch.object(RTCPeerConnection, '_RTCPeerConnection__connect', connect):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        try:
            task = asyncio.create_task(session.peer._RTCPeerConnection__connect())
            await task
            self.assertTrue(session.failed.is_set())
            self.assertIn('connector failed', session.error_message)
            self.assertFalse(session._peer_connect_tasks)
        finally:
            await session.stop()

    async def test_late_scheduled_connector_cannot_revive_a_stopped_peer(self):
        connect = AsyncMock()
        with patch.object(RTCPeerConnection, '_RTCPeerConnection__connect', connect):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        await session.stop()
        await session.peer._RTCPeerConnection__connect()
        connect.assert_not_awaited()
        self.assertEqual(session.peer.connectionState, 'closed')

    async def test_cancelled_stop_still_closes_peer_and_detaches_handler(self):
        entered=asyncio.Event()
        async def request(*args,**kwargs):
            entered.set()
            await asyncio.sleep(.03)
        server=SimpleNamespace(request=request,remove_notification_handler=AsyncMock())
        # The notification callback is synchronous, just like CodexAppServer.
        removed=[]
        server.remove_notification_handler=removed.append
        session=CodexWebRtcSession(output_rate=48000,on_pcm=lambda _:None)
        session.thread_id='isolated'
        stopping=asyncio.create_task(session.stop(server))
        await entered.wait()
        stopping.cancel()
        await asyncio.gather(stopping,return_exceptions=True)
        self.assertEqual(session.peer.connectionState,'closed')
        self.assertEqual(len(removed),1)
        await session.stop(server)
        self.assertEqual(len(removed),1)

    async def test_inband_quota_error_fails_even_without_bridge_observer(self):
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _:None)
        try:
            session.events.emit('message', '{"type":"error","error":{"code":"rate_limit_exceeded","message":"You have reached your usage limit."}}')
            self.assertTrue(session.failed.is_set())
            self.assertEqual(session.failure_code, 'rate_limit_exceeded')
            self.assertIn('usage limit', session.readiness_error())
        finally:
            await session.stop()
    async def test_transport_readiness_requires_live_event_channel(self):
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        real_peer = session.peer
        session.peer = SimpleNamespace(connectionState='connected')
        session.events = SimpleNamespace(readyState='connecting')
        session.started.set()
        try:
            self.assertIn('event channel', session.readiness_error())
            session.events.readyState = 'open'
            self.assertIsNone(session.readiness_error())
            session.events.readyState = 'closed'
            self.assertIn('closed', session.readiness_error())
        finally:
            await real_peer.close()

    async def test_start_waits_until_event_channel_really_opens(self):
        class Server:
            def add_notification_handler(self, handler): self.handler = handler
            async def request(self, method, params, **kwargs):
                self.handler({'method':'thread/realtime/sdp', 'params':{'threadId':'thread', 'sdp':'answer'}})
                self.handler({'method':'thread/realtime/started', 'params':{'threadId':'thread'}})
                return {}
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        real_peer = session.peer
        session.peer = SimpleNamespace(connectionState='connected', setRemoteDescription=AsyncMock())
        session.events = SimpleNamespace(readyState='connecting')
        session.create_offer = AsyncMock(return_value='offer')
        session.connected.set()
        task = asyncio.create_task(session.start(server=Server(), thread_id='thread', prompt='test'))
        try:
            for _ in range(25): await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(session.start_diagnostics['phase'], 'event_channel_open')
            session.events.readyState = 'open'
            session.channel_open.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(session.start_diagnostics['phase'], 'ready')
            self.assertIn('start_request', session.start_diagnostics['completed_ms'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await real_peer.close()

    async def test_failure_releases_event_channel_wait_without_claiming_ready(self):
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        try:
            waiting = asyncio.create_task(session.channel_open.wait())
            session._fail('synthetic connection failure')
            await asyncio.wait_for(waiting, 1)
            self.assertEqual(session.readiness_error(), 'synthetic connection failure')
        finally:
            await session.stop()

    async def test_native_offer_requests_fec_without_global_codec_changes(self):
        from aiortc import RTCRtpSender
        import ctypes.util
        if not ctypes.util.find_library('opus'):
            self.skipTest('optional native mode requires libopus')
        from aiortc.codecs import CODECS
        before=repr(CODECS)
        session=CodexWebRtcSession(output_rate=48000,on_pcm=lambda _:None,recover_opus_loss=True)
        try:
            sdp=await session.create_offer()
            self.assertIn('useinbandfec=1',sdp)
            self.assertRegex(sdp, r'a=rtcp-fb:\d+ nack\r?\n')
            self.assertEqual(repr(CODECS),before)
        finally:
            await session.stop()

    async def test_audio_feedback_requires_negotiated_generic_nack(self):
        from aiortc.rtcrtpparameters import RTCRtpCodecParameters, RTCRtcpFeedback
        from aiortc.sdp import SessionDescription
        for enabled, feedback, expected in (
                (True, [], False), (False, [RTCRtcpFeedback(type='nack')], False),
                (True, [RTCRtcpFeedback(type='nack', parameter='pli')], False),
                (True, [RTCRtcpFeedback(type='nack')], True)):
            with self.subTest(enabled=enabled, expected=expected):
                session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None,
                                            recover_opus_loss=enabled, audio_retransmission_window_packets=12)
                try:
                    offer = await session.create_offer()
                    answer = SessionDescription.parse(offer)
                    for media in answer.media:
                        for codec in media.rtp.codecs:
                            codec.rtcpFeedback = feedback
                    transceiver = session.peer.getTransceivers()[0]
                    # Mimic the pinned receiver's global feedback filtering.
                    for codec in transceiver._codecs:
                        codec.rtcpFeedback = []
                    session._configure_audio_feedback(offer, str(answer))
                    self.assertEqual(session.audio_feedback_diagnostics()['nack_negotiated'], expected)
                    self.assertEqual(session.audio_jitter.prefetch, 12 if expected else 4)
                    receiver = transceiver.receiver
                    self.assertIs(receiver._RTCRtpReceiver__nack_generator, session.audio_nack)
                    before = session.audio_jitter
                    session._configure_audio_feedback(offer, str(answer))
                    self.assertIs(session.audio_jitter, before)
                finally:
                    await session.stop()

    async def test_negotiated_audio_loss_sends_feedback_and_releases_original_once(self):
        from aiortc.rtcrtpparameters import RTCRtpCodecParameters, RTCRtcpFeedback
        from aiortc.rtp import RtpPacket
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None, recover_opus_loss=True,
                                    audio_retransmission_window_packets=12)
        other = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        try:
            offer = await session.create_offer()
            transceiver = session.peer.getTransceivers()[0]
            codec = RTCRtpCodecParameters(mimeType='audio/opus', clockRate=48000, channels=2,
                payloadType=96, rtcpFeedback=[RTCRtcpFeedback(type='nack')])
            transceiver._codecs = [codec]
            session._configure_audio_feedback(offer, offer)
            receiver = transceiver.receiver
            receiver._RTCRtpReceiver__codecs[96] = codec
            receiver._send_rtcp_nack = AsyncMock()
            for seq in (0, 2, 3, 1, *range(4, 25)):
                await receiver._handle_rtp_packet(RtpPacket(payload_type=96, sequence_number=seq,
                    timestamp=seq*960, ssrc=1, payload=b'\xf8\xff\xfe'), seq * 20)
            receiver._send_rtcp_nack.assert_awaited_once_with(1, [1])
            self.assertEqual(session.audio_nack.recovered, 1)
            self.assertEqual(session.audio_jitter.missing, 0)
            self.assertEqual(session.audio_jitter.emitted, 13)
            self.assertIsNone(other.peer.getTransceivers()[0].receiver._RTCRtpReceiver__nack_generator)
            self.assertEqual(other.audio_jitter.prefetch, 4)
        finally:
            await session.stop()
            await other.stop()

    async def test_live_default_does_not_increase_buffering_when_nack_negotiates(self):
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None, recover_opus_loss=True)
        try:
            offer = await session.create_offer()
            session._configure_audio_feedback(offer, offer)
            self.assertTrue(session.audio_feedback_diagnostics()['nack_negotiated'])
            self.assertEqual(session.audio_feedback_diagnostics()['reorder_budget_ms'], 80)
        finally:
            await session.stop()

    async def test_predial_window_has_fixed_bound_and_requires_negotiation(self):
        from aiortc.sdp import SessionDescription
        for support in (True, False):
            session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None,
                recover_opus_loss=True, audio_retransmission_window_packets=32)
            try:
                offer = await session.create_offer()
                answer = SessionDescription.parse(offer)
                if not support:
                    for media in answer.media:
                        for codec in media.rtp.codecs:
                            codec.rtcpFeedback = []
                session._configure_audio_feedback(offer, str(answer))
                self.assertEqual(session.audio_jitter.prefetch, 32 if support else 4)
            finally:
                await session.stop()
        for bad in (0, 33, 64, -1, True, 12.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None,
                                  audio_retransmission_window_packets=bad)

    async def test_midcall_closed_notification_sets_failure(self) -> None:
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        session.thread_id = 'source'
        try:
            session._on_codex_notification({'method':'thread/realtime/closed', 'params':{'threadId':'source'}})
            self.assertTrue(session.failed.is_set())
            self.assertIn('closed', session.error_message)
        finally:
            await session.stop()

    async def test_bad_audio_frame_sets_failure_instead_of_silently_dying(self) -> None:
        class BrokenTrack:
            async def recv(self): raise ValueError('bad frame')
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        try:
            await session._consume_audio(BrokenTrack())
            self.assertTrue(session.failed.is_set())
            self.assertIn('bad frame', session.error_message)
        finally:
            await session.stop()

    async def test_intentional_stop_is_not_a_transport_failure(self) -> None:
        session = CodexWebRtcSession(output_rate=48000, on_pcm=lambda _: None)
        await session.stop()
        self.assertFalse(session.failed.is_set())

    async def test_restores_media_timestamp_gap_as_silence(self) -> None:
        chunks: list[bytes] = []
        session = CodexWebRtcSession(
            output_rate=48_000,
            on_pcm=chunks.append,
            preserve_timeline=True,
        )
        track = FakeTrack(
            [
                make_frame(1, 0),
                # The first frame ends at 20ms; the next begins at 40ms.
                make_frame(2, 1_920),
            ]
        )

        try:
            await session._consume_audio(track)  # type: ignore[arg-type]
        finally:
            await session.peer.close()

        audio = b"".join(chunks)
        self.assertEqual(len(audio), 5_760)
        self.assertEqual(audio[:1_920], bytes((1, 0)) * 960)
        self.assertEqual(audio[1_920:3_840], b"\x00" * 1_920)
        self.assertEqual(audio[3_840:], bytes((2, 0)) * 960)

    async def test_default_does_not_change_plivo_timing(self) -> None:
        chunks: list[bytes] = []
        session = CodexWebRtcSession(output_rate=48_000, on_pcm=chunks.append)
        track = FakeTrack([make_frame(1, 0), make_frame(2, 1_920)])

        try:
            await session._consume_audio(track)  # type: ignore[arg-type]
        finally:
            await session.peer.close()

        self.assertEqual(len(b"".join(chunks)), 3_840)

    async def test_rejects_text_output_over_webrtc_before_start(self) -> None:
        session = CodexWebRtcSession(output_rate=48_000, on_pcm=lambda _: None)
        try:
            with self.assertRaisesRegex(ValueError, "not available over WebRTC"):
                await session.start(  # type: ignore[arg-type]
                    server=None,
                    thread_id="thread",
                    prompt="brief",
                    output_modality="text",
                )
        finally:
            await session.peer.close()

    async def test_sends_compact_v3_context_and_disables_ack_filler(self) -> None:
        requests: list[tuple[str, dict[str, Any]]] = []

        class FakeServer:
            def add_notification_handler(self, handler: Any) -> None:
                self.handler = handler

            async def request(
                self, method: str, params: dict[str, Any], **kwargs: Any
            ) -> dict[str, Any]:
                del kwargs
                requests.append((method, params))
                self.handler(
                    {
                        "method": "thread/realtime/sdp",
                        "params": {"threadId": "thread", "sdp": "answer"},
                    }
                )
                self.handler(
                    {
                        "method": "thread/realtime/started",
                        "params": {"threadId": "thread"},
                    }
                )
                return {}

        class FakePeer:
            connectionState = "connected"

            async def setRemoteDescription(self, description: Any) -> None:
                self.description = description

        session = CodexWebRtcSession(output_rate=48_000, on_pcm=lambda _: None)
        real_peer = session.peer
        session.peer = FakePeer()  # type: ignore[assignment]
        session.create_offer = AsyncMock(return_value="offer")  # type: ignore[method-assign]
        session.connected.set()
        session.events = SimpleNamespace(readyState='open')
        try:
            await session.start(
                server=FakeServer(),  # type: ignore[arg-type]
                thread_id="thread",
                prompt="brief",
                include_startup_context=False,
                initial_items=[{"role": "developer", "text": "current task"}],
                delegation_ack_filler=False,
            )
        finally:
            await real_peer.close()

        params = requests[0][1]
        self.assertIs(params["includeStartupContext"], False)
        self.assertIs(params["delegationAckFiller"], False)
        self.assertEqual(
            params["initialItems"],
            [{"role": "developer", "text": "current task"}],
        )

    async def test_timeline_gap_can_be_handled_by_playout_buffer(self) -> None:
        chunks: list[bytes] = []
        gaps: list[int] = []

        def handle_gap(samples: int) -> bool:
            gaps.append(samples)
            return True

        session = CodexWebRtcSession(
            output_rate=48_000,
            on_pcm=chunks.append,
            on_timeline_gap=handle_gap,
            preserve_timeline=True,
        )
        track = FakeTrack([make_frame(1, 0), make_frame(2, 1_920)])

        try:
            await session._consume_audio(track)  # type: ignore[arg-type]
        finally:
            await session.peer.close()

        self.assertEqual(gaps, [960])
        self.assertEqual(len(b"".join(chunks)), 3_840)


class PhoneInputTrackTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_input_prebuffer_before_forwarding_speech(self) -> None:
        track = PhoneInputTrack(prebuffer_ms=40)
        first = bytes((1, 0)) * 960
        second = bytes((2, 0)) * 960
        track.push_pcm48k(first)

        waiting = await track.recv()
        self.assertEqual(bytes(waiting.planes[0])[:1920], b"\x00" * 1920)

        track.push_pcm48k(second)
        released = await track.recv()
        self.assertEqual(bytes(released.planes[0])[:1920], first)

    async def test_input_underflow_rebuffers_instead_of_repeating_tail(self) -> None:
        track = PhoneInputTrack(prebuffer_ms=0, rebuffer_ms=40)
        first = bytes((3, 0)) * 960
        track.push_pcm48k(first)
        self.assertEqual(bytes((await track.recv()).planes[0])[:1920], first)

        underflow = await track.recv()
        self.assertEqual(bytes(underflow.planes[0])[:1920], b"\x00" * 1920)
        self.assertEqual(track.underrun_count, 1)

        second = bytes((4, 0)) * 960
        third = bytes((5, 0)) * 960
        track.push_pcm48k(second + third)
        recovered = await track.recv()
        self.assertEqual(bytes(recovered.planes[0])[:1920], second)

    async def test_input_clock_control_absorbs_slow_drift(self) -> None:
        track = PhoneInputTrack(
            prebuffer_ms=100,
            adaptive_rate_percent=0.5,
        )
        buffered_samples = 7_680
        track.push_pcm48k(array("h", range(buffered_samples)).tobytes())

        frame = await track.recv()

        self.assertEqual(frame.samples, 960)
        self.assertEqual(track.adaptive_speedup_count, 1)
        self.assertEqual(len(track._buffer) // 2, buffered_samples - 965)


if __name__ == "__main__":
    unittest.main()
