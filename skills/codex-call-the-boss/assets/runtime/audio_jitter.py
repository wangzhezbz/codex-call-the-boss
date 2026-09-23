"""Bounded packet reordering for the pinned aiortc audio receiver only.

Opus and G.711 RTP packets are independently packetized audio units, not video
fragments to assemble by timestamp. Keep a short reorder window but skip an
unrecoverable sequence hole instead of discarding additional received speech
until a 16-slot video-style ring fills. Missing packets remain missing; this
does not invent lost audio or claim loss concealment.
"""
from __future__ import annotations

from aiortc.jitterbuffer import JitterFrame
from aiortc.rtp import RtpPacket


class AudioPacketNackGenerator:
    """Request only recent, duration-consistent 20 ms audio losses.

    Implements the pinned receiver's add()/missing interface without its
    video-sized history or a potentially huge sequence-hole loop. Enable only
    after Opus Generic NACK was negotiated on this peer. Returning an original
    packet is distinct from decoder concealment or intelligibility acceptance.
    """
    def __init__(self, window: int = 12) -> None:
        if not 1 <= window <= 32:
            raise ValueError('invalid audio retransmission window')
        self.window = window
        self.missing: set[int] = set()
        self._pending: set[int] = set()
        self._highest = self._timestamp = self._ssrc = None
        self.requests = self.recovered = self.expired = self.feedback = 0

    def add(self, packet: RtpPacket) -> bool:
        if self._ssrc != packet.ssrc:
            self._ssrc = packet.ssrc
            self._highest = packet.sequence_number
            self._timestamp = packet.timestamp
            self._pending.clear()
            self.missing.clear()
            return False
        delta = ((packet.sequence_number - (self._highest & 65535) + 32768) & 65535) - 32768
        sequence = self._highest + delta
        request = False
        if delta > 0:
            duration = (packet.timestamp - self._timestamp) & 0xffffffff
            if 1 < delta <= self.window and duration == delta * 960:
                lost = set(range(self._highest + 1, sequence))
                self._pending.update(lost)
                self.requests += len(lost)
                request = True
            self._highest, self._timestamp = sequence, packet.timestamp
            expired = {n for n in self._pending if self._highest - n >= self.window}
            self.expired += len(expired)
            self._pending.difference_update(expired)
        elif sequence in self._pending:
            self._pending.remove(sequence)
            self.recovered += 1
        self.missing = {n & 65535 for n in self._pending}
        if request and self.missing:
            self.feedback += 1
            return True
        return False

    def diagnostics(self) -> dict[str, int]:
        return {'requested_packets': self.requests, 'recovered_packets': self.recovered,
                'expired_packets': self.expired, 'feedback_batches': self.feedback,
                'pending_packets': len(self.missing), 'window_packets': self.window}


class AudioPacketJitterBuffer:
    def __init__(self, *, prefetch: int = 4, capacity: int = 64) -> None:
        if not 1 <= prefetch < capacity:
            raise ValueError('invalid audio reorder window')
        self.prefetch = prefetch
        self.capacity = capacity
        self._packets: dict[int, RtpPacket] = {}
        self._next: int | None = None
        self._highest: int | None = None
        self._ssrc: int | None = None
        self._primed = False
        self.emitted = 0
        self.missing = 0
        self.late = 0
        self.duplicates = 0
        self.overflow_drops = 0
        self._reset_decoder = True

    def add(self, packet: RtpPacket) -> tuple[bool, JitterFrame | None]:
        if self._ssrc != packet.ssrc:
            self._ssrc = packet.ssrc
            self.overflow_drops += len(self._packets)
            self._packets.clear()
            self._next = self._highest = packet.sequence_number
            self._primed = False
            self._reset_decoder = True
        assert self._next is not None and self._highest is not None
        delta = ((packet.sequence_number - (self._highest & 65535) + 32768) & 65535) - 32768
        sequence = self._highest + delta
        if sequence < self._next and not self._primed and self._next-sequence <= self.prefetch:
            self._next = sequence
        if sequence < self._next:
            self.late += 1
            return False, None
        if sequence in self._packets:
            self.duplicates += 1
            return False, None
        self._highest = max(self._highest, sequence)
        self._packets[sequence] = packet
        if len(self._packets) > self.capacity:
            self._packets.pop(min(self._packets))
            self.overflow_drops += 1
        if not self._primed:
            if self._highest - self._next < self.prefetch:
                return False, None
            self._primed = True
        missing = 0
        if self._next not in self._packets:
            if self._highest - self._next < self.prefetch:
                return False, None
            following = min(self._packets)
            missing = following - self._next
            self.missing += missing
            self._next = following
        selected = self._packets.pop(self._next)
        self._next += 1
        self.emitted += 1
        frame = JitterFrame(
            data=getattr(selected, '_data', selected.payload),
            timestamp=selected.timestamp,
        )
        frame.missing_packets = missing
        frame.reset_decoder = self._reset_decoder
        self._reset_decoder = False
        return False, frame

    def diagnostics(self) -> dict[str, int]:
        return {'emitted_packets':self.emitted, 'missing_packets':self.missing,
                'late_packets':self.late, 'duplicate_packets':self.duplicates,
                'overflow_drops':self.overflow_drops, 'buffered_packets':len(self._packets),
                'reorder_window_packets':self.prefetch}
