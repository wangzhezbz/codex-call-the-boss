"""Output-only evidence guards. No telephone, model request or real devices."""
import asyncio
import json
import stat
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from phone_output_probe import PhoneOutputProbe, clock_summary
from session_registry import enable_session, disable_session, set_output_probe, output_probe_seconds
from iphone_audio import IPhoneAudioPipe
from phone_agent import IPhoneVoiceBridge, PendingCall


class Stream:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.closed = False
    def start(self): pass
    def stop(self): pass
    def close(self): self.closed = True


class Device:
    def __init__(self):
        self.inputs = []
        self.outputs = []
    def query_devices(self):
        return [{'name': 'caller', 'max_input_channels': 2, 'max_output_channels': 2},
                {'name': 'assistant-feed', 'max_input_channels': 16, 'max_output_channels': 16}]
    def RawInputStream(self, **kwargs):
        stream = Stream(**kwargs)
        self.inputs.append(stream)
        return stream
    def RawOutputStream(self, **kwargs):
        stream = Stream(**kwargs)
        self.outputs.append(stream)
        return stream


def timing(stamp):
    return SimpleNamespace(currentTime=stamp-.12, outputBufferDacTime=stamp,
                           inputBufferAdcTime=stamp)


class OutputProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Retained fixtures, no recursive deletion.
        self.base = Path(tempfile.mkdtemp(prefix='phone-output-probe-test-'))

    def make(self, **kwargs):
        return PhoneOutputProbe(self.base, sounddevice_module=Device(), device=1, **kwargs)

    def test_only_active_call_audio_is_saved_and_input_is_bounded(self):
        probe = self.make(seconds=1)
        probe.start()
        pcm = b'\x01\x10' * 960
        probe.output(pcm, 960, timing(0), False)
        probe._capture(pcm, 960, timing(0), False)
        self.assertEqual(probe.data['output'], b'')
        probe.activate()
        for i in range(100):
            probe.output(pcm, 960, timing(i*.02), False)
            probe._capture(pcm, 960, timing(i*.02), False)
        probe.close(ledger=[{'id': 'owned-output'}])
        self.assertTrue(probe.saved)
        self.assertEqual(probe.summary()['frames'], {'feed': 48000, 'output': 48000})
        self.assertEqual(probe.truncated, {'feed': True, 'output': True})
        record = json.loads((probe.directory/'probe.json').read_text())
        self.assertFalse(record['human_listening_verified'])
        self.assertEqual(record['playback_ledger'], [{'id': 'owned-output'}])
        self.assertEqual(record['clock_summary']['feed']['device_gap_count_over_1ms'], 0)
        for kind in ('output', 'feed'):
            with wave.open(str(probe.directory/(kind+'.wav'))) as stream:
                self.assertEqual(stream.readframes(48000), pcm*50)
        for path in probe.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        probe.output(pcm, 960, timing(3), False)
        self.assertEqual(len(probe.data['output']), 96000)

    def test_missing_or_bad_clocks_do_not_throw_or_claim_valid_timing(self):
        probe = self.make()
        probe.start(); probe.activate()
        for stamp in ({}, {'outputBufferDacTime': float('nan')}, None):
            probe.output(b'\0\0'*960, 960, stamp, False)
        probe.close()
        record = json.loads((probe.directory/'probe.json').read_text())
        self.assertEqual(record['clock_summary']['output']['valid_device_intervals'], 0)
        self.assertIsNone(record['clock_summary']['output']['max_abs_device_gap_ms'])

    def test_wall_scheduling_delay_does_not_imply_a_dac_gap(self):
        rows = [{'frames':960, 'device_time':i*.02, 'wall_time':wall}
                for i, wall in enumerate((0, .02, .105, .106))]
        summary = clock_summary(rows, 48000)
        self.assertEqual(summary['device_gap_count_over_1ms'], 0)
        self.assertGreater(summary['max_positive_wall_gap_ms'], 64)
        rows[-1]['device_time'] += .04
        self.assertEqual(clock_summary(rows, 48000)['device_gap_count_over_1ms'], 1)

    def test_probe_failure_does_not_fail_audio_output_and_closes_stream(self):
        probe = self.make()
        probe.sd.RawInputStream = Mock(side_effect=OSError('not available'))
        probe.start(); probe.activate()
        self.assertFalse(probe.active)
        self.assertGreater(probe.error_count, 0)
        self.assertTrue(probe.closed)
        self.assertFalse(probe.summary()['activated'])

    def test_save_failure_is_not_success_and_close_is_idempotent(self):
        probe = self.make()
        probe.start(); probe.activate()
        with patch('phone_output_probe.atomic_write', side_effect=OSError('disk full')):
            probe.close()
        self.assertFalse(probe.saved)
        self.assertTrue(probe.stream.closed)
        errors = probe.error_count
        probe.close()
        self.assertEqual(probe.error_count, errors)

    def test_metadata_and_invalid_callback_errors_are_bounded(self):
        probe = self.make(seconds=120)
        probe.start(); probe.activate()
        for i in range(9000):
            probe.output(b'\0\0', 1, timing(i/48000), False)
        self.assertEqual(len(probe.clocks['output']), 8192)
        self.assertTrue(probe.truncated['output'])
        for _ in range(40):
            probe._capture(b'odd', 10, timing(0), False)
        self.assertEqual(len(probe.errors), 16)
        probe.close()

    async def test_pipe_probe_reads_feed_not_caller_and_preserves_output_bytes(self):
        device = Device()
        caller = Mock()
        pipe = IPhoneAudioPipe('caller', 'assistant-feed', caller,
            prebuffer_ms=0, adaptive_rate_percent=0, output_probe_dir=self.base,
            sounddevice_module=device)
        pipe.start()
        self.assertEqual([stream.options['device'] for stream in device.inputs], [0, 1])
        pcm = b'\x01\x10' * 1920
        pipe.activate_output_probe()
        pipe.play_pcm48k(pcm, utterance_id='whole-clip')
        rendered = bytearray()
        for i in range(2):
            chunk = bytearray(1920)
            pipe._output_callback(chunk, 960, timing(i*.02), False)
            rendered.extend(chunk)
            device.inputs[1].options['callback'](chunk, 960, timing(i*.02), False)
        self.assertEqual(rendered, pcm)
        caller.assert_not_called()
        pipe.close()
        self.assertTrue(pipe.output_probe_diagnostics()['saved'])
        self.assertTrue(all(stream.closed for stream in device.inputs + device.outputs))

    async def test_default_does_not_open_an_extra_stream_or_directory(self):
        device = Device()
        pipe = IPhoneAudioPipe('caller', 'assistant-feed', lambda _: None,
                               sounddevice_module=device)
        pipe.start(); pipe.activate_output_probe(); pipe.close()
        self.assertEqual(len(device.inputs), 1)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_policy_is_exact_session_scoped_disabled_by_default_and_validated(self):
        registry = self.base/'sessions.json'
        enable_session('source-a12345678', path=registry)
        enable_session('source-b12345678', path=registry)
        self.assertEqual(output_probe_seconds('source-a12345678', path=registry), 0)
        set_output_probe('source-a12345678', 60, path=registry)
        self.assertEqual(output_probe_seconds('source-a12345678', path=registry), 60)
        self.assertEqual(output_probe_seconds('source-b12345678', path=registry), 0)
        for bad in (True, -1, 121, 1.5, '60'):
            with self.assertRaises(ValueError):
                set_output_probe('source-a12345678', bad, path=registry)
        disable_session('source-a12345678', path=registry)
        self.assertEqual(output_probe_seconds('source-a12345678', path=registry), 0)
        with self.assertRaises(ValueError):
            set_output_probe('source-a12345678', 60, path=registry)
        with self.assertRaises(ValueError):
            set_output_probe('missing-12345678', 60, path=registry)

    def test_bridge_requires_a_real_job_and_its_exact_enabled_source(self):
        registry = self.base/'sessions.json'
        enable_session('source-a12345678', path=registry)
        set_output_probe('source-a12345678', 60, path=registry)
        source = self.base/'calling'/'job.json'
        source.parent.mkdir()
        daemon = SimpleNamespace(config={}, source_thread_id=lambda job: job['thread_id'])
        pending = PendingCall('job', {'thread_id':'source-a12345678'}, 'number-unused', source)
        bridge = IPhoneVoiceBridge(daemon, pending, local_tts=SimpleNamespace())
        self.assertEqual(bridge._output_probe_options(), {})
        source.write_text('{}')
        self.assertEqual(bridge._output_probe_options(),
                         {'output_probe_dir':self.base/'output-probes', 'output_probe_seconds':60})
        pending.job['thread_id'] = 'source-b12345678'
        self.assertEqual(bridge._output_probe_options(), {})


if __name__ == '__main__':
    unittest.main()
