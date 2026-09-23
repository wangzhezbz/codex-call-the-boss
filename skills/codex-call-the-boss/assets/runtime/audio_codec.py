from __future__ import annotations

import base64
import struct
from array import array


_BIAS = 0x84
_CLIP = 32635


def resample_pcm16(payload: bytes, output_frames: int) -> bytes:
    """Linearly resize one mono PCM16 block without adding dependencies."""
    if len(payload) % 2:
        raise ValueError("PCM16 payload has an odd byte count")
    samples = array("h")
    samples.frombytes(payload)
    if len(samples) == output_frames:
        return payload
    if not samples:
        return b"\x00" * (output_frames * 2)
    if len(samples) == 1:
        return array("h", [samples[0]] * output_frames).tobytes()
    if output_frames == 1:
        return array("h", [samples[0]]).tobytes()
    scale = (len(samples) - 1) / (output_frames - 1)
    rendered = array("h")
    for index in range(output_frames):
        position = index * scale
        left = int(position)
        fraction = position - left
        right = min(left + 1, len(samples) - 1)
        value = round(samples[left] + (samples[right] - samples[left]) * fraction)
        rendered.append(max(-32768, min(32767, value)))
    return rendered.tobytes()


def ulaw_to_linear(sample: int) -> int:
    value = (~sample) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    linear = ((mantissa << 3) + _BIAS) << exponent
    linear -= _BIAS
    return -linear if sign else linear


def linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not magnitude & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def plivo_ulaw_8k_to_codex_pcm24k(payload_b64: str) -> tuple[str, int]:
    """Convert one Plivo mu-law frame to mono little-endian PCM16 at 24 kHz."""
    encoded = base64.b64decode(payload_b64, validate=True)
    samples_8k = [ulaw_to_linear(value) for value in encoded]
    samples_24k = [sample for sample in samples_8k for _ in range(3)]
    pcm = struct.pack(f"<{len(samples_24k)}h", *samples_24k)
    return base64.b64encode(pcm).decode("ascii"), len(samples_24k)


def plivo_ulaw_8k_to_pcm48k(payload_b64: str) -> bytes:
    encoded = base64.b64decode(payload_b64, validate=True)
    samples_48k = [
        sample
        for value in encoded
        for sample in (ulaw_to_linear(value),) * 6
    ]
    return struct.pack(f"<{len(samples_48k)}h", *samples_48k)


def pcm16_8k_to_plivo_ulaw(payload: bytes) -> str:
    if len(payload) % 2:
        raise ValueError("PCM16 payload has an odd byte count")
    count = len(payload) // 2
    samples = struct.unpack(f"<{count}h", payload)
    encoded = bytes(linear_to_ulaw(sample) for sample in samples)
    return base64.b64encode(encoded).decode("ascii")


class CodexPcm24kToPlivoUlaw8k:
    def __init__(self) -> None:
        self._offset = 0

    def convert(self, payload_b64: str) -> str:
        pcm = base64.b64decode(payload_b64, validate=True)
        if len(pcm) % 2:
            raise ValueError("PCM16 payload has an odd byte count")
        count = len(pcm) // 2
        samples = struct.unpack(f"<{count}h", pcm)
        selected: list[int] = []
        index = self._offset
        while index < count:
            selected.append(samples[index])
            index += 3
        self._offset = index - count
        encoded = bytes(linear_to_ulaw(sample) for sample in selected)
        return base64.b64encode(encoded).decode("ascii")
