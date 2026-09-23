import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import doubao_protocols as p
import doubao_tts as tts


def packet(event, session_id='', payload=b'{}', kind=p.MsgType.FullServerResponse):
    msg = p.Message(type=kind, flag=p.MsgTypeFlagBits.WithEvent,
                    event=event, session_id=session_id, payload=payload)
    raw = msg.marshal()
    if event in (p.EventType.ConnectionStarted, p.EventType.ConnectionFailed):
        # Downstream connection ID precedes the payload; the supplied helper
        # only writes upstream messages and does not write that ID itself.
        raw = raw[:8] + b'\0\0\0\0' + raw[8:]
    return raw


class FakeSocket:
    def __init__(self, *, rejected=False, wrong_session=False, no_audio=False):
        self.response = SimpleNamespace(headers={'x-tt-logid': 'test-log'})
        self.frames = []
        self.sent = []
        self.closed = False
        self.rejected, self.wrong_session, self.no_audio = rejected, wrong_session, no_audio

    async def send(self, raw):
        msg = p.Message.from_bytes(raw)
        self.sent.append(msg)
        if msg.event == p.EventType.StartConnection:
            self.frames.append(packet(p.EventType.ConnectionStarted))
        elif msg.event == p.EventType.StartSession:
            event = p.EventType.SessionFailed if self.rejected else p.EventType.SessionStarted
            payload = b'{"code":45000000,"message":"speaker rejected"}' if self.rejected else b'{}'
            sid = 'wrong-session' if self.wrong_session else msg.session_id
            self.frames.append(packet(event, sid, payload))
        elif msg.event == p.EventType.FinishSession:
            if not self.no_audio:
                self.frames.append(packet(p.EventType.TTSResponse, msg.session_id,
                    b'\x01\x00' * 4800, p.MsgType.AudioOnlyServer))
            self.frames.append(packet(p.EventType.SessionFinished, msg.session_id,
                                      b'{"usage":{"text_words":4}}'))
        elif msg.event == p.EventType.FinishConnection:
            self.frames.append(packet(p.EventType.ConnectionFinished))

    async def recv(self):
        return self.frames.pop(0)

    async def close(self):
        self.closed = True


class DoubaoTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_model_and_voice_reach_the_wire(self):
        for resource in tts.MODEL_OPTIONS:
            socket = FakeSocket()
            connect = AsyncMock(return_value=socket)
            client = tts.DoubaoClient('test-key', connect=connect,
                                     resource_id=resource, speaker='chosen_catalog_voice')
            await client.synthesize('测试所选音色')
            self.assertEqual(connect.call_args.kwargs['additional_headers']['X-Api-Resource-Id'], resource)
            for msg in socket.sent:
                if msg.event in (p.EventType.StartSession, p.EventType.TaskRequest):
                    self.assertEqual(json.loads(msg.payload)['req_params']['speaker'], 'chosen_catalog_voice')
            self.assertEqual(client.last_result['model'], tts.MODEL_OPTIONS[resource])

    def test_interactive_choice_is_required_and_private(self):
        path = Path(tempfile.mkdtemp(prefix='doubao-choice-'))/'credentials.json'
        with patch('builtins.input', side_effect=['seed-icl-2.0', 'chosen_voice', 'YES']), \
                patch.object(tts.getpass, 'getpass', return_value='test-secret'):
            tts.configure_private(path, choose_profile=True)
        profile = tts.load_profile(path)
        self.assertEqual(profile['speaker'], 'chosen_voice')
        self.assertEqual(profile['resource_id'], 'seed-icl-2.0')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cancelled_or_unsupported_choice_writes_nothing(self):
        for answers in (['seed-tts-2.0', 'chosen_voice', 'NO'], ['unknown-model', 'voice']):
            path = Path(tempfile.mkdtemp(prefix='doubao-choice-refused-'))/'credentials.json'
            with patch('builtins.input', side_effect=answers), \
                    patch.object(tts.getpass, 'getpass') as secret:
                with self.assertRaises(tts.DoubaoError):
                    tts.configure_private(path, choose_profile=True)
                secret.assert_not_called()
                self.assertFalse(path.exists())

    async def test_exact_model_and_voice_full_lifecycle(self):
        socket = FakeSocket()
        connect = AsyncMock(return_value=socket)
        client = tts.DoubaoClient('test-secret', connect=connect)
        pcm = await client.synthesize('语音测试')
        self.assertEqual(len(pcm), 9600)
        self.assertTrue(client.last_result['passed'])
        self.assertTrue(client.last_result['session_finished'])
        self.assertTrue(client.last_result['connection_finished'])
        self.assertEqual(client.last_result['usage'], {'text_words': 4})
        self.assertTrue(socket.closed)
        connect.assert_awaited_once()
        headers = connect.call_args.kwargs['additional_headers']
        self.assertEqual(headers['X-Api-Resource-Id'], 'seed-tts-2.0')
        self.assertEqual(headers['X-Api-Key'], 'test-secret')
        for msg in socket.sent:
            if msg.event in (p.EventType.StartSession, p.EventType.TaskRequest):
                params = json.loads(msg.payload)['req_params']
                self.assertEqual(params['speaker'], 'zh_female_tianmeixiaoyuan_uranus_bigtts')
                self.assertNotIn('model', params)
                self.assertEqual(params['audio_params'], {'format': 'pcm', 'sample_rate': 48000})
        self.assertNotIn('test-secret', json.dumps(client.last_result))

    async def test_speaker_failure_stops_without_text_or_fallback(self):
        socket = FakeSocket(rejected=True)
        connect = AsyncMock(return_value=socket)
        client = tts.DoubaoClient('secret', connect=connect)
        with self.assertRaises(tts.DoubaoError):
            await client.synthesize('测试')
        self.assertFalse(client.last_result['passed'])
        self.assertFalse(client.last_result['text_submitted'])
        self.assertEqual(client.last_result['stage'], 'session_start')
        self.assertFalse(any(msg.event == p.EventType.TaskRequest for msg in socket.sent))
        connect.assert_awaited_once()
        self.assertTrue(socket.closed)

    async def test_mismatched_session_refused(self):
        socket = FakeSocket(wrong_session=True)
        client = tts.DoubaoClient('secret', connect=AsyncMock(return_value=socket))
        with self.assertRaisesRegex(tts.DoubaoError, 'session_identity_mismatch'):
            await client.synthesize('测试')
        self.assertFalse(client.last_result['text_submitted'])

    async def test_no_audio_is_not_success(self):
        client = tts.DoubaoClient('secret', connect=AsyncMock(return_value=FakeSocket(no_audio=True)))
        with self.assertRaisesRegex(tts.DoubaoError, 'missing_or_invalid_audio'):
            await client.synthesize('测试')
        self.assertFalse(client.last_result['passed'])

    async def test_connection_error_redacts_key_and_does_not_retry(self):
        connect = AsyncMock(side_effect=RuntimeError('bad owner-secret'))
        client = tts.DoubaoClient('owner-secret', connect=connect)
        with self.assertRaises(tts.DoubaoError) as captured:
            await client.synthesize('测试')
        self.assertNotIn('owner-secret', str(captured.exception))
        self.assertNotIn('owner-secret', json.dumps(client.last_result))
        connect.assert_awaited_once()

    def test_private_key_mode_and_pinned_profile(self):
        root = Path(tempfile.mkdtemp(prefix='doubao-private-test-'))
        path = root/'credentials.json'
        with patch.object(tts.getpass, 'getpass', return_value='fake-test-key'):
            tts.configure_private(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(tts.load_private(path), 'fake-test-key')
        with self.assertRaises(tts.DoubaoError):
            tts.configure_private(path)
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(tts.DoubaoError, 'credentials_permissions'):
            tts.load_private(path)

    def test_approved_voice_migration_preserves_key_model_and_backup(self):
        root = Path(tempfile.mkdtemp(prefix='doubao-selection-test-'))
        path = root/'credentials.json'
        with patch.object(tts.getpass, 'getpass', return_value='fake-test-key'), \
                patch.object(tts, 'SPEAKER', tts.PREVIOUS_SPEAKER):
            tts.configure_private(path)
        original = json.loads(path.read_text())
        with self.assertRaisesRegex(tts.DoubaoError, 'pinned_model_or_voice_mismatch'):
            tts.load_private(path)
        result = tts.select_approved_speaker(path)
        updated = json.loads(path.read_text())
        self.assertEqual(updated, {**original, 'speaker': tts.SPEAKER})
        self.assertEqual(json.loads(Path(result['backup']).read_text()), original)
        self.assertEqual(Path(result['backup']).stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(tts.load_private(path), 'fake-test-key')
        self.assertNotIn('fake-test-key', json.dumps(result))
        self.assertFalse(tts.select_approved_speaker(path)['changed'])

    def test_selection_rejects_unrelated_profile_without_backup(self):
        root = Path(tempfile.mkdtemp(prefix='doubao-invalid-profile-test-'))
        path = root/'credentials.json'
        with patch.object(tts.getpass, 'getpass', return_value='fake-test-key'), \
                patch.object(tts, 'RESOURCE_ID', 'seed-tts-1.0'):
            tts.configure_private(path)
        with self.assertRaisesRegex(tts.DoubaoError, 'pinned_model_or_voice_mismatch'):
            tts.select_approved_speaker(path)
        self.assertEqual(len(list(root.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
