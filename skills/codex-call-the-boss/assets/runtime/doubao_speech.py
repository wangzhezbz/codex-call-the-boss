"""Explicitly selected Doubao 2.0 phone speech; Codex still owns all answers.

Prepared reports/notices and live sentences use one pinned profile. This module
does not dial, recognize caller speech, select another voice, or retry a request.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import time
import uuid
import wave

from doubao_tts import DoubaoClient, DoubaoError, MODEL_NAME, RESOURCE_ID, SPEAKER, SAMPLE_RATE, load_private
from native_speech import NativeNoticeLibraryError, NOTICE_TEXTS, atomic_write
from speech_quality import ALIGNMENT_REVISION


RENDERER = 'doubao-2.0'
CACHE_REVISION = 'doubao-pcm48-full-utterance-v1'


def live_speech_chunks(text, start=0, *, final=False):
    """Return exact contiguous (start, end) spans, not isolated quote fragments.

    Prefer at least 32 spoken characters of sentence context. A short final
    answer still flushes; a streaming suffix waits for a complete boundary.
    The 400-character bound remains below the provider's 500-character gate.
    Never rewrite the answer or reuse an already scheduled prefix.
    """
    pairs = {'“': '”', '‘': '’', '「': '」', '『': '』'}
    closers = set(pairs.values()) | {'"'}
    stack, spans = [], []
    cursor, spoken, terminal = start, 0, False
    for index, char in enumerate(text):
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == '"':
            stack.append('"')
        if index < start:
            continue
        spoken += int(char.isalnum())
        if char in '。！？!?':
            terminal = True
        elif char not in closers and not char.isspace():
            terminal = False
        end = index + 1
        boundary = (terminal and not stack
                    and (end < len(text) or final)
                    and (end == len(text) or text[end] not in closers | set('。！？!?')))
        if (boundary and spoken >= 32) or end - cursor >= 400:
            spans.append((cursor, end))
            cursor, spoken, terminal = end, 0, False
    if final and cursor < len(text):
        # Keep a tiny final clause with its preceding context where neither
        # has been scheduled yet. Do not merge into already played speech.
        if spans and len(text) - spans[-1][0] <= 400:
            spans[-1] = (spans[-1][0], len(text))
        else:
            spans.append((cursor, len(text)))
    return spans


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(pcm)
    return output.getvalue()


class DoubaoSpeechRenderer:
    def __init__(self, *, cache_dir, validate, credential_path, client_factory=DoubaoClient):
        self.cache_dir = Path(cache_dir)
        self.credential_path = Path(credential_path)
        self.validate = validate
        self.client_factory = client_factory
        self.voice = SPEAKER
        self.cache_only = False
        self.last_spoken_text = ''
        self.rejected_evidence = []
        self._lock = asyncio.Lock()
        self._phase = 'idle'
        self.renders = self.cache_hits = self.errors = 0
        self.last_render_ms = self.last_audio_ms = 0

    def _profile(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise DoubaoError('invalid_text')
        return {'renderer': RENDERER, 'revision': CACHE_REVISION,
                'alignment_revision': ALIGNMENT_REVISION, 'model': MODEL_NAME,
                'resource_id': RESOURCE_ID, 'speaker': SPEAKER,
                'sample_rate': SAMPLE_RATE, 'format': 'pcm_s16le', 'text': text}

    def _paths(self, text):
        profile = self._profile(text)
        key = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir/(key+'.json'), self.cache_dir/(key+'.pcm'), profile

    def cached(self, text):
        metadata, audio, profile = self._paths(text)
        if metadata.is_symlink() or audio.is_symlink():
            raise DoubaoError('cache_symlink_refused')
        if not metadata.exists():
            return None
        try:
            record = json.loads(metadata.read_bytes())
            pcm = audio.read_bytes()
            if (record.get('profile') != profile or record.get('passed') is not True
                    or record.get('quality', {}).get('passed') is not True
                    or record.get('listener_rejected') is True
                    or not pcm or len(pcm) % 2
                    or record.get('pcm_sha256') != hashlib.sha256(pcm).hexdigest()):
                raise DoubaoError('cache_integrity_failed')
        except (OSError, ValueError, AttributeError) as exc:
            raise DoubaoError('cache_integrity_failed') from exc
        return pcm

    def require_cached(self, texts):
        missing = [text for text in texts if self.cached(text) is None]
        if missing:
            raise NativeNoticeLibraryError(missing)

    def notice_readiness(self):
        missing = [text for text in NOTICE_TEXTS if self.cached(text) is None]
        return {'ready': not missing, 'total': len(NOTICE_TEXTS),
                'available': len(NOTICE_TEXTS)-len(missing), 'missing': missing,
                'renderer': RENDERER, 'speaker': SPEAKER}

    async def prepare(self, texts):
        for text in texts:
            await self._synthesize(text, allow_generation=True)

    async def synthesize(self, text):
        return await self._synthesize(text, allow_generation=not self.cache_only)

    async def synthesize_live(self, text):
        # Only classified/current answer sentences call this entry point.
        # Missing prepared notices/reports must never enter it as a fallback.
        if text in NOTICE_TEXTS:
            self.require_cached([text])
            return await self._synthesize(text, allow_generation=False)
        return await self._synthesize(text, allow_generation=True)

    async def _synthesize(self, text, *, allow_generation):
        async with self._lock:
            self.last_spoken_text = ''
            pcm = self.cached(text)
            if pcm is not None:
                self.cache_hits += 1
                self.last_spoken_text = text
                return pcm
            if not allow_generation:
                raise DoubaoError('prepared_audio_missing')
            metadata, audio_path, profile = self._paths(text)
            key = load_private(self.credential_path)
            client = self.client_factory(key)
            self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.cache_dir/'attempts').mkdir(mode=0o700, exist_ok=True)
            attempt = self.cache_dir/'attempts'/uuid.uuid4().hex
            attempt.mkdir(mode=0o700, parents=True, exist_ok=False)
            record = {'profile': profile, 'passed': False, 'human_listening_verified': False,
                      'phone_hearing_verified': False, 'network_attempts': 1}
            started = time.monotonic()
            try:
                # One absolute bound includes synthesis plus owned offline QA.
                async with asyncio.timeout(35):
                    self._phase = 'synthesis'
                    self.renders += 1
                    pcm = await client.synthesize(text, timeout=20)
                    path = attempt/'audio.wav'
                    atomic_write(path, wav_bytes(pcm))
                    self._phase = 'content_validation'
                    quality = await self.validate(pcm, text, text, path)
                    record.update(quality=quality, pcm_sha256=hashlib.sha256(pcm).hexdigest())
                    if quality.get('passed') is not True:
                        raise DoubaoError('speech_content_rejected')
                    record.update(passed=True, attempt=str(attempt), provider=client.last_result)
                    # Keep the returned waveform intact: no clipping, time
                    # stretching, old-address splice or another-voice fallback.
                    atomic_write(audio_path, pcm)
                    atomic_write(metadata, json.dumps(record, ensure_ascii=False, indent=2).encode())
                    self.last_spoken_text = text
                    self.last_audio_ms = round(len(pcm) / 2 / SAMPLE_RATE * 1000)
                    return pcm
            except (Exception, asyncio.CancelledError) as exc:
                self.errors += 1
                record.update(error_code=getattr(exc, 'code', type(exc).__name__),
                              provider=client.last_result)
                self.rejected_evidence.append(str(attempt/'result.json'))
                raise
            finally:
                self.last_render_ms = round((time.monotonic()-started)*1000)
                record.update(elapsed_ms=self.last_render_ms, phase=self._phase)
                atomic_write(attempt/'result.json', json.dumps(record, ensure_ascii=False, indent=2).encode())
                self._phase = 'idle'

    async def close(self):
        # Each synthesis owns and closes its bounded connection, including
        # cancellation. No background socket is kept alive across calls.
        return None

    def diagnostics(self):
        return {'renderer': RENDERER, 'model': MODEL_NAME, 'voice': SPEAKER,
                'sample_rate': SAMPLE_RATE, 'synthesis_count': self.renders,
                'cache_hits': self.cache_hits, 'error_count': self.errors,
                'last_render_ms': self.last_render_ms, 'last_audio_ms': self.last_audio_ms,
                'phase': self._phase, 'human_listening_verified': False,
                'phone_hearing_verified': False}
