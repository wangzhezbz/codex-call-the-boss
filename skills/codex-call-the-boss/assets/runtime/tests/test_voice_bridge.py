from __future__ import annotations

import asyncio
import unittest
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from phone_agent import (
    VOICE_PROMPT,
    IPhoneVoiceBridge,
    PendingCall,
    VoiceBridge,
    brief_phone_report,
    phone_announcement,
)


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def remove_notification_handler(self, handler: Any) -> None:
        pass

    def add_notification_handler(self, handler: Any) -> None:
        pass

    async def request(
        self, method: str, params: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        del kwargs
        self.requests.append((method, params))
        if method == 'config/read':
            return {'config': {'mcp_servers': {}}}
        if method == 'thread/start':
            return {'thread': {'id': 'isolated-intent-test'}}
        return {}


class FakeRtc:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.error_message = None
        self.input_track = SimpleNamespace(diagnostics=lambda: {})

    def readiness_error(self):
        return self.error_message

    async def stop(self, server: Any) -> None:
        self.stop_calls += 1


class FakeAudio:
    def __init__(self) -> None:
        self.clear_calls = 0
        self.played: list[bytes] = []
        self.timeline_gaps: list[int] = []

    def clear_output(self) -> None:
        self.clear_calls += 1

    def play_pcm48k(self, payload: bytes) -> None:
        self.played.append(payload)

    def play_timeline_gap48k(self, frames: int) -> None:
        self.timeline_gaps.append(frames)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def diagnostics(self) -> dict[str, int]:
        return {}


class FakeLocalTts:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.texts.append(text)
        await asyncio.sleep(0)
        return b"\x01\x00" * 960

    def diagnostics(self) -> dict[str, Any]:
        return {"renderer": "fake", "synthesis_count": len(self.texts)}


class FakeDaemon:
    def __init__(self) -> None:
        self.codex = FakeServer()
        self.config: dict[str, Any] = {
            "to_number": "+8613800138000",
            "connect_delay_seconds": 0,
            "max_call_seconds": 15,
            "announcement_attempts": 4,
        }
        self.active_threads: set[str] = set()
        self.busy_realtime_threads: set[str] = set()
        self.detached: set[asyncio.Task[None]] = set()
        self.relayed: list[str] = []

    def track_detached_session(self, task: asyncio.Task[None]) -> None:
        self.detached.add(task)
        task.add_done_callback(self.detached.discard)

    @staticmethod
    def source_thread_id(job: dict[str, Any]) -> str:
        return str(job.get("thread_id") or job.get("session_id") or "")

    async def relay_phone_task(self, job: dict[str, Any], prompt: str, **kwargs: Any) -> None:
        del job
        self.relayed.append(prompt)

    async def classify_phone_intent(self, text, context):
        # Unit tests stub the structured Codex classifier, never use TTS words.
        import re
        action = bool(re.search(r'写|修改|创建|修复|生成|执行|运行|整理|礼花|继续做|开始做|做个|做一个|帮我做|做任务|做测试', text))
        if re.search(r'天气|为什么|怎么|哪里|能不能|重复|朗读|吗|？|\?', text):
            action = False
        return {'kind': 'action' if action else 'question', 'clarification': ''}


class FakeDialer:
    def __init__(self, disconnected: bool = True) -> None:
        self.disconnected = disconnected
        self.dialed: list[str] = []
        self.waited: list[float] = []

    async def dial(self, number: str) -> None:
        self.dialed.append(number)

    async def wait_for_disconnect(self, timeout: float) -> bool:
        self.waited.append(timeout)
        await asyncio.sleep(0)
        return self.disconnected


def mark_realtime_ready(bridge):
    """Prepare fake transport explicitly; direct dial cannot bypass readiness."""
    bridge.rtc = FakeRtc()
    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)
    bridge._realtime_start_task = completed


class VoiceBridgeTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self) -> tuple[FakeDaemon, VoiceBridge, FakeRtc]:
        daemon = FakeDaemon()
        pending = PendingCall(
            job_id="job",
            job={"session_id": "source", "report": "done"},
            token="token",
            source_path=Path("job.json"),
        )
        bridge = VoiceBridge(daemon, object(), pending, "stream")  # type: ignore[arg-type]
        bridge.thread_id = "phone-thread"
        rtc = FakeRtc()
        bridge.rtc = rtc  # type: ignore[assignment]
        daemon.active_threads.add(bridge.thread_id)
        daemon.busy_realtime_threads.add(bridge.thread_id)
        return daemon, bridge, rtc

    async def test_hangup_keeps_active_codex_turn_running(self) -> None:
        daemon, bridge, rtc = self.make_bridge()
        bridge.delegation_seen.set()
        bridge.turn_started.set()

        await bridge.stop()

        self.assertEqual(rtc.stop_calls, 0)
        self.assertNotIn(bridge.thread_id, daemon.active_threads)
        self.assertIn(bridge.thread_id, daemon.busy_realtime_threads)
        self.assertEqual(len(daemon.detached), 1)

        bridge.turn_finished.set()
        await asyncio.gather(*tuple(daemon.detached))
        self.assertEqual(rtc.stop_calls, 1)
        self.assertNotIn(bridge.thread_id, daemon.busy_realtime_threads)

    async def test_hangup_stops_idle_realtime_session(self) -> None:
        daemon, bridge, rtc = self.make_bridge()

        await bridge.stop()

        self.assertEqual(rtc.stop_calls, 1)
        self.assertFalse(daemon.detached)
        self.assertNotIn(bridge.thread_id, daemon.busy_realtime_threads)


class IPhoneVoiceBridgeTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self) -> tuple[FakeDaemon, IPhoneVoiceBridge, FakeAudio]:
        daemon = FakeDaemon()
        pending = PendingCall(
            job_id="job",
            job={
                "session_id": "source",
                "report": "窗口里是一段很长的详细结果，绝不能直接播报。",
                "spoken_report": "done",
            },
            token="local-iphone",
            source_path=Path("job.json"),
        )
        bridge = IPhoneVoiceBridge(
            daemon, pending, local_tts=FakeLocalTts()
        )  # type: ignore[arg-type]
        audio = FakeAudio()
        bridge.audio = audio  # type: ignore[assignment]
        return daemon, bridge, audio

    def test_announcement_uses_phone_copy_instead_of_visible_reply(self) -> None:
        _, bridge, _ = self.make_bridge()

        self.assertEqual(bridge._announcement_text, "老板，done。")
        self.assertNotIn("窗口", bridge._announcement_text)

    def test_local_pcm_greeting_releases_report_without_asr(self) -> None:
        _, bridge, _ = self.make_bridge()
        voice = array("h", [1_000] * 960).tobytes()
        silence = array("h", [0] * 960).tobytes()

        for _ in range(3):
            bridge._observe_local_greeting(voice)
        for _ in range(9):
            bridge._observe_local_greeting(silence)

        self.assertTrue(bridge.remote_speech_seen.is_set())
        self.assertTrue(bridge.greeting_finished.is_set())
        self.assertEqual(
            bridge.pending.job["phone_greeting_detection"]["source"],
            "local_pcm",
        )

    async def test_missed_greeting_has_short_complete_fallback(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config["announcement_wait_for_greeting_seconds"] = 0.01

        started = asyncio.get_running_loop().time()
        await bridge._announce_until_speech()
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(bridge.pending.job["announcement_trigger"], "pickup_timeout")
        self.assertEqual(audio.played, [b"\x01\x00" * 960])

    def test_phone_realtime_compacts_media_timeline_before_buffering(self) -> None:
        options: dict[str, Any] = {}

        def rtc_factory(**kwargs: Any) -> FakeRtc:
            options.update(kwargs)
            return FakeRtc()

        daemon = FakeDaemon()
        pending = PendingCall(
            job_id="job",
            job={"session_id": "source", "report": "done"},
            token="local-iphone",
            source_path=Path("job.json"),
        )
        bridge = IPhoneVoiceBridge(
            daemon, pending, rtc_factory=rtc_factory  # type: ignore[arg-type]
        )

        bridge.rtc = bridge._create_rtc()

        self.assertIs(options["preserve_timeline"], False)
        self.assertTrue(callable(options["on_timeline_gap"]))

    async def test_ready_to_dial_waits_for_slow_realtime(self) -> None:
        class InputTrack:
            def push_pcm48k(self, payload: bytes) -> None:
                pass

            def diagnostics(self) -> dict[str, int]:
                return {}

        class SlowRtc(FakeRtc):
            def __init__(self) -> None:
                super().__init__()
                self.release = asyncio.Event()
                self.input_track = InputTrack()

            async def start(self, **kwargs: Any) -> None:
                del kwargs
                await self.release.wait()

        daemon = FakeDaemon()
        daemon.ensure_codex = AsyncMock()  # type: ignore[attr-defined]
        daemon.create_phone_context = AsyncMock(  # type: ignore[attr-defined]
            return_value="phone-thread"
        )
        rtc = SlowRtc()
        audio = FakeAudio()
        pending = PendingCall(
            job_id="fast-dial",
            job={"session_id": "source", "report": "done"},
            token="local-iphone",
            source_path=Path("job.json"),
        )
        bridge = IPhoneVoiceBridge(
            daemon,
            pending,
            rtc_factory=lambda **kwargs: rtc,
            audio_factory=lambda **kwargs: audio,
            local_tts=FakeLocalTts(),
        )  # type: ignore[arg-type]

        preparing = asyncio.create_task(bridge.start())
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertIsNotNone(bridge._realtime_start_task)
        self.assertFalse(bridge._realtime_start_task.done())
        self.assertFalse(preparing.done())
        self.assertNotIn("ready_to_dial_at", pending.job["phone_latency"])
        rtc.release.set()
        await asyncio.wait_for(preparing, timeout=1)
        self.assertTrue(pending.job['phone_latency']['realtime_ready_before_dial'])
        self.assertIn("prepare_total_ms", pending.job["phone_latency"])
        await bridge.stop()

    async def test_direct_dial_cannot_bypass_unfinished_realtime(self) -> None:
        _, bridge, _ = self.make_bridge()
        bridge.dialer = FakeDialer()
        release = asyncio.Event()

        async def finish_realtime() -> None:
            await release.wait()

        bridge._realtime_start_task = asyncio.create_task(finish_realtime())
        with self.assertRaisesRegex(RuntimeError, '未拨号'):
            await bridge.dial_and_wait()
        self.assertFalse(bridge.accept_phone_audio)
        self.assertEqual(bridge.dialer.dialed, [])
        release.set()
        await bridge._realtime_start_task
        await bridge.stop()

    def test_realtime_context_is_bounded_to_the_corresponding_task(self) -> None:
        _, bridge, _ = self.make_bridge()
        bridge.pending.job["cwd"] = "/tmp/current-project"

        prompt = bridge._realtime_prompt()

        self.assertIn("source", prompt)
        self.assertIn("/tmp/current-project", prompt)
        self.assertIn("窗口里是一段很长的详细结果", prompt)
        self.assertNotIn("spoken_report", prompt)

    def test_opening_and_command_receipt_are_brief_but_conversation_is_normal(self) -> None:
        self.assertIn("开场任务完成汇报只说一句", VOICE_PROMPT)
        self.assertIn("除下达任务后的固定回执外，普通提问按正常对话", VOICE_PROMPT)
        self.assertIn("完整、自然、有回应", VOICE_PROMPT)
        self.assertIn("不要只回答一两个字", VOICE_PROMPT)
        self.assertNotIn("最多一个短句", VOICE_PROMPT)

    def test_local_renderer_discards_realtime_voice_and_its_gaps(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_pcm_output(b"unstable-realtime-audio")
        handled = bridge._on_timeline_gap(48_000)

        self.assertTrue(handled)
        self.assertEqual(audio.played, [])
        self.assertEqual(audio.timeline_gaps, [])

    async def test_realtime_voice_is_released_once_as_one_complete_buffer(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "现在修好了吗"
        payload = array("h", [1_000] * (960 * 50)).tobytes()
        bridge._begin_realtime_capture("assistant-buffered")
        bridge._on_pcm_output(b"\x00\x00" * (960 * 100))
        self.assertFalse(bridge._realtime_audio_buffer)
        bridge._on_pcm_output(payload[:8_000])
        bridge._on_pcm_output(payload[8_000:])

        self.assertEqual(audio.played, [])
        self.assertTrue(bridge._on_timeline_gap(48_000))
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-buffered",
                    "role": "assistant",
                    "transcript": "已经修好了。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [payload])
        self.assertEqual(audio.timeline_gaps, [])
        diagnostics = bridge.pending.job["phone_realtime_audio_diagnostics"]
        self.assertEqual(diagnostics["released_turns"], 1)
        self.assertEqual(diagnostics["last_turn"]["exact_zero_blocks"], 0)

    async def test_unintelligible_realtime_answer_falls_back_before_playback(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_semantic_gate": True,
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge._realtime_semantic_checker = AsyncMock(return_value=False)
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "现在修好了吗"
        payload = array("h", [1_000] * (960 * 50)).tobytes()
        bridge._begin_realtime_capture("assistant-garbled")
        bridge._on_pcm_output(payload)

        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-garbled",
                    "role": "assistant",
                    "transcript": "已经修好了。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [b"\x01\x00" * 960])
        self.assertEqual(
            bridge.local_tts.texts,
            ["已经修好了。"],
        )  # type: ignore[attr-defined]
        diagnostics = bridge.pending.job["phone_realtime_audio_diagnostics"]
        self.assertEqual(
            diagnostics["last_fallback_reason"],
            "unintelligible_realtime_voice",
        )

    async def test_intelligible_realtime_answer_passes_semantic_gate(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_semantic_gate": True,
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge._realtime_semantic_checker = AsyncMock(return_value=True)
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "现在修好了吗"
        payload = array("h", [1_000] * (960 * 50)).tobytes()
        bridge._begin_realtime_capture("assistant-clear")
        bridge._on_pcm_output(payload)

        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-clear",
                    "role": "assistant",
                    "transcript": "已经修好了。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [payload])
        self.assertEqual(bridge.local_tts.texts, [])  # type: ignore[attr-defined]

    async def test_complete_sentence_streams_after_a_safe_audio_lead(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_early_prebuffer_ms": 400,
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "说一段完整的话"
        first = array("h", [1_000] * (960 * 25)).tobytes()
        tail = array("h", [800] * (960 * 5)).tobytes()
        bridge._begin_realtime_capture("assistant-streamed")
        bridge._on_pcm_output(first)

        bridge._handle_assistant_ready(
            "assistant-streamed", "这是一句已经完整生成的回答。"
        )
        self.assertEqual(audio.played, [first])
        bridge._on_pcm_output(tail)
        self.assertEqual(audio.played, [first, tail])

        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-streamed",
                    "role": "assistant",
                    "transcript": "这是一句已经完整生成的回答。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [first, tail])
        diagnostics = bridge.pending.job["phone_realtime_audio_diagnostics"]
        self.assertIs(diagnostics["early_stream_started"], True)
        self.assertEqual(diagnostics["released_turns"], 1)

    async def test_greeting_reply_drops_its_buffered_realtime_audio(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = True
        bridge._latest_user_text = "喂"
        bridge._begin_realtime_capture("assistant-greeting")
        bridge._on_pcm_output(array("h", [900] * (960 * 10)).tobytes())
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-greeting",
                    "role": "assistant",
                    "transcript": "你好，有什么可以帮你？",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [])
        self.assertEqual(
            bridge.pending.job["suppressed_greeting_reply"],
            "你好，有什么可以帮你？",
        )
        self.assertFalse(bridge._realtime_audio_buffer)

    async def test_empty_realtime_turn_falls_back_to_selected_local_voice(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "请回答"
        bridge._begin_realtime_capture("assistant-empty")
        bridge._on_pcm_output(b"\x00\x00" * (960 * 10))
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-empty",
                    "role": "assistant",
                    "transcript": "这是本地兜底回答。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [b"\x01\x00" * 960])
        self.assertEqual(bridge.local_tts.texts, ["这是本地兜底回答。"])  # type: ignore[attr-defined]
        diagnostics = bridge.pending.job["phone_realtime_audio_diagnostics"]
        self.assertEqual(diagnostics["local_fallbacks"], 1)

    async def test_interrupted_realtime_turn_cannot_play_or_fallback_late(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge.daemon.config.update(
            {
                "phone_voice_renderer": "realtime-buffered",
                "phone_realtime_early_prebuffer_ms": 1200,
                "phone_realtime_tail_min_wait_ms": 0,
                "phone_realtime_tail_max_wait_ms": 0,
            }
        )
        bridge.remote_speech_seen.set()
        bridge._first_user_is_greeting = False
        bridge._first_assistant_response_pending = False
        bridge._latest_user_text = "先回答这个问题"
        bridge._begin_realtime_capture("assistant-interrupted")
        bridge._on_pcm_output(array("h", [900] * (960 * 10)).tobytes())

        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_stopped"})
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-interrupted",
                    "role": "assistant",
                    "transcript": "这条旧回答不应该晚到。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._realtime_release_tasks))

        self.assertEqual(audio.played, [])
        self.assertEqual(audio.clear_calls, 1)
        self.assertEqual(bridge.local_tts.texts, [])  # type: ignore[attr-defined]

    def test_codex_input_transcript_marks_remote_speech(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_realtime_event(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "你好"},
            }
        )

        self.assertTrue(bridge.remote_speech_seen.is_set())
        self.assertEqual(audio.clear_calls, 0)
        self.assertEqual(
            bridge.pending.job["phone_transcript"],
            [{"role": "user", "text": "你好"}],
        )

    async def test_only_speech_leading_edge_interrupts_output(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        bridge._on_realtime_event(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "你好"},
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "user-turn",
                    "role": "user",
                    "transcript": "你好",
                },
            }
        )

        self.assertEqual(audio.clear_calls, 1)

        bridge._on_realtime_event({"type": "input_audio_buffer.speech_stopped"})
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(audio.clear_calls, 2)

    async def test_codex_turn_events_build_complete_transcript(self) -> None:
        _, bridge, _ = self.make_bridge()
        bridge._on_realtime_event(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "请"},
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "user-turn",
                    "role": "user",
                    "transcript": "请",
                },
            }
        )
        bridge._on_realtime_event(
            {"type": "turn.delta", "turn_id": "user-turn", "delta": "回答"}
        )
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "assistant-turn",
                    "role": "assistant",
                    "transcript": "好的。",
                },
            }
        )
        # A model response alone cannot finalize the caller's words. Exercise
        # the real input settle and structured classification boundaries.
        await bridge._input_settle_task
        await asyncio.gather(*tuple(bridge._relay_tasks))
        await asyncio.gather(*tuple(bridge._local_speech_tasks))

        self.assertEqual(
            bridge.pending.job["phone_transcript"],
            [
                {"role": "user", "text": "请回答"},
                {"role": "assistant", "text": "好的。", 'playback_status':'not_verified','output_ms':0},
                {"role": "assistant", "text": "老板，done。", 'playback_status':'not_verified','output_ms':0},
            ],
        )

    def test_public_realtime_transcription_event_is_supported(self) -> None:
        _, bridge, _ = self.make_bridge()

        bridge._on_realtime_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "公开事件也能识别",
            }
        )

        self.assertTrue(bridge.remote_speech_seen.is_set())
        self.assertEqual(
            bridge.pending.job["phone_transcript"],
            [{"role": "user", "text": "公开事件也能识别"}],
        )

    async def test_task_acceptance_relays_original_user_words_once(self) -> None:
        daemon, bridge, _ = self.make_bridge()
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "user-task",
                    "role": "user",
                    "transcript": "把刚才的文件整理一下",
                },
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "assistant-task",
                    "role": "assistant",
                    "transcript": "收到,我现在开始。",
                },
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.delta",
                "turn_id": "assistant-task",
                "delta": "收到，我现在开始。",
            }
        )
        bridge._complete_transcript_turn('user', '把刚才的文件整理一下', 'user-task')
        await asyncio.gather(*tuple(daemon.detached))

        self.assertEqual(daemon.relayed, ["把刚才的文件整理一下"])

    async def test_hangup_reconciles_late_action_with_its_exact_user_turn(self) -> None:
        daemon, bridge, _ = self.make_bridge()
        bridge._transcript_turns = [
            {"id": "question", "role": "user", "text": "现在是什么状态"},
            {
                "id": "answer",
                "role": "assistant",
                "text": "当前功能已经可以继续测试了。",
            },
            {"id": "action", "role": "user", "text": "放两次礼花"},
            {
                "id": "accepted",
                "role": "assistant",
                "text": "收到，我现在开始。",
            },
            {"id": "farewell", "role": "user", "text": "好的，我先挂了"},
        ]
        bridge._transcript_turn_indexes = {
            turn["id"]: index for index, turn in enumerate(bridge._transcript_turns)
        }
        bridge._completed_transcript_turn_ids.update({'question', 'action', 'farewell'})

        await bridge.stop()

        self.assertEqual(daemon.relayed, ["放两次礼花"])

    async def test_late_reconciliation_never_replays_an_already_sent_action(self) -> None:
        daemon, bridge, _ = self.make_bridge()
        bridge._transcript_turns = [
            {"id": "action", "role": "user", "text": "放两次礼花"},
            {
                "id": "accepted",
                "role": "assistant",
                "text": "收到，我现在开始。",
            },
        ]
        bridge._transcript_turn_indexes = {"action": 0, "accepted": 1}
        bridge._schedule_phone_task("action", "放两次礼花")

        await bridge.stop()

        self.assertEqual(daemon.relayed, ["放两次礼花"])

    async def test_completed_codex_answer_is_spoken_locally_once(self) -> None:
        _, bridge, audio = self.make_bridge()
        local_tts = bridge.local_tts
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "user-question",
                    "role": "user",
                    "transcript": "当前状态是什么",
                },
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "assistant-answer",
                    "role": "assistant",
                    "transcript": "",
                },
            }
        )
        done = {
            "type": "turn.done",
            "turn": {"role": "assistant", "transcript": "这是 Codex 的回答。"},
        }

        bridge._on_realtime_event(done)
        bridge._on_realtime_event(done)
        await asyncio.gather(*tuple(bridge._local_speech_tasks))

        self.assertEqual(  # type: ignore[attr-defined]
            local_tts.texts,
            ["老板，done。", "这是 Codex 的回答。"],
        )
        self.assertEqual(audio.played, [b"\x01\x00" * 960] * 2)
        self.assertEqual(
            bridge.pending.job["phone_transcript"],
            [
                {"role": "user", "text": "当前状态是什么"},
                {"role": "assistant", "text": "这是 Codex 的回答。", 'playback_status':'not_verified','output_ms':0},
                {"role": "assistant", "text": "老板，done。", 'playback_status':'not_verified','output_ms':0},
            ],
        )

    async def test_final_punctuation_starts_local_voice_before_turn_done(self) -> None:
        _, bridge, audio = self.make_bridge()
        bridge._register_first_user_text("现在怎么样")
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "assistant-fast",
                    "role": "assistant",
                    "transcript": "链路",
                },
            }
        )
        bridge._on_realtime_event(
            {"type": "turn.delta", "turn_id": "assistant-fast", "delta": "正常"}
        )
        self.assertEqual(audio.played, [])

        bridge._on_realtime_event(
            {"type": "turn.delta", "turn_id": "assistant-fast", "delta": "。"}
        )
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(  # type: ignore[attr-defined]
            bridge.local_tts.texts,
            ["老板，done。", "链路正常。"],
        )

        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-fast",
                    "role": "assistant",
                    "transcript": "链路正常。",
                },
            }
        )
        await asyncio.sleep(0)
        self.assertEqual(  # type: ignore[attr-defined]
            bridge.local_tts.texts,
            ["老板，done。", "链路正常。"],
        )

    async def test_later_sentences_are_spoken_instead_of_discarded(self) -> None:
        _, bridge, _ = self.make_bridge()
        bridge._register_first_user_text("详细说一下")
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "assistant-multi",
                    "role": "assistant",
                    "transcript": "可以。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._local_speech_tasks))

        bridge._on_realtime_event(
            {
                "type": "turn.delta",
                "turn_id": "assistant-multi",
                "delta": "我会把原因和结果完整说清楚。",
            }
        )
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-multi",
                    "role": "assistant",
                    "transcript": "可以。我会把原因和结果完整说清楚。",
                },
            }
        )
        await asyncio.sleep(0)

        self.assertEqual(  # type: ignore[attr-defined]
            bridge.local_tts.texts,
            [
                "老板，done。",
                "可以。",
                "我会把原因和结果完整说清楚。",
            ],
        )

    async def test_user_speech_invalidates_pending_local_announcement(self) -> None:
        _, bridge, audio = self.make_bridge()
        task = bridge._schedule_local_speech("announcement", "先前的汇报")
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        if task is not None:
            await task

        self.assertEqual(audio.clear_calls, 1)
        self.assertEqual(audio.played, [])

    async def test_real_hangup_finishes_without_waiting_full_call_limit(self) -> None:
        daemon = FakeDaemon()
        pending = PendingCall(
            job_id="job",
            job={"session_id": "source", "report": "done"},
            token="local-iphone",
            source_path=Path("job.json"),
        )
        dialer = FakeDialer(disconnected=True)
        bridge = IPhoneVoiceBridge(
            daemon, pending, dialer=dialer  # type: ignore[arg-type]
        )
        mark_realtime_ready(bridge)
        outcome = await bridge.dial_and_wait()
        await asyncio.sleep(0)

        self.assertEqual(outcome, "completed")
        self.assertEqual(dialer.dialed, ["+8613800138000"])
        self.assertEqual(len(dialer.waited), 1)
        self.assertAlmostEqual(dialer.waited[0], 15.0, delta=0.1)

    async def test_announcement_is_never_repeated_automatically(self) -> None:
        daemon, bridge, audio = self.make_bridge()

        first = bridge._schedule_announcement()
        second = bridge._schedule_announcement()
        if first is not None:
            await first

        self.assertEqual(daemon.codex.requests, [])
        self.assertIsNone(second)
        self.assertEqual(len(audio.played), 1)
        self.assertEqual(
            bridge.local_tts.texts,  # type: ignore[attr-defined]
            ["老板，done。"],
        )

    async def test_greeting_plays_report_and_suppresses_filler_reply(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_stopped"})
        bridge._on_realtime_event(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "喂，你好"},
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-greeting",
                    "role": "assistant",
                    "transcript": "你好，在呢，需要我做什么？",
                },
            }
        )
        # A model completion cannot finalize a caller's greeting prefix.
        # Complete the acoustic input before requiring greeting suppression.
        bridge._finalize_pending_input()
        await asyncio.gather(*tuple(bridge._local_speech_tasks))

        self.assertEqual(bridge.local_tts.texts, ["老板，done。"])  # type: ignore[attr-defined]
        self.assertEqual(len(audio.played), 1)
        self.assertEqual(
            bridge.pending.job["suppressed_greeting_reply"],
            "你好，在呢，需要我做什么？",
        )

    async def test_assistant_output_before_first_user_is_never_spoken(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "startup-filler",
                    "role": "assistant",
                    "transcript": "好了没？",
                },
            }
        )
        await asyncio.sleep(0)

        self.assertEqual(audio.played, [])
        self.assertTrue(bridge._first_assistant_response_pending)

        bridge._register_first_user_text("现在能听见吗")
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "actual-answer",
                    "role": "assistant",
                    "transcript": "能听见。",
                },
            }
        )
        await asyncio.gather(*tuple(bridge._local_speech_tasks))
        self.assertEqual(
            bridge.local_tts.texts,  # type: ignore[attr-defined]
            ["老板，done。", "能听见。"],
        )

    async def test_substantive_first_utterance_gets_report_then_answer(self) -> None:
        _, bridge, audio = self.make_bridge()

        bridge._on_realtime_event({"type": "input_audio_buffer.speech_started"})
        bridge._on_realtime_event({"type": "input_audio_buffer.speech_stopped"})
        bridge._on_realtime_event(
            {
                "type": "input_transcript.added",
                "item": {"type": "input_transcript", "text": "现在能做什么"},
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "assistant-answer",
                    "role": "assistant",
                    "transcript": "可以继续接收你的任务。",
                },
            }
        )
        await bridge._input_settle_task
        await asyncio.gather(*tuple(bridge._relay_tasks))
        await asyncio.gather(*tuple(bridge._local_speech_tasks))

        self.assertEqual(
            bridge.local_tts.texts,  # type: ignore[attr-defined]
            ["老板，done。", "可以继续接收你的任务。"],
        )
        self.assertEqual(len(audio.played), 2)

    async def test_farewell_is_silent_and_never_relayed_as_task(self) -> None:
        daemon, bridge, audio = self.make_bridge()
        bridge._first_assistant_response_pending = False
        bridge._on_realtime_event(
            {
                "type": "turn.created",
                "turn": {
                    "id": "farewell-user",
                    "role": "user",
                    "transcript": "好的，我先挂了",
                },
            }
        )
        bridge._on_realtime_event(
            {
                "type": "turn.done",
                "turn": {
                    "id": "farewell-assistant",
                    "role": "assistant",
                    "transcript": "收到，我现在开始。",
                },
            }
        )
        await asyncio.sleep(0)

        self.assertEqual(audio.played, [])
        self.assertEqual(daemon.relayed, [])
        self.assertEqual(
            bridge.pending.job["suppressed_farewell_reply"],
            "收到，我现在开始。",
        )


class BriefPhoneReportTests(unittest.TestCase):
    def test_keeps_only_first_status_sentence(self) -> None:
        self.assertEqual(
            brief_phone_report(
                "Codex 电话功能已经安装完成。"
                "你现在可以直接问问题，或者下达任务。"
            ),
            "Codex 电话功能已经安装完成。",
        )

    def test_overlong_copy_falls_back_instead_of_stopping_mid_sentence(self) -> None:
        result = brief_phone_report("已经完成" * 50, limit=20)
        self.assertEqual(result, "任务已完成。")

    def test_announcement_starts_with_boss_and_has_one_status_sentence(self) -> None:
        self.assertEqual(
            phone_announcement("修复已完成。你可以继续测试。"),
            "老板，修复已完成。",
        )

if __name__ == "__main__":
    unittest.main()
