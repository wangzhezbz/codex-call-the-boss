"""Opt-in, bounded assistant-feed evidence during an actual call.

Neither callback writes files or changes PCM. The physical recording is from
the dedicated virtual microphone feed, never the caller's capture device.
Matching these two files does not verify Phone.app processing or handset audio.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import tempfile
import time
import wave
from pathlib import Path

from native_speech import atomic_write


def _stamp(timing, key):
    try:
        value = float(timing[key] if isinstance(timing, dict) else getattr(timing, key))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, AttributeError, KeyError):
        return None


def clock_summary(rows, rate):
    """Device clock gaps and Python wall scheduling are different evidence."""
    gaps, wall_gaps = [], []
    for a, b in zip(rows, rows[1:]):
        duration = a['frames'] / rate
        if a['device_time'] is not None and b['device_time'] is not None:
            gaps.append((b['device_time'] - a['device_time'] - duration) * 1000)
        wall_gaps.append((b['wall_time'] - a['wall_time'] - duration) * 1000)
    return {'callback_count': len(rows), 'valid_device_intervals': len(gaps),
            'device_gap_count_over_1ms': sum(abs(gap) > 1 for gap in gaps),
            'max_abs_device_gap_ms': max(map(abs, gaps), default=None),
            'max_positive_wall_gap_ms': max([0.0, *wall_gaps])}


class PhoneOutputProbe:
    def __init__(self, directory, *, sounddevice_module, device, seconds=60,
                 sample_rate=48000, blocksize=960):
        if type(seconds) is not int or not 1 <= seconds <= 120:
            raise ValueError('Output probe must be bounded to 1-120 seconds')
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix='call-feed-', dir=base))
        self.sd, self.device = sounddevice_module, device
        self.rate, self.blocksize, self.seconds = sample_rate, blocksize, seconds
        self.limit = sample_rate * seconds
        self.active = self.activated = self.closed = False
        self.activated_at = None
        self.stream = None
        self.errors = []
        self.error_count = 0
        self.data = {'output': bytearray(), 'feed': bytearray()}
        self.clocks = {'output': [], 'feed': []}
        self.truncated = {'output': False, 'feed': False}
        self.saved = False

    def _error(self, label):
        self.error_count += 1
        if len(self.errors) < 16:
            self.errors.append(label)

    def start(self):
        try:
            self.stream = self.sd.RawInputStream(device=self.device,
                samplerate=self.rate, blocksize=self.blocksize, channels=1,
                dtype='int16', latency=.12, callback=self._capture)
            self.stream.start()
        except Exception as exc:
            self._error('start:' + type(exc).__name__)
            self.close()

    def activate(self):
        if not self.closed and self.stream is not None and not self.activated:
            self.activated_at = time.monotonic()
            self.activated = self.active = True

    def _record(self, kind, payload, frames, timing, status):
        if not self.active:
            return
        try:
            used = len(self.data[kind]) // 2
            available = self.limit - used
            if available <= 0 or len(self.clocks[kind]) >= 8192:
                self.truncated[kind] = True
                return
            if len(payload) != frames * 2 or frames <= 0:
                raise ValueError('Invalid PCM frame')
            size = min(frames, available)
            self.data[kind].extend(memoryview(payload)[:size * 2])
            key = 'outputBufferDacTime' if kind == 'output' else 'inputBufferAdcTime'
            self.clocks[kind].append({'sample_offset': used, 'frames': size,
                'device_time': _stamp(timing, key), 'current_time': _stamp(timing, 'currentTime'),
                'wall_time': time.monotonic(), 'status_flag': bool(status)})
            if status:
                self._error(kind + ':stream_status')
            if size < frames:
                self.truncated[kind] = True
        except Exception as exc:
            # Evidence collection cannot interrupt the telephone's output.
            self._error(kind + ':' + type(exc).__name__)

    def output(self, payload, frames, timing, status):
        self._record('output', payload, frames, timing, status)

    def _capture(self, payload, frames, timing, status):
        self._record('feed', payload, frames, timing, status)

    def summary(self):
        return {'enabled': True, 'activated': self.activated,
                'max_seconds': self.seconds, 'saved': self.saved,
                'directory': str(self.directory), 'error_count': self.error_count,
                'frames': {key: len(value) // 2 for key, value in self.data.items()},
                'truncated': dict(self.truncated),
                'human_listening_verified': False,
                'scope': 'assistant_virtual_microphone_feed_not_handset'}

    def close(self, *, ledger=None):
        if self.closed:
            return
        self.active = False
        self.closed = True
        if self.stream is not None:
            for method in ('stop', 'close'):
                try:
                    getattr(self.stream, method)()
                except Exception as exc:
                    self._error(method + ':' + type(exc).__name__)
        try:
            artifacts = {}
            for kind, payload in self.data.items():
                if not payload:
                    continue
                stream = io.BytesIO()
                with wave.open(stream, 'wb') as wav:
                    wav.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
                    wav.writeframes(payload)
                path = self.directory / (kind + '.wav')
                atomic_write(path, stream.getvalue())
                artifacts[kind] = {'file': path.name,
                    'pcm_sha256': hashlib.sha256(payload).hexdigest()}
            self.saved = True
            record = {**self.summary(), 'sample_rate': self.rate,
                'activated_monotonic': self.activated_at,
                'errors': self.errors, 'artifacts': artifacts,
                'clock_summary': {key: clock_summary(rows, self.rate)
                                  for key, rows in self.clocks.items()},
                'clocks': self.clocks, 'playback_ledger': ledger or []}
            atomic_write(self.directory / 'probe.json',
                         json.dumps(record, ensure_ascii=False).encode())
        except Exception as exc:
            self.saved = False
            self._error('save:' + type(exc).__name__)
