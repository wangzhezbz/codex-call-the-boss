from __future__ import annotations

import asyncio
import platform
import re
import subprocess
import tempfile
import threading
import time
import unittest
from array import array
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from iphone_audio import (
    IPhoneAudioError,
    IPhoneAudioPipe,
    IPhoneDialer,
    InputSignalStats,
    confirmation_read_diagnostic,
    resolve_audio_device,
)


class FakeStream:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeSoundDevice:
    def __init__(self) -> None:
        self.input_stream: FakeStream | None = None
        self.output_stream: FakeStream | None = None

    @staticmethod
    def query_devices() -> list[dict[str, Any]]:
        return [
            {
                "name": "BlackHole 2ch",
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48_000,
            },
            {
                "name": "BlackHole 16ch",
                "max_input_channels": 16,
                "max_output_channels": 16,
                "default_samplerate": 48_000,
            },
        ]

    def RawInputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802
        self.input_stream = FakeStream(**kwargs)
        return self.input_stream

    def RawOutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802
        self.output_stream = FakeStream(**kwargs)
        return self.output_stream


class InputSignalTests(unittest.TestCase):
    def test_silence_quiet_input_and_fragmented_energy_stay_distinct(self):
        stats = InputSignalStats()
        for index, value in enumerate((0, 120, 300, 300, 0, 300)):
            payload = array('h', [value] * 960).tobytes()
            original = bytes(payload)
            self.assertEqual(stats.observe(payload, threshold=180, now=index*.02), (abs(value), 20))
            self.assertEqual(payload, original)
        result = stats.snapshot(now=.14)
        self.assertEqual(result['audio_ms'], 120)
        self.assertEqual(result['all_zero_callbacks'], 2)
        self.assertEqual(result['peak_block_rms'], 300)
        self.assertEqual(result['above_threshold_ms'], 60)
        self.assertEqual(result['max_contiguous_above_threshold_ms'], 40)
        self.assertEqual(result['last_callback_age_ms'], 40)
        self.assertFalse(result['proves_caller_speech'])
        self.assertFalse(result['contains_audio'])
        self.assertTrue(all(not isinstance(value, (bytes, bytearray, list, dict, array))
                            for value in vars(stats).values()))

    def test_no_input_odd_empty_bytes_and_callback_pause_are_not_speech(self):
        stats = InputSignalStats()
        self.assertIsNone(stats.snapshot(now=5)['last_callback_age_ms'])
        self.assertEqual(stats.observe(b'\x00', threshold=180, now=5), (0, 0))
        stats.observe(array('h', [-180]*960).tobytes(), threshold=180, now=5)
        stats.observe(bytes(1920), threshold=180, now=5.5)
        snapshot = stats.snapshot(now=6)
        self.assertEqual(snapshot['odd_bytes'], 1)
        self.assertEqual(snapshot['max_callback_gap_ms'], 500)
        self.assertEqual(snapshot['last_callback_age_ms'], 500)
        self.assertEqual(snapshot['callbacks'], 2)

    def test_input_overflow_is_not_misreported_as_output_failure(self):
        pipe = IPhoneAudioPipe('fake-in', 'fake-out', lambda _: None, sounddevice_module=object())
        pipe._input_callback(bytes(1920), 960, None, 'input overflow')
        pipe._input_callback(bytes(1920), 960, None, 'input underflow')
        metrics = pipe.diagnostics()
        self.assertEqual(metrics['input_callback_count'], 2)
        self.assertEqual(metrics['input_callback_frames'], 1920)
        self.assertEqual(metrics['input_stream_overflow_count'], 1)
        self.assertEqual(metrics['input_stream_underflow_count'], 1)
        self.assertEqual(metrics['stream_overflow_count'], 0)
        self.assertEqual(metrics['stream_underflow_count'], 0)


