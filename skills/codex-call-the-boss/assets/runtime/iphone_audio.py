from __future__ import annotations

from playback_ledger import PlaybackLedger

import asyncio
import inspect
import json
import queue
import re
import subprocess
import threading
import time
from array import array
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from audio_codec import resample_pcm16


PcmCallback = Callable[[bytes], Any]


def confirmation_read_diagnostic(result: Any) -> dict[str, Any]:
    """Keep only allowlisted helper facts, never labels, numbers or raw stderr."""
    code = int(getattr(result, "returncode", 1))
    diagnostic: dict[str, Any] = {"exit_code": code, "result": "read_failed"}
    if code in (0, 65, 77):
        diagnostic["result"] = {0: "unique_confirmation", 65: "not_visible",
                                77: "permission_unavailable"}[code]
        return diagnostic
    detail = str(getattr(result, "stderr", "") or "").strip()
    if code == 75:
        diagnostic["result"] = "read_unavailable"
        multiple = re.fullmatch(
            r"expected exactly one Phone\.app communication-audio button, found (\d{1,4})", detail)
        if multiple and 1 < int(multiple[1]) <= 800:
            diagnostic.update(result="ambiguous", candidate_count=int(multiple[1]))
        elif detail == "Phone.app accessibility inspection timed out; no action performed":
            diagnostic["result"] = "inspection_timeout"
        else:
            failure = re.fullmatch(
                r"Phone\.app accessibility inspection unavailable \((-?\d{1,6})\); "
                r"(?:attribute=(AXRole|AXTitle|AXDescription|AXHelp|AXIdentifier|AXValue|AXChildren|actionNames); )?"
                r"no action performed", detail)
            if failure:
                diagnostic["ax_error_code"] = int(failure[1])
                if failure[2]:
                    diagnostic["attribute"] = failure[2]
    return diagnostic


class IPhoneAudioError(RuntimeError):
    pass


def _load_sounddevice() -> Any:
    try:
        import sounddevice  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise IPhoneAudioError(
            "缺少本地音频组件：请安装 sounddevice 和 PortAudio"
        ) from exc
    return sounddevice


def audio_devices(sounddevice_module: Any | None = None) -> list[dict[str, Any]]:
    sd = sounddevice_module or _load_sounddevice()
    devices: list[dict[str, Any]] = []
    for index, raw in enumerate(sd.query_devices()):
        devices.append(
            {
                "index": index,
                "name": str(raw.get("name") or ""),
                "inputs": int(raw.get("max_input_channels") or 0),
                "outputs": int(raw.get("max_output_channels") or 0),
                "sample_rate": int(float(raw.get("default_samplerate") or 0)),
            }
        )
    return devices


def resolve_audio_device(
    name: str,
    direction: str,
    sounddevice_module: Any | None = None,
) -> int:
    if direction not in {"input", "output"}:
        raise ValueError(f"无效的音频方向：{direction}")
    key = "inputs" if direction == "input" else "outputs"
    candidates = [device for device in audio_devices(sounddevice_module) if device[key] > 0]
    exact = [device for device in candidates if device["name"].casefold() == name.casefold()]
    matches = exact or [
        device for device in candidates if name.casefold() in device["name"].casefold()
    ]
    if not matches:
        available = "、".join(device["name"] for device in candidates) or "无"
        raise IPhoneAudioError(
            f"找不到{direction}音频设备“{name}”。可用设备：{available}"
        )
    if len(matches) > 1:
        found = "、".join(device["name"] for device in matches)
        raise IPhoneAudioError(f"音频设备名不唯一：{found}")
    return int(matches[0]["index"])


class InputSignalStats:
    """Numeric-only input evidence. Never retain audio or authorize speech."""

    def __init__(self):
        self.callbacks = self.samples = self.zero_callbacks = self.odd_bytes = 0
        self.peak_rms = self.above_threshold_ms = self.max_voiced_run_ms = 0.0
        self.voiced_run_ms = self.max_callback_gap_ms = 0.0
        self.last_callback_at = None
        self.threshold = None

    def observe(self, payload, *, threshold, now):
        samples = array('h')
        self.odd_bytes += len(payload) % 2
        samples.frombytes(payload[:len(payload) - len(payload) % 2])
        if not samples:
            return 0.0, 0.0
        rms = (sum(value * value for value in samples) / len(samples)) ** .5
        duration_ms = len(samples) / 48
        self.callbacks += 1
        self.samples += len(samples)
        self.zero_callbacks += rms == 0
        self.peak_rms = max(self.peak_rms, rms)
        if self.last_callback_at is not None:
            self.max_callback_gap_ms = max(self.max_callback_gap_ms,
                                          max(0, now - self.last_callback_at) * 1000)
        self.last_callback_at = now
        self.threshold = threshold
        if rms >= threshold:
            self.above_threshold_ms += duration_ms
            self.voiced_run_ms += duration_ms
            self.max_voiced_run_ms = max(self.max_voiced_run_ms, self.voiced_run_ms)
        else:
            self.voiced_run_ms = 0
        return rms, duration_ms

    def snapshot(self, *, now):
        return {
            'scope': 'accepted_phone_input_numeric_only',
            'callbacks': self.callbacks,
            'audio_ms': round(self.samples / 48, 1),
            'all_zero_callbacks': self.zero_callbacks,
            'peak_block_rms': round(self.peak_rms, 1),
            'rms_threshold': self.threshold,
            'above_threshold_ms': round(self.above_threshold_ms, 1),
            'max_contiguous_above_threshold_ms': round(self.max_voiced_run_ms, 1),
            # Arrival spacing at the event loop is not hardware packet loss.
            'max_callback_gap_ms': round(self.max_callback_gap_ms, 1),
            'last_callback_age_ms': (round(max(0, now - self.last_callback_at) * 1000, 1)
                                     if self.last_callback_at is not None else None),
            'odd_bytes': self.odd_bytes,
            'contains_audio': False,
            'proves_caller_speech': False,
        }


