"""Keep timestamped media across event races without replaying an old turn."""
from collections import deque
from array import array
import math
import re


class OutputTranscriptClock:
    """Use word media timestamps when aggregate turn.created arrives late."""
    def __init__(self):
        # A 30-second reply can already contain 129 fragments including its
        # final punctuation. Keep the whole bounded 60-second speech window.
        self.words = deque(maxlen=512)
        self.fragments = deque(maxlen=512)
        self.media_observations = deque(maxlen=512)
        self.domain_offset_ms = None

    def clear(self):
        self.words.clear()
        self.fragments.clear()
        self.media_observations.clear()
        self.domain_offset_ms = None

    def anchor_domain(self, text_ms, media_ms):
        """Bind a proven single-clip onset without changing raw word events.

        Live conversation retains its per-word receive observations. A fresh
        literal renderer can instead supply its independently observed onset;
        later silence or tail receipt must never move that anchor to pass QA.
        """
        if self.domain_offset_ms is not None:
            return False
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   and value >= 0 for value in (text_ms, media_ms)):
            return False
        self.domain_offset_ms = text_ms - media_ms
        return True

    def observe(self, event, *, media_ms=None):
        stamp = event.get('start_ms')
        text = str((event.get('item') or {}).get('text') or event.get('text') or '')
        if isinstance(stamp, (int, float)) and text:
            self.words.append((stamp, text))
            if isinstance(media_ms, (int, float)) and math.isfinite(media_ms) and media_ms >= 0:
                # V3 transcript time and the RTP sample counter are separate
                # domains; their offset can grow during one connection. Pair
                # this word event with the freshly observed receive cursor.
                # This changes no PCM, playback rate or input authorization.
                self.media_observations.append((stamp, media_ms))
            end = event.get('end_ms')
            if isinstance(end, (int, float)) and end >= stamp:
                self.fragments.append((stamp, end, text))

    def media_time(self, stamp):
        if not isinstance(stamp, (int, float)):
            return stamp
        if self.domain_offset_ms is not None:
            return max(0, stamp - self.domain_offset_ms)
        for word_stamp, media in reversed(self.media_observations):
            if 0 <= stamp - word_stamp <= 10000:
                return max(0, media + stamp - word_stamp)
        return stamp  # Legacy/non-PCM events have no receive-clock proof.

    def end_for(self, turn):
        """Prefer the final spoken word, not v3's trailing aggregate silence.

        Require a complete, newest text match in this turn's bounded start
        window. A stale word/prefix from another reply cannot certify its tail.
        """
        fallback, start = turn.get('end_ms'), turn.get('start_ms')
        normalize = lambda s: re.sub(r'[^\u3400-\u9fffA-Za-z0-9]', '', s).casefold()
        target = normalize(str(turn.get('transcript') or ''))
        if not target or not isinstance(start, (int, float)):
            return self.media_time(fallback)
        fragments = list(self.fragments)
        for index in range(len(fragments)-1, -1, -1):
            if not start-5000 <= fragments[index][0] <= start:
                continue
            combined = ''
            for _, end, text in fragments[index:]:
                if isinstance(fallback, (int, float)) and end > fallback:
                    break
                combined += normalize(text)
                if combined == target:
                    return self.media_time(end)
                if not target.startswith(combined):
                    break
        return self.media_time(fallback)

    def start_for(self, turn, *, raw=False):
        fallback = turn.get('start_ms')
        convert = (lambda value: value) if raw else self.media_time
        normalize = lambda s: re.sub(r'[^\u3400-\u9fffA-Za-z0-9]', '', s).casefold()
        target = normalize(str(turn.get('transcript') or ''))
        if not target:
            return convert(fallback)
        words = list(self.words)
        # Prefer the newest exact prefix match. An old common acknowledgement
        # must not pull media from an earlier reply into this one.
        for index in range(len(words)-1, -1, -1):
            stamp = words[index][0]
            if isinstance(fallback, (int,float)) and not fallback-5000 <= stamp <= fallback:
                continue
            combined = ''
            for _, text in words[index:]:
                combined += normalize(text)
                if combined and combined.startswith(target):
                    return convert(stamp)
                if not target.startswith(combined):
                    break
        return convert(fallback)


