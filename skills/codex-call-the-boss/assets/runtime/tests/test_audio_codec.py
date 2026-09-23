from __future__ import annotations

import base64
import struct
import unittest

from audio_codec import (
    CodexPcm24kToPlivoUlaw8k,
    linear_to_ulaw,
    pcm16_8k_to_plivo_ulaw,
    plivo_ulaw_8k_to_codex_pcm24k,
    plivo_ulaw_8k_to_pcm48k,
    resample_pcm16,
    ulaw_to_linear,
)


class AudioCodecTests(unittest.TestCase):
    def test_ulaw_zero(self) -> None:
        self.assertEqual(linear_to_ulaw(0), 0xFF)
        self.assertEqual(ulaw_to_linear(0xFF), 0)

    def test_ulaw_round_trip_is_close(self) -> None:
        for sample in (-30000, -10000, -1000, 0, 1000, 10000, 30000):
            decoded = ulaw_to_linear(linear_to_ulaw(sample))
            self.assertLess(abs(decoded - sample), max(40, abs(sample) // 20))

    def test_input_frame_is_upsampled_three_times(self) -> None:
        source = bytes([linear_to_ulaw(1000), linear_to_ulaw(-1000)])
        encoded, samples = plivo_ulaw_8k_to_codex_pcm24k(
            base64.b64encode(source).decode("ascii")
        )
        pcm = base64.b64decode(encoded)
        unpacked = struct.unpack("<6h", pcm)
        self.assertEqual(samples, 6)
        self.assertEqual(unpacked[:3], (ulaw_to_linear(source[0]),) * 3)
        self.assertEqual(unpacked[3:], (ulaw_to_linear(source[1]),) * 3)

    def test_output_resampler_keeps_phase_across_chunks(self) -> None:
        converter = CodexPcm24kToPlivoUlaw8k()
        first = struct.pack("<4h", 0, 1000, 2000, 3000)
        second = struct.pack("<5h", 4000, 5000, 6000, 7000, 8000)
        one = base64.b64decode(converter.convert(base64.b64encode(first).decode()))
        two = base64.b64decode(converter.convert(base64.b64encode(second).decode()))
        self.assertEqual(one + two, bytes(map(linear_to_ulaw, (0, 3000, 6000))))

    def test_phone_webrtc_codec_lengths(self) -> None:
        source = bytes([linear_to_ulaw(1200)] * 160)
        pcm48 = plivo_ulaw_8k_to_pcm48k(base64.b64encode(source).decode())
        self.assertEqual(len(pcm48), 160 * 6 * 2)
        pcm8 = struct.pack("<160h", *([1200] * 160))
        encoded = base64.b64decode(pcm16_8k_to_plivo_ulaw(pcm8))
        self.assertEqual(len(encoded), 160)

    def test_pcm16_block_resampler_preserves_endpoints_and_size(self) -> None:
        source = struct.pack("<3h", -1000, 0, 1000)
        rendered = struct.unpack("<5h", resample_pcm16(source, 5))
        self.assertEqual(rendered, (-1000, -500, 0, 500, 1000))

    def test_pcm16_block_resampler_rejects_odd_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "odd byte count"):
            resample_pcm16(b"\x00", 960)


if __name__ == "__main__":
    unittest.main()