class AudioDeviceTests(unittest.TestCase):
    def test_time_neighbor_ids_errors_are_not_the_bound_calls_diagnosis(self):
        own = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        def event(second, message):
            return {'timestamp': f'2026-09-08 21:00:{second:02d}.000000+0800',
                    'eventMessage': message}
        for reason in ('IDSSessionEndedReasonNoRemoteNetwork', 'IDSSessionEndedReasonRemoteUnanswered'):
            for hint in ('', f' session={other}', f' uPI={other}', f' uPI={own} uPI={other}'):
                with self.subTest(reason=reason, hint=hint):
                    events = [event(1, f'TUCallCenterCallStatusChangedNotification stat=Sending uPI={own}'),
                              event(2, reason + hint),
                              event(3, f'TUCallCenterCallStatusChangedNotification stat=Disconnected uPI={own}')]
                    result = IPhoneDialer._summarize_call_failure(events, 0, own)
                    self.assertEqual(result['failure_stage'], 'unresolved')
                    self.assertEqual(result['ids_reasons'], [reason])
                    self.assertEqual(result['bound_ids_reasons'], [])

    def test_disconnect_request_is_correlated_without_guessing_who_clicked(self):
        identity = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        def event(second, message):
            return {'timestamp':f'2026-09-07 01:30:{second}.000000+0800', 'eventMessage':message}
        events = [
            event('16', f'TUCallCenterCallStatusChangedNotification stat=Sending uPI={identity}'),
            event('18', f'Disconnecting call with identifier: {other}, client: processName=Unrelated processBundleIdentifier=other'),
            event('19', f'Disconnecting call with identifier: {identity}, client: processName=FaceTimeNotificationExtension processBundleIdentifier=private'),
            event('19', 'IDSSessionEndedReasonInitiatorCancelled'),
            event('20', f'TUCallCenterCallStatusChangedNotification stat=Disconnected dR=41 fR=0 uPI={identity}'),
        ]
        result = IPhoneDialer._summarize_call_failure(events, 0)
        self.assertEqual(result['failure_stage'], 'local_call_disconnect_request')
        self.assertEqual(result['disconnect_requested_by'], ['FaceTimeNotificationExtension'])
        self.assertEqual(result['disconnect_reason_codes'], [41])
        self.assertEqual(result['ids_reason_scope'], 'time_window_only')
        self.assertNotIn('private', str(result))
        self.assertIn('触发原因未确认', result['explanation'])

    def test_other_call_disconnect_and_malformed_rows_cannot_supply_cause(self):
        identity = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        events = [None, [], {'timestamp':'bad', 'eventMessage':'private'},
            {'timestamp':'2026-09-07 01:30:16.000000+0800','eventMessage':
             f'TUCallCenterCallStatusChangedNotification stat=Sending uPI={identity}'},
            {'timestamp':'2026-09-07 01:30:19.000000+0800','eventMessage':
             f'Disconnecting call with identifier: {other}, client: processName=OtherCall processBundleIdentifier=other'}]
        result = IPhoneDialer._summarize_call_failure(events, 0)
        self.assertEqual(result['failure_stage'], 'unresolved')
        self.assertEqual(result['disconnect_requested_by'], [])

    def test_remote_network_failure_is_separate_from_audio_and_settings(self):
        events=[{'timestamp':'2026-09-06 01:43:00.000000+0800','eventMessage':
            'TUCallCenterCallStatusChangedNotification stat=Sending uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'},
            {'timestamp':'2026-09-06 01:43:04.000000+0800','eventMessage':
             'IDSSessionEndedReasonNoRemoteNetwork uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'},
            {'timestamp':'2026-09-06 01:43:05.000000+0800','eventMessage':
            'TUCallCenterCallStatusChangedNotification stat=Disconnected uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'}]
        result=IPhoneDialer._summarize_call_failure(events,0)
        self.assertEqual(result['failure_stage'],'iphone_relay_network_unavailable')
        self.assertFalse(result['reached_active'])
        self.assertIn('不代表某个手机设置一定错误',result['explanation'])

    def test_ids_unanswered_is_not_called_callee_unanswered(self):
        events = [
            {'timestamp':'2026-09-05 18:19:37.000000+0800', 'eventMessage':
             'TUCallCenterCallStatusChangedNotification stat=Sending uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'},
            {'timestamp':'2026-09-05 18:20:07.000000+0800', 'eventMessage':
             'IDSSessionEndedReasonRemoteUnanswered (4) uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'},
            {'timestamp':'2026-09-05 18:20:08.000000+0800', 'eventMessage':
             'TUCallCenterCallStatusChangedNotification stat=Disconnected uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'},
        ]
        result = IPhoneDialer._summarize_call_failure(events, 0)
        self.assertEqual(result['failure_stage'], 'iphone_relay_setup')
        self.assertFalse(result['reached_active'])
        events.append({'timestamp':'2026-09-05 18:20:09.000000+0800', 'eventMessage':
            'TUCallCenterCallStatusChangedNotification stat=Active uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'})
        self.assertEqual(IPhoneDialer._summarize_call_failure(events, 0)['failure_stage'], 'unresolved')

    def test_old_or_ambiguous_call_cannot_supply_failure_reason(self):
        events = [{'timestamp':'2020-01-01 00:00:00.000000+0800', 'eventMessage':
            'IDSSessionEndedReasonRemoteUnanswered TUCallCenterCallStatusChangedNotification stat=Sending uPI=2B1D012A-B42A-4672-92D7-DBF14D33CA7E'}]
        result = IPhoneDialer._summarize_call_failure(events, time.time())
        self.assertEqual(result['failure_stage'], 'unresolved')
        self.assertEqual(result['ids_reasons'], [])

    def test_resolves_device_by_direction(self) -> None:
        sd = FakeSoundDevice()
        self.assertEqual(resolve_audio_device("BlackHole 2ch", "input", sd), 0)
        self.assertEqual(resolve_audio_device("BlackHole 16ch", "output", sd), 1)

    def test_missing_device_is_explicit(self) -> None:
        with self.assertRaisesRegex(IPhoneAudioError, "找不到input音频设备"):
            resolve_audio_device("missing", "input", FakeSoundDevice())


class AudioPipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_same_capture_and_feed_device(self):
        sd = FakeSoundDevice()
        pipe = IPhoneAudioPipe('BlackHole 2ch','BlackHole 2ch',lambda _:None,sounddevice_module=sd)
        with self.assertRaisesRegex(IPhoneAudioError,'回声环路'):
            pipe.start()
        self.assertIsNone(sd.input_stream)
        self.assertIsNone(sd.output_stream)

    async def test_routes_raw_pcm_in_both_directions(self) -> None:
        received: list[bytes] = []
        sd = FakeSoundDevice()
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            received.append,
            prebuffer_ms=0,
            sounddevice_module=sd,
        )
        pipe.start()
        assert sd.input_stream is not None and sd.output_stream is not None
        self.assertTrue(sd.input_stream.started)
        self.assertTrue(sd.output_stream.started)
        self.assertEqual(sd.output_stream.kwargs["latency"], 0.12)

        sd.input_stream.kwargs["callback"](b"\x01\x02" * 960, 960, None, None)
        await asyncio.sleep(0)
        self.assertEqual(received, [b"\x01\x02" * 960])

        pipe.play_pcm48k(b"\x03\x04" * 960)
        output = bytearray(1920)
        sd.output_stream.kwargs["callback"](output, 960, None, None)
        self.assertEqual(output, b"\x03\x04" * 960)

        pipe.close()
        self.assertTrue(sd.input_stream.closed)
        self.assertTrue(sd.output_stream.closed)

    async def test_waits_for_jitter_prebuffer_before_playback(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=40,
            sounddevice_module=FakeSoundDevice(),
        )
        first = b"\x01\x00" * 960
        second = b"\x02\x00" * 960

        pipe.play_pcm48k(first, utterance_id='stream', final=False)
        output = bytearray(1920)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, b"\x00" * 1920)

        pipe.play_pcm48k(second, utterance_id='stream', final=True)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, first)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, second)

    async def test_short_utterance_is_released_by_continuous_webrtc_audio(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=400,
            sounddevice_module=FakeSoundDevice(),
        )
        short_reply = b"\x03\x00" * 960
        trailing_silence = b"\x00" * (19 * 1920)
        pipe.play_pcm48k(short_reply + trailing_silence)

        output = bytearray(1920)
        pipe._output_callback(output, 960, None, None)

        self.assertEqual(output, short_reply)
        self.assertTrue(pipe._playback_ready)

    async def test_mid_segment_underflow_uses_short_recovery_buffer(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=40,
            rebuffer_ms=40,
            sounddevice_module=FakeSoundDevice(),
        )
        first = b"\x01\x00" * 960
        second = b"\x02\x00" * 960
        pipe.play_pcm48k(first + second, utterance_id='stream', final=False)
        output = bytearray(1920)

        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, first)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, second)
        pipe._output_callback(output, 960, None, None)
        tapered = array("h")
        tapered.frombytes(output)
        self.assertGreater(tapered[0], 0)
        self.assertEqual(tapered[-1], 0)
        self.assertEqual(pipe.underrun_count, 1)
        self.assertEqual(pipe.rebuffer_count, 1)
        self.assertTrue(pipe._recovering)

        partial = b"\x03\x00" * 480
        pipe.play_pcm48k(partial, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, b"\x00" * 1920)
        self.assertEqual(bytes(pipe.output_buffer), partial)

        continuation = b"\x04\x00" * 1440
        pipe.play_pcm48k(continuation, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        recovered = array("h")
        recovered.frombytes(output)
        self.assertEqual(len(recovered), 960)
        self.assertLess(recovered[0], recovered[-1])
        self.assertFalse(pipe._recovering)

    async def test_underflow_is_smoothed_instead_of_hard_cut(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=0,
            fade_ms=8,
            sounddevice_module=FakeSoundDevice(),
        )
        frame = array("h", [12_000] * 960).tobytes()
        output = bytearray(1920)
        pipe.play_pcm48k(frame, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        pipe._output_callback(output, 960, None, None)

        samples = array("h")
        samples.frombytes(output)
        self.assertGreater(samples[0], 11_000)
        self.assertEqual(samples[-1], 0)
        self.assertLess(max(abs(b - a) for a, b in zip(samples, samples[1:])), 100)
        self.assertEqual(pipe.inserted_silence_frames, 960)

    async def test_default_recovery_resumes_after_one_complete_frame(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=20,
            sounddevice_module=FakeSoundDevice(),
        )
        output = bytearray(1920)
        first = array("h", [10_000] * 960).tobytes()
        resumed = array("h", [8_000] * 960).tobytes()

        pipe.play_pcm48k(first, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        pipe._output_callback(output, 960, None, None)
        self.assertTrue(pipe._recovering)

        pipe.play_pcm48k(resumed, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        samples = array("h")
        samples.frombytes(output)

        self.assertFalse(pipe._recovering)
        self.assertGreater(samples[-1], 7_500)
        self.assertNotEqual(output, b"\x00" * 1920)

    async def test_repeated_short_underflows_never_add_a_recovery_block(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=20,
            sounddevice_module=FakeSoundDevice(),
        )
        frame = array("h", [8_000] * 960).tobytes()
        output = bytearray(1920)

        for _ in range(41):
            pipe.play_pcm48k(frame, utterance_id='stream', final=False)
            pipe._output_callback(output, 960, None, None)
            self.assertNotEqual(output, b"\x00" * 1920)

            pipe._output_callback(output, 960, None, None)
            self.assertTrue(pipe._recovering)

        self.assertEqual(pipe.underrun_count, 41)
        self.assertEqual(pipe.rebuffer_count, 41)

    async def test_timeline_gap_deducts_silence_already_played(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=20,
            sounddevice_module=FakeSoundDevice(),
        )
        frame = array("h", [8_000] * 960).tobytes()
        output = bytearray(1920)

        pipe.play_pcm48k(frame, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        pipe._output_callback(output, 960, None, None)
        pipe._output_callback(output, 960, None, None)

        pipe.play_timeline_gap48k(2_880)
        pipe.play_pcm48k(frame, utterance_id='stream', final=False)
        pipe._output_callback(output, 960, None, None)
        self.assertEqual(output, b"\x00" * 1920)
        pipe._output_callback(output, 960, None, None)
        self.assertNotEqual(output, b"\x00" * 1920)

        diagnostics = pipe.diagnostics()
        self.assertEqual(diagnostics["timeline_gap_compensated_ms"], 40)
        self.assertEqual(diagnostics["timeline_gap_queued_ms"], 20)

    async def test_interrupt_uses_short_fade_out(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=0,
            fade_ms=8,
            sounddevice_module=FakeSoundDevice(),
        )
        output = bytearray(1920)
        pipe.play_pcm48k(array("h", [10_000] * 960).tobytes())
        pipe._output_callback(output, 960, None, None)
        pipe.clear_output()
        pipe._output_callback(output, 960, None, None)

        samples = array("h")
        samples.frombytes(output)
        self.assertGreater(samples[0], 9_000)
        self.assertEqual(samples[-1], 0)

    async def test_adaptive_clock_control_keeps_buffer_near_target(self) -> None:
        pipe = IPhoneAudioPipe(
            "BlackHole 2ch",
            "BlackHole 16ch",
            lambda payload: None,
            prebuffer_ms=100,
            adaptive_rate_percent=0.5,
            sounddevice_module=FakeSoundDevice(),
        )
        buffered_samples = 7_680
        pipe.play_pcm48k(array("h", range(buffered_samples)).tobytes())
        output = bytearray(1920)
        pipe._output_callback(output, 960, None, None)

        self.assertEqual(pipe.adaptive_speedup_count, 1)
        self.assertEqual(len(output), 1920)
        self.assertEqual(len(pipe.output_buffer) // 2, buffered_samples - 965)


class DialerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Never read the user's real CallServices state in a unit test. AX
        # success alone is now insufficient; provide an explicit state probe.
        probe = patch.object(IPhoneDialer, '_system_call_snapshot_since', return_value=('active', 'synthetic-call'))
        probe.start()
        self.addCleanup(probe.stop)

    async def test_incomplete_pre_dial_inspection_never_opens_or_clicks(self):
        for responses in ([75], [3, 75], [77], [3, 77]):
            with self.subTest(responses=responses):
                results = iter(responses)
                opener = AsyncMock()
                dialer = IPhoneDialer(opener=opener, process_finder=lambda _: [123],
                    helper_runner=lambda *a, **kw: SimpleNamespace(returncode=next(results)))
                with patch.object(dialer, '_press_recent_once', new=AsyncMock()) as recent:
                    with self.assertRaisesRegex(RuntimeError, '没有触发拨号'):
                        await dialer.dial('+8613800138000')
                    opener.assert_not_called()
                    recent.assert_not_awaited()

    async def test_confirmation_read_timeout_reobserves_without_double_click(self):
        state = {'value': 'sending'}
        async def confirm_call(*_):
            state['value'] = 'active'
        results = iter([subprocess.TimeoutExpired('read-only', 1), 75, 0])
        def helper(*args, **kwargs):
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 1)
            result = next(results)
            if isinstance(result, Exception): raise result
            return SimpleNamespace(returncode=result, stdout='', stderr='')
        dialer = IPhoneDialer(opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda _: [], helper_runner=helper, prompt_timeout=1,
            call_state_probe=lambda _: state['value'], active_timeout=2)
        with patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=123)), \
             patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
             patch.object(dialer, '_press_recent_once', new=AsyncMock()) as recent, \
             patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock(side_effect=confirm_call)) as confirm:
            await dialer.dial('+8613800138000')
        recent.assert_awaited_once()
        confirm.assert_awaited_once()

    async def test_continuous_unavailable_confirmation_stops_without_click(self):
        dialer = IPhoneDialer(helper_runner=lambda *a, **kw: SimpleNamespace(returncode=75),
                             prompt_timeout=.5)
        with patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock()) as confirm:
            with self.assertRaisesRegex(RuntimeError, '没有确认拨号'):
                await asyncio.wait_for(dialer._wait_for_phone_confirmation(123), 1)
            confirm.assert_not_awaited()

    async def test_confirmation_press_timeout_is_not_retried(self):
        helper = Mock(side_effect=subprocess.TimeoutExpired('press', 1))
        dialer = IPhoneDialer(helper_runner=helper)
        with self.assertRaises(subprocess.TimeoutExpired):
            await dialer._press_phone_confirmation_once(123)
        helper.assert_called_once()

    async def test_bound_active_is_read_before_unrelated_slow_failure_ui(self):
        probes = []
        def helper(*args, **kwargs):
            probes.append('unbound-ui')
            time.sleep(.08)
            return SimpleNamespace(returncode=4, stdout='old call failed')
        dialer = IPhoneDialer(process_finder=lambda _: [123], helper_runner=helper,
                             call_state_probe=lambda _: 'active', active_timeout=.5)
        await dialer._wait_for_active_call(123, time.time())
        self.assertEqual(probes, [])

    async def test_stale_failure_banner_cannot_abort_a_sending_call(self):
        states = iter(['sending', 'active'])
        dialer = IPhoneDialer(process_finder=lambda _: [123],
            helper_runner=lambda *a, **kw: SimpleNamespace(returncode=4, stdout='old call failed'),
            call_state_probe=lambda _: next(states), active_timeout=1)
        await dialer._wait_for_active_call(123, time.time())

    async def test_failed_confirmation_retains_observation_started_before_recent_click(self):
        observed = []
        dialer = IPhoneDialer(opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda _: [], call_state_probe=lambda _: 'unknown', active_timeout=.5)
        # The recent-row action may already create Sending. Retain that
        # boundary even if the later confirmation cannot be inspected.
        async def recent(*_):
            self.assertIsNotNone(dialer.call_started_at)
            observed.append(dialer.call_started_at)
        with patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=123)), \
             patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
             patch.object(dialer, '_press_recent_once', new=AsyncMock(side_effect=recent)), \
             patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock(side_effect=RuntimeError('missing confirmation'))), \
             patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock()) as confirm:
            with self.assertRaisesRegex(RuntimeError, 'missing confirmation'):
                await dialer.dial('+8613800138000')
        self.assertEqual(observed, [dialer.call_started_at])
        confirm.assert_not_awaited()

    async def test_sending_created_by_recent_row_before_confirmation_is_observed(self):
        # Replay the silent call's actual ordering with an anonymous identity.
        # Every UI action and system-log read is fake: this never places a call.
        identity = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        def timestamp(value):
            return datetime.fromisoformat(value).timestamp()
        before_recent = timestamp('2026-09-06 01:20:51.908000+08:00')
        sending_at = timestamp('2026-09-06 01:20:52.073635+08:00')
        confirmation_at = timestamp('2026-09-06 01:20:52.431519+08:00')
        clock = [before_recent]
        events = []
        intents = []
        def event(stamp, state):
            return {'timestamp': stamp,
                    'eventMessage': f'TUCallCenterCallStatusChangedNotification '
                                    f'uPI={identity} stat={state}'}
        async def press_recent(*_):
            events.append(event('2026-09-06 01:20:52.073635+0800', 'Sending'))
            clock[0] = sending_at
        async def wait_confirmation(*_):
            clock[0] = confirmation_at
        async def press_confirmation(*_):
            events.append(event('2026-09-06 01:21:00.027515+0800', 'Active'))
        def snapshot(started_at, call_uuid=''):
            return IPhoneDialer._state_from_call_events(events, started_at, call_uuid)
        dialer = IPhoneDialer(
            opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda name: [456] if name == IPhoneDialer.PHONE_PROCESS else [],
            helper_runner=lambda command, **kw: SimpleNamespace(
                returncode=65 if 'has-phone-confirmation' in command else 3, stdout='pending'),
            on_dial_request=intents.append,
            active_timeout=.5,
        )
        with patch('iphone_audio.time.time', side_effect=lambda: clock[0]), \
             patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
             patch.object(dialer, '_press_recent_once', new=AsyncMock(side_effect=press_recent)) as recent, \
             patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock(side_effect=wait_confirmation)), \
             patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock(side_effect=press_confirmation)) as confirm, \
             patch.object(IPhoneDialer, '_system_call_snapshot_since', side_effect=snapshot):
            await dialer.dial('+8613800138000')
            self.assertEqual(dialer.system_call_uuid, identity)
            self.assertEqual(dialer.call_started_at, before_recent)
            self.assertEqual(intents, [before_recent])
            self.assertIn('system_call_active', dialer.timing_ms)
            events.append(event('2026-09-06 01:21:17.497095+0800', 'Disconnected'))
            self.assertTrue(await dialer.wait_for_disconnect(0))
            recent.assert_awaited_once()
            confirm.assert_awaited_once()

    def test_observation_does_not_backdate_or_guess_an_unrelated_call(self):
        first = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        other = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        stamp = datetime.fromisoformat('2026-09-06 01:20:51.908000+08:00').timestamp()
        def event(at, identity, state):
            return {'timestamp': at, 'eventMessage':
                    f'TUCallCenterCallStatusChangedNotification uPI={identity} stat={state}'}
        events = [
            event('2026-09-06 01:20:51.800000+0800', other, 'Sending'),
            event('2026-09-06 01:21:00.000000+0800', other, 'Active'),
        ]
        self.assertEqual(IPhoneDialer._state_from_call_events(events, stamp), ('unknown', ''))
        events.append(event('2026-09-06 01:20:52.073635+0800', first, 'Sending'))
        self.assertEqual(IPhoneDialer._state_from_call_events(events, stamp), ('sending', first))
        events.append(event('2026-09-06 01:21:17.497095+0800', other, 'Disconnected'))
        self.assertEqual(IPhoneDialer._state_from_call_events(events, stamp), ('sending', first))

    async def test_rejected_confirmation_keeps_possible_call_but_never_confirms(self):
        def reject(_):
            raise RuntimeError('voice no longer ready')
        dialer = IPhoneDialer(on_dial_request=reject,
            opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda _: [], call_state_probe=lambda _: 'unknown', active_timeout=.5)
        # Only exercise the UI order using mocked helpers, never Phone.app.
        with patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=456)), \
             patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
             patch.object(dialer, '_press_recent_once', new=AsyncMock()), \
             patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock()), \
             patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock()) as press:
            with self.assertRaisesRegex(RuntimeError, 'voice no longer ready'):
                await dialer.dial('+8613800138000')
            press.assert_not_called()
            self.assertIsNotNone(dialer.call_started_at)

    def test_compiled_helper_keeps_action_position_for_safe_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "phone_ax_helper"
            helper.write_text("binary", encoding="utf-8")
            helper.chmod(0o700)
            calls: list[list[str]] = []

            def runner(command: list[str], **kwargs: Any) -> Any:
                del kwargs
                calls.append(command)
                return SimpleNamespace(returncode=3, stdout="pending", stderr="")

            with patch.object(IPhoneDialer, "COMPILED_AX_HELPER", helper):
                IPhoneDialer(helper_runner=runner)._run_helper("call-status", 123)

            self.assertEqual(calls[0][1:4], ["--compiled", "call-status", "123"])

    async def test_uses_url_then_presses_once_and_verifies_active_call(self) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []
        helper_calls: list[list[str]] = []
        opened = False
        confirmed = False
        intents: list[float] = []

        def opener(command: list[str], **kwargs: Any) -> Any:
            nonlocal opened
            calls.append((command, kwargs))
            opened = True
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name == IPhoneDialer.PROMPT_PROCESS:
                return [123] if opened else []
            if name == IPhoneDialer.PHONE_PROCESS:
                return [456]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            nonlocal confirmed
            helper_calls.append(command)
            if not opened:
                return SimpleNamespace(returncode=65 if 'has-phone-confirmation' in command else 3,
                                       stdout='inactive', stderr='')
            if command[2] == 'press-phone-confirmation':
                self.assertEqual(len(intents), 1)
                confirmed = True
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        await IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
            on_dial_request=intents.append,
            call_state_probe=lambda _: 'active' if confirmed else 'sending',
        ).dial("+8613800138000")
        self.assertEqual(
            calls[0][0],
            [
                "/usr/bin/open",
                "/System/Applications/Phone.app",
            ],
        )
        self.assertFalse(calls[0][1]["check"])
        self.assertEqual(
            [call[2] for call in helper_calls],
            [
                "is-call-active",
                "has-phone-confirmation",
                "recent-signature",
                "press-recent-call",
                "has-phone-confirmation",
                "press-phone-confirmation",
            ],
        )
        self.assertEqual(helper_calls[2][-1], "+8613800138000")
        self.assertEqual(helper_calls[3][-1], "+8613800138000")
        self.assertFalse(any(".xpc" in part for call in helper_calls for part in call))
        self.assertFalse(any("mobilephone-recents:" in part for part in calls[0][0]))

    async def test_existing_prompt_blocks_duplicate_request(self) -> None:
        calls: list[list[str]] = []

        def opener(command: list[str], **kwargs: Any) -> Any:
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name in {
                IPhoneDialer.PROMPT_PROCESS,
                IPhoneDialer.PHONE_PROCESS,
            }:
                return [123]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            action = command[2]
            code = 0 if action == "has-phone-confirmation" else 3
            return SimpleNamespace(returncode=code, stdout="", stderr="")

        dialer = IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
        )

        with self.assertRaisesRegex(RuntimeError, "拒绝再次触发"):
            await dialer.dial("+8613800138000")
        self.assertEqual(calls, [])

    async def test_existing_call_without_notification_process_still_blocks(self):
        calls = []
        dialer = IPhoneDialer(
            opener=lambda *args,**kwargs:calls.append(args),
            process_finder=lambda name:[123] if name == IPhoneDialer.PHONE_PROCESS else [],
            helper_runner=lambda *args,**kwargs:SimpleNamespace(returncode=0))
        with self.assertRaisesRegex(RuntimeError,'拒绝再次触发'):
            await dialer.dial('+8613800138000')
        self.assertEqual(calls, [])

    async def test_stale_notification_process_does_not_block_call(self) -> None:
        calls: list[list[str]] = []
        pressed_recent = False

        def opener(command: list[str], **kwargs: Any) -> Any:
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name in {
                IPhoneDialer.PROMPT_PROCESS,
                IPhoneDialer.PHONE_PROCESS,
            }:
                return [123]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            nonlocal pressed_recent
            action = command[2]
            if action == "press-recent-call":
                pressed_recent = True
                return SimpleNamespace(returncode=0, stdout="pressed", stderr="")
            if action == "has-phone-confirmation":
                code = 0 if pressed_recent else 65
                return SimpleNamespace(returncode=code, stdout="", stderr="")
            if action == "is-call-active":
                return SimpleNamespace(returncode=3, stdout="inactive", stderr="")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        await IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
        ).dial("+8613800138000")

        self.assertEqual(len(calls), 1)
        self.assertTrue(pressed_recent)

    async def test_failed_press_is_never_retried(self) -> None:
        opened = False
        helper_actions: list[str] = []

        def opener(command: list[str], **kwargs: Any) -> Any:
            nonlocal opened
            opened = True
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name == IPhoneDialer.PROMPT_PROCESS:
                return []
            if name == IPhoneDialer.PHONE_PROCESS and opened:
                return [456]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            action = command[2]
            helper_actions.append(action)
            if action == "press-recent-call":
                return SimpleNamespace(returncode=66, stdout="", stderr="blocked")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        dialer = IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
            call_state_probe=lambda _: 'unknown', active_timeout=.5,
        )
        with self.assertRaisesRegex(RuntimeError, "不会重试"):
            await dialer.dial("+8613800138000")
        self.assertEqual(helper_actions.count("press-recent-call"), 1)

    async def test_unbound_failure_alert_without_system_state_cannot_prove_connection(self) -> None:
        opened = False
        helper_actions: list[str] = []

        def opener(command: list[str], **kwargs: Any) -> Any:
            nonlocal opened
            opened = True
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name == IPhoneDialer.PROMPT_PROCESS:
                return []
            if name == IPhoneDialer.PHONE_PROCESS and opened:
                return [456]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            action = command[2]
            helper_actions.append(action)
            if action == "call-status":
                return SimpleNamespace(
                    returncode=4,
                    stdout="呼叫失败：iPhone 电话接力不可用",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        dialer = IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
            call_state_probe=lambda _: 'unknown', active_timeout=.5,
        )
        with self.assertRaisesRegex(RuntimeError, "没有观察到系统真实通话状态"):
            await dialer.dial("+8613800138000")

        self.assertEqual(helper_actions.count("press-recent-call"), 1)
        self.assertEqual(helper_actions.count("press-phone-confirmation"), 1)
        self.assertEqual(helper_actions.count("call-status"), 0)  # UI is not state evidence.

    async def test_system_call_state_detects_answer_when_phone_ax_is_pending(
        self,
    ) -> None:
        helper_actions: list[str] = []
        system_states = iter(["sending", "active"])
        pressed_recent = False

        def opener(command: list[str], **kwargs: Any) -> Any:
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name == IPhoneDialer.PHONE_PROCESS:
                return [456]
            if name == IPhoneDialer.PROMPT_PROCESS:
                return [789]
            if name == IPhoneDialer.NOTIFICATION_PROCESS:
                return [790]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            nonlocal pressed_recent
            action = command[2]
            helper_actions.append(action)
            if action == "press-recent-call":
                pressed_recent = True
                return SimpleNamespace(returncode=0, stdout="pressed", stderr="")
            if action == "has-phone-confirmation" and not pressed_recent:
                return SimpleNamespace(returncode=65, stdout="", stderr="")
            if action == "call-status":
                return SimpleNamespace(returncode=3, stdout="pending", stderr="")
            if action == "is-call-active":
                return SimpleNamespace(returncode=3, stdout="inactive", stderr="")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        def call_state_probe(started_at: float) -> str:
            self.assertGreater(started_at, 0)
            return next(system_states)

        await IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
            call_state_probe=call_state_probe,
            active_timeout=2,
        ).dial("+8613800138000")

        self.assertEqual(helper_actions.count("press-recent-call"), 1)
        self.assertEqual(helper_actions.count("press-phone-confirmation"), 0)  # Already Active.
        self.assertEqual(helper_actions.count("call-status"), 0)

    async def test_system_disconnection_is_not_treated_as_connected(self) -> None:
        helper_actions: list[str] = []
        opened = False

        def opener(command: list[str], **kwargs: Any) -> Any:
            nonlocal opened
            opened = True
            return SimpleNamespace(returncode=0, stderr="")

        def process_finder(name: str) -> list[int]:
            if name == IPhoneDialer.PHONE_PROCESS:
                return [456]
            return []

        def helper_runner(command: list[str], **kwargs: Any) -> Any:
            action = command[2]
            helper_actions.append(action)
            if not opened:
                return SimpleNamespace(returncode=65 if action == 'has-phone-confirmation' else 3,
                                       stdout='inactive',stderr='')
            if action == "call-status":
                return SimpleNamespace(returncode=3, stdout="pending", stderr="")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        dialer = IPhoneDialer(
            opener=opener,
            process_finder=process_finder,
            helper_runner=helper_runner,
            call_state_probe=lambda started_at: "disconnected",
        )
        with self.assertRaisesRegex(RuntimeError, "已断开"):
            await dialer.dial("+8613800138000")

        self.assertEqual(helper_actions.count("press-recent-call"), 1)
        self.assertEqual(helper_actions.count("press-phone-confirmation"), 0)  # Already Disconnected.

    async def test_wait_for_disconnect_stops_on_terminal_system_state(self) -> None:
        states = iter(["active", "active", "disconnected"])
        dialer = IPhoneDialer(
            call_state_probe=lambda started_at: next(states),
        )
        dialer.call_started_at = time.time()

        self.assertTrue(
            await dialer.wait_for_disconnect(2, poll_interval=0.01)
        )

    async def test_rejects_non_e164_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "E.164"):
            await IPhoneDialer().dial("13800138000")