class AudioTurnFence:
    def __init__(self):
        self.pending = deque(maxlen=250)  # At most 5 seconds of event/media skew.
        self.turn_id = ''
        self.minimum_ms = None
        self.discarded_frames = 0
        self._seeking_onset = False
        self._start_ms = None
        self._previous_end_ms = None
        self._ready = []

    def interrupt(self):
        self.turn_id = ''
        self.minimum_ms = None
        self.pending.clear()
        self._ready.clear()
        self._seeking_onset = False
        self._start_ms = None

    def begin(self, identity, start_ms=None, *, previous_end_ms=None):
        self.turn_id = identity
        # V3 transcript timestamps trail the measured waveform onset by about
        # 60 ms. Keep a bounded 120 ms phoneme lead, not an arrival-time delay.
        self.minimum_ms = max(0, start_ms - 120) if isinstance(start_ms, (int, float)) else None
        self._start_ms = start_ms
        self._previous_end_ms = previous_end_ms
        self._seeking_onset = self.minimum_ms is not None
        if self._seeking_onset and not any(
                isinstance(meta.get('media_ms'),(int,float)) and meta['media_ms']>=self.minimum_ms
                for _,meta in self.pending):
            # Data can beat media too. Keep the below-boundary frames until
            # the first boundary frame arrives; otherwise there is no waveform
            # yet from which to measure the earlier phoneme onset.
            return []
        return self._resolve_pending()

    def _resolve_pending(self):
        self._seeking_onset = False
        if isinstance(self._start_ms, (int, float)):
            self.minimum_ms = self._preserve_connected_voice_onset(self._start_ms, self.minimum_ms)
        ready = []
        while self.pending:
            pcm, metadata = self.pending.popleft()
            if self.minimum_ms is not None and metadata.get('media_ms') is not None and metadata['media_ms'] >= self.minimum_ms:
                ready.append(pcm)
            else:
                self.discarded_frames += 1
        return ready

    def take_ready(self):
        ready,self._ready=self._ready,[]
        return ready

    def _preserve_connected_voice_onset(self, start_ms, default):
        # Word timestamps quantize/lag the actual phoneme onset by a variable
        # amount (observed up to 1.12 s after a long idle), even after fixing
        # aggregate turn time. The quiet-onset and connected-group checks,
        # not a blanket lookback, keep separate preceding speech excluded.
        # Extend only through the connected voiced segment at the boundary;
        # a preceding tail separated by real silence must remain excluded.
        rows=[]
        for pcm, meta in self.pending:
            stamp=meta.get('media_ms')
            if not isinstance(stamp,(int,float)) or not start_ms-1700 <= stamp <= start_ms+200:
                continue
            samples=array('h',pcm[:len(pcm)-len(pcm)%2])
            rms=(sum(v*v for v in samples)/len(samples))**.5 if samples else 0
            rows.append((stamp,rms>=90))
        voiced=[stamp for stamp,active in rows if active]
        groups=[]
        for stamp in voiced:
            if not groups or stamp-groups[-1][-1]>160:
                groups.append([])
            groups[-1].append(stamp)
        # A brief opening clause ("在的。") can be separated from the rest by
        # a natural pause. Keep it only when the previous assistant turn is
        # proven distant from the entire lookback window, with an extra 1 s
        # margin. Without that evidence retain the strict old-tail exclusion.
        previous_distant = (isinstance(self._previous_end_ms, (int, float))
                            and self._previous_end_ms <= start_ms-2700)
        if previous_distant and groups:
            onset = groups[0][0]
            quiet = [active for stamp, active in rows if onset-140 <= stamp < onset]
            if onset < default and len(quiet) >= 5 and not any(quiet):
                return max(0, start_ms-1600, onset-40)
        for group in reversed(groups):
            if group[-1] < default or group[0] > start_ms+120:
                continue
            onset=group[0]
            quiet=[active for stamp,active in rows if onset-140 <= stamp < onset]
            if onset < default and len(quiet)>=5 and not any(quiet):
                return max(0,start_ms-1600,onset-40)
            break
        return default

    def receive(self, pcm, metadata):
        if not self.turn_id:
            self.pending.append((pcm, metadata))
            return False
        stamp = metadata.get('media_ms')
        if self._seeking_onset:
            self.pending.append((pcm,metadata))
            if isinstance(stamp,(int,float)) and stamp>=self.minimum_ms:
                self._ready.extend(self._resolve_pending())
            return False
        if self.minimum_ms is not None and (stamp is None or stamp < self.minimum_ms):
            self.discarded_frames += 1
            return False
        return True
