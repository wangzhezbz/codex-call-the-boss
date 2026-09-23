"""Output accounting: normal completed-turn idle is not stream starvation."""
from array import array
import unittest

from iphone_audio import IPhoneAudioPipe


class OutputIdleTests(unittest.TestCase):
    def pipe(self, prebuffer_ms=20):
        return IPhoneAudioPipe('unused', 'unused', lambda _: None,
            prebuffer_ms=prebuffer_ms, adaptive_rate_percent=0, sounddevice_module=object())

    def output(self, pipe, count=1):
        result = bytearray()
        for _ in range(count):
            frame = bytearray(1920)
            pipe._output_callback(frame, 960, None, None)
            result.extend(frame)
        return result

    def test_finished_report_then_idle_does_not_count_as_underrun(self):
        pipe = self.pipe()
        pipe.play_pcm48k(array('h', [1000] * 9600).tobytes(), utterance_id='report')
        self.output(pipe, 60)
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'output_complete')
        self.assertEqual(pipe.underrun_count, 0)
        self.assertEqual(pipe.inserted_silence_frames, 0)
        self.assertFalse(pipe._recovering)
        self.assertEqual(pipe.diagnostics()['output_idle_silence_ms'], 1000)

    def test_short_sealed_tail_is_not_stuck_below_prebuffer(self):
        pipe = self.pipe(prebuffer_ms=400)
        clip = array('h', [1000] * 480).tobytes()
        pipe.play_pcm48k(clip, utterance_id='short')
        output = self.output(pipe)
        self.assertEqual(output[:len(clip)], clip)
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'output_complete')
        self.assertEqual(pipe.underrun_count, 0)

    def test_open_stream_still_counts_a_real_missing_continuation(self):
        pipe = self.pipe()
        pipe.play_pcm48k(array('h', [1000] * 960).tobytes(),
                        utterance_id='stream', final=False)
        self.output(pipe, 3)
        self.assertEqual(pipe.underrun_count, 1)
        self.assertEqual(pipe.inserted_silence_frames, 1920)
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'playing')
        pipe.seal_utterance('stream')
        self.output(pipe, 10)
        self.assertEqual(pipe.inserted_silence_frames, 1920)
        self.assertFalse(pipe._recovering)
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'output_complete')

    def test_sealed_tail_resumes_after_stream_gap_without_recovery_delay(self):
        pipe = self.pipe()
        pipe.play_pcm48k(array('h', [1000] * 960).tobytes(),
                        utterance_id='stream', final=False)
        self.output(pipe, 2)
        pipe.play_pcm48k(array('h', [1000] * 240).tobytes(),
                        utterance_id='stream', final=True)
        self.assertTrue(any(self.output(pipe)))
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'output_complete')
        self.assertEqual(pipe.underrun_count, 1)
        self.assertFalse(pipe._recovering)

    def test_cancelled_stream_does_not_keep_idle_in_fault_recovery(self):
        pipe = self.pipe()
        pipe.play_pcm48k(array('h', [1000] * 960).tobytes(),
                        utterance_id='stream', final=False)
        self.output(pipe)
        pipe.clear_output()
        self.output(pipe, 10)
        self.assertEqual(pipe.underrun_count, 0)
        self.assertEqual(pipe.inserted_silence_frames, 0)
        self.assertEqual(pipe.playback_snapshot()[0]['status'], 'partial_cancelled')


if __name__ == '__main__':
    unittest.main()
