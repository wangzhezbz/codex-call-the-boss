import unittest
import ctypes.util
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtp import RtpPacket
from opus_recovery import OpusRecoveryDecoder
from audio_jitter import AudioPacketJitterBuffer


def frame(timestamp, missing=0, reset=False):
    # A valid 20 ms Opus silence packet, not random bytes pretending to be Opus.
    result=JitterFrame(b'\xf8\xff\xfe',timestamp)
    result.missing_packets=missing; result.reset_decoder=reset
    return result


@unittest.skipUnless(ctypes.util.find_library('opus'),'optional native mode requires libopus')
class OpusRecoveryTests(unittest.TestCase):
    def setUp(self): self.decoder=OpusRecoveryDecoder()
    def tearDown(self): self.decoder.close()

    def test_contiguous_audio_is_not_stretched(self):
        for i in range(10):
            result=self.decoder.decode(frame(i*960))
            self.assertEqual([f.samples for f in result],[960])
        self.assertEqual(self.decoder.metrics['concealed_packets'],0)

    def test_one_missing_packet_gets_one_concealment_frame(self):
        self.decoder.decode(frame(0))
        result=self.decoder.decode(frame(1920,1))
        self.assertEqual([f.pts for f in result],[960,1920])
        self.assertEqual(self.decoder.metrics['concealed_packets'],1)

    def test_three_missing_packets_keep_chronological_order(self):
        self.decoder.decode(frame(0))
        result=self.decoder.decode(frame(3840,3))
        self.assertEqual([f.pts for f in result],[960,1920,2880,3840])
        self.assertEqual(self.decoder.metrics['concealed_packets'],3)

    def test_timestamp_jump_alone_never_synthesizes_silence(self):
        self.decoder.decode(frame(0))
        result=self.decoder.decode(frame(480000))
        self.assertEqual(len(result),1)
        self.assertEqual(self.decoder.metrics['concealed_packets'],0)

    def test_large_or_irregular_loss_is_not_filled(self):
        self.decoder.decode(frame(0))
        self.assertEqual(len(self.decoder.decode(frame(20160,20))),1)
        self.assertEqual(len(self.decoder.decode(frame(24000,1))),1)
        self.assertEqual(self.decoder.metrics['concealed_packets'],0)

    def test_ssrc_reset_does_not_conceal_an_old_speaker(self):
        self.decoder.decode(frame(0))
        self.assertEqual(len(self.decoder.decode(frame(1920,1,True))),1)

    def test_jitter_marks_gap_once_and_ssrc_reset(self):
        jitter=AudioPacketJitterBuffer(prefetch=1)
        frames=[]
        for seq in [0,1,3,4,5,6]:
            p=RtpPacket(payload_type=96,sequence_number=seq,timestamp=seq*960,
                        ssrc=1,payload=b'\xf8\xff\xfe')
            p._data=p.payload
            result=jitter.add(p)[1]
            if result: frames.append(result)
        self.assertTrue(frames[0].reset_decoder)
        self.assertEqual(sum(f.missing_packets for f in frames),1)
        self.assertEqual([f.timestamp for f in frames if f.missing_packets],[2880])
