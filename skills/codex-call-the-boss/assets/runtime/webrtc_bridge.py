from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from copy import deepcopy
from collections.abc import Callable
from fractions import Fraction
from typing import Any

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import SessionDescription
from aiortc.rtcrtpparameters import RTCRtcpFeedback
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from av.audio.resampler import AudioResampler

from audio_codec import resample_pcm16
from audio_jitter import AudioPacketJitterBuffer, AudioPacketNackGenerator
from opus_recovery import install_recovery_decoder
from codex_rpc import CodexAppServer
from socks_media import install_socks_media


PcmCallback = Callable[[bytes], Any]
EventCallback = Callable[[dict[str, Any]], Any]
TimelineGapCallback = Callable[[int], Any]
REALTIME_STARTUP_TIMEOUT_SECONDS = 45.0


class PhoneInputTrack(MediaStreamTrack):
    kind = "audio"
    sample_rate = 48000
    frame_samples = 960

    def __init__(
        self,
        *,
        prebuffer_ms: int = 0,
        rebuffer_ms: int = 80,
        adaptive_rate_percent: float = 0.25,
    ) -> None:
        super().__init__()
        self._buffer = bytearray()
        self._pts = 0
        self._started_at: float | None = None
        self.prebuffer_ms = max(0, int(prebuffer_ms))
        self.prebuffer_bytes = self.sample_rate * 2 * self.prebuffer_ms // 1000
        self.rebuffer_ms = max(20, int(rebuffer_ms))
        self.rebuffer_bytes = self.sample_rate * 2 * self.rebuffer_ms // 1000
        self.adaptive_rate_limit = min(
            0.02, max(0.0, float(adaptive_rate_percent) / 100.0)
        )
        self._ready = self.prebuffer_bytes == 0
        self._recovering = False
        self.underrun_count = 0
        self.inserted_silence_samples = 0
        self.adaptive_speedup_count = 0
        self.adaptive_slowdown_count = 0
        self.max_buffered_samples = 0

    def push_pcm48k(self, payload: bytes) -> None:
        if len(payload) % 2:
            payload = payload[:-1]
        self._buffer.extend(payload)
        self.max_buffered_samples = max(
            self.max_buffered_samples, len(self._buffer) // 2
        )

    async def recv(self) -> AudioFrame:
        if self._started_at is None:
            self._started_at = time.monotonic()
        target = self._started_at + self._pts / self.sample_rate
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        needed = self.frame_samples * 2
        if not self._ready:
            target = self.rebuffer_bytes if self._recovering else self.prebuffer_bytes
            if len(self._buffer) >= max(needed, target):
                self._ready = True
                self._recovering = False

        if self._ready:
            available_samples = len(self._buffer) // 2
            consume_samples = self._adaptive_consume_samples(available_samples)
        else:
            available_samples = 0
            consume_samples = self.frame_samples

        if self._ready and available_samples >= consume_samples:
            consume_bytes = consume_samples * 2
            source = bytes(self._buffer[:consume_bytes])
            del self._buffer[:consume_bytes]
            payload = resample_pcm16(source, self.frame_samples)
            if consume_samples > self.frame_samples:
                self.adaptive_speedup_count += 1
            elif consume_samples < self.frame_samples:
                self.adaptive_slowdown_count += 1
        elif self._ready:
            # Do not keep a half-frame and replay it late.  Send it once,
            # complete this 20ms packet with silence, then rebuild a short
            # cushion before resuming speech recognition input.
            payload = bytes(self._buffer)
            self._buffer.clear()
            missing = self.frame_samples - len(payload) // 2
            payload += b"\x00" * (missing * 2)
            self.inserted_silence_samples += missing
            self.underrun_count += 1
            self._ready = False
            self._recovering = True
        else:
            payload = b"\x00" * needed

        frame = AudioFrame(format="s16", layout="mono", samples=self.frame_samples)
        frame.planes[0].update(payload)
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.sample_rate)
        self._pts += self.frame_samples
        return frame

    def _adaptive_consume_samples(self, available_samples: int) -> int:
        if not self.adaptive_rate_limit or self.prebuffer_bytes <= 0:
            return self.frame_samples
        target_samples = max(self.frame_samples, self.prebuffer_bytes // 2)
        deadband = max(self.frame_samples * 2, target_samples // 4)
        adjustment = max(1, round(self.frame_samples * self.adaptive_rate_limit))
        if available_samples > target_samples + deadband:
            return self.frame_samples + adjustment
        if (
            available_samples < target_samples - deadband
            and available_samples >= self.frame_samples - adjustment
        ):
            return self.frame_samples - adjustment
        return self.frame_samples

    def diagnostics(self) -> dict[str, int]:
        return {
            "input_underrun_count": self.underrun_count,
            "inserted_silence_ms": round(
                self.inserted_silence_samples * 1000 / self.sample_rate
            ),
            "prebuffer_ms": self.prebuffer_ms,
            "rebuffer_ms": self.rebuffer_ms,
            "adaptive_speedup_count": self.adaptive_speedup_count,
            "adaptive_slowdown_count": self.adaptive_slowdown_count,
            "max_buffered_ms": round(
                self.max_buffered_samples * 1000 / self.sample_rate
            ),
            "buffered_at_stop_ms": round(
                (len(self._buffer) // 2) * 1000 / self.sample_rate
            ),
        }


class CodexWebRtcSession:
    def __init__(
        self,
        output_rate: int,
        on_pcm: PcmCallback,
        on_event: EventCallback | None = None,
        on_timeline_gap: TimelineGapCallback | None = None,
        *,
        preserve_timeline: bool = False,
        input_prebuffer_ms: int = 0,
        input_rebuffer_ms: int = 80,
        recover_opus_loss: bool = False,
        on_pcm_frame: Any = None,
        media_proxy: tuple[str, int] | None = None,
        on_encoded_audio: Any = None,
        audio_retransmission_window_packets: int = 4,
    ) -> None:
        if (type(audio_retransmission_window_packets) is not int
                or not 1 <= audio_retransmission_window_packets <= 32):
            raise ValueError('invalid audio retransmission window')
        self.audio_retransmission_window_packets = audio_retransmission_window_packets
        self.output_rate = output_rate
        self.on_pcm = on_pcm
        self.on_pcm_frame = on_pcm_frame
        self.encoded_audio_diagnostics = {'observed_packets': 0, 'observer_errors': 0}
        self.receive_trace = deque(maxlen=12000)
        self.on_event = on_event
        self.on_timeline_gap = on_timeline_gap
        self.preserve_timeline = preserve_timeline
        self.recover_opus_loss = recover_opus_loss
        self.peer = RTCPeerConnection()
        self.input_track = PhoneInputTrack(
            prebuffer_ms=input_prebuffer_ms,
            rebuffer_ms=input_rebuffer_ms,
        )
        self.peer.addTrack(self.input_track)
        # aiortc is pinned and exposes no public jitter-buffer option. Adapt
        # only this peer's audio receiver, never the module/global class. This
        # interface is covered by RTP replay and actual negotiation tests.
        self.audio_jitter = AudioPacketJitterBuffer()
        self.audio_nack = None
        self._audio_feedback_configured = False
        self.opus_recovery_diagnostics = {'enabled':recover_opus_loss}
        for transceiver in self.peer.getTransceivers():
            if transceiver.kind == 'audio':
                receiver = transceiver.receiver
                if not hasattr(receiver, '_RTCRtpReceiver__jitter_buffer'):
                    raise RuntimeError('Unsupported aiortc audio receiver interface')
                receiver._RTCRtpReceiver__jitter_buffer = self.audio_jitter
                if on_encoded_audio is not None:
                    # Observe this receiver before its first packet (including
                    # decoder priming). Never modify packets or the decode path.
                    handler = receiver._handle_rtp_packet
                    async def observe_packet(packet, arrival_time_ms, *,
                                             original=handler, owner=receiver):
                        try:
                            codec = owner._RTCRtpReceiver__codecs.get(packet.payload_type)
                            if codec and codec.mimeType.casefold() == 'audio/opus':
                                on_encoded_audio(bytes(packet.payload), {
                                    'ssrc': packet.ssrc, 'sequence': packet.sequence_number,
                                    'timestamp': packet.timestamp, 'arrival_time_ms': arrival_time_ms,
                                    'codec': 'audio/opus', 'clock_rate': codec.clockRate})
                                self.encoded_audio_diagnostics['observed_packets'] += 1
                        except Exception:
                            # Evidence failure must not turn healthy voice into
                            # lost packets; record only a content-free counter.
                            self.encoded_audio_diagnostics['observer_errors'] += 1
                        await original(packet, arrival_time_ms)
                    receiver._handle_rtp_packet = observe_packet
                if recover_opus_loss:
                    install_recovery_decoder(receiver,self._fail,self.opus_recovery_diagnostics)
        self.events = self.peer.createDataChannel("oai-events")
        self.media_route_diagnostics = {'route': 'direct_udp'}
        if media_proxy is not None:
            transports = [t.receiver.transport.transport for t in self.peer.getTransceivers()]
            transports.append(self.peer.sctp.transport.transport)
            connections = {id(t._connection):t._connection for t in transports}
            for connection in connections.values():
                install_socks_media(connection, media_proxy, self.media_route_diagnostics)
        self.answer_sdp: asyncio.Future[str] | None = None
        self.started = asyncio.Event()
        self.connected = asyncio.Event()
        self.channel_open = asyncio.Event()
        self.failed = asyncio.Event()
        self._startup_abort = asyncio.Event()
        self.start_diagnostics: dict[str, Any] = {}
        self._stopping = False
        self._stop_task = None
        self.error_message: str | None = None
        self.failure_code = ''
        self.thread_id = ""
        self._server: CodexAppServer | None = None
        self._consume_tasks: list[asyncio.Task[None]] = []
        self._peer_connect_tasks: set[asyncio.Task] = set()
        # Pinned aiortc schedules __connect without retaining its tasks. Its
        # close() can therefore close ICE while an old connector is still
        # moving on to SCTP. Own only this peer's connector, just as we adapt
        # only this peer's receiver; never patch the library/global class.
        connect = getattr(self.peer, '_RTCPeerConnection__connect', None)
        if not callable(connect):
            raise RuntimeError('Unsupported aiortc connection lifecycle interface')

        async def managed_connect():
            task = asyncio.current_task()
            if self._stopping:
                return
            self._peer_connect_tasks.add(task)
            try:
                await connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping:
                    self._fail(f'WebRTC connector failed: {type(exc).__name__}')
            finally:
                self._peer_connect_tasks.discard(task)

        self.peer._RTCPeerConnection__connect = managed_connect

        @self.events.on("message")
        def on_message(message: str | bytes) -> None:
            try:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                event = json.loads(message)
                if not isinstance(event, dict):
                    return
                if event.get('type') == 'error':
                    error = event.get('error') or {}
                    self.failure_code = str(error.get('code') or error.get('type') or 'realtime_error')
                    self._fail(str(error.get('message') or self.failure_code))
                if self.on_event is not None:
                    result = self.on_event(event)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return

        @self.events.on("close")
        def on_events_closed() -> None:
            if not self._stopping:
                self._fail("Codex Realtime event channel closed")

        @self.events.on("open")
        def on_events_opened() -> None:
            self.channel_open.set()

        @self.peer.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._consume_tasks.append(asyncio.create_task(self._consume_audio(track)))

        @self.peer.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            if self.peer.connectionState in {"connected", "completed"}:
                self.connected.set()
            elif self.peer.connectionState in {"failed", "closed"}:
                if not self._stopping:
                    self._fail(f"WebRTC connection {self.peer.connectionState}")

    async def create_offer(self) -> str:
        offer = await self.peer.createOffer()
        if self.recover_opus_loss:
            description = SessionDescription.parse(offer.sdp)
            for media in description.media:
                if media.kind == 'audio':
                    for codec in media.rtp.codecs:
                        if codec.mimeType.casefold() == 'audio/opus':
                            # Advertise receive support. Calling decode_fec
                            # alone cannot request redundancy from the sender.
                            codec.parameters['useinbandfec'] = 1
                            codec.rtcpFeedback.append(RTCRtcpFeedback(type='nack'))
            # Audio codecs in this pinned version still reference the global
            # catalog. Copy them before adding per-peer negotiated feedback.
            for transceiver in self.peer.getTransceivers():
                if transceiver.kind == 'audio':
                    transceiver._codecs = deepcopy(transceiver._codecs)
                    for codec in transceiver._codecs:
                        if (codec.mimeType.casefold() == 'audio/opus'
                                and not any(f.type == 'nack' and not f.parameter for f in codec.rtcpFeedback)):
                            codec.rtcpFeedback.append(RTCRtcpFeedback(type='nack'))
            offer = RTCSessionDescription(sdp=str(description),type=offer.type)
        await self.peer.setLocalDescription(offer)
        if self.peer.localDescription is None:
            raise RuntimeError("WebRTC 未生成本地 SDP")
        return self.peer.localDescription.sdp

    def _configure_audio_feedback(self, offer_sdp: str, answer_sdp: str) -> None:
        if self._audio_feedback_configured or not self.recover_opus_loss:
            return
        self._audio_feedback_configured = True
        offer = SessionDescription.parse(offer_sdp)
        answer = SessionDescription.parse(answer_sdp)
        def supports(description, mid, payload_type):
            return any(media.kind == 'audio' and media.rtp.muxId == mid
                and any(c.payloadType == payload_type and c.mimeType.casefold() == 'audio/opus'
                    and any(f.type == 'nack' and not f.parameter for f in c.rtcpFeedback)
                    for c in media.rtp.codecs) for media in description.media)
        for transceiver in self.peer.getTransceivers():
            if transceiver.kind != 'audio':
                continue
            # aiortc intersects RTCP feedback with its global codec catalog,
            # whose audio table omits NACK. Use the actual accepted offer and
            # answer for this selected codec instead of changing that catalog.
            negotiated = any(c.mimeType.casefold() == 'audio/opus' and c.clockRate == 48000
                and supports(offer, transceiver.mid, c.payloadType)
                and supports(answer, transceiver.mid, c.payloadType) for c in transceiver._codecs)
            if not negotiated:
                continue  # No unsolicited feedback or extra buffering.
            receiver = transceiver.receiver
            if not hasattr(receiver, '_RTCRtpReceiver__nack_generator'):
                raise RuntimeError('Unsupported aiortc audio feedback interface')
            # Live conversation retains its original 80 ms budget. Only the
            # pre-dial literal renderer opts into 640 ms to await originals;
            # there is no added pickup/Q&A wait or adaptive growing latency.
            # FEC/PLC stays the fallback when an original misses this budget.
            window = self.audio_retransmission_window_packets
            self.audio_jitter = AudioPacketJitterBuffer(prefetch=window)
            self.audio_nack = AudioPacketNackGenerator(window=window)
            receiver._RTCRtpReceiver__jitter_buffer = self.audio_jitter
            receiver._RTCRtpReceiver__nack_generator = self.audio_nack

    def audio_feedback_diagnostics(self) -> dict[str, Any]:
        return {'nack_negotiated': self.audio_nack is not None,
                'reorder_budget_ms': self.audio_jitter.prefetch * 20,
                **(self.audio_nack.diagnostics() if self.audio_nack else {})}

    async def start(
        self,
        server: CodexAppServer,
        thread_id: str,
        prompt: str,
        start_instructions: str | None = None,
        voice: str = "juniper",
        output_modality: str = "audio",
        include_startup_context: bool = True,
        initial_items: list[dict[str, str]] | None = None,
        delegation_ack_filler: bool | None = None,
        client_managed_handoffs: bool | None = None,
    ) -> None:
        if output_modality != "audio":
            raise ValueError(
                "Codex Realtime text output is not available over WebRTC"
            )
        self.thread_id = thread_id
        self._server = server
        loop = asyncio.get_running_loop()
        self.answer_sdp = loop.create_future()
        server.add_notification_handler(self._on_codex_notification)
        began = time.monotonic()
        self.start_diagnostics = {"phase": "create_offer", "completed_ms": {},
                                  "timeout_seconds": REALTIME_STARTUP_TIMEOUT_SECONDS,
                                  "outcome": "pending"}
        server_closed = getattr(server, 'closed', None)

        def ensure_running() -> None:
            if self._stopping or self._startup_abort.is_set():
                raise RuntimeError('Codex Realtime is stopping' if self._stopping
                                   else 'Codex Realtime startup cancelled')
            if self.failed.is_set():
                raise RuntimeError(self.error_message or 'Codex Realtime startup failed')
            if server_closed is not None and server_closed.is_set():
                raise RuntimeError('Codex app-server closed during Realtime startup')

        def completed(phase: str, next_phase: str) -> None:
            # A successful RPC or SDP and a terminal event can arrive together.
            # Failure wins before progressing or publishing a ready state.
            ensure_running()
            self.start_diagnostics["completed_ms"][phase] = round((time.monotonic() - began) * 1000)
            self.start_diagnostics["phase"] = next_phase

        async def connect() -> None:
            ensure_running()
            offer_sdp = await self.create_offer()
            completed("create_offer", "start_request")
            params: dict[str, Any] = {
                "threadId": thread_id,
                "outputModality": output_modality,
                "version": "v3",
                "voice": voice,
                "includeStartupContext": include_startup_context,
                "prompt": prompt,
                "realtimeStartInstructions": start_instructions,
                "transport": {"type": "webrtc", "sdp": offer_sdp},
            }
            if initial_items:
                params["initialItems"] = initial_items
            if delegation_ack_filler is not None:
                params["delegationAckFiller"] = delegation_ack_filler
            if client_managed_handoffs is not None:
                params['clientManagedHandoffs'] = client_managed_handoffs
            await server.request("thread/realtime/start", params,
                                 timeout=REALTIME_STARTUP_TIMEOUT_SECONDS)
            completed("start_request", "answer_sdp")
            answer_sdp = await self.answer_sdp
            completed("answer_sdp", "remote_description")
            await self.peer.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp, type="answer")
            )
            self._configure_audio_feedback(offer_sdp, answer_sdp)
            completed("remote_description", "started_event")
            await self.started.wait()
            completed("started_event", "peer_connected")
            await self.connected.wait()
            completed("peer_connected", "event_channel_open")
            if self.events.readyState == "open":
                self.channel_open.set()
            await self.channel_open.wait()
            completed("event_channel_open", "readiness_check")
            error = self.readiness_error()
            if error:
                raise RuntimeError(error)
            ensure_running()

        startup = asyncio.create_task(connect())
        waiters = [startup, asyncio.create_task(self.failed.wait()),
                   asyncio.create_task(self._startup_abort.wait())]
        if server_closed is not None:
            waiters.append(asyncio.create_task(server_closed.wait()))
        try:
            # One budget includes offer, request acknowledgement, SDP, peer and
            # data channel. The enclosing phone preparation can still impose
            # its shorter original budget. Never restart or retry a request.
            async with asyncio.timeout(REALTIME_STARTUP_TIMEOUT_SECONDS):
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                ensure_running()
                await startup
                completed("readiness_check", "ready")
                self.start_diagnostics['outcome'] = 'ready'
        except TimeoutError as exc:
            self.start_diagnostics.update(outcome='timeout',
                failure_phase=self.start_diagnostics['phase'])
            self._fail('Codex WebRTC 语音连接超时')
            raise RuntimeError(self.error_message or "Codex WebRTC 语音连接超时") from exc
        except asyncio.CancelledError:
            self.start_diagnostics.update(outcome='cancelled',
                failure_phase=self.start_diagnostics['phase'])
            self._startup_abort.set()
            raise
        except Exception as exc:
            self.start_diagnostics.update(outcome='stopped' if self._stopping else 'failed',
                failure_phase=self.start_diagnostics['phase'])
            if not self._stopping:
                self._fail(str(exc))
            raise
        finally:
            self.start_diagnostics['elapsed_ms'] = round((time.monotonic() - began) * 1000)
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            # A terminal event may set the SDP exception before the startup
            # task ever reaches that future. Do not leak an unobserved error.
            if not self.answer_sdp.done():
                self.answer_sdp.cancel()
            elif not self.answer_sdp.cancelled():
                self.answer_sdp.exception()

    def readiness_error(self) -> str | None:
        """Check current transport state, not a stale successful-start flag."""
        if self._stopping:
            return "Codex Realtime is stopping"
        if self._startup_abort.is_set():
            return "Codex Realtime startup cancelled"
        if self.error_message:
            return self.error_message
        if not self.started.is_set():
            return "Codex Realtime startup not confirmed"
        if self.peer.connectionState not in {"connected", "completed"}:
            return f"WebRTC connection not ready: {self.peer.connectionState}"
        if self.events.readyState != "open":
            return f"Codex Realtime event channel not ready: {self.events.readyState}"
        return None

    def append_speech(self, text: str) -> dict[str, Any]:
        """Queue literal speech on this authenticated V3 peer, exactly once.

        This is the v0.153.4 StandaloneSpeech wire payload. The app-server RPC
        otherwise queues it for a separate sideband socket which can lag the
        already-ready peer. Do not also submit that RPC or retry an uncertain
        send. Enqueue is not service acknowledgement or completed speech.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Literal speech must be nonempty text')
        error = self.readiness_error()
        if error or self.failed.is_set():
            raise RuntimeError(error or 'Codex Realtime failed before speech enqueue')
        if self._server is None or self._server.closed.is_set():
            raise RuntimeError('Codex app-server closed before speech enqueue')
        # Match upstream's 500-byte UTF-8 chunks, preserving every character.
        # Build all frames before sending; never retry a partly queued clip.
        encoded = text.encode('utf-8')
        frames = []
        start = 0
        while start < len(encoded):
            end = min(start + 500, len(encoded))
            while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
                end -= 1
            chunk = encoded[start:end].decode('utf-8')
            frames.append(json.dumps({'type': 'session.context.append',
                'channel': 'speakable', 'content': [{'type': 'input_text', 'text': chunk}]},
                ensure_ascii=False))
            start = end
        if self.events.bufferedAmount + sum(len(frame.encode()) for frame in frames) > 256 * 1024:
            raise RuntimeError('Codex Realtime control send buffer is full')
        for frame in frames:
            self.events.send(frame)
        return {'transport': 'webrtc_data_channel', 'state': 'locally_queued',
                'frames': len(frames), 'service_acknowledged': False}

    async def stop(self, server: CodexAppServer | None = None) -> None:
        self._stopping = True
        self._startup_abort.set()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._close_transport(server))
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            # Cancellation of an enclosing call/harness must not abandon an
            # aiortc close midway and leave its peer/media tasks running.
            await self._stop_task
            raise

    async def _close_transport(self, server: CodexAppServer | None = None) -> None:
        server = server or self._server
        try:
            connectors = tuple(self._peer_connect_tasks)
            for task in connectors:
                task.cancel()
            await asyncio.gather(*connectors, return_exceptions=True)
            if server is not None and self.thread_id:
                try:
                    await server.request(
                        "thread/realtime/stop", {"threadId": self.thread_id}, timeout=10
                    )
                except Exception:
                    pass
        finally:
            for task in self._consume_tasks:
                task.cancel()
            await asyncio.gather(*self._consume_tasks, return_exceptions=True)
            await self.peer.close()
            if server is not None:
                server.remove_notification_handler(self._on_codex_notification)

    def _on_codex_notification(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        if params.get("threadId") != self.thread_id:
            return
        method = message.get("method")
        if method == "thread/realtime/sdp" and self.answer_sdp is not None:
            if not self.answer_sdp.done():
                self.answer_sdp.set_result(str(params.get("sdp") or ""))
        elif method == "thread/realtime/started":
            self.started.set()
        elif method == "thread/realtime/error":
            self._fail(str(params.get("message") or "Codex 语音错误"))
        elif method == "thread/realtime/closed" and not self._stopping:
            self._fail("Codex Realtime session closed")

    def _fail(self, message: str) -> None:
        if self._stopping or self.failed.is_set():
            return
        self.error_message = message
        self.failed.set()
        self.started.set()
        self.connected.set()
        self.channel_open.set()
        if self.answer_sdp is not None and not self.answer_sdp.done():
            self.answer_sdp.set_exception(RuntimeError(message))

    async def _consume_audio(self, track: MediaStreamTrack) -> None:
        try:
            await self._read_audio(track)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(f"Realtime audio reader failed: {type(exc).__name__}: {exc}")

    async def _read_audio(self, track: MediaStreamTrack) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=self.output_rate)
        previous_end: Fraction | None = None
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                if not self._stopping:
                    self._fail("Realtime audio track ended")
                return
            if self.preserve_timeline:
                previous_end = await self._restore_timeline_gap(frame, previous_end)
            for converted in resampler.resample(frame):
                length = converted.samples * 2
                payload = bytes(converted.planes[0])[:length]
                media_ms = (float(frame.pts * frame.time_base) * 1000
                            if frame.pts is not None and frame.time_base is not None else None)
                metadata = {'media_ms': media_ms, 'arrival': time.monotonic(),
                            'duration_ms': converted.samples * 1000 / self.output_rate}
                self.receive_trace.append({**metadata, 'nonzero': any(payload)})
                if self.on_pcm_frame is not None:
                    result = self.on_pcm_frame(payload, metadata)
                    if asyncio.iscoroutine(result):
                        await result
                else:
                    await self._emit_pcm(payload)

    async def _restore_timeline_gap(
        self, frame: AudioFrame, previous_end: Fraction | None
    ) -> Fraction | None:
        if frame.pts is None or frame.time_base is None or not frame.sample_rate:
            return None

        frame_start = Fraction(frame.pts) * Fraction(frame.time_base)
        frame_end = frame_start + Fraction(frame.samples, frame.sample_rate)
        if previous_end is not None:
            gap = frame_start - previous_end
            # RTP delivery can arrive late even when its media timestamp is
            # continuous. Restore only a real media-timeline gap, not arrival
            # jitter. Bound corrupt timestamp jumps so they cannot enqueue an
            # unbounded amount of silence.
            if Fraction(1, 1000) < gap <= Fraction(5, 1):
                silence_samples = round(float(gap) * self.output_rate)
                handled = False
                if self.on_timeline_gap is not None:
                    result = self.on_timeline_gap(silence_samples)
                    if asyncio.iscoroutine(result):
                        result = await result
                    handled = result is True
                if not handled:
                    await self._emit_silence(silence_samples)
        if previous_end is None:
            return frame_end
        return max(previous_end, frame_end)

    async def _emit_silence(self, samples: int) -> None:
        chunk_samples = max(1, self.output_rate // 50)
        while samples > 0:
            current = min(samples, chunk_samples)
            await self._emit_pcm(b"\x00" * (current * 2))
            samples -= current

    async def _emit_pcm(self, payload: bytes) -> None:
        result = self.on_pcm(payload)
        if asyncio.iscoroutine(result):
            await result
