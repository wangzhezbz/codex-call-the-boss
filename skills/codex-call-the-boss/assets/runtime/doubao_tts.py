"""Owner-requested Doubao 2.0 connectivity client; no phone-mode changes.

The resource and speaker are intentionally pinned. No model/voice fallback,
automatic retry, or API credential in project files is permitted.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import stat
import time
import uuid

import websockets
from websockets.exceptions import InvalidStatus

import doubao_protocols as protocol


ENDPOINT = 'wss://openspeech.bytedance.com/api/v3/tts/bidirection'
MODEL_NAME = 'Doubao-语音合成-2.0'
RESOURCE_ID = 'seed-tts-2.0'
SPEAKER = 'zh_female_tianmeixiaoyuan_uranus_bigtts'
PREVIOUS_SPEAKER = 'zh_female_tianmeixiaoyuan_moon_bigtts'
SAMPLE_RATE = 48000
CREDENTIAL_PATH = Path.home() / '.codex-phone' / 'doubao-tts.json'

# The supplied SDK logs request text at INFO. This integration does not.
protocol.logger.setLevel(logging.CRITICAL)


class DoubaoError(RuntimeError):
    def __init__(self, code, message=''):
        self.code = str(code)
        super().__init__(message or self.code)


def redact(value, secret):
    return str(value).replace(secret, '[redacted]')[:800]


def configure_private(path=CREDENTIAL_PATH):
    """One exclusive mode-600 write via hidden stdin, never argv or env."""
    if path.exists() or path.is_symlink():
        raise DoubaoError('credentials_already_exist', 'Private credentials already exist; not overwritten')
    secret = getpass.getpass('Doubao API key (hidden): ').strip()
    if not secret or any(ch.isspace() for ch in secret):
        raise DoubaoError('invalid_credential_format')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'api_key': secret, 'resource_id': RESOURCE_ID,
                   'model_name': MODEL_NAME, 'speaker': SPEAKER}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    return {'stored': True, 'mode': '0600', 'model': MODEL_NAME, 'speaker': SPEAKER}


def _load_profile(path, speakers):
    if path.is_symlink():
        raise DoubaoError('credentials_symlink_refused')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid()):
            raise DoubaoError('credentials_permissions')
        config = json.load(stream)
    if (config.get('resource_id') != RESOURCE_ID or config.get('speaker') not in speakers
            or config.get('model_name') != MODEL_NAME):
        raise DoubaoError('pinned_model_or_voice_mismatch')
    key = config.get('api_key')
    if not isinstance(key, str) or not key or any(ch.isspace() for ch in key):
        raise DoubaoError('invalid_credential_format')
    return config


def load_private(path=CREDENTIAL_PATH):
    return _load_profile(path, {SPEAKER})['api_key']


def select_approved_speaker(path=CREDENTIAL_PATH):
    """Owner-approved moon -> uranus migration; preserve the key and original."""
    config = _load_profile(path, {SPEAKER, PREVIOUS_SPEAKER})
    if config['speaker'] == SPEAKER:
        return {'changed': False, 'speaker': SPEAKER, 'model': MODEL_NAME}
    original_stat = path.stat()
    token = uuid.uuid4().hex
    backup = path.with_name(f'{path.stem}.before-voice-{token}.json')
    pending = path.with_name(f'.{path.name}.{token}.pending')

    def write_exclusive(destination, profile):
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(profile, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())

    write_exclusive(backup, config)
    updated = {**config, 'speaker': SPEAKER}
    write_exclusive(pending, updated)
    # Refuse an intervening replacement or edit rather than overwriting it.
    current = _load_profile(path, {PREVIOUS_SPEAKER})
    current_stat = path.stat()
    if current != config or (current_stat.st_dev, current_stat.st_ino) != (
            original_stat.st_dev, original_stat.st_ino):
        raise DoubaoError('credentials_changed_during_selection')
    os.replace(pending, path)
    return {'changed': True, 'speaker': SPEAKER, 'model': MODEL_NAME,
            'backup': str(backup), 'mode': '0600'}


def request_parameters():
    # Standard catalog voice: do not set the clone-only req_params.model.
    # Model selection is the mandatory X-Api-Resource-Id header above.
    return {'speaker': SPEAKER,
            'audio_params': {'format': 'pcm', 'sample_rate': SAMPLE_RATE}}


class DoubaoClient:
    def __init__(self, key, *, connect=None):
        if not isinstance(key, str) or not key:
            raise DoubaoError('missing_credential')
        self._key = key
        self._connect = connect or websockets.connect
        self.last_result = {}

    def _payload(self, msg):
        if not msg.payload:
            return {}
        try:
            value = json.loads(msg.payload)
            return value if isinstance(value, dict) else {}
        except (ValueError, UnicodeError):
            return {}

    async def _receive(self, websocket, result, *, session_id=None):
        raw = await websocket.recv()
        if not isinstance(raw, bytes):
            raise DoubaoError('unexpected_text_frame')
        msg = protocol.Message.from_bytes(raw)
        if len(result['events']) >= 4096:
            raise DoubaoError('event_limit')
        result['events'].append({'type': int(msg.type), 'event': int(msg.event)})
        payload = self._payload(msg) if msg.type != protocol.MsgType.AudioOnlyServer else {}
        if msg.type == protocol.MsgType.Error or msg.event in (
                protocol.EventType.ConnectionFailed, protocol.EventType.SessionFailed):
            error_code = payload.get('code') or msg.error_code or int(msg.event)
            message = payload.get('message') or payload.get('error') or payload.get('error_msg')
            if not message:
                message = msg.payload.decode('utf-8', 'replace')
            raise DoubaoError(error_code, redact(message, self._key))
        if session_id is not None and msg.session_id != session_id:
            raise DoubaoError('session_identity_mismatch')
        usage = payload.get('usage')
        if isinstance(usage, dict):
            result['usage'] = {name: value for name, value in usage.items()
                               if isinstance(value, (int, float)) and not isinstance(value, bool)}
        return msg

    async def _expect(self, websocket, event, result, *, session_id=None):
        msg = await self._receive(websocket, result, session_id=session_id)
        if msg.type != protocol.MsgType.FullServerResponse or msg.event != event:
            raise DoubaoError('unexpected_event', f'Expected event {int(event)}, received {int(msg.event)}')
        return msg

    async def synthesize(self, text, *, timeout=35):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise DoubaoError('invalid_text')
        started = time.monotonic()
        websocket = None
        result = self.last_result = {
            'passed': False, 'model': MODEL_NAME, 'resource_id': RESOURCE_ID,
            'speaker': SPEAKER, 'format': 'pcm_s16le', 'sample_rate': SAMPLE_RATE,
            'text': text, 'text_characters': len(text), 'text_submitted': False,
            'audio_bytes': 0, 'events': [], 'usage': {}, 'network_attempts': 1,
            'production_changed': False, 'dial_attempted': False,
        }
        audio = bytearray()
        stage = 'websocket_connect'
        try:
            async with asyncio.timeout(timeout):
                websocket = await self._connect(ENDPOINT, additional_headers={
                    'X-Api-Key': self._key, 'X-Api-Resource-Id': RESOURCE_ID,
                    'X-Api-Connect-Id': str(uuid.uuid4()),
                    'X-Control-Require-Usage-Tokens-Return': '*',
                }, max_size=10 * 1024 * 1024, open_timeout=10, close_timeout=3,
                    ping_interval=20, ping_timeout=10)
                result['websocket_connected_ms'] = round((time.monotonic() - started) * 1000)
                response = getattr(websocket, 'response', None)
                if response is not None:
                    result['log_id'] = str(response.headers.get('x-tt-logid', ''))[:160]
                stage = 'connection_start'
                await protocol.start_connection(websocket)
                await self._expect(websocket, protocol.EventType.ConnectionStarted, result)
                result['connection_started'] = True
                stage = 'session_start'
                session_id = str(uuid.uuid4())
                params = request_parameters()
                await protocol.start_session(websocket, json.dumps({
                    'event': int(protocol.EventType.StartSession), 'req_params': params,
                }, ensure_ascii=False).encode(), session_id)
                await self._expect(websocket, protocol.EventType.SessionStarted, result,
                                   session_id=session_id)
                result['session_started'] = True
                stage = 'text_send'
                await protocol.task_request(websocket, json.dumps({
                    'event': int(protocol.EventType.TaskRequest),
                    'req_params': {**params, 'text': text},
                }, ensure_ascii=False).encode(), session_id)
                result['text_submitted'] = True
                await protocol.finish_session(websocket, session_id)
                stage = 'audio_receive'
                while True:
                    msg = await self._receive(websocket, result, session_id=session_id)
                    if msg.type == protocol.MsgType.AudioOnlyServer:
                        if msg.event != protocol.EventType.TTSResponse:
                            raise DoubaoError('unexpected_audio_event')
                        audio.extend(msg.payload)
                        if len(audio) > 12 * 1024 * 1024:
                            raise DoubaoError('audio_size_limit')
                        if msg.payload:
                            result.setdefault('first_audio_ms', round((time.monotonic() - started) * 1000))
                    elif msg.type != protocol.MsgType.FullServerResponse:
                        raise DoubaoError('unexpected_message_type')
                    elif msg.event == protocol.EventType.SessionFinished:
                        result['session_finished'] = True
                        break
                    elif msg.event not in (protocol.EventType.TTSSentenceStart,
                                          protocol.EventType.TTSSentenceEnd,
                                          protocol.EventType.TTSSubtitle,
                                          protocol.EventType.UsageResponse):
                        raise DoubaoError('unexpected_session_event')
                if not audio or len(audio) % 2:
                    raise DoubaoError('missing_or_invalid_audio')
                stage = 'connection_finish'
                await protocol.finish_connection(websocket)
                await self._expect(websocket, protocol.EventType.ConnectionFinished, result)
                result['connection_finished'] = True
                result.update(passed=True, audio_bytes=len(audio),
                              duration_ms=round(len(audio) / 2 / SAMPLE_RATE * 1000))
                return bytes(audio)
        except InvalidStatus as exc:
            result['http_status'] = exc.response.status_code
            result['log_id'] = str(exc.response.headers.get('x-tt-logid', ''))[:160]
            message = redact(exc.response.body.decode('utf-8', 'replace'), self._key)
            result.update(error_code='http_rejected', error=message)
            raise DoubaoError('http_rejected', message) from None
        except Exception as exc:
            result.update(error_code=getattr(exc, 'code', type(exc).__name__),
                          error=redact(exc, self._key))
            raise DoubaoError(result['error_code'], result['error']) from None
        finally:
            result['stage'] = 'complete' if result['passed'] else stage
            result['received_audio_bytes'] = len(audio)
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            if websocket is not None:
                try:
                    await asyncio.wait_for(websocket.close(), timeout=4)
                except Exception as exc:
                    result['cleanup_error'] = type(exc).__name__


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['configure']:
        print(json.dumps(configure_private(), ensure_ascii=False))
    elif sys.argv[1:] == ['select-approved-speaker', '--confirm']:
        print(json.dumps(select_approved_speaker(), ensure_ascii=False))
    else:
        raise SystemExit('Use: doubao_tts.py configure or select-approved-speaker --confirm')
