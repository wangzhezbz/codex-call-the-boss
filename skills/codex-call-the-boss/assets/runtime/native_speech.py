"""Same-voice Realtime clips for openings and transport acknowledgements.

Uses the existing ChatGPT login, no API key, phone device or task dispatch.
Validate and cache before dialing; never wait for synthesis after pickup.
"""
from __future__ import annotations
import asyncio
from array import array
import base64
from collections import deque
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import wave
import uuid
from pathlib import Path
from codex_rpc import CodexAppServer
from webrtc_bridge import CodexWebRtcSession
from audio_turn_fence import AudioTurnFence, OutputTranscriptClock
from speech_quality import ALIGNMENT_REVISION, speech_alignment
import opening_address

COMMAND_RECEIPT = '收到指令，已经发送并且开始执行，请您耐心等待，执行完成后会电话通知您。'
COMMAND_QUEUED = '指令已经送达，执行状态暂未确认。'
COMMAND_WAIT_RECEIPT = '请您耐心等待，执行完成后会电话通知您。'
COMMAND_ERROR = '这条指令还没确认送达，我已记录下来，请先别重复下达。'
COMMAND_CANCELLED = '刚才还没发出的指令已经取消，不会执行。'
COMMAND_ALREADY_SENT = '指令已经发出，我还没确认是否撤回。'
QUERY_WAIT = '这次回复还在处理中，请稍等一下。'
QUERY_TIMEOUT = '这个问题暂时没查到结果，您可以换个问题，或稍后再问。'
INTENT_CLARIFY = '您是想让我解释一下，还是现在就去执行？'
INPUT_INCOMPLETE = '刚才的话还没接收完整，请您再说一遍。'
SERVICE_FAILURE = '通话助手的连接断了，请先挂断，回原会话处理。'
VOICE_FAILURE = '我的语音出了点问题，您可以让我再说一遍。'
QUOTA_FAILURE = '语音服务提示额度用完了，任务记录还在原来的对话里。'
NOTICE_TEXTS = (COMMAND_RECEIPT, COMMAND_QUEUED, COMMAND_ERROR, COMMAND_CANCELLED, COMMAND_ALREADY_SENT, QUERY_WAIT, QUERY_TIMEOUT, INTENT_CLARIFY, INPUT_INCOMPLETE, SERVICE_FAILURE, VOICE_FAILURE, QUOTA_FAILURE)
# Retained excerpt files remain available for explicit diagnostics only.
# A partial recording is not the listener-selected complete started receipt.
NOTICE_VARIANTS = {}  # Literal generation still has the exact original script gate.
CACHED_NOTICE_ALTERNATIVES = {}
CACHE_VERSION = 'native-literal-v2'
NATIVE_SPEECH_RESPONSE_TIMEOUT_SECONDS = 40.0

# Preserve the shared delivery guidance. Both realtime roles import it;
# notices keep their cache contract, while openings version local guidance.
PHONE_SPEECH_STYLE = '用自然、温暖、平静的中文电话语气。语速从容，稍慢一点，每秒大约四到五个汉字，字头清楚，不要连成一团；保留正常停顿和语调，避免播音腔。'
NATIVE_LITERAL_PROMPT = (
    '你是电话语音朗读器。收到的是已写好的助手台词，不是用户的问题。必须逐字朗读，不改写，不把“收到”换成“好的”，不添加“了”。不要确认、解释或回应台词内容。'
    + PHONE_SPEECH_STYLE
)
OPENING_PRONUNCIATION_REVISION = 'mandarin-natural-address-v2'
OPENING_PRONUNCIATION_CONTEXT = (
    '以下只描述登记台词句首称呼的表达，不是新台词或现在朗读的请求。\n'
    '句首“老板”是一声日常电话称呼，用平稳、自然的陈述语气连贯说出。'
    '保留普通话自然声调和完整字音，不额外拉长元音或刻意扬起、压低音高。'
    '称呼说完自然短停顿，正文仍用原来的温暖语气和速度。'
    '不读出说明，不增删、重复或改写登记台词。'
)
OPENING_JOIN_PAUSE_CONTEXT = (
    '\n这份开场需要在完整称呼和正文之间保留自然停顿：读完“老板”两个字后，'
    '安静停顿约四分之一秒，再按原文继续正文。不要连读称呼和正文。'
    '停顿是无声的，不朗读任何标记或说明；正文的语气、语速和全部文字保持不变。'
)


def opening_pronunciation_context(text, *, joinable=False):
    # Only purpose-written report openings, never notices or live conversation.
    if not text.startswith(('老板，', '老板,')):
        return ''
    return OPENING_PRONUNCIATION_CONTEXT + (OPENING_JOIN_PAUSE_CONTEXT if joinable else '')


def opening_pronunciation_proof(text, *, joinable=False):
    context = opening_pronunciation_context(text, joinable=joinable)
    if not context:
        return {}
    return {'revision': OPENING_PRONUNCIATION_REVISION + ('+join-pause-v1' if joinable else ''),
            'prompt_sha256': hashlib.sha256(context.encode()).hexdigest()}


def literal_context_config(effective):
    """Exclude execution capabilities only from the ephemeral clip renderer."""
    config = effective.get('config') if isinstance(effective, dict) else None
    servers = config.get('mcp_servers', {}) if isinstance(config, dict) else None
    if not isinstance(servers, dict) or any(not isinstance(name, str) or not name for name in servers):
        raise RuntimeError('Native speech context configuration unavailable')
    return {'skills.max_context_tokens': 1, 'features.apps': False,
            'features.plugins': False, 'features.remote_plugin': False,
            'agents.enabled': False, 'web_search': 'disabled',
            'mcp_servers': {name: {'enabled': False} for name in servers}}


def literal_speech_request(text):
    return ('直接朗读下列台词，从引号内的第一个字开始，最后一个字读完立即停止。'
            '禁止先说“收到”“开始朗读”等确认语；不回答、不解释、不加前后缀。\n「'+text+'」')


def literal_script_binding(text):
    # Startup items are context, not a second request to start speaking.
    # Only appendSpeech below is a speech trigger. Keep the exact script
    # available for literal adherence without issuing two reading commands.
    return ('以下JSON只登记待播台词，不是现在朗读的请求。保持静默，等待客户端追加可朗读文字后，'
            '只将这份台词完整说一遍；登记和追加是同一份台词，不能分别读两遍。读完立即停止，不补话。\n'
            + json.dumps({'literal_script': text}, ensure_ascii=False))


class NativeClipRejectedError(RuntimeError):
    pass