class PhoneAxReadPolicyTests(unittest.TestCase):
    @unittest.skipUnless(platform.system() == 'Darwin' and Path('/usr/bin/swift').is_file(), 'macOS Swift required')
    def test_ipc_timeout_never_outlives_the_original_inspection_budget(self):
        source = IPhoneDialer.AX_HELPER.read_text()
        function = re.search(r'func inspectionMessageTimeout\(.*?\n\}', source, re.S)
        self.assertIsNotNone(function)
        program = function.group(0) + '''
precondition(inspectionMessageTimeout(remaining: 1.5) == 0.75)
precondition(inspectionMessageTimeout(remaining: 0.5) == 0.5)
precondition(inspectionMessageTimeout(remaining: 0.125) == 0.125)
precondition(inspectionMessageTimeout(remaining: 0) == 0)
precondition(inspectionMessageTimeout(remaining: -0.1) == 0)
'''
        result = subprocess.run(['/usr/bin/swift', '-'], input=program,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('inspectionDeadline - ProcessInfo.processInfo.systemUptime', source)
        self.assertIn('inspectionMessageTimeout(remaining: remaining)', source)

    @unittest.skipUnless(platform.system() == 'Darwin' and Path('/usr/bin/swift').is_file(), 'macOS Swift required')
    def test_only_non_actionable_group_value_is_excluded(self):
        source = IPhoneDialer.AX_HELPER.read_text()
        function = re.search(r'func shouldReadValueAttribute\(.*?\n\}', source, re.S)
        self.assertIsNotNone(function)
        program = 'import ApplicationServices\n' + function.group(0) + '''
precondition(!shouldReadValueAttribute(role: "AXGroup", actions: []))
precondition(shouldReadValueAttribute(role: "AXGroup", actions: ["AXPress"]))
precondition(shouldReadValueAttribute(role: "AXGroup", actions: ["AXShowMenu"]))
for role in ["AXButton", "AXStaticText", "AXTextField", "AXTextArea", "AXUnknown"] {
    precondition(shouldReadValueAttribute(role: role, actions: []))
}
'''
        result = subprocess.run(['/usr/bin/swift', '-'], input=program,
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('value: shouldReadValueAttribute(role: role, actions: actions)', source)
        self.assertIn('checkReadError(error)', source)


class DialStateRaceTests(unittest.IsolatedAsyncioTestCase):
    """Possible-call UI failures never stand in for the bound system state."""

    def fixture(self, state_probe, *, timeout=1):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        dialer = IPhoneDialer(
            opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda _: [],
            helper_runner=lambda *a, **kw: (_ for _ in ()).throw(AssertionError('real helper forbidden')),
            call_state_probe=state_probe, active_timeout=timeout)
        stack.enter_context(patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=123)))
        stack.enter_context(patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()))
        mocks = {}
        for name in ('_press_recent_once', '_wait_for_phone_confirmation', '_press_phone_confirmation_once'):
            mocks[name] = stack.enter_context(patch.object(dialer, name, new=AsyncMock()))
        return dialer, mocks

    async def test_confirmation_trace_distinguishes_missing_ambiguous_and_ready_without_contents(self):
        dialer = IPhoneDialer(prompt_timeout=1)
        dialer._dial_started_monotonic = time.monotonic()
        replies = iter([
            SimpleNamespace(returncode=65, stdout='private phone number', stderr='private UI'),
            SimpleNamespace(returncode=75, stdout='private phone number',
                stderr='expected exactly one Phone.app communication-audio button, found 2'),
            SimpleNamespace(returncode=0, stdout='private phone number', stderr=''),
        ])
        with patch.object(dialer, '_run_helper', side_effect=lambda *a, **kw: next(replies)) as helper:
            await dialer._wait_for_phone_confirmation(123)
        trace = dialer.dial_diagnostics['confirmation_reads']
        self.assertEqual([row['result'] for row in trace], ['not_visible', 'ambiguous', 'unique_confirmation'])
        self.assertNotIn('private', str(trace))
        self.assertEqual(helper.call_count, 3)
        self.assertTrue(all(call.args[0] == 'has-phone-confirmation' for call in helper.call_args_list))

    async def test_ax_error_is_not_ambiguous_and_reobservation_never_clicks(self):
        dialer = IPhoneDialer(prompt_timeout=1)
        dialer._dial_started_monotonic = time.monotonic()
        replies = iter([
            SimpleNamespace(returncode=75, stdout='private contact', stderr=
                'Phone.app accessibility inspection unavailable (-25200); attribute=AXValue; no action performed'),
            SimpleNamespace(returncode=0, stdout='phone-confirmation-ready', stderr=''),
        ])
        with patch.object(dialer, '_run_helper', side_effect=lambda *a, **kw: next(replies)) as helper:
            await dialer._wait_for_phone_confirmation(123)
        first = dialer.dial_diagnostics['confirmation_reads'][0]
        self.assertEqual(first['result'], 'read_unavailable')
        self.assertEqual(first['ax_error_code'], -25200)
        self.assertEqual(first['attribute'], 'AXValue')
        self.assertNotIn('private', str(dialer.dial_diagnostics))
        self.assertEqual([call.args[0] for call in helper.call_args_list],
                         ['has-phone-confirmation', 'has-phone-confirmation'])

    def test_confirmation_error_diagnostic_is_strict_and_content_free(self):
        cases = [
            ('Phone.app accessibility inspection unavailable (-25200); no action performed',
             {'result': 'read_unavailable', 'ax_error_code': -25200}),
            ('Phone.app accessibility inspection timed out; no action performed',
             {'result': 'inspection_timeout'}),
            ('expected exactly one Phone.app communication-audio button, found 2',
             {'result': 'ambiguous', 'candidate_count': 2}),
            ('private contact: expected exactly one Phone.app communication-audio button, found 2',
             {'result': 'read_unavailable'}),
            ('Phone.app accessibility inspection unavailable (-25200); attribute=private_contact; no action performed',
             {'result': 'read_unavailable'}),
            ('expected exactly one Phone.app communication-audio button, found 9000',
             {'result': 'read_unavailable'}),
            ('unknown private UI', {'result': 'read_unavailable'}),
        ]
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                actual = confirmation_read_diagnostic(SimpleNamespace(
                    returncode=75, stdout='private contact', stderr=stderr))
                self.assertEqual(actual, {'exit_code': 75, **expected})
                self.assertNotIn('private', str(actual))

    async def test_cancelled_confirmation_read_is_retained_without_late_action(self):
        dialer = IPhoneDialer(prompt_timeout=1)
        dialer._dial_started_monotonic = time.monotonic()
        began, release = threading.Event(), threading.Event()
        def read(*args, **kwargs):
            began.set()
            release.wait(1)
            return SimpleNamespace(returncode=0)
        with patch.object(dialer, '_run_helper', side_effect=read):
            task = asyncio.create_task(dialer._wait_for_phone_confirmation(123))
            try:
                await asyncio.to_thread(began.wait, .5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(dialer.dial_diagnostics['confirmation_reads'][0]['result'], 'read_cancelled')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_confirmation_read_error_keeps_waiting_for_the_original_call(self):
        ready_at = time.monotonic() + .12
        dialer, mocks = self.fixture(lambda _: 'active' if time.monotonic() >= ready_at else 'sending')
        mocks['_wait_for_phone_confirmation'].side_effect = RuntimeError('AX inspection unavailable')
        await dialer.dial('+8613800138000')
        mocks['_press_recent_once'].assert_awaited_once()
        mocks['_press_phone_confirmation_once'].assert_not_awaited()
        self.assertIn('system_call_active', dialer.timing_ms)

    async def test_bound_active_cancels_a_stuck_confirmation_read(self):
        state = {'value': 'sending'}
        waiting = asyncio.Event()
        cancelled = asyncio.Event()
        async def read(*_):
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        dialer, mocks = self.fixture(lambda _: state['value'])
        mocks['_wait_for_phone_confirmation'].side_effect = read
        task = asyncio.create_task(dialer.dial('+8613800138000'))
        try:
            await asyncio.wait_for(waiting.wait(), .5)
            state['value'] = 'active'
            await asyncio.wait_for(task, .6)
            self.assertTrue(cancelled.is_set())
            mocks['_press_phone_confirmation_once'].assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_active_when_confirmation_appears_never_clicks_it(self):
        state = {'value': 'sending'}
        async def read(*_):
            state['value'] = 'active'
        dialer, mocks = self.fixture(lambda _: state['value'])
        mocks['_wait_for_phone_confirmation'].side_effect = read
        await dialer.dial('+8613800138000')
        mocks['_press_phone_confirmation_once'].assert_not_awaited()

    async def test_confirmation_click_timeout_is_observed_not_repeated(self):
        state = {'value': 'sending'}
        async def press(*_):
            state['value'] = 'active'
            raise subprocess.TimeoutExpired('synthetic one-time click', 1)
        dialer, mocks = self.fixture(lambda _: state['value'])
        mocks['_press_phone_confirmation_once'].side_effect = press
        await dialer.dial('+8613800138000')
        mocks['_press_recent_once'].assert_awaited_once()
        mocks['_press_phone_confirmation_once'].assert_awaited_once()

    async def test_recent_click_error_can_still_belong_to_an_active_call(self):
        state = {'value': 'unknown'}
        async def recent(*_):
            state['value'] = 'active'
            raise RuntimeError('click result unavailable after action')
        dialer, mocks = self.fixture(lambda _: state['value'])
        mocks['_press_recent_once'].side_effect = recent
        await dialer.dial('+8613800138000')
        mocks['_press_recent_once'].assert_awaited_once()
        mocks['_press_phone_confirmation_once'].assert_not_awaited()

    async def test_disconnected_before_confirmation_never_confirms_or_succeeds(self):
        dialer, mocks = self.fixture(lambda _: 'disconnected')
        with self.assertRaisesRegex(RuntimeError, '已断开'):
            await dialer.dial('+8613800138000')
        mocks['_press_phone_confirmation_once'].assert_not_awaited()

    async def test_unknown_call_and_ui_failure_have_one_bounded_deadline(self):
        dialer, mocks = self.fixture(lambda _: 'unknown', timeout=.5)
        mocks['_wait_for_phone_confirmation'].side_effect = RuntimeError('no confirmation')
        began = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, '没有观察到系统真实通话状态'):
            await asyncio.wait_for(dialer.dial('+8613800138000'), 1)
        self.assertGreaterEqual(time.monotonic() - began, .45)
        mocks['_press_recent_once'].assert_awaited_once()
        mocks['_press_phone_confirmation_once'].assert_not_awaited()
        self.assertIsNotNone(dialer.call_started_at)

    async def test_cancellation_during_confirmation_never_clicks_later(self):
        reading, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def blocked_helper():
            loop.call_soon_threadsafe(reading.set)
            release.wait(1)
            loop.call_soon_threadsafe(finished.set)
        async def read(*_):
            # Cancelling to_thread cannot stop an already dispatched AX read.
            # Its late return must never resume the next confirmation click.
            await asyncio.to_thread(blocked_helper)
        dialer, mocks = self.fixture(lambda _: 'sending')
        mocks['_wait_for_phone_confirmation'].side_effect = read
        task = asyncio.create_task(dialer.dial('+8613800138000'))
        try:
            await asyncio.wait_for(reading.wait(), .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.wait_for(finished.wait(), .5)
            await asyncio.sleep(.02)
            mocks['_press_phone_confirmation_once'].assert_not_awaited()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unready_voice_forbids_confirmation_but_observes_existing_call(self):
        state = {'value': 'sending'}
        dialer, mocks = self.fixture(lambda _: state['value'])
        def no_longer_ready(_):
            state['value'] = 'active'
            raise RuntimeError('voice no longer ready')
        dialer.on_dial_request = no_longer_ready
        await dialer.dial('+8613800138000')
        mocks['_press_phone_confirmation_once'].assert_not_awaited()

    async def test_other_calls_active_state_cannot_resolve_our_sending_call(self):
        own, other = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        def probe(started):
            stamp = datetime.fromtimestamp(started + .001).astimezone().strftime('%Y-%m-%d %H:%M:%S.%f%z')
            events = [{'timestamp': stamp, 'eventMessage':
                       f'TUCallCenterCallStatusChangedNotification uPI={identity} stat={state}'}
                      for identity, state in ((own, 'Sending'), (other, 'Active'), (other, 'Disconnected'))]
            result, bound = IPhoneDialer._state_from_call_events(events, started)
            self.assertEqual(bound, own)
            return result
        dialer, mocks = self.fixture(probe, timeout=.5)
        mocks['_wait_for_phone_confirmation'].side_effect = RuntimeError('unavailable')
        with self.assertRaisesRegex(RuntimeError, '没有接通'):
            await asyncio.wait_for(dialer.dial('+8613800138000'), 1)
        mocks['_press_phone_confirmation_once'].assert_not_awaited()

    async def test_slow_state_read_cannot_revive_an_expired_attempt(self):
        def slow_probe(_):
            time.sleep(.7)
            return 'active'
        dialer, mocks = self.fixture(slow_probe, timeout=.5)
        began = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, '没有观察到系统真实通话状态'):
            await asyncio.wait_for(dialer.dial('+8613800138000'), .65)
        self.assertLess(time.monotonic()-began, .65)
        await asyncio.sleep(.25)
        mocks['_press_phone_confirmation_once'].assert_not_awaited()
        self.assertNotIn('system_call_active', dialer.timing_ms)


if __name__ == "__main__":
    unittest.main()
