"""Sample-consumption receipts. Queued PCM is not proof of phone playback."""
from __future__ import annotations

from collections import deque
from threading import RLock
import time


class PlaybackLedger:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = RLock()
        self.records = {}
        self.spans = deque()
        self.open_streams = set()

    def enqueue(self, size, identity='', *, kind='', text='', final=True):
        with self.lock:
            identity = identity or f'pcm-{len(self.records) + 1}'
            item = self.records.setdefault(identity, {
                'id': identity, 'kind': kind, 'text': text, 'queued_bytes': 0,
                'output_bytes': 0, 'status': 'queued', 'sealed': False,
                'queued_at': self.clock(), 'evidence': 'audio_device_callback',
            })
            if item['status'] in {'cancelled', 'partial_cancelled'}:
                raise ValueError('Cannot append to a cancelled playback identity')
            item['queued_bytes'] += size
            item['text'] = text or item['text']
            item['sealed'] = final
            if final:
                self.open_streams.discard(identity)
            else:
                self.open_streams.add(identity)
            self.spans.append([identity, size])
            return identity

    def consume(self, size):
        with self.lock:
            while size > 0 and self.spans:
                identity, remaining = self.spans[0]
                amount = min(size, remaining)
                item = self.records[identity]
                item.setdefault('first_output_at', self.clock())
                item['output_bytes'] += amount
                item['status'] = 'playing'
                size -= amount
                remaining -= amount
                if remaining:
                    self.spans[0][1] = remaining
                else:
                    self.spans.popleft()
                self._finish(item)

    def seal(self, identity, text=''):
        with self.lock:
            item = self.records.get(identity)
            if item:
                item['sealed'] = True
                self.open_streams.discard(identity)
                item['text'] = text or item['text']
                self._finish(item)

    def _finish(self, item):
        if (item['sealed'] and item['output_bytes'] == item['queued_bytes']
                and item['status'] not in {'cancelled', 'partial_cancelled'}):
            item['status'] = 'output_complete'
            item.setdefault('output_finished_at', self.clock())

    def cancel(self):
        with self.lock:
            for item in self.records.values():
                if item['status'] in {'queued', 'playing'}:
                    item['status'] = 'partial_cancelled' if item['output_bytes'] else 'cancelled'
                    item['cancelled_at'] = self.clock()
            self.spans.clear()
            self.open_streams.clear()

    def expects_more_audio(self):
        """Unsealed streaming turns may starve; a finished clip may be idle."""
        with self.lock:
            return bool(self.open_streams)

    def snapshot(self):
        with self.lock:
            return [dict(item) for item in self.records.values()]
