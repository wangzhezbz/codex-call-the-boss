"""Readiness and dial-order regressions. No real phone, network or sound I/O."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import phone_agent
from phone_agent import IPhoneVoiceBridge, PendingCall
from iphone_audio import IPhoneDialer
from types import SimpleNamespace
from test_voice_bridge import FakeAudio, FakeDaemon, FakeDialer, FakeLocalTts, FakeRtc


class DoctorReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_failure_does_not_hide_local_checks_or_claim_logged_out(self):
        import io
        from contextlib import redirect_stdout
        from codex_rpc import CodexRpcError
        output = io.StringIO()
        with patch.object(phone_agent, 'read_subscription_status', new=AsyncMock(side_effect=CodexRpcError(
                '{"code":-32603,"message":"workspace routing discovery timed out"}'))), \
             patch.object(phone_agent, 'compatibility', return_value={'python_supported':True, 'dependencies':{}}), \
             patch.object(phone_agent, 'load_config', return_value={'provider':'iphone','relay_caller_thread_id':'relay',
                'phone_voice_renderer':'macos','to_number':'+8613800138000'}), \
             patch.object(phone_agent, 'audio_devices', return_value=[{'name':'BlackHole 2ch'},{'name':'BlackHole 16ch'}]) as audio, \
             patch.object(phone_agent, 'AppToolsRelayClient', return_value=SimpleNamespace(health=AsyncMock(return_value=True))), \
             patch.object(phone_agent, 'completion_hook_installed', return_value=True), \
             patch.object(phone_agent, 'background_daemon_pid', return_value=123), \
             patch.object(phone_agent, '_daemon_status_ready', return_value=(True, 'ready')), \
             patch.object(phone_agent, '_load_json', return_value={'completion_fallback_ready':True}), \
             patch.object(phone_agent, 'launch_agent_loaded', return_value=False), redirect_stdout(output):
            result = await phone_agent.doctor()
        self.assertEqual(result, 1)
        audio.assert_called_once()
        text = output.getvalue()
        for value in ('workspace_routing_timeout', 'BlackHole 2ch: ok', 'task command transport: ok',
                      'background daemon: running', 'completion fallback: watching'):
            self.assertIn(value, text)
        self.assertNotIn('当前不是 ChatGPT 登录', text)

    async def test_doctor_cancellation_is_not_swallowed_as_readiness_failure(self):
        with patch.object(phone_agent, 'compatibility', return_value={}), \
             patch.object(phone_agent, 'read_subscription_status', new=AsyncMock(side_effect=asyncio.CancelledError)), \
             patch.object(phone_agent, 'load_config') as config:
            with self.assertRaises(asyncio.CancelledError):
                await phone_agent.doctor()
            config.assert_not_called()


class StartupRtc(FakeRtc):
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.began = asyncio.Event()
        self.cancelled = False
        self.start_diagnostics = {'phase': 'not_started'}

    async def start(self, **kwargs):
        self.start_diagnostics['phase'] = 'fake_handshake'
        self.began.set()
        try:
            await self.release.wait()
            if self.error_message:
                raise RuntimeError(self.error_message)
            self.start_diagnostics['phase'] = 'ready'
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class StartupReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_hangup_does_not_erase_detected_service_failure(self):
        _, bridge, rtc, _, dialer = self.pipeline()
        rtc.release.set()
        await bridge.start()
        bridge._watch_call_health = AsyncMock(return_value='synthetic classifier closed')
        bridge._announce_service_failure = AsyncMock()
        dialer.wait_for_disconnect = AsyncMock(return_value=True)
        try:
            result = await bridge.dial_and_wait()
            self.assertEqual(result, 'failed: phone_service_disconnected')
            self.assertEqual(bridge.pending.job['phone_service_error'], 'synthetic classifier closed')
            self.assertTrue(bridge.pending.job['phone_call_end_confirmed'])
            bridge._announce_service_failure.assert_not_awaited()
        finally:
            await bridge.stop()
    async def test_source_context_error_records_pre_dial_phase_and_stops(self):
        daemon, bridge, _, _, dialer = self.pipeline()
        daemon.create_phone_context = AsyncMock(side_effect=RuntimeError('synthetic source projection failure'))
        with self.assertRaisesRegex(RuntimeError, 'projection failure'):
            await bridge.start()
        self.assertEqual(bridge.pending.job['phone_startup_failure'], {
            'stage':'source_context', 'reason':'RuntimeError', 'dial_attempted':False})
        self.assertFalse(dialer.dialed)
        await bridge.stop()

    async def test_private_classifier_loss_is_observed_during_live_call(self):
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        bridge._intent_router = SimpleNamespace(
            connection_error=lambda: 'server_closed', close=AsyncMock())
        bridge._intent_warmup_task = asyncio.create_task(asyncio.sleep(0))
        await bridge._intent_warmup_task
        error = await asyncio.wait_for(bridge._watch_call_health(0), .1)
        self.assertEqual(error, 'Codex phone intent service disconnected')
        await bridge.stop()

    async def test_cleanup_exception_still_closes_owned_classifier(self):
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        bridge._intent_router = SimpleNamespace(close=AsyncMock())
        bridge._stop_rtc = AsyncMock(side_effect=RuntimeError('synthetic cleanup failure'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup failure'):
            await bridge.stop()
        bridge._intent_router.close.assert_awaited_once()

    async def test_isolated_classifier_death_after_warmup_blocks_first_call_action(self):
        daemon, bridge, rtc, _, dialer = self.pipeline()
        daemon.classify_phone_intent = None
        rtc.release.set()
        class Router:
            def __init__(self, server, cwd):
                self.server_argument = server
                self.error = None
                self.close = AsyncMock()
            async def classify(self, *args, **kwargs):
                return {'kind': 'greeting', 'clarification': ''}
            def readiness_error(self):
                return self.error
        with patch.object(phone_agent, 'PhoneIntentRouter', Router):
            await bridge.start()
        router = bridge._intent_router
        self.assertIsNone(router.server_argument)
        router.error = 'server_closed'
        with patch.object(phone_agent, '_atomic_write_json') as write:
            with self.assertRaisesRegex(RuntimeError, '指令判断通道已断开'):
                bridge._record_call_observation(1234)
            write.assert_not_called()
        self.assertFalse(dialer.dialed)
        flushed = False
        async def flush():
            nonlocal flushed
            router.close.assert_not_awaited()
            flushed = True
        bridge._flush_task_relays = flush
        await bridge.stop()
        self.assertTrue(flushed)
        router.close.assert_awaited_once()

    async def test_recent_click_is_guarded_before_confirmation_failure(self):
        root = Path(tempfile.mkdtemp(prefix='phone-early-line-guard-'))
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        dialer = IPhoneDialer(opener=lambda *a, **kw: SimpleNamespace(returncode=0),
            process_finder=lambda _: [], on_call_observation=bridge._record_call_observation,
            on_dial_request=bridge._record_dial_request,
            call_state_probe=lambda _: 'unknown', active_timeout=.5)
        async def recent(*_):
            guard = json.loads((root/'phone-line-unconfirmed.json').read_text())
            self.assertEqual(guard['call_started_at'], dialer.call_started_at)
            self.assertEqual(guard['job_id'], bridge.pending.job_id)
            self.assertNotIn('confirmation_requested_at', bridge.pending.job['phone_latency'])
        with patch.object(phone_agent, 'STATE_DIR', root), \
             patch.object(dialer, '_wait_for_process', new=AsyncMock(return_value=123)), \
             patch.object(dialer, '_wait_for_recent_row', new=AsyncMock()), \
             patch.object(dialer, '_press_recent_once', new=AsyncMock(side_effect=recent)), \
             patch.object(dialer, '_wait_for_phone_confirmation', new=AsyncMock(side_effect=RuntimeError('missing confirmation'))), \
             patch.object(dialer, '_press_phone_confirmation_once', new=AsyncMock()) as confirm, \
             patch.object(IPhoneDialer, '_system_call_state_since', return_value='sending'):
            with self.assertRaisesRegex(RuntimeError, 'missing confirmation'):
                await dialer.dial('+8613800138000')
            confirm.assert_not_awaited()
            self.assertTrue(await phone_agent.PhoneDaemon({})._phone_line_unconfirmed())
            self.assertTrue((root/'phone-line-unconfirmed.json').exists())
        await bridge.stop()

    async def test_first_click_readiness_failure_does_not_create_a_guard(self):
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        rtc.error_message = 'lost before recent row'
        with patch.object(phone_agent, '_atomic_write_json') as write:
            with self.assertRaisesRegex(RuntimeError, '未拨号'):
                bridge._record_call_observation(1234)
            write.assert_not_called()
        await bridge.stop()

    async def test_dependency_drift_stops_before_media_and_dial(self):
        _, bridge, rtc, audio, dialer = self.pipeline()
        with patch.object(phone_agent, 'dependency_compatibility', return_value={'aiortc':{'matches':False}}):
            with self.assertRaisesRegex(RuntimeError, '依赖'):
                await bridge.start()
        self.assertFalse(rtc.began.is_set())
        self.assertFalse(dialer.dialed)
        self.assertEqual(bridge.pending.job['phone_startup_failure']['stage'], 'runtime_compatibility')
        await bridge.stop()

    def pipeline(self):
        daemon = FakeDaemon()
        daemon.ensure_codex = AsyncMock()
        daemon.create_phone_context = AsyncMock(return_value='phone-ephemeral')
        rtc = StartupRtc()
        audio = FakeAudio()
        dialer = FakeDialer()
        pending = PendingCall('startup-test', {'thread_id': 'source', 'report': 'test',
            'spoken_report': '测试中。'}, 'test', Path('unused-test.json'))
        bridge = IPhoneVoiceBridge(daemon, pending, rtc_factory=lambda **_: rtc,
            audio_factory=lambda **_: audio, dialer=dialer, local_tts=FakeLocalTts())
        return daemon, bridge, rtc, audio, dialer

    async def test_slow_start_waits_before_the_single_dial(self):
        _, bridge, rtc, audio, dialer = self.pipeline()
        async def run():
            await bridge.start()
            return await bridge.dial_and_wait()
        task = asyncio.create_task(run())
        await rtc.began.wait()
        await asyncio.sleep(.03)
        self.assertFalse(task.done())
        self.assertFalse(dialer.dialed)
        self.assertFalse(bridge.accept_phone_audio)
        self.assertFalse(audio.played)
        rtc.release.set()
        self.assertEqual(await asyncio.wait_for(task, 1), 'completed')
        self.assertEqual(len(dialer.dialed), 1)
        latency = bridge.pending.job['phone_latency']
        self.assertTrue(latency['realtime_ready_before_dial'])
        self.assertLessEqual(latency['realtime_ready_at'], latency['dial_requested_at'])
        self.assertEqual(latency['realtime_start_stages']['phase'], 'ready')
        await bridge.stop()

    async def test_timeout_cancels_startup_and_never_dials_or_speaks(self):
        daemon, bridge, rtc, audio, dialer = self.pipeline()
        daemon.config['phone_realtime_start_timeout_seconds'] = .1
        with self.assertRaisesRegex(RuntimeError, '准备超时，未拨号'):
            await bridge.start()
        self.assertTrue(rtc.cancelled)
        rtc.release.set()  # A late success cannot now resurrect this job.
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, '未拨号'):
            await bridge.dial_and_wait()
        self.assertFalse(dialer.dialed)
        self.assertFalse(audio.played)
        self.assertFalse(bridge.accept_phone_audio)
        await bridge.stop()

    async def test_immediate_startup_error_never_dials(self):
        _, bridge, rtc, audio, dialer = self.pipeline()
        rtc.error_message = 'synthetic startup failure'
        rtc.release.set()
        with self.assertRaisesRegex(RuntimeError, 'synthetic startup failure'):
            await bridge.start()
        self.assertFalse(dialer.dialed)
        self.assertFalse(audio.played)
        self.assertFalse(bridge.pending.job['phone_startup_failure']['dial_attempted'])
        await bridge.stop()

    async def test_connection_loss_after_preparation_blocks_dial(self):
        _, bridge, rtc, _, dialer = self.pipeline()
        rtc.release.set()
        await bridge.start()
        rtc.error_message = 'synthetic event channel closed'
        with self.assertRaisesRegex(RuntimeError, '未拨号'):
            await bridge.dial_and_wait()
        self.assertFalse(dialer.dialed)
        await bridge.stop()

    async def test_last_click_guard_rechecks_connection_without_writing_line_state(self):
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        rtc.error_message = 'lost while preparing Phone UI'
        with patch.object(phone_agent, '_atomic_write_json') as write:
            with self.assertRaisesRegex(RuntimeError, '未拨号'):
                bridge._record_dial_request(1234)
            write.assert_not_called()
        await bridge.stop()

    async def test_call_older_than_ten_seconds_does_not_timeout_prepared_voice(self):
        _, bridge, rtc, _, _ = self.pipeline()
        rtc.release.set()
        await bridge.start()
        monitor = asyncio.create_task(bridge._watch_call_health(
            asyncio.get_running_loop().time() - 20))
        await asyncio.sleep(.03)
        self.assertFalse(monitor.done())
        self.assertEqual(bridge.pending.job['phone_latency']['realtime_wait_after_active_ms'], 0)
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await bridge.stop()

    async def test_caller_cancellation_cancels_preparation_and_never_dials(self):
        _, bridge, rtc, _, dialer = self.pipeline()
        task = asyncio.create_task(bridge.start())
        await rtc.began.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(rtc.cancelled)
        self.assertFalse(dialer.dialed)
        await bridge.stop()

    async def test_intent_timeout_is_not_misreported_as_media_timeout(self):
        daemon,bridge,rtc,_,dialer=self.pipeline()
        daemon.classify_phone_intent=None
        rtc.release.set()
        class Router:
            def __init__(self,*args): pass
            async def prepare(self): pass
            async def classify(self,*args,**kwargs): raise TimeoutError('classification timeout')
        with patch.object(phone_agent,'PhoneIntentRouter',Router):
            with self.assertRaisesRegex(RuntimeError,'指令判断预热'):
                await bridge.start()
        self.assertEqual(bridge.pending.job['phone_startup_failure']['stage'],'intent_warmup')
        self.assertFalse(dialer.dialed)
        await bridge.stop()

    async def test_intent_failure_never_starts_the_answer_or_media_service(self):
        daemon, bridge, rtc, _, dialer = self.pipeline()
        daemon.classify_phone_intent = None
        class Router:
            def __init__(self, *args): pass
            async def classify(self, *args, trace, **kwargs):
                trace.update(failure_phase='awaiting_completion', failure_reason='server_closed')
                raise phone_agent.PhoneIntentError('server_closed')
        with patch.object(phone_agent, 'PhoneIntentRouter', Router):
            with self.assertRaisesRegex(RuntimeError, '指令判断预热'):
                await asyncio.wait_for(bridge.start(), .5)
        self.assertFalse(rtc.began.is_set())
        self.assertFalse(dialer.dialed)
        self.assertEqual(bridge.pending.job['phone_startup_failure']['intent_phase'], 'awaiting_completion')
        self.assertEqual(bridge.pending.job['phone_intent_warmup']['failure_reason'], 'server_closed')
        await bridge.stop()

    async def test_cancellation_while_warming_never_starts_media(self):
        daemon, bridge, rtc, _, dialer = self.pipeline()
        daemon.classify_phone_intent = None
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Router:
            def __init__(self, *args): pass
            async def classify(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        with patch.object(phone_agent, 'PhoneIntentRouter', Router):
            task = asyncio.create_task(bridge.start())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(rtc.began.is_set())
        self.assertFalse(dialer.dialed)
        await bridge.stop()

    async def test_serial_gates_share_original_total_deadline(self):
        daemon, bridge, rtc, _, dialer = self.pipeline()
        daemon.classify_phone_intent = None
        daemon.config['phone_prepare_timeout_seconds'] = .1
        class Router:
            def __init__(self, *args): self.close = AsyncMock()
            async def classify(self, *args, **kwargs):
                self_test.assertFalse(rtc.began.is_set())
                await asyncio.sleep(.07)
                return {'kind': 'greeting', 'clarification': ''}
        self_test = self
        with patch.object(phone_agent, 'PhoneIntentRouter', Router):
            with self.assertRaisesRegex(RuntimeError, '总时限'):
                await asyncio.wait_for(bridge.start(), .25)
        self.assertTrue(rtc.began.is_set())
        self.assertTrue(rtc.cancelled)
        self.assertFalse(dialer.dialed)
        self.assertEqual(bridge.pending.job['phone_latency']['preflight_order'], 'intent_then_conversation')
        await bridge.stop()
        bridge._intent_router.close.assert_awaited_once()

    async def test_failed_classifier_cannot_be_bypassed_by_calling_dial_again(self):
        daemon, bridge, rtc, _, dialer = self.pipeline()
        daemon.classify_phone_intent = None
        rtc.release.set()
        class Router:
            def __init__(self, *args): pass
            async def classify(self, *args, **kwargs):
                return {'kind': 'action', 'clarification': ''}
        with patch.object(phone_agent, 'PhoneIntentRouter', Router):
            with self.assertRaisesRegex(RuntimeError, '指令判断预热'):
                await bridge.start()
        with self.assertRaisesRegex(RuntimeError, '指令判断预热'):
            await bridge.dial_and_wait()
        self.assertEqual(bridge.pending.job['phone_startup_failure']['reason'], 'unexpected_warmup_decision')
        self.assertFalse(dialer.dialed)
        await bridge.stop()


if __name__ == '__main__':
    unittest.main()