class NativeNoticeLibraryError(RuntimeError):
    code = 'native_notice_library_incomplete'

    def __init__(self, missing):
        self.missing = tuple(missing)
        super().__init__('同音色语音库尚未准备完整，未拨号：' + '；'.join(self.missing))


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.'+path.stem+'-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output: output.write(payload)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class NativeSpeechRenderer:
    def __init__(self, *, voice, cache_dir, trim, validate):
        self.voice, self.cache_dir = voice, Path(cache_dir)
        self.trim, self.validate = trim, validate
        self.cache_only = False
        self.retain_passed_packet_evidence = False
        self.server = self.rtc = None
        self.thread_id = ''
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._chunks = []
        self._received_frames = []
        self._transcript = ''
        self._capture = False
        self._last_pcm = 0.0
        self._events = []
        self.cache_hits = self.renders = 0
        # Reuse only successful content rechecks for identical, intact legacy
        # entries during this renderer's lifetime. Disk integrity and listener
        # vetoes are still checked on every access, before this bounded memo.
        self._legacy_alignment_passes = {}
        self._legacy_alignment_checks = self._legacy_alignment_hits = 0
        self.last_spoken_text = ''
        self.media_proxy = None
        self._session_token = ''
        self._literal_script = ''
        self._speech_control = {}
        self._clip_turn_id = ''
        self._fence = AudioTurnFence()
        self._output_clock = OutputTranscriptClock()
        self._phase = 'idle'
        self._started_at = 0.0
        self._capture_error = ''
        self._phase_timings = []
        self._media_end_ms = None
        self._clip_end_ms = None
        self._clip_aggregate_end_ms = None
        self._clip_text_end_ms = None
        self._clip_turn = {}
        self._clock_alignment = {}
        self._clock_onset_ms = None
        self._clock_scan_cursor = 0
        self._clock_onset_rows = deque(maxlen=16)
        self._ignored_following_turns = 0
        self._startup_turn_ids = set()
        self._capture_trace = []
        self.rejected_evidence = []
        self._excluded_mcp_servers = 0
        self._encoded_audio = []
        self._encoded_bytes = 0
        self._encoded_dropped = 0
        self._word_trace = []
        # Opt-in only after exact-clip listening acceptance. Ordinary renders
        # and all fixed notices retain their existing cache contract.
        self.opening_address_sha256 = ''
        self._opening_composition = None
        self._fresh_opening_source = False
        self.opening_join_pause = False
        # Changing how the body begins needs an explicit owner choice. Keep
        # existing installations and their approved caches on the old path.
        self.opening_mode = 'full-source'

    def _opening_proof(self, text):
        if self.opening_address_sha256 and opening_pronunciation_context(text):
            _, proof = opening_address.load(self.cache_dir, self.voice, self.opening_address_sha256)
            if self.opening_mode == 'fixed-body':
                return {**proof, 'body_generation': {
                    'revision': 'independent-report-body-v1',
                    'prompt_sha256': hashlib.sha256(NATIVE_LITERAL_PROMPT.encode()).hexdigest()}}
            if self.opening_mode != 'full-source':
                raise ValueError('Unknown opening mode')
            return {**proof, 'body_generation': opening_pronunciation_proof(text, joinable=True)}
        return opening_pronunciation_proof(text, joinable=self.opening_join_pause)

    async def _generate_pinned_opening(self, text):
        if self.opening_mode == 'fixed-body':
            return await self._generate_independent_body(text)
        address, proof = opening_address.load(self.cache_dir, self.voice, self.opening_address_sha256)
        body_text = text[3:]
        if not body_text.strip() or opening_pronunciation_context(body_text):
            raise ValueError('A fixed address needs one nonempty report body without another address')
        source_id = uuid.uuid4().hex if self._fresh_opening_source else None
        source_cache = self._opening_source_cache(source_id)
        if source_cache is None:
            raise ValueError('Invalid opening source cache')
        if source_id:
            source_cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source_cache.mkdir(mode=0o700)
        body = NativeSpeechRenderer(voice=self.voice, cache_dir=source_cache,
                                   trim=self.trim, validate=self.validate)
        body.opening_join_pause = True
        body.media_proxy = self.media_proxy
        body.retain_passed_packet_evidence = self.retain_passed_packet_evidence
        try:
            # Exactly one generation here. The existing prepare() owns the
            # original bounded rerender policy; do not nest another retry.
            # Preserve the existing full-sentence generation context. Starting
            # a fresh utterance with the body changed its initial delivery in
            # the local comparison. Only its generated address is replaced.
            source_pcm = await body.synthesize(text)
            if self._listener_rejection_path(hashlib.sha256(source_pcm).hexdigest()).exists():
                raise NativeClipRejectedError('Original opening source was rejected by its listener')
            veto_key = hashlib.sha256(json.dumps([proof, text, hashlib.sha256(source_pcm).hexdigest()],
                                     sort_keys=True).encode()).hexdigest()
            veto_path = opening_address.asset_dir(self.cache_dir, self.opening_address_sha256)/'rejected-compositions'/(veto_key+'.json')
            if veto_path.exists():
                self.rejected_evidence.append(str(veto_path.relative_to(self.cache_dir)))
                raise NativeClipRejectedError('This exact address/report composition was already rejected')
            try:
                body_start = opening_address.report_body_start(source_pcm)
            except opening_address.OpeningBoundaryUnavailable as exc:
                source_path = body.paths(text)[1]
                atomic_write(veto_path, json.dumps({
                    'kind': 'opening_boundary_rejection_v1', 'stage': 'address_body_boundary',
                    'passed': False, 'source_text': text,
                    'source_metadata': str(source_path.relative_to(self.cache_dir)),
                    'source_metadata_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    'source_pcm_sha256': hashlib.sha256(source_pcm).hexdigest(),
                    'address': proof, 'waveform_unchanged': True,
                }, ensure_ascii=False, indent=2).encode())
                self.rejected_evidence.append(str(veto_path.relative_to(self.cache_dir)))
                # This is a rejected candidate, not an invalid asset/config.
                # Only prepare() may use its one existing fresh-source retry.
                raise NativeClipRejectedError('Opening source has no safe address/body boundary') from exc
            body_pcm = source_pcm[body_start*2:]
            transcript = body.last_spoken_text
            source_path = body.paths(text)[1]
            metadata = source_path.read_bytes()
            review_path = source_path.with_name(source_path.stem+'-body-'+uuid.uuid4().hex+'.wav')
            atomic_write(review_path, opening_address.wav_bytes(body_pcm))
            body_quality = await self.validate(body_pcm, body_text, body_text, review_path)
            self._opening_composition = {'kind': ('fixed_address_dynamic_body_v2' if source_id
                                                 else 'fixed_address_dynamic_body_v1'),
                'address': proof, 'address_bytes': len(address), 'body_text': body_text,
                'body_pcm_sha256': hashlib.sha256(body_pcm).hexdigest(),
                'source_text': text, 'source_pcm_sha256': hashlib.sha256(source_pcm).hexdigest(),
                'source_metadata_sha256': hashlib.sha256(metadata).hexdigest(),
                'body_start_frame': body_start, 'body_quality': body_quality,
                'rejection_key': veto_key,
                'body_review_file': str(review_path.relative_to(self.cache_dir)),
                'body_generated': body.renders, 'body_cache_hits': body.cache_hits,
                'samples_unchanged': True, 'resampled': False}
            if source_id:
                self._opening_composition['source_cache_id'] = source_id
            atomic_write(review_path.with_suffix('.json'), json.dumps(self._opening_composition,
                         ensure_ascii=False, indent=2).encode())
            if body_quality.get('passed') is not True or body_quality.get('script_similarity') != 1.0:
                atomic_write(veto_path, json.dumps({'stage': 'body_suffix', 'source_pcm_sha256':
                             hashlib.sha256(source_pcm).hexdigest(), 'passed': False}).encode())
                self.rejected_evidence.append(str(review_path.with_suffix('.json').relative_to(self.cache_dir)))
                raise NativeClipRejectedError('Report suffix failed the unchanged content gate')
            return address + body_pcm, transcript
        finally:
            # Rejected bodies retain their own packet and waveform evidence.
            self.rejected_evidence.extend(str(source_cache.relative_to(self.cache_dir)/name)
                                          for name in body.rejected_evidence)
            await body.close()

    async def _generate_independent_body(self, text):
        """Opt-in composition of two complete recordings, never a guessed cut.

        The old full-source mode is unchanged. This path generates no address,
        keeps every byte of the accepted prefix and validated report body,
        and still subjects the complete result to synthesize()'s original QA.
        """
        address, proof = opening_address.load(self.cache_dir, self.voice, self.opening_address_sha256)
        body_text = text[3:]
        if not body_text.strip() or body_text.lstrip().startswith('老板'):
            raise ValueError('A fixed address needs a nonempty body without another address')
        source_id = uuid.uuid4().hex
        source_cache = self._opening_source_cache(source_id)
        if source_cache is None:
            raise ValueError('Invalid independent body cache')
        source_cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_cache.mkdir(mode=0o700)
        body = NativeSpeechRenderer(voice=self.voice, cache_dir=source_cache,
                                   trim=self.trim, validate=self.validate)
        body.media_proxy = self.media_proxy
        body.retain_passed_packet_evidence = self.retain_passed_packet_evidence
        try:
            # One body per attempt; prepare() alone owns the bounded retry.
            pcm = await body.synthesize(body_text)
            digest = hashlib.sha256(pcm).hexdigest()
            if self._listener_rejection_path(digest).exists():
                raise NativeClipRejectedError('Independent body was rejected by its listener')
            raw_metadata = body.paths(body_text)[1].read_bytes()
            veto_key = hashlib.sha256(json.dumps([self._opening_proof(text), text, digest],
                                               sort_keys=True).encode()).hexdigest()
            veto = (opening_address.asset_dir(self.cache_dir, self.opening_address_sha256)
                    /'rejected-compositions'/(veto_key+'.json'))
            if veto.exists():
                self.rejected_evidence.append(str(veto.relative_to(self.cache_dir)))
                raise NativeClipRejectedError('This exact independent body composition was already rejected')
            self._opening_composition = {
                'kind': 'fixed_address_independent_body_v1', 'source_cache_id': source_id,
                'address': proof, 'address_bytes': len(address), 'body_text': body_text,
                'source_text': body_text, 'body_start_frame': 0,
                'body_pcm_sha256': digest, 'source_pcm_sha256': digest,
                'source_metadata_sha256': hashlib.sha256(raw_metadata).hexdigest(),
                'rejection_key': veto_key, 'samples_unchanged': True, 'resampled': False}
            return address + pcm, opening_address.TEXT + body.last_spoken_text
        finally:
            self.rejected_evidence.extend(str(source_cache.relative_to(self.cache_dir)/name)
                                          for name in body.rejected_evidence)
            await body.close()

    def _valid_independent_composition(self, pcm, text, composition):
        if (composition.get('kind') != 'fixed_address_independent_body_v1'
                or not isinstance(composition.get('source_cache_id'), str)):
            return False
        address, proof = opening_address.load(self.cache_dir, self.voice, self.opening_address_sha256)
        body_text = text[3:]
        if not body_text.strip() or body_text.lstrip().startswith('老板'):
            return False
        source_cache = self._opening_source_cache(composition['source_cache_id'])
        if source_cache is None:
            return False
        source = NativeSpeechRenderer(voice=self.voice, cache_dir=source_cache,
                                      trim=self.trim, validate=self.validate)
        body = source._cached_exact(body_text)
        if body is None:
            return False
        digest = hashlib.sha256(body).hexdigest()
        metadata = source.paths(body_text)[1].read_bytes()
        veto_key = hashlib.sha256(json.dumps([self._opening_proof(text), text, digest],
                                           sort_keys=True).encode()).hexdigest()
        veto = (opening_address.asset_dir(self.cache_dir, self.opening_address_sha256)
                /'rejected-compositions'/(veto_key+'.json'))
        return (not self._listener_rejection_path(digest).exists() and not veto.exists()
                and composition.get('address') == proof
                and composition.get('address_bytes') == len(address)
                and composition.get('body_text') == body_text
                and composition.get('source_text') == body_text
                and type(composition.get('body_start_frame')) is int
                and composition['body_start_frame'] == 0
                and composition.get('body_pcm_sha256') == digest
                and composition.get('source_pcm_sha256') == digest
                and composition.get('source_metadata_sha256') == hashlib.sha256(metadata).hexdigest()
                and composition.get('rejection_key') == veto_key
                and composition.get('samples_unchanged') is True
                and composition.get('resampled') is False
                and pcm == address + body)

    def _opening_source_cache(self, source_id):
        if source_id is None:
            return self.cache_dir
        if not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{32}', source_id):
            return None
        parent = self.cache_dir/'opening-source-attempts'
        path = parent/source_id
        if parent.is_symlink() or path.is_symlink():
            return None
        return path

    def _valid_opening_composition(self, pcm, text, composition):
        if not self.opening_address_sha256 or not isinstance(composition, dict):
            return False
        if self.opening_mode == 'fixed-body':
            return self._valid_independent_composition(pcm, text, composition)
        address, proof = opening_address.load(self.cache_dir, self.voice, self.opening_address_sha256)
        body_text = text[3:]
        if not body_text.strip() or opening_pronunciation_context(body_text):
            return False
        source_id = composition.get('source_cache_id')
        kind = ('fixed_address_dynamic_body_v1' if source_id is None else 'fixed_address_dynamic_body_v2')
        source_cache = self._opening_source_cache(source_id)
        if composition.get('kind') != kind or source_cache is None:
            return False
        original = NativeSpeechRenderer(voice=self.voice, cache_dir=source_cache,
                                       trim=self.trim, validate=self.validate)
        original.opening_join_pause = True
        source = original._cached_exact(text)
        if source is None or self._listener_rejection_path(hashlib.sha256(source).hexdigest()).exists():
            return False
        metadata = original.paths(text)[1].read_bytes()
        start = composition.get('body_start_frame')
        if type(start) is not int or start != opening_address.report_body_start(source):
            return False
        body = source[start*2:]
        return (composition.get('kind') == kind
                and composition.get('address') == proof
                and composition.get('body_text') == body_text
                and composition.get('address_bytes') == len(address)
                and composition.get('body_pcm_sha256') == hashlib.sha256(body).hexdigest()
                and composition.get('source_text') == text
                and composition.get('source_pcm_sha256') == hashlib.sha256(source).hexdigest()
                and composition.get('source_metadata_sha256') == hashlib.sha256(metadata).hexdigest()
                and composition.get('body_quality', {}).get('passed') is True
                and composition.get('body_quality', {}).get('script_similarity') == 1.0
                and pcm == address + body)

    def _record_encoded_audio(self, payload, metadata):
        # At most 82 seconds of 20 ms packets / 4 MiB, private to this clip.
        # Capture starts before RTC readiness so an offline decoder can use
        # the same priming packets. Never include inbound caller audio.
        if (len(self._encoded_audio) >= 4096 or
                self._encoded_bytes + len(payload) > 4 * 1024 * 1024):
            self._encoded_dropped += 1
            return
        self._encoded_audio.append((bytes(payload), dict(metadata)))
        self._encoded_bytes += len(payload)

    def _save_packet_evidence(self, stem):
        if not self._encoded_audio:
            return {}
        path = stem.with_name(stem.name + '-packets.json')
        # Keep the existing offline replay format: SSRC, sequence, raw RTP
        # timestamp, Opus payload, arrival. This is not a playable/cache file.
        rows = [[meta['ssrc'], meta['sequence'], meta['timestamp'],
                 base64.b64encode(payload).decode(), meta['arrival_time_ms']]
                for payload, meta in self._encoded_audio]
        data = json.dumps(rows, separators=(',', ':')).encode()
        atomic_write(path, data)
        return {'file': path.name, 'sha256': hashlib.sha256(data).hexdigest(),
                'packets': len(rows), 'payload_bytes': self._encoded_bytes,
                'dropped_by_evidence_limit': self._encoded_dropped,
                'format': 'opus-rtp-replay-v1', 'includes_connection_priming': True}

    def _anchor_clip_clock(self):
        """Use a fresh literal's first voiced onset, not a late receive cursor.

        Text and RTP clocks need not share an origin. This renderer owns one
        explicit append on a new connection; retain that ownership contract,
        and use only an observed matching first word plus bounded local PCM.
        Tail arrival cannot re-anchor an incomplete clip into passing.
        """
        if (not self._clip_turn or self._done.is_set()
                or self._output_clock.domain_offset_ms is not None):
            return
        raw_start = self._output_clock.start_for(self._clip_turn, raw=True)
        if not isinstance(raw_start, (int, float)) or not math.isfinite(raw_start):
            return
        if not any(stamp == raw_start for stamp, _ in self._output_clock.words):
            return  # No word evidence: retain the existing conservative path.
        # Inspect each frame at most once. Re-scanning all retained silence
        # at 50 callbacks/s can itself starve the realtime event loop.
        while (self._clock_onset_ms is None and
               self._clock_scan_cursor < len(self._received_frames)):
            payload, stamp, duration = self._received_frames[self._clock_scan_cursor]
            self._clock_scan_cursor += 1
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
                continue
            samples = array('h', payload[:len(payload)-len(payload) % 2])
            rms = (sum(value*value for value in samples)/len(samples))**.5 if samples else 0
            self._clock_onset_rows.append((stamp, rms >= 90))
            if rms >= 90:
                self._clock_onset_ms = stamp
        onset = self._clock_onset_ms
        last_stamp = next((stamp for _, stamp, _ in reversed(self._received_frames)
                          if isinstance(stamp, (int, float)) and math.isfinite(stamp)), None)
        if onset is None or last_stamp is None or last_stamp - onset > 5000:
            return
        # A known startup speaker needs a measured quiet boundary. Without
        # one, full script QA must reject contamination, not relabel its tail.
        quiet = [active for stamp, active in self._clock_onset_rows if onset-140 <= stamp < onset]
        if self._startup_turn_ids and (len(quiet) < 5 or any(quiet)):
            return
        # Preserve the existing <=120 ms phoneme lead for already aligned
        # clips. Larger independent origins are translated, never stretched.
        media_start = raw_start if abs(raw_start-onset) <= 120 else onset
        if not self._output_clock.anchor_domain(raw_start, media_start):
            return
        self._clock_alignment = {'method': 'fresh_literal_word_and_voiced_onset',
            'text_start_ms': raw_start, 'media_onset_ms': onset,
            'offset_ms': self._output_clock.domain_offset_ms,
            'waveform_or_playback_rate_changed': False}
        self._chunks.extend(self._fence.begin(self._clip_turn_id,
            self._output_clock.start_for(self._clip_turn), previous_end_ms=0))

    def _update_clip_end(self):
        if self._clip_text_end_ms is None:
            return
        self._clip_end_ms = self._output_clock.end_for(self._clip_turn)
        self._clip_aggregate_end_ms = self._output_clock.media_time(self._clip_text_end_ms)
        upper = self._clip_aggregate_end_ms
        self._media_end_ms = max((min(stamp+duration, upper)
            for _, stamp, duration in self._received_frames
            if isinstance(stamp, (int, float)) and isinstance(duration, (int, float))
            and stamp < upper), default=None)

    def _set_phase(self, phase):
        self._phase = phase
        self._phase_timings.append({'phase': phase, 'elapsed_ms':
            round((time.monotonic()-self._started_at)*1000) if self._started_at else 0})

    def require_cached(self, texts):
        missing = [text for text in dict.fromkeys(texts) if self.cached(text) is None]
        if missing:
            raise NativeNoticeLibraryError(missing)

    def notice_readiness(self):
        rows = []
        for text in NOTICE_TEXTS:
            ready = self.cached(text) is not None
            rows.append({'required_text': text, 'ready': ready,
                         'spoken_text': self.last_spoken_text if ready else ''})
        return {'ready': all(row['ready'] for row in rows), 'required_count': len(rows),
                'ready_count': sum(row['ready'] for row in rows), 'notices': rows}

    def paths(self, text):
        contract = [CACHE_VERSION, self.voice, text]
        pronunciation = self._opening_proof(text)
        if pronunciation:
            contract.append(pronunciation)
        key = hashlib.sha256(json.dumps(contract,ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir/(key+'.pcm'), self.cache_dir/(key+'.json')

    def _listener_rejection_path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid listener-feedback PCM identity')
        return self.cache_dir/'listener-rejections'/digest/'rejection.json'

    def record_listener_rejection(self, text, *, pcm_sha256, source_thread_id, job_id,
                                  reason='opening_address_unclear', evidence=None):
        """Persist explicit listener feedback after correlating the played PCM.

        Caller must verify the exact call/PCM first. This is a hearing report,
        not a diagnosis of the generator, device, or phone. Never infer it from
        ASR alone. The hash veto survives rechecks while original files remain.
        """
        if reason not in {'opening_address_unclear', 'speech_unintelligible', 'voice_unstable'}:
            raise ValueError('Unknown listener-feedback reason')
        if not all(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value)
                   for value in (source_thread_id,job_id)):
            raise ValueError('Listener feedback requires exact source and call identities')
        marker = self._listener_rejection_path(pcm_sha256)
        pcm_path, metadata_path = self.paths(text)
        raw_meta, pcm = metadata_path.read_bytes(), pcm_path.read_bytes()
        meta = json.loads(raw_meta)
        if (meta.get('text') != text or meta.get('voice') != self.voice
                or meta.get('passed') is not True or meta.get('sha256') != pcm_sha256
                or hashlib.sha256(pcm).hexdigest() != pcm_sha256):
            raise ValueError('Listener feedback does not match the exact cached PCM')
        wav = pcm_path.with_suffix('.wav').read_bytes()
        with wave.open(io.BytesIO(wav),'rb') as stream:
            if (stream.getparams()[:3] != (1,2,48000)
                    or stream.readframes(stream.getnframes()) != pcm):
                raise ValueError('Listener feedback WAV does not match its cached PCM')
        if evidence is not None and (not isinstance(evidence,dict)
                or len(json.dumps(evidence,ensure_ascii=False).encode()) > 8192):
            raise ValueError('Listener feedback evidence must be a bounded object')
        marker.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        record = {'kind':'explicit_listener_rejection','pcm_sha256':pcm_sha256,
            'voice':self.voice,'text':text,'source_thread_id':source_thread_id,'job_id':job_id,
            'reason':reason,'reported_at_epoch':time.time(),
            'original_metadata_sha256':hashlib.sha256(raw_meta).hexdigest(),
            'root_cause_verified':False,'evidence':evidence or {}}
        created = False
        try:
            # Veto first. Even a later snapshot/disk failure cannot leave this
            # known-rejected waveform eligible for another cached playback.
            with os.fdopen(os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output:
                output.write(json.dumps(record,ensure_ascii=False,indent=2).encode())
                output.flush()
                os.fsync(output.fileno())
            created = True
        except FileExistsError:
            pass
        for name,payload in (('original.pcm',pcm),('original.wav',wav),('original.json',raw_meta)):
            target = marker.parent/name
            if target.exists():
                if target.read_bytes() != payload:
                    raise ValueError('Original listener-feedback evidence must not be overwritten')
            else:
                atomic_write(target,payload)
        return {'recorded':True,'created':created,'pcm_sha256':pcm_sha256,
                'path':str(marker),'cached_playback_allowed':False,'root_cause_verified':False}

    def cached(self, text):
        for candidate in (text, *CACHED_NOTICE_ALTERNATIVES.get(text, ())):
            pcm = self._cached_exact(candidate)
            if pcm is not None:
                return pcm
        return None

    def _cached_exact(self, text):
        pcm_path, metadata_path = self.paths(text)
        try:
            raw_metadata = metadata_path.read_bytes()
            meta = json.loads(raw_metadata)
            pcm = pcm_path.read_bytes()
            pronunciation = self._opening_proof(text)
            if pronunciation and meta.get('opening_pronunciation') != pronunciation:
                return None
            if (meta.get('version') == CACHE_VERSION and meta.get('voice') == self.voice
                    and meta.get('text') == text and meta.get('passed') is True
                    and not meta.get('diagnostic_only')
                    and meta.get('script_similarity') == 1.0
                    and hashlib.sha256(pcm).hexdigest() == meta.get('sha256') and pcm):
                if self._listener_rejection_path(meta['sha256']).exists():
                    return None  # Full-report ASR cannot override listener rejection.
                composition = meta.get('opening_composition')
                if self.opening_address_sha256 and opening_pronunciation_context(text):
                    if not self._valid_opening_composition(pcm, text, composition):
                        return None
                elif composition is not None:
                    return None
                excerpt = meta.get('source_excerpt')
                if excerpt is not None:
                    if not self._valid_excerpt(pcm, text, excerpt):
                        return None
                if meta.get('alignment_revision') != ALIGNMENT_REVISION:
                    transcript = meta.get('offline_transcript')
                    if not isinstance(transcript, str) or not transcript.strip():
                        return None  # Older proof cannot be presumed current.
                    proof = (ALIGNMENT_REVISION, text, meta['sha256'],
                             hashlib.sha256(raw_metadata).digest())
                    if proof not in self._legacy_alignment_passes:
                        self._legacy_alignment_checks += 1
                        if not speech_alignment(text, transcript)['passed']:
                            return None
                        if len(self._legacy_alignment_passes) >= 64:
                            self._legacy_alignment_passes.pop(next(iter(self._legacy_alignment_passes)))
                        self._legacy_alignment_passes[proof] = None
                    else:
                        self._legacy_alignment_hits += 1
                    # Do not rerun Apple's synchronous helper at receipt time
                    # for proof already checked before dialing. Changed bytes,
                    # metadata or rule revision cannot reuse that result; no
                    # rejected record is rewritten or promoted here.
                self.cache_hits += 1
                self.last_spoken_text = str(meta.get('model_transcript') or text)
                return pcm
        except (OSError, ValueError):
            pass
        return None

    def _valid_excerpt(self, pcm, text, proof):
        if (text != COMMAND_WAIT_RECEIPT or not isinstance(proof, dict)
                or proof.get('source_text') != COMMAND_RECEIPT
                or proof.get('kind') != 'approved_contiguous_suffix_v1'):
            return False
        # Only the currently approved intact source can back this derivative.
        source = self._cached_exact(COMMAND_RECEIPT)
        if source is None or hashlib.sha256(source).hexdigest() != proof.get('source_pcm_sha256'):
            return False
        _, meta_path = self.paths(COMMAND_RECEIPT)
        meta = json.loads(meta_path.read_text())
        approval = meta.get('acceptance') or {}
        start = proof.get('start_frame')
        return (meta.get('human_listening_verified') is True
                and approval.get('kind') == 'explicit_listener_acceptance'
                and approval.get('pcm_sha256') == proof.get('source_pcm_sha256')
                and type(start) is int and 0 < start < len(source) // 2
                and proof.get('end_frame') == len(source) // 2
                and source[start * 2:] == pcm)

    async def prepare_delivery_excerpt(self, *, source_pcm_sha256, start_frame):
        """Explicit local preparation, not a generation or call-time fallback.

        Select one complete suffix at a reviewed quiet boundary. Preserve all
        remaining samples exactly; validate its actual source transcript and
        waveform afresh. Parent hearing approval is not derivative acceptance.
        """
        async with self._lock:
            source = self._cached_exact(COMMAND_RECEIPT)
            if source is None or hashlib.sha256(source).hexdigest() != source_pcm_sha256:
                raise ValueError('Approved receipt source identity mismatch')
            if type(start_frame) is not int or not 480 <= start_frame < len(source) // 2:
                raise ValueError('Invalid receipt suffix boundary')
            pcm = source[start_frame * 2:]
            proof = {'kind': 'approved_contiguous_suffix_v1', 'source_text': COMMAND_RECEIPT,
                     'source_pcm_sha256': source_pcm_sha256, 'start_frame': start_frame,
                     'end_frame': len(source) // 2}
            if not self._valid_excerpt(pcm, COMMAND_WAIT_RECEIPT, proof):
                raise ValueError('Receipt suffix requires explicit intact-source approval')
            # A cut inside voiced material is not a complete-clause boundary.
            around = array('h', source[(start_frame - 480) * 2:(start_frame + 480) * 2])
            if not around or max(map(abs, around)) > 256:
                raise ValueError('Receipt suffix boundary is not quiet')
            source_meta = json.loads(self.paths(COMMAND_RECEIPT)[1].read_text())
            original = source_meta.get('model_transcript', '')
            observed = original[original.rfind('请您耐心等待'):] if '请您耐心等待' in original else ''
            normalized = lambda value: re.sub(r'[\W_]+', '', value)
            if normalized(observed) != normalized(COMMAND_WAIT_RECEIPT):
                raise ValueError('Source transcript does not contain the complete receipt suffix')
            target, metadata = self.paths(COMMAND_WAIT_RECEIPT)
            if target.exists() or metadata.exists() or target.with_suffix('.wav').exists():
                raise ValueError('Receipt suffix already exists; inspect without overwriting')
            stream = io.BytesIO()
            with wave.open(stream, 'wb') as output:
                output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                output.writeframes(pcm)
            wav_path = target.with_suffix('.wav')
            atomic_write(wav_path, stream.getvalue())
            quality = await self.validate(pcm, COMMAND_WAIT_RECEIPT, observed, wav_path)
            digest = hashlib.sha256(pcm).hexdigest()
            meta = {'version': CACHE_VERSION, 'voice': self.voice, 'text': COMMAND_WAIT_RECEIPT,
                    'model_transcript': observed, 'transcript_source': 'validated_source_suffix',
                    'alignment_revision': ALIGNMENT_REVISION, 'sha256': digest,
                    'source_excerpt': proof, **quality,
                    'human_listening_verified': False, 'phone_hearing_verified': False}
            if self._listener_rejection_path(digest).exists():
                meta['passed'] = False
            atomic_write(metadata, json.dumps(meta, ensure_ascii=False, indent=2).encode())
            if meta.get('passed') is not True:
                raise NativeClipRejectedError('Receipt suffix failed the unchanged content gate')
            atomic_write(target, pcm)
            return {'prepared': True, 'text': COMMAND_WAIT_RECEIPT, 'sha256': digest,
                    'source_unchanged': True, 'generated': False, 'human_listening_verified': False}

    async def prepare(self, texts):
        try:
            for text in dict.fromkeys(texts):
                try:
                    await self.synthesize(text)
                except NativeClipRejectedError:
                    # One bounded pre-dial rerender, not another phone call.
                    # A fresh context prevents a rejected phrase spilling into
                    # the next attempt. Authentication/startup errors stop.
                    await self.close()
                    # A passing original can still fail after address assembly.
                    # Do not retry that same vetoed source from cache or replace
                    # its evidence. Only prepare's existing second attempt may
                    # allocate one fresh, private, provenance-bound source.
                    self._fresh_opening_source = True
                    try:
                        await self.synthesize(text)
                    finally:
                        self._fresh_opening_source = False
        finally:
            await self.close()
        self.cache_only = True

    async def synthesize(self, text):
        async with self._lock:
            cached = self.cached(text)
            if cached is not None: return cached
            if self.cache_only:
                raise RuntimeError('Native notice was not prepared before dialing')
            self._started_at = time.monotonic()
            self._phase_timings = []
            self._set_phase('generation')
            packet_evidence = {}
            self._opening_composition = None
            try:
                pinned = bool(self.opening_address_sha256 and opening_pronunciation_context(text))
                if pinned:
                    pcm, transcript = await self._generate_pinned_opening(text)
                else:
                    pcm, transcript = await self._generate(text)
                owned_pcm = pcm
                # Both components are already trimmed and validated. Preserve
                # the listener-selected address and dynamic body byte-for-byte.
                if not pinned:
                    pcm = self.trim(pcm)
                pcm_path, metadata_path = self.paths(text)
                # Retain rejected samples privately; do not label them accepted.
                wav_path = pcm_path.with_suffix('.wav')
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                with wave.open(str(wav_path), 'wb') as output:
                    output.setnchannels(1); output.setsampwidth(2); output.setframerate(48000)
                    output.writeframes(pcm)
                os.chmod(wav_path, 0o600)
                self._set_phase('content_validation')
                quality = await self.validate(pcm, text, transcript, wav_path)
                meta = {'version':CACHE_VERSION,'voice':self.voice,'text':text,
                        'opening_pronunciation':self._opening_proof(text),
                        'alignment_revision':ALIGNMENT_REVISION,
                        'model_transcript':transcript,'sha256':hashlib.sha256(pcm).hexdigest(),
                        'script_binding':'initial_item_context_only',
                        'clip_turn_id':self._clip_turn_id, 'phase_timings':self._phase_timings,
                        'events':self._events[-40:],
                        'media':self._media_diagnostics(), **quality}
                if pinned:
                    meta['opening_composition'] = self._opening_composition
                    if not self._valid_opening_composition(pcm, text, self._opening_composition):
                        meta['passed'] = False
                if self._listener_rejection_path(meta['sha256']).exists():
                    meta.update(passed=False,listener_rejected=True)
                if meta.get('passed') and self.retain_passed_packet_evidence:
                    # An ASR-passing clip can still receive negative human
                    # feedback. Keep its exact bounded input for the opted-in
                    # source's diagnosis, without re-generating cached audio.
                    try:
                        packet_evidence = self._save_packet_evidence(pcm_path.with_name(
                            pcm_path.stem + '-captured-' + uuid.uuid4().hex))
                        meta['packet_evidence'] = packet_evidence
                    except Exception as exc:
                        meta['packet_evidence_error'] = type(exc).__name__
                atomic_write(metadata_path, json.dumps(meta,ensure_ascii=False,indent=2).encode())
                if not meta.get('passed'):
                    if pinned and self._opening_composition:
                        rejection_key = self._opening_composition['rejection_key']
                        veto = opening_address.asset_dir(self.cache_dir, self.opening_address_sha256)/'rejected-compositions'/(rejection_key+'.json')
                        atomic_write(veto, json.dumps({'stage': 'assembled_opening', 'pcm_sha256': meta['sha256'],
                                     'passed': False}).encode())
                    rejected = pcm_path.with_name(pcm_path.stem+'-rejected-'+uuid.uuid4().hex)
                    os.replace(wav_path, rejected.with_suffix('.wav'))
                    # Keep both sides of local slicing for rejected clips.
                    # Decoded input is evidence, never an accepted cache entry.
                    # Retain raw Opus as well, including connection priming,
                    # for independent decoding of this exact failed attempt.
                    artifacts = {}
                    for label, payload in (('decoded', b''.join(p for p, _, _ in self._received_frames)),
                                           ('owned', owned_pcm)):
                        stream = io.BytesIO()
                        with wave.open(stream, 'wb') as output:
                            output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                            output.writeframes(payload)
                        path = rejected.with_name(rejected.name + '-' + label + '.wav')
                        atomic_write(path, stream.getvalue())
                        artifacts[label] = {'file': path.name, 'pcm_bytes': len(payload),
                                            'sha256': hashlib.sha256(payload).hexdigest()}
                    meta['rejected_audio_stages'] = artifacts
                    packet_evidence = self._save_packet_evidence(rejected)
                    meta['rejected_packet_evidence'] = packet_evidence
                    atomic_write(rejected.with_suffix('.json'),json.dumps(meta,ensure_ascii=False,indent=2).encode())
                    self.rejected_evidence.append(rejected.with_suffix('.json').name)
                    raise NativeClipRejectedError('Native clip did not pass pre-dial content/length checks: '+str(rejected.with_suffix('.json')))
                atomic_write(pcm_path, pcm)
                self.renders += 1
                self.last_spoken_text = transcript
                self._set_phase('ready')
                return pcm
            except (Exception, asyncio.CancelledError) as exc:
                _, failure_path = self.paths(text)
                if not packet_evidence:
                    packet_evidence = self._save_packet_evidence(failure_path.with_name(
                        failure_path.stem + '-failed-' + uuid.uuid4().hex))
                atomic_write(failure_path.with_suffix('.failure.json'), json.dumps({
                    'error':str(exc) or type(exc).__name__,'voice':self.voice,'text':text,
                    'cancelled': isinstance(exc, asyncio.CancelledError), 'phase':self._phase,
                    'elapsed_ms':round((time.monotonic()-self._started_at)*1000),
                    'clip_turn_id':self._clip_turn_id, 'phase_timings':self._phase_timings,
                    'media':self._media_diagnostics(),
                    'packet_evidence': packet_evidence,
                    'pcm_bytes':sum(map(len,self._chunks)), 'events':self._events[-40:]},ensure_ascii=False,indent=2).encode())
                raise

    async def _open(self):
        if self.server is not None: return
        self._set_phase('speech_context_start')
        token = self._session_token = uuid.uuid4().hex
        self._startup_turn_ids = set()
        self._encoded_audio = []
        self._encoded_bytes = self._encoded_dropped = 0
        self.server = CodexAppServer()
        self._set_phase('app_server_start')
        await self.server.start()
        self._set_phase('account_read')
        account = (await self.server.request('account/read', {'refreshToken':False})).get('account') or {}
        if account.get('type') != 'chatgpt':
            raise RuntimeError('Native speech requires the existing ChatGPT login, not API-key auth')
        self._set_phase('literal_config_read')
        effective = await asyncio.wait_for(self.server.request('config/read',
            {'cwd': str(Path(__file__).parent), 'includeLayers': False}), 3)
        overrides = literal_context_config(effective)
        self._excluded_mcp_servers = len(overrides['mcp_servers'])
        self._set_phase('literal_thread_start')
        result = await self.server.request('thread/start', {'cwd':str(Path(__file__).parent),
            'ephemeral':True,'approvalPolicy':'never','sandbox':'read-only',
            'environments': [], 'selectedCapabilityRoots': [], 'config': overrides})
        self.thread_id = str(result.get('thread',{}).get('id') or '')
        if not self.thread_id: raise RuntimeError('No ephemeral speech context')
        def on_pcm_frame(pcm, metadata):
            if token == self._session_token and self._capture:
                stamp, duration = metadata.get('media_ms'), metadata.get('duration_ms')
                self._received_frames.append((pcm, stamp, duration))
                self._anchor_clip_clock()
                self._update_clip_end()
                if (self._clip_aggregate_end_ms is not None and isinstance(stamp, (int, float))
                        and stamp >= self._clip_aggregate_end_ms):
                    return  # Following audio is not this completed clip's tail.
                if isinstance(stamp, (int, float)) and isinstance(duration, (int, float)):
                    end = stamp + duration
                    if self._clip_aggregate_end_ms is not None:
                        end = min(end, self._clip_aggregate_end_ms)
                    self._media_end_ms = max(self._media_end_ms or 0, end)
                accepted=self._fence.receive(pcm, metadata)
                ready=self._fence.take_ready()
                if accepted: ready.append(pcm)
                if len(self._capture_trace) < 3000:
                    self._capture_trace.append({'media_ms': stamp, 'duration_ms': duration,
                        'arrival_ms': round((time.monotonic()-self._started_at)*1000),
                        'accepted_bytes': sum(map(len, ready)), 'received_bytes': len(pcm)})
                if ready:
                    self._chunks.extend(ready)
                    self._last_pcm = time.monotonic()
        def on_event(event):
            if token != self._session_token:
                return
            turn = event.get('turn') or {}
            if not self._capture:
                if turn.get('role') == 'assistant' and turn.get('id'):
                    self._startup_turn_ids.add(turn['id'])
                return
            if turn.get('id') in self._startup_turn_ids:
                return  # A late startup completion cannot own this append.
            if event.get('type') == 'output_transcript.added':
                if len(self._word_trace) < 512:
                    self._word_trace.append({**event, 'received_after_ms':
                        round((time.monotonic()-self._started_at)*1000)})
                self._output_clock.observe(event)
                self._anchor_clip_clock()
                self._update_clip_end()
            if event.get('type') in {'turn.created','turn.done','error'}:
                self._events.append({**event, 'received_after_ms':
                    round((time.monotonic()-self._started_at)*1000)})
            if event.get('type') == 'error':
                self._capture_error = 'Native speech event error: ' + str(event.get('error') or '')[:300]
                self._done.set()
            if turn.get('role') != 'assistant' or not turn.get('id'):
                return
            if event.get('type') == 'turn.created':
                if self._clip_turn_id and self._clip_turn_id != turn['id']:
                    start = turn.get('start_ms')
                    if (self._done.is_set() and self._clip_text_end_ms is not None
                            and isinstance(start, (int, float)) and start >= self._clip_text_end_ms):
                        # The requested turn is immutable once complete. A
                        # disjoint extra response cannot invalidate it or add
                        # PCM, even if that response's media beat its event.
                        self._ignored_following_turns += 1
                        return
                    self._capture_error = 'More than one assistant turn for a single native clip'
                    self._done.set()
                    return
                if self._clip_turn_id == turn['id']:
                    return
                self._clip_turn_id = turn['id']
                self._clip_turn = dict(turn)
                self._anchor_clip_clock()
                if self._fence.turn_id != turn['id']:
                    self._chunks.extend(self._fence.begin(turn['id'], self._output_clock.start_for(turn),
                                                         previous_end_ms=0))
                self._set_phase('receiving_audio')
            if event.get('type') == 'turn.done' and turn['id'] == self._clip_turn_id:
                self._transcript = str(turn.get('transcript') or '')
                self._clip_turn = dict(turn)
                self._anchor_clip_clock()
                end = turn.get('end_ms')
                self._clip_text_end_ms = end if isinstance(end, (int, float)) else None
                self._update_clip_end()
                self._done.set()
        def on_encoded_audio(payload, metadata):
            if token == self._session_token:
                self._record_encoded_audio(payload, metadata)
        self.rtc = CodexWebRtcSession(output_rate=48000, on_pcm=lambda pcm: None,
                                     on_pcm_frame=on_pcm_frame, on_event=on_event,
                                     on_encoded_audio=on_encoded_audio,
                                     audio_retransmission_window_packets=32,
                                     preserve_timeline=False,recover_opus_loss=True,media_proxy=self.media_proxy)
        initial_items = [{'role':'developer','text':'这是专用语音输出通道。客户端追加的文字是你的台词，逐字说出，不回答台词，不确认，不改写。自然温暖地说，保留正常语调。'}]
        if self._literal_script:
            initial_items.append({'role':'developer', 'text':literal_script_binding(self._literal_script)})
            pronunciation = opening_pronunciation_context(self._literal_script, joinable=self.opening_join_pause)
            if pronunciation:
                initial_items.append({'role':'developer', 'text':pronunciation})
        self._set_phase('realtime_connect')
        await self.rtc.start(server=self.server,thread_id=self.thread_id,voice=self.voice,
            prompt=NATIVE_LITERAL_PROMPT,
            start_instructions='这是一个仅渲染指定语音的测试会话，不需要你执行任务或生成任何回复。由客户端显式提供朗读内容。',
            include_startup_context=False,delegation_ack_filler=False,client_managed_handoffs=True,
            initial_items=initial_items)

    async def _generate(self, text):
        # One uncached clip per transport/context. A late callback from the old
        # RTC object cannot end or add audio to the next clip.
        await self.close()
        self._literal_script = text
        self._speech_control = {}
        await self._open()
        self._chunks = []
        self._received_frames = []
        self._events = []
        self._transcript = ''
        self._done.clear()
        self._last_pcm = 0.0
        self._clip_turn_id = ''
        self._capture_error = ''
        self._media_end_ms = self._clip_end_ms = None
        self._clip_aggregate_end_ms = None
        self._clip_text_end_ms = None
        self._clip_turn = {}
        self._clock_alignment = {}
        self._clock_onset_ms = None
        self._clock_scan_cursor = 0
        self._clock_onset_rows.clear()
        self._ignored_following_turns = 0
        self._capture_trace = []
        self._word_trace = []
        self._fence = AudioTurnFence()
        self._output_clock = OutputTranscriptClock()
        self._capture = True
        self._set_phase('speech_request')
        try:
            # The exact script is already a role-bearing startup item. Do not
            # inject a second live context update immediately before speech.
            # Keep one explicit speakable append, original words and the same
            # per-clip transcript/waveform checks; this is not a new voice.
            await self._request_and_wait_for_clip(text)
            self._set_phase('audio_tail_drain')
            started = time.monotonic()
            while True:
                # A text completion is not proof its independently delivered
                # media tail arrived. Keep the existing bounded drain window,
                # but never cache a partial clip or a failed reader as success.
                if self._capture_error or self.rtc.failed.is_set() or self.server.closed.is_set():
                    raise RuntimeError(self._capture_error or self.rtc.error_message or
                                       'Native clip disconnected during audio tail drain')
                complete = (not isinstance(self._clip_end_ms, (int, float)) or
                            (self._media_end_ms is not None and self._media_end_ms >= self._clip_end_ms))
                elapsed = time.monotonic()-started
                if complete and elapsed >= .5 and time.monotonic()-self._last_pcm >= .24:
                    break
                if elapsed >= 1.6:
                    if not complete:
                        raise RuntimeError('Native clip audio tail incomplete before deadline')
                    break
                await asyncio.sleep(.05)
            if self._capture_error:
                raise RuntimeError(self._capture_error)
            return self._owned_pcm(), self._transcript
        finally:
            self._capture = False

    async def _request_and_wait_for_clip(self, text):
        """Keep one submission and the original bound on completed speech.

        A locally queued frame is not service acknowledgement. It cannot pass
        without its own completed turn and waveform QA. Failure wins a
        simultaneous completion; never resend via the sideband RPC.
        """
        def check_failure():
            if self._capture_error or self.rtc.failed.is_set() or self.server.closed.is_set():
                raise RuntimeError(self._capture_error or self.rtc.error_message
                    or getattr(self.server, 'last_error', '') or 'Native clip generation disconnected')

        check_failure()  # Do not append new speech to an already failed channel.
        request = asyncio.create_task(self._submit_speech(text))
        waits = {request, asyncio.create_task(self._done.wait()),
                 asyncio.create_task(self.rtc.failed.wait()),
                 asyncio.create_task(self.server.closed.wait())}
        pending = set(waits)
        submitted = False
        try:
            async with asyncio.timeout(NATIVE_SPEECH_RESPONSE_TIMEOUT_SECONDS):
                while True:
                    _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    check_failure()
                    if request.done() and not submitted:
                        request.result()  # A failed send cannot produce an accepted clip.
                        submitted = True
                        self._set_phase('awaiting_speech_completion')
                    if submitted and self._done.is_set():
                        return
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _submit_speech(self, text):
        self._speech_control = self.rtc.append_speech(text)

    def _owned_pcm(self):
        """Apply the known turn end also to media received before turn.done.

        The onset still comes from the existing phoneme-preserving fence. Use
        the aggregate end as the upper ownership boundary, not the earlier
        word-clock end used to decide whether the spoken tail has arrived.
        """
        if self._clip_aggregate_end_ms is None:
            return b''.join(self._chunks)
        lower, upper = self._fence.minimum_ms, self._clip_aggregate_end_ms
        owned = []
        for payload, stamp, duration in self._received_frames:
            if not isinstance(stamp, (int, float)):
                raise RuntimeError('Native clip has untimestamped media at its completed boundary')
            if (lower is not None and stamp < lower) or stamp >= upper:
                continue
            if isinstance(duration, (int, float)) and duration > 0 and stamp + duration > upper:
                size = int(len(payload) * (upper-stamp) / duration)
                payload = payload[:size-size % 2]
            owned.append(payload)
        return b''.join(owned)

    async def close(self):
        self._capture = False
        self._session_token = ''
        try:
            if self.rtc is not None: await self.rtc.stop(self.server)
        finally:
            if self.server is not None: await self.server.close()
            self.server = self.rtc = None

    def diagnostics(self):
        return {'renderer':'codex-native-cached','voice':self.voice,'cache_hits':self.cache_hits,
                'synthesis_count':self.renders,'cache_only':self.cache_only,'sample_rate':48000,
                'legacy_content_rechecks': self._legacy_alignment_checks,
                'legacy_content_recheck_hits': self._legacy_alignment_hits,
                'phase':self._phase, 'phase_timings':self._phase_timings}

    def _media_diagnostics(self):
        if self.rtc is None:
            return {}
        jitter = getattr(self.rtc, 'audio_jitter', None)
        return {'jitter':jitter.diagnostics() if jitter is not None else {},
                'speech_control': dict(self._speech_control),
                'feedback': self.rtc.audio_feedback_diagnostics()
                    if hasattr(self.rtc, 'audio_feedback_diagnostics') else {},
                'literal_context': {'excluded_mcp_servers': self._excluded_mcp_servers},
                'startup': dict(getattr(self.rtc, 'start_diagnostics', {})),
                'recovery':dict(getattr(self.rtc,'opus_recovery_diagnostics',{})),
                'route':dict(getattr(self.rtc,'media_route_diagnostics',{})),
                'encoded_evidence': {'packets': len(self._encoded_audio),
                    'payload_bytes': self._encoded_bytes,
                    'dropped_by_limit': self._encoded_dropped,
                    **dict(getattr(self.rtc, 'encoded_audio_diagnostics', {}))},
                'fence_discarded_frames':self._fence.discarded_frames,
                'capture': {'received_end_ms': self._media_end_ms, 'expected_end_ms': self._clip_end_ms,
                            'owned_end_ms': self._clip_aggregate_end_ms,
                            'text_end_ms': self._clip_text_end_ms,
                            'clock_alignment': dict(self._clock_alignment),
                            'ignored_following_turns': self._ignored_following_turns,
                            'ignored_startup_turns': len(self._startup_turn_ids),
                            'output_fragments': list(self._output_clock.fragments),
                            'word_trace': self._word_trace,
                            'trace': self._capture_trace}}