class IPhoneAudioPipe:
    """Routes Phone.app audio to and from the Codex WebRTC session.

    Phone.app speaker -> BlackHole 2ch -> this input stream -> Codex
    Codex -> this output stream -> BlackHole 16ch -> Phone.app microphone
    """

    def __init__(
        self,
        input_device: str,
        output_device: str,
        on_input: PcmCallback,
        *,
        sample_rate: int = 48_000,
        blocksize: int = 960,
        prebuffer_ms: int = 400,
        rebuffer_ms: int = 20,
        output_latency_ms: int = 120,
        fade_ms: int = 8,
        adaptive_rate_percent: float = 0.5,
        output_probe_dir: Path | None = None,
        output_probe_seconds: int = 60,
        sounddevice_module: Any | None = None,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.on_input = on_input
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.prebuffer_ms = max(0, prebuffer_ms)
        self.prebuffer_bytes = self.sample_rate * 2 * self.prebuffer_ms // 1000
        self.rebuffer_ms = max(20, rebuffer_ms)
        self.rebuffer_bytes = self.sample_rate * 2 * self.rebuffer_ms // 1000
        self.output_latency_ms = max(20, int(output_latency_ms))
        self.fade_frames = max(1, self.sample_rate * max(1, int(fade_ms)) // 1000)
        self.adaptive_rate_limit = min(
            0.02, max(0.0, float(adaptive_rate_percent) / 100.0)
        )
        self.sd = sounddevice_module or _load_sounddevice()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.input_stream: Any | None = None
        self.output_stream: Any | None = None
        self.output_queue: queue.Queue[bytes] = queue.Queue(maxsize=512)
        self.output_buffer = bytearray()
        self._output_lock = threading.Lock()
        self.playback_ledger = PlaybackLedger()
        self._playback_ready = False
        self._recovering = False
        self._fade_in_pending = False
        self._fade_out_pending = False
        self._last_output_sample = 0
        self._started_at: float | None = None
        self.underrun_count = 0
        self.rebuffer_count = 0
        self.underrun_times_ms: list[int] = []
        self.inserted_silence_frames = 0
        self.idle_silence_frames = 0
        self.timeline_gap_queued_frames = 0
        self.timeline_gap_compensated_frames = 0
        self._unbuffered_silence_frames = 0
        self.clear_count = 0
        self.queue_overflow_count = 0
        self.stream_underflow_count = 0
        self.stream_overflow_count = 0
        self.input_callback_count = self.input_callback_frames = 0
        self.input_stream_overflow_count = self.input_stream_underflow_count = 0
        self.adaptive_speedup_count = 0
        self.adaptive_slowdown_count = 0
        self.max_buffered_frames = 0
        self.status_messages: queue.Queue[str] = queue.Queue(maxsize=32)
        self.output_probe_dir = output_probe_dir
        self.output_probe_seconds = output_probe_seconds
        self.output_probe = None
        self.output_probe_error = ''

    def start(self) -> None:
        if self.input_stream is not None or self.output_stream is not None:
            return
        self.loop = asyncio.get_running_loop()
        self._started_at = time.monotonic()
        input_index = resolve_audio_device(self.input_device, "input", self.sd)
        output_index = resolve_audio_device(self.output_device, "output", self.sd)
        if input_index == output_index:
            raise IPhoneAudioError('电话拾音和回传不能使用同一个虚拟音频设备，否则会形成回声环路')
        try:
            self.input_stream = self.sd.RawInputStream(
                device=input_index,
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="int16",
                latency="low",
                callback=self._input_callback,
            )
            self.output_stream = self.sd.RawOutputStream(
                device=output_index,
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="int16",
                # BlackHole reports a surprisingly small "high" output
                # latency (about 10.7ms on this Mac), which is shorter than
                # our 20ms callback block.  Request a real hardware-side
                # cushion so an occasional Python/WebRTC scheduling delay
                # cannot turn into crackle on the telephone line.
                latency=self.output_latency_ms / 1000,
                callback=self._output_callback,
            )
            self.input_stream.start()
            self.output_stream.start()
        except Exception as exc:
            self.close()
            raise IPhoneAudioError(f"无法打开 iPhone 双向音频通道：{exc}") from exc
        if self.output_probe_dir is not None:
            try:
                from phone_output_probe import PhoneOutputProbe
                self.output_probe = PhoneOutputProbe(self.output_probe_dir,
                    sounddevice_module=self.sd, device=output_index,
                    seconds=self.output_probe_seconds, sample_rate=self.sample_rate,
                    blocksize=self.blocksize)
                self.output_probe.start()
            except Exception as exc:
                self.output_probe_error = type(exc).__name__

    def activate_output_probe(self) -> None:
        if self.output_probe is not None:
            self.output_probe.activate()

    def output_probe_diagnostics(self):
        if self.output_probe is not None:
            return self.output_probe.summary()
        return {'enabled': self.output_probe_dir is not None,
                'error': self.output_probe_error}

    def play_pcm48k(self, payload: bytes, *, utterance_id: str = '', kind: str = '',
                    text: str = '', final: bool = True) -> None:
        if not payload:
            return
        with self._output_lock:
            try:
                self.output_queue.put_nowait(payload)
            except queue.Full:
                self.queue_overflow_count += 1
                raise IPhoneAudioError('Audio output queue overflow; no speech was silently dropped')
            self.playback_ledger.enqueue(len(payload), utterance_id, kind=kind, text=text, final=final)

    def playback_snapshot(self):
        return self.playback_ledger.snapshot()

    def seal_utterance(self, identity, text=''):
        with self._output_lock:
            self.playback_ledger.seal(identity, text)

    def play_timeline_gap48k(self, frames: int) -> None:
        """Queue only the media gap that has not already played as silence."""
        frames = max(0, int(frames))
        if not frames:
            return
        with self._output_lock:
            compensated = min(frames, self._unbuffered_silence_frames)
            self._unbuffered_silence_frames = 0
            self.timeline_gap_compensated_frames += compensated
            remaining = frames - compensated
            self.timeline_gap_queued_frames += remaining
        if remaining:
            self.play_pcm48k(b"\x00" * (remaining * 2))

    def clear_output(self) -> None:
        self.clear_count += 1
        with self._output_lock:
            self.playback_ledger.cancel()
            self.output_buffer.clear()
            self._playback_ready = False
            self._recovering = False
            self._unbuffered_silence_frames = 0
            # A user interrupt used to cut a non-zero waveform straight to
            # digital zero.  Render a short ramp in the next callback instead
            # so barge-in does not produce a loud click.
            self._fade_out_pending = self._last_output_sample != 0
            self._fade_in_pending = False
            while True:
                try:
                    self.output_queue.get_nowait()
                except queue.Empty:
                    return

    def close(self) -> None:
        for stream in (self.input_stream, self.output_stream):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.input_stream = None
        self.output_stream = None
        if self.output_probe is not None:
            self.output_probe.close(ledger=self.playback_snapshot())
        self.clear_output()

    def _input_callback(
        self, indata: Any, frames: int, time_info: Any, status: Any
    ) -> None:
        del time_info
        self.input_callback_count += 1
        self.input_callback_frames += frames
        self._remember_status(status)
        payload = bytes(indata)
        if not payload or self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._deliver_input, payload)

    def _deliver_input(self, payload: bytes) -> None:
        result = self.on_input(payload)
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    def _output_callback(
        self, outdata: Any, frames: int, time_info: Any, status: Any
    ) -> None:
        self._remember_status(status)
        needed = frames * 2
        with self._output_lock:
            while True:
                try:
                    self.output_buffer.extend(self.output_queue.get_nowait())
                except queue.Empty:
                    break
            self.max_buffered_frames = max(
                self.max_buffered_frames, len(self.output_buffer) // 2
            )
            expects_more = self.playback_ledger.expects_more_audio()
            if not self.output_buffer and not expects_more:
                # End-of-turn silence is normal, not a streaming underrun.
                self._playback_ready = False
                self._recovering = False
                self._fade_in_pending = False
                self._unbuffered_silence_frames = 0
                self._fade_out_pending |= self._last_output_sample != 0

            if not self._playback_ready:
                target_bytes = (
                    self.rebuffer_bytes if self._recovering else self.prebuffer_bytes
                )
                enough_buffered = len(self.output_buffer) >= max(
                    needed, target_bytes
                )
                # A sealed short reply/tail cannot acquire any more samples.
                # Release it even when it is smaller than one callback block.
                enough_buffered |= bool(self.output_buffer) and not expects_more
                if enough_buffered:
                    was_recovering = self._recovering
                    self._playback_ready = True
                    self._recovering = False
                    self._fade_in_pending = was_recovering
                    # A continuous media frame can arrive late without a
                    # timestamp gap. Do not let that unplanned silence offset
                    # a later, unrelated media pause.
                    self._unbuffered_silence_frames = 0

            if not self._playback_ready:
                if not expects_more and not self.output_buffer:
                    self.idle_silence_frames += frames
                if self._fade_out_pending:
                    payload = self._ramp_to_zero(self._last_output_sample, frames)
                    self._fade_out_pending = False
                else:
                    payload = b"\x00" * needed
                    if self._recovering:
                        self.inserted_silence_frames += frames
                        self._unbuffered_silence_frames += frames
            else:
                available_frames = len(self.output_buffer) // 2
                consume_frames = self._adaptive_consume_frames(
                    frames, available_frames
                )
                if available_frames >= consume_frames:
                    consume_bytes = consume_frames * 2
                    source = bytes(self.output_buffer[:consume_bytes])
                    self.playback_ledger.consume(consume_bytes)
                    del self.output_buffer[:consume_bytes]
                    payload = resample_pcm16(source, frames)
                    if consume_frames > frames:
                        self.adaptive_speedup_count += 1
                    elif consume_frames < frames:
                        self.adaptive_slowdown_count += 1
                    if self._fade_in_pending:
                        payload = self._fade_in(payload)
                        self._fade_in_pending = False
                else:
                    # Consume the tail once, taper it smoothly to zero, then
                    # wait for a meaningful recovery buffer.  The old code
                    # inserted an abrupt full silent block and replayed the
                    # partial tail later, which is heard as tremolo/clicking.
                    source = bytes(self.output_buffer[: available_frames * 2])
                    self.playback_ledger.consume(available_frames * 2)
                    self.output_buffer.clear()
                    payload = self._conceal_underflow(source, frames)
                    missing_frames = max(0, frames - available_frames)
                    self._playback_ready = False
                    self._recovering = expects_more
                    self._fade_in_pending = expects_more
                    if expects_more:
                        self.underrun_count += 1
                        self.rebuffer_count += 1
                        self.inserted_silence_frames += missing_frames
                        self._unbuffered_silence_frames += missing_frames
                        if (
                            self._started_at is not None
                            and len(self.underrun_times_ms) < 256
                        ):
                            self.underrun_times_ms.append(
                                round((time.monotonic() - self._started_at) * 1000)
                            )
                    else:
                        self.idle_silence_frames += missing_frames
                        self._unbuffered_silence_frames = 0
            if len(payload) >= 2:
                self._last_output_sample = int.from_bytes(
                    payload[-2:], byteorder="little", signed=True
                )
        outdata[:] = payload
        if self.output_probe is not None:
            self.output_probe.output(payload, frames, time_info, status)

    def _adaptive_consume_frames(self, frames: int, available_frames: int) -> int:
        """Keep the jitter buffer centred without audible pitch wobble."""
        if not self.adaptive_rate_limit or self.prebuffer_bytes <= 0:
            return frames
        target_frames = max(frames, self.prebuffer_bytes // 2)
        deadband = max(frames * 2, target_frames // 4)
        max_adjustment = max(1, round(frames * self.adaptive_rate_limit))
        if available_frames > target_frames + deadband:
            return frames + max_adjustment
        if (
            available_frames < target_frames - deadband
            and available_frames >= frames - max_adjustment
        ):
            return max(1, frames - max_adjustment)
        return frames

    def _fade_in(self, payload: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(payload)
        count = min(self.fade_frames, len(samples))
        for index in range(count):
            samples[index] = round(samples[index] * (index + 1) / count)
        return samples.tobytes()

    def _ramp_to_zero(self, start_sample: int, output_frames: int) -> bytes:
        count = min(self.fade_frames, output_frames)
        rendered = array("h")
        for index in range(count):
            rendered.append(round(start_sample * (count - index - 1) / count))
        rendered.extend([0] * (output_frames - count))
        return rendered.tobytes()

    def _conceal_underflow(self, source: bytes, output_frames: int) -> bytes:
        samples = array("h")
        samples.frombytes(source)
        if len(samples) >= output_frames:
            return array("h", samples[:output_frames]).tobytes()
        start_sample = samples[-1] if samples else self._last_output_sample
        missing = output_frames - len(samples)
        ramp_count = min(self.fade_frames, missing)
        for index in range(ramp_count):
            samples.append(round(start_sample * (ramp_count - index - 1) / ramp_count))
        samples.extend([0] * (missing - ramp_count))
        return samples.tobytes()

    def diagnostics(self) -> dict[str, Any]:
        with self._output_lock:
            buffered_frames = len(self.output_buffer) // 2
        with self.status_messages.mutex:
            messages = list(self.status_messages.queue)
        actual_latency = getattr(self.output_stream, "latency", None)
        try:
            actual_latency_ms = round(float(actual_latency) * 1000, 1)
        except (TypeError, ValueError):
            actual_latency_ms = None
        return {
            "output_clear_count": self.clear_count,
            "output_underrun_count": self.underrun_count,
            "inserted_silence_ms": round(
                self.inserted_silence_frames * 1000 / self.sample_rate
            ),
            "output_idle_silence_ms": round(self.idle_silence_frames * 1000 / self.sample_rate),
            "underrun_scope": "unsealed_stream_starvation",
            "prebuffer_ms": self.prebuffer_ms,
            "rebuffer_ms": self.rebuffer_ms,
            "device_output_latency_ms": self.output_latency_ms,
            "actual_output_latency_ms": actual_latency_ms,
            "rebuffer_count": self.rebuffer_count,
            "timeline_gap_queued_ms": round(
                self.timeline_gap_queued_frames * 1000 / self.sample_rate
            ),
            "timeline_gap_compensated_ms": round(
                self.timeline_gap_compensated_frames * 1000 / self.sample_rate
            ),
            "underrun_times_ms": self.underrun_times_ms,
            "queue_overflow_count": self.queue_overflow_count,
            "stream_underflow_count": self.stream_underflow_count,
            "stream_overflow_count": self.stream_overflow_count,
            "input_callback_count": self.input_callback_count,
            "input_callback_frames": self.input_callback_frames,
            "input_stream_overflow_count": self.input_stream_overflow_count,
            "input_stream_underflow_count": self.input_stream_underflow_count,
            "adaptive_speedup_count": self.adaptive_speedup_count,
            "adaptive_slowdown_count": self.adaptive_slowdown_count,
            "max_buffered_ms": round(
                self.max_buffered_frames * 1000 / self.sample_rate
            ),
            "buffered_at_stop_ms": round(buffered_frames * 1000 / self.sample_rate),
            "stream_status_messages": messages,
            "output_probe": self.output_probe_diagnostics(),
        }

    def _remember_status(self, status: Any) -> None:
        if not status:
            return
        message = str(status)
        lowered = message.casefold()
        if "output underflow" in lowered:
            self.stream_underflow_count += 1
        if "output overflow" in lowered:
            self.stream_overflow_count += 1
        if "input overflow" in lowered:
            self.input_stream_overflow_count += 1
        if "input underflow" in lowered:
            self.input_stream_underflow_count += 1
        try:
            self.status_messages.put_nowait(message)
        except queue.Full:
            pass


class IPhoneDialer:
    # Only Sending within the original attempt window can bind an unknown
    # call. A later manual/other call must never release an old line guard.
    CALL_BINDING_WINDOW_SECONDS = 60.0
    PROMPT_PROCESS = "FaceTimeNotificationViewBridgeService"
    NOTIFICATION_PROCESS = "FaceTimeNotificationExtension"
    PHONE_PROCESS = "Phone"
    AX_HELPER = Path(__file__).with_name("phone_ax_helper.swift")
    COMPILED_AX_HELPER = Path.home() / ".codex-phone" / "bin" / "phone_ax_helper"

    def __init__(
        self,
        opener: Callable[..., Any] | None = None,
        *,
        process_finder: Callable[[str], list[int]] | None = None,
        helper_runner: Callable[..., Any] | None = None,
        call_state_probe: Callable[[float], str] | None = None,
        prompt_timeout: float = 6.0,
        active_timeout: float = 60.0,
        on_call_observation: Callable[[float], Any] | None = None,
        on_dial_request: Callable[[float], Any] | None = None,
    ) -> None:
        self.opener = opener or subprocess.run
        self.process_finder = process_finder or self._find_processes
        self.helper_runner = helper_runner or subprocess.run
        self.call_state_probe = call_state_probe or self._probe_current_call
        self.system_call_uuid = ''
        self.prompt_timeout = max(0.5, prompt_timeout)
        self.active_timeout = max(0.5, active_timeout)
        self.call_started_at: float | None = None
        self.on_call_observation = on_call_observation
        self.on_dial_request = on_dial_request
        self.timing_ms: dict[str, int] = {}
        self._dial_started_monotonic = 0.0
        self.dial_diagnostics: dict[str, Any] = {}
        self._connection_deadline: float | None = None
        self._state_probe_lock = asyncio.Lock()
        self._dial_terminal_state = ''
        self._last_dial_state = 'unknown'
        self._dial_ui_error: Exception | None = None

    def _mark_timing(self, stage: str) -> None:
        if self._dial_started_monotonic <= 0:
            return
        self.timing_ms[stage] = round(
            (time.monotonic() - self._dial_started_monotonic) * 1000
        )

    async def dial(self, number: str) -> None:
        if not re.fullmatch(r"\+[1-9]\d{6,14}", number):
            raise ValueError("接听号码不是 E.164 格式")
        self.timing_ms = {}
        self.dial_diagnostics = {'ui_errors': [], 'system_states': [],
                                 'confirmation_attempted': False}
        self._dial_terminal_state = ''
        self._last_dial_state = 'unknown'
        self._dial_ui_error = None
        self._connection_deadline = None
        self.system_call_uuid = ''
        self.call_started_at = None
        self._dial_started_monotonic = time.monotonic()
        # FaceTimeNotificationViewBridgeService is allowed to remain alive
        # after an earlier call. Its PID alone is not proof that a confirmation
        # banner is visible. Only block when Phone.app itself exposes an active
        # call or its real communication-audio confirmation button.
        phone_pids = await asyncio.to_thread(self.process_finder, self.PHONE_PROCESS)
        if phone_pids:
            phone_pid = phone_pids[0]
            active = await asyncio.to_thread(self._run_helper, "is-call-active", phone_pid)
            if int(getattr(active, "returncode", 1)) not in {0, 3}:
                raise RuntimeError("无法确认 Phone.app 当前是否空闲；没有触发拨号")
            confirmation = await asyncio.to_thread(self._run_helper, "has-phone-confirmation", phone_pid)
            if int(getattr(confirmation, "returncode", 1)) not in {0, 65}:
                raise RuntimeError("无法完整读取 Phone.app 确认状态；没有触发拨号")
            if (int(getattr(active, "returncode", 1)) == 0
                or int(getattr(confirmation, "returncode", 1)) == 0):
                raise RuntimeError("Phone.app 已有通话或确认条，已拒绝再次触发，避免重复拨号")

        result = await asyncio.to_thread(
            self.opener,
            # Bring up Phone.app itself. macOS currently logs
            # `mobilephone-recents:` as an unsupported URL even when the app
            # happens to open, so do not route through that scheme. The
            # configured number is still selected from Phone.app's own recent
            # calls and no .xpc, extension, or tel: URL is opened.
            ["/usr/bin/open", "/System/Applications/Phone.app"],
            capture_output=True,
            text=True,
            check=False,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()
            raise RuntimeError(f"无法打开 Mac 电话 App：{detail}")
        self._mark_timing("phone_app_opened")

        phone_pid = await self._wait_for_process(self.PHONE_PROCESS, self.prompt_timeout)
        if phone_pid is None:
            raise RuntimeError("Phone.app 没有启动；没有拨号，也不会自动重试")
        self._mark_timing("phone_process_ready")

        await self._wait_for_recent_row(phone_pid, number)
        self._mark_timing("recent_row_ready")
        # Phone.app creates the CallServices Sending record when its recent
        # row is pressed, BEFORE the separate confirmation click. Observe
        # from this first action so that record can bind our exact call UUID.
        # Do not compensate by accepting an unbound Active or older call.
        call_started_at = time.time()
        if self.on_call_observation is not None:
            self.on_call_observation(call_started_at)
        # This is the earliest possible call creation, not evidence that a
        # confirmation was pressed or that cellular dialing/Active occurred.
        self.call_started_at = call_started_at
        self._mark_timing("call_state_observation_started")
        # The UI acknowledgement is not a call state. Watch the exact system
        # call concurrently from the first possible action; even a helper
        # timeout may follow an action that actually reached the phone.
        self._connection_deadline = asyncio.get_running_loop().time() + self.active_timeout
        ui_task = asyncio.create_task(self._drive_call_ui(phone_pid, number, call_started_at))
        state_task = asyncio.create_task(self._wait_for_active_call(phone_pid, call_started_at))
        try:
            await state_task
        finally:
            # Only cancel our pending coroutine/reads. A dispatched one-time
            # helper action may finish later, but cannot trigger another step.
            if not ui_task.done():
                self.dial_diagnostics['ui_task_cancelled'] = True
                ui_task.cancel()
            if not state_task.done():
                state_task.cancel()
            await asyncio.gather(ui_task, state_task, return_exceptions=True)
        self._mark_timing("system_call_active")
        self.dial_diagnostics['resolved_by'] = 'bound_system_active'

    async def _drive_call_ui(self, phone_pid: int, number: str, started_at: float) -> None:
        phase = 'recent_press'
        try:
            await self._press_recent_once(phone_pid, number)
            self._mark_timing('recent_row_pressed')
            self.dial_diagnostics['recent_request_method'] = 'named_accessibility_action'
            self.dial_diagnostics['recent_action_accepted'] = True
            if await self._observe_dial_state(started_at) in {'active', 'disconnected'}:
                return
            if asyncio.get_running_loop().time() >= self._connection_deadline:
                return
            phase = 'confirmation_read'
            await self._wait_for_phone_confirmation(phone_pid)
            self._mark_timing('confirmation_ready')
            # A banner may be stale or disappear as the other phone answers.
            # Recheck the bound call, never click based on an old UI result.
            if await self._observe_dial_state(started_at) in {'active', 'disconnected'}:
                return
            if asyncio.get_running_loop().time() >= self._connection_deadline:
                return
            phase = 'confirmation_readiness'
            if self.on_dial_request is not None:
                self.on_dial_request(started_at)
            phase = 'confirmation_press'
            self.dial_diagnostics['confirmation_attempted'] = True
            self._mark_timing('confirmation_press_started')
            await self._press_phone_confirmation_once(phone_pid)
            self._mark_timing('system_dial_requested')
        except asyncio.CancelledError:
            self.dial_diagnostics['ui_cancelled_phase'] = phase
            raise
        except Exception as exc:
            # Stop UI actions, not the already possible telephone call. Keep
            # the original bounded state watcher alive; no second click, no
            # new call, no fabricated Active. Cancellation is not swallowed.
            self._dial_ui_error = exc
            self.dial_diagnostics['ui_errors'].append({'stage': phase, 'type': type(exc).__name__})

    async def _observe_dial_state(self, started_at: float) -> str:
        if self._dial_terminal_state:
            return self._dial_terminal_state
        remaining = self._connection_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return self._last_dial_state
        try:
            # Serialize binding/probing so a slow older read cannot override
            # a newer Active/Disconnected result or extend the overall deadline.
            async with asyncio.timeout(remaining):
                async with self._state_probe_lock:
                    if self._dial_terminal_state:
                        return self._dial_terminal_state
                    state = await asyncio.to_thread(self.call_state_probe, started_at)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            self.dial_diagnostics['state_read_failures'] = self.dial_diagnostics.get('state_read_failures', 0) + 1
            return 'unknown'
        if asyncio.get_running_loop().time() >= self._connection_deadline:
            return 'unknown'  # A late read cannot revive an expired attempt.
        if state not in {'sending', 'active', 'disconnected', 'idle'}:
            state = 'unknown'
        self._last_dial_state = state
        observations = self.dial_diagnostics.setdefault('system_states', [])
        if len(observations) < 16 and (not observations or observations[-1]['state'] != state):
            observations.append({'state': state, 'elapsed_ms': round(
                (time.monotonic() - self._dial_started_monotonic) * 1000)})
        if state in {'active', 'disconnected'}:
            self._dial_terminal_state = state
        return state

    async def wait_for_disconnect(
        self, timeout: float, *, poll_interval: float = 0.75
    ) -> bool:
        """Return promptly when CallServices confirms that this call ended."""
        if self.call_started_at is None:
            raise RuntimeError("尚未建立可监听的电话")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            state = await asyncio.to_thread(
                self.call_state_probe, self.call_started_at
            )
            if state == "disconnected":
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(max(0.2, poll_interval), remaining))

    async def _wait_for_process(self, name: str, timeout: float) -> int | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pids = await asyncio.to_thread(self.process_finder, name)
            if pids:
                return pids[0]
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def _wait_for_recent_row(self, phone_pid: int, number: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.prompt_timeout
        last_detail = ""
        while True:
            result = await asyncio.to_thread(
                self._run_helper, "recent-signature", phone_pid, number
            )
            if int(getattr(result, "returncode", 1)) == 0:
                return
            last_detail = str(
                getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
            ).strip()
            if loop.time() >= deadline:
                raise RuntimeError(
                    "没有找到号码对应的最近通话记录；没有点击："
                    + (last_detail or "Phone.app 最近通话尚未加载")
                )
            if not await asyncio.to_thread(self.process_finder, self.PHONE_PROCESS):
                raise RuntimeError("Phone.app 已经退出；没有点击")
            await asyncio.sleep(0.25)

    async def _press_recent_once(self, phone_pid: int, number: str) -> None:
        result = await asyncio.to_thread(
            self._run_helper, "press-recent-call", phone_pid, number
        )
        if int(getattr(result, "returncode", 1)) == 0:
            return
        detail = str(
            getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
        ).strip()
        raise RuntimeError(
            "最近通话的呼叫动作没有确认成功；为避免重复，不会重试："
            + (detail or "未知辅助功能错误")
        )

    async def _wait_for_phone_confirmation(self, phone_pid: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.prompt_timeout
        last_detail = ""
        observations = self.dial_diagnostics.setdefault('confirmation_reads', [])
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "Phone.app 没有出现唯一的通信音频确认按钮；没有确认拨号："
                    + (last_detail or "确认按钮尚未出现")
                )
            observation = {'elapsed_ms': round((time.monotonic() - self._dial_started_monotonic) * 1000),
                           'result': 'pending'}
            if len(observations) < 32:
                observations.append(observation)
            read_started = loop.time()
            try:
                result = await asyncio.to_thread(
                    self._run_helper, "has-phone-confirmation", phone_pid,
                    timeout=min(2.0, remaining),
                )
            except subprocess.TimeoutExpired:
                # This was only a read. Re-observe within the existing deadline;
                # never repeat the recent-row or confirmation click.
                result = None
                observation['result'] = 'read_timeout'
            except asyncio.CancelledError:
                observation['result'] = 'read_cancelled'
                raise
            except Exception:
                observation['result'] = 'read_failed'
                raise
            finally:
                observation['duration_ms'] = round((loop.time() - read_started) * 1000)
            code = int(getattr(result, 'returncode', 1))
            if result is not None:
                observation.update(confirmation_read_diagnostic(result))
            if code == 0:
                return
            last_detail = str(
                getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
            ).strip()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "Phone.app 没有出现唯一的通信音频确认按钮；没有确认拨号："
                    + (last_detail or "确认按钮尚未出现")
                )
            await asyncio.sleep(min(0.2, remaining))

    async def _press_phone_confirmation_once(self, phone_pid: int) -> None:
        result = await asyncio.to_thread(
            self._run_helper, "press-phone-confirmation", phone_pid
        )
        if int(getattr(result, "returncode", 1)) == 0:
            return
        detail = str(
            getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
        ).strip()
        raise RuntimeError(
            "通信音频确认按钮没有成功点击；为避免重复，不会重试："
            + (detail or "未知辅助功能错误")
        )

    async def _wait_for_active_call(
        self, phone_pid: int, call_started_at: float
    ) -> None:
        loop = asyncio.get_running_loop()
        if self._connection_deadline is None:
            self._connection_deadline = loop.time() + self.active_timeout
        deadline = self._connection_deadline
        last_system_state = "unknown"
        while True:
            # No accessibility/process scan can delay or overrule this bound
            # call. Phone.app can disappear while CallServices keeps calling.
            system_state = await self._observe_dial_state(call_started_at)
            if system_state == "active":
                return
            if system_state == "disconnected":
                raise RuntimeError(
                    "系统电话状态显示本次呼叫已断开；不会重试"
                )
            if system_state in {"sending", "idle", "unknown"}:
                last_system_state = system_state
            if loop.time() >= deadline:
                if last_system_state == "sending":
                    raise RuntimeError(
                        "电话已经拨出，但在等待时间内没有接通；不会重试"
                    )
                raise RuntimeError(
                    "没有观察到系统真实通话状态；已按拨打失败处理，"
                    "不会重试："
                    + (str(self._dial_ui_error) if self._dial_ui_error else "通话状态一直未建立")
                ) from self._dial_ui_error
            await asyncio.sleep(min(0.2, max(0, deadline - loop.time())))

    @staticmethod
    def _summarize_call_failure(events: list[dict[str, Any]], started_at: float, call_uuid: str = '') -> dict[str, Any]:
        """Keep diagnostic facts, never phone handles or unrelated log text."""
        states: list[str] = []
        reasons: set[str] = set()
        calls: set[str] = set()
        rows = []
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                stamp = datetime.strptime(str(event.get('timestamp', '')), '%Y-%m-%d %H:%M:%S.%f%z').timestamp()
            except (ValueError, TypeError):
                continue
            if stamp < started_at:
                continue
            message = str(event.get('eventMessage') or '')
            rows.append(message)
            reasons.update(re.findall(r'IDSSessionEndedReason[A-Za-z]+', message))
            if 'TUCallCenterCallStatusChangedNotification' in message or 'TUCallCenterCallConnectedNotification' in message:
                calls.update(identity.lower() for identity in re.findall(r'\buPI=([A-Fa-f0-9-]{36})', message))
        identity = call_uuid.lower() if call_uuid else next(iter(calls)) if len(calls) == 1 else ''
        requesters, disconnect_codes, bound_reasons = set(), set(), set()
        for message in rows:
            identities = {value.lower() for value in re.findall(r'\buPI=([A-Fa-f0-9-]{36})', message)}
            if identity and identities == {identity}:
                bound_reasons.update(re.findall(r'IDSSessionEndedReason[A-Za-z]+', message))
            if (identity and identities == {identity} and ('TUCallCenterCallStatusChangedNotification' in message
                                                         or 'TUCallCenterCallConnectedNotification' in message)):
                for state in re.findall(r'\bstat=(Sending|Active|Disconnected|Idle)\b', message):
                    if not states or states[-1] != state:
                        states.append(state)
                if re.search(r'\bstat=Disconnected\b', message):
                    disconnect_codes.update(int(code) for code in re.findall(r'\bdR=([0-9]{1,5})\b', message))
            request = re.search(r'Disconnecting call with identifier: ([A-Fa-f0-9-]{36}), client: .*?\bprocessName=([A-Za-z][A-Za-z0-9_.-]{0,95})\b', message)
            if identity and request and request.group(1).lower() == identity:
                requesters.add(request.group(2))
        result: dict[str, Any] = {'states': states, 'ids_reasons': sorted(reasons),
            'ids_reason_scope':'time_window_only', 'disconnect_requested_by':sorted(requesters),
            'bound_ids_reasons': sorted(bound_reasons),
            'disconnect_reason_codes':sorted(disconnect_codes),
            'reached_active': 'Active' in states, 'observed_call_count': len(calls)}
        if identity:
            result['system_call_uuid'] = identity
        if (requesters and 'Sending' in states and 'Active' not in states):
            result['failure_stage'] = 'local_call_disconnect_request'
            result['explanation'] = '系统记录了本机通话进程的断开请求；触发原因未确认，不能据此认定接听人挂断或网络故障'
        elif ('Sending' in states and 'Active' not in states
                and 'IDSSessionEndedReasonNoRemoteNetwork' in bound_reasons):
            result['failure_stage'] = 'iphone_relay_network_unavailable'
            result['explanation'] = '系统报告电话接力的远端网络不可用；未接通，不代表某个手机设置一定错误'
        elif ('Sending' in states and 'Active' not in states
                and 'IDSSessionEndedReasonRemoteUnanswered' in bound_reasons):
            result['failure_stage'] = 'iphone_relay_setup'
            result['explanation'] = 'Mac 与 iPhone 的电话接力未建立；不是接听人未接电话的证据'
        else:
            result['failure_stage'] = 'unresolved'
        return result

    def failure_diagnostics(self) -> dict[str, Any]:
        if self.call_started_at is None:
            return {'failure_stage': 'before_system_dial'}
        try:
            result = subprocess.run([
                '/usr/bin/log', 'show', '--style', 'json', '--info',
                '--start', datetime.fromtimestamp(self.call_started_at).strftime('%Y-%m-%d %H:%M:%S'),
                '--end', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                '--predicate', 'process == "callservicesd" AND '
                '(eventMessage CONTAINS "TUCallCenterCall" OR eventMessage CONTAINS "IDSSessionEndedReason" '
                'OR eventMessage CONTAINS "Disconnecting call with identifier:")',
            ], capture_output=True, text=True, check=False, timeout=4)
            events = json.loads(result.stdout) if result.returncode == 0 else []
            if not isinstance(events, list):
                events = []
            return self._summarize_call_failure(events, self.call_started_at, self.system_call_uuid)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {'failure_stage': 'unresolved', 'diagnostic_read_failed': True}

    def _probe_current_call(self, started_at: float) -> str:
        state, identity = self._system_call_snapshot_since(started_at, self.system_call_uuid)
        if identity:
            self.system_call_uuid = identity
        return state

    @staticmethod
    def _system_call_state_since(started_at: float, call_uuid: str = '') -> str:
        return IPhoneDialer._system_call_snapshot_since(started_at, call_uuid)[0]

    @staticmethod
    def _system_call_snapshot_since(started_at: float, call_uuid: str = '') -> tuple[str, str]:
        """Read CallServices' state transitions without opening any UI bundle."""
        start_text = datetime.fromtimestamp(max(0.0, started_at - 1.0)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        predicate = (
            'process == "callservicesd" AND '
            '(eventMessage CONTAINS "TUCallCenterCallConnectedNotification" OR '
            'eventMessage CONTAINS "TUCallCenterCallStatusChangedNotification")'
        )
        try:
            result = subprocess.run(
                [
                    "/usr/bin/log",
                    "show",
                    "--start",
                    start_text,
                    "--style",
                    "json",
                    "--predicate",
                    predicate,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=6,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown", call_uuid
        if result.returncode != 0:
            return "unknown", call_uuid
        try:
            events = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return "unknown", call_uuid
        if not isinstance(events, list):
            return "unknown", call_uuid
        return IPhoneDialer._state_from_call_events(events, started_at, call_uuid)

    @staticmethod
    def _state_from_call_events(events, started_at, call_uuid=''):
        transitions = []
        candidates = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            timestamp = str(event.get("timestamp") or "")
            try:
                event_time = datetime.strptime(
                    timestamp, "%Y-%m-%d %H:%M:%S.%f%z"
                ).timestamp()
            except ValueError:
                continue
            if event_time < started_at:
                continue
            message = str(event.get("eventMessage") or "")
            identities = re.findall(r'\buPI=([A-Fa-f0-9-]{36})', message)
            if len(set(identities)) != 1:
                continue
            identity = identities[0].lower()
            matches = re.findall(
                r"\bstat=(Sending|Active|Disconnected|Idle)\b", message
            )
            if matches:
                state = matches[-1].lower()
                if state == 'sending' and event_time <= started_at + IPhoneDialer.CALL_BINDING_WINDOW_SECONDS:
                    candidates.add(identity)
                transitions.append((event_time, identity, state))
        identity = call_uuid.lower()
        if not identity:
            if len(candidates) != 1:
                return 'unknown', ''  # Ambiguous/out-of-window: never guess.
            identity = next(iter(candidates))
        latest = 'unknown'
        for _, seen, state in sorted(transitions):
            if seen == identity and latest != 'disconnected':
                latest = state
        return latest, identity

    def _run_helper(self, action: str, pid: int, *arguments: str, timeout: float = 8) -> Any:
        if (
            self.COMPILED_AX_HELPER.is_file()
            and self.COMPILED_AX_HELPER.stat().st_mode & 0o100
        ):
            command = [
                str(self.COMPILED_AX_HELPER),
                "--compiled",
                action,
                str(pid),
                *arguments,
            ]
        else:
            command = [
                "/usr/bin/swift",
                str(self.AX_HELPER),
                action,
                str(pid),
                *arguments,
            ]
        return self.helper_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    @staticmethod
    def _find_processes(name: str) -> list[int]:
        result = subprocess.run(
            ["/bin/ps", "ax", "-o", "pid=,comm="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        matches: list[int] = []
        for raw in result.stdout.splitlines():
            fields = raw.strip().split(maxsplit=1)
            if len(fields) != 2 or Path(fields[1]).name != name:
                continue
            try:
                matches.append(int(fields[0]))
            except ValueError:
                continue
        return matches
