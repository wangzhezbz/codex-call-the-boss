from __future__ import annotations

import unittest
from aiortc.rtp import RtpPacket
from audio_jitter import AudioPacketJitterBuffer, AudioPacketNackGenerator


def packet(sequence: int, *, ssrc: int = 1) -> RtpPacket:
    item = RtpPacket(payload_type=96, sequence_number=sequence & 65535,
                     timestamp=(sequence*960) & 0xffffffff, ssrc=ssrc,
                     payload=sequence.to_bytes(4, 'little'))
    item._data = item.payload
    return item


class AudioJitterTests(unittest.TestCase):
    def replay(self, sequence):
        buffer = AudioPacketJitterBuffer()
        heard = []
        for number in sequence:
            pli, frame = buffer.add(packet(number))
            self.assertFalse(pli)
            if frame: heard.append(int.from_bytes(frame.data, 'little'))
        return buffer, heard

    def test_contiguous_sequence_preserves_order_and_four_packet_lead(self):
        buffer, heard = self.replay(range(100))
        self.assertEqual(heard, list(range(96)))
        self.assertEqual(buffer.missing, 0)

    def test_loss_does_not_discard_received_speech(self):
        received = [i for i in range(500) if i%9 != 5]
        buffer, heard = self.replay(received)
        self.assertEqual(heard, received[:len(heard)])
        self.assertLessEqual(len(received)-len(heard), 4)
        self.assertEqual(buffer.overflow_drops, 0)
        self.assertGreater(buffer.missing, 40)

    def test_out_of_order_inside_window_is_recovered(self):
        buffer, heard = self.replay([0,2,1,3,4,5,7,6,8,9])
        self.assertEqual(heard, list(range(6)))
        self.assertEqual(buffer.missing, 0)

    def test_initial_reorder_does_not_drop_the_first_syllable(self):
        buffer, heard = self.replay([2,1,0,3,4,5,6,7,8])
        self.assertEqual(heard, list(range(5)))
        self.assertEqual(buffer.late, 0)

    def test_duplicate_does_not_replay_audio(self):
        buffer, heard = self.replay([0,1,1,2,3,4,5,6,7,8])
        self.assertEqual(heard, list(range(5)))
        self.assertEqual(buffer.duplicates, 1)

    def test_sequence_wrap(self):
        _, heard = self.replay(range(65530,65550))
        self.assertEqual(heard, list(range(65530,65546)))

    def test_late_packet_is_dropped_without_stalling(self):
        buffer, heard = self.replay([0,1,2,3,4,5,6,7,8,1,9,10])
        self.assertEqual(heard, list(range(7)))
        self.assertEqual(buffer.late, 1)

    def test_ssrc_change_does_not_mix_queued_packets(self):
        buffer = AudioPacketJitterBuffer()
        for i in range(4): buffer.add(packet(i))
        for i in range(10,14): self.assertIsNone(buffer.add(packet(i,ssrc=2))[1])
        frame = buffer.add(packet(14,ssrc=2))[1]
        self.assertEqual(int.from_bytes(frame.data,'little'),10)

    def test_retransmitted_original_inside_twelve_packet_window_is_not_lost(self):
        sequence = [i for i in range(40) if i != 5]
        sequence.insert(sequence.index(13), 5)  # Original returns 140 ms late.
        old, original = AudioPacketJitterBuffer(), AudioPacketJitterBuffer(prefetch=12)
        heard = []
        for number in sequence:
            old.add(packet(number))
            frame = original.add(packet(number))[1]
            if frame:
                heard.append(int.from_bytes(frame.data, 'little'))
        self.assertEqual(old.missing, 1)
        self.assertEqual(old.late, 1)
        self.assertEqual(heard, list(range(len(heard))))
        self.assertEqual(original.missing, 0)
        self.assertEqual(original.late, 0)


class AudioNackTests(unittest.TestCase):
    def test_one_bounded_gap_requests_and_recovers_the_original(self):
        nack = AudioPacketNackGenerator()
        self.assertFalse(nack.add(packet(0)))
        self.assertTrue(nack.add(packet(2)))
        self.assertEqual(nack.missing, {1})
        self.assertFalse(nack.add(packet(1)))
        self.assertEqual(nack.missing, set())
        self.assertEqual(nack.diagnostics()['recovered_packets'], 1)

    def test_contiguous_duplicate_or_timestamp_jump_alone_needs_no_request(self):
        nack = AudioPacketNackGenerator()
        for seq in (0, 1, 1, 2):
            self.assertFalse(nack.add(packet(seq)))
        jumped = packet(3)
        jumped.timestamp += 480000
        self.assertFalse(nack.add(jumped))
        self.assertEqual(nack.missing, set())

    def test_large_or_duration_inconsistent_gap_is_not_requested(self):
        for following in (10000, 3):
            with self.subTest(following=following):
                nack = AudioPacketNackGenerator()
                nack.add(packet(0))
                p = packet(following)
                if following == 3:
                    p.timestamp += 480000
                self.assertFalse(nack.add(p))
                self.assertEqual(nack.missing, set())

    def test_old_missing_packets_expire_without_endless_requests(self):
        nack = AudioPacketNackGenerator()
        nack.add(packet(0))
        self.assertTrue(nack.add(packet(2)))
        for seq in range(3, 20):
            self.assertFalse(nack.add(packet(seq)))
        self.assertEqual(nack.missing, set())
        self.assertEqual(nack.diagnostics()['expired_packets'], 1)
        self.assertFalse(nack.add(packet(1)))
        self.assertEqual(nack.diagnostics()['recovered_packets'], 0)

    def test_ssrc_change_cannot_request_an_old_speakers_packets(self):
        nack = AudioPacketNackGenerator()
        nack.add(packet(0))
        nack.add(packet(2))
        self.assertFalse(nack.add(packet(500, ssrc=2)))
        self.assertEqual(nack.missing, set())
        self.assertFalse(nack.add(packet(501, ssrc=2)))

    def test_sequence_and_timestamp_wrap_remain_bounded(self):
        for seq in (65535, (2**32 // 960) - 1):
            with self.subTest(seq=seq):
                nack = AudioPacketNackGenerator()
                nack.add(packet(seq))
                self.assertTrue(nack.add(packet(seq + 2)))
                self.assertEqual(nack.missing, {(seq + 1) & 65535})
                self.assertFalse(nack.add(packet(seq + 1)))
                self.assertFalse(nack.missing)

    def test_missing_state_is_bounded_over_long_lossy_input(self):
        nack = AudioPacketNackGenerator()
        for seq in range(0, 5000, 3):
            nack.add(packet(seq))
            self.assertLessEqual(len(nack.missing), 12)


if __name__ == '__main__': unittest.main()
