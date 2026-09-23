from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from mac_tts import MacTextToSpeech, installed_chinese_voices


class MacTextToSpeechTests(unittest.IsolatedAsyncioTestCase):
    def test_lists_installed_chinese_voices(self) -> None:
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "声音2|com.apple.siri.natural.Linfei|2\n"
                "声音4|com.apple.ttsbundle.premium|2\n"
            ),
            stderr="",
        )
        with patch("mac_tts.subprocess.run", return_value=completed):
            voices = installed_chinese_voices()

        self.assertEqual(
            voices,
            [
                {
                    "name": "声音2",
                    "identifier": "com.apple.siri.natural.Linfei",
                    "quality": 2,
                },
                {
                    "name": "声音4",
                    "identifier": "com.apple.ttsbundle.premium",
                    "quality": 2,
                },
            ],
        )

    def test_strips_embedded_speech_controls(self) -> None:
        self.assertEqual(
            MacTextToSpeech._normalize_text(
                "正常回答 [[rate 999]] 仍然只读文字"
            ),
            "正常回答 仍然只读文字",
        )

    @unittest.skipUnless(Path("/usr/bin/say").is_file(), "requires macOS say")
    async def test_renders_phone_ready_pcm(self) -> None:
        renderer = MacTextToSpeech(voice="Tingting", rate=190)

        payload = await renderer.synthesize("这是本地语音通道测试。")

        self.assertGreater(len(payload), 48_000)
        self.assertEqual(len(payload) % 2, 0)
        self.assertEqual(renderer.diagnostics()["synthesis_count"], 1)
        self.assertEqual(renderer.diagnostics()["sample_rate"], 48_000)

    @unittest.skipUnless(
        Path("/usr/bin/swift").is_file()
        and Path("/usr/bin/afconvert").is_file(),
        "requires macOS AVSpeechSynthesizer",
    )
    async def test_renders_enhanced_siri_voice_as_phone_pcm(self) -> None:
        renderer = MacTextToSpeech(
            voice="com.apple.siri.natural.Linfei",
            av_rate=0.50,
            pitch=1.04,
        )
        try:
            payload = await renderer.synthesize("这是增强自然语音测试。")
            second = await renderer.synthesize("第二句不再重启语音进程。")

            self.assertGreater(len(payload), 48_000)
            self.assertGreater(len(second), 48_000)
            diagnostics = renderer.diagnostics()
            self.assertEqual(diagnostics["renderer"], "macos-avspeech")
            self.assertEqual(
                diagnostics["voice"], "com.apple.siri.natural.Linfei"
            )
            self.assertEqual(diagnostics["error_count"], 0)
            self.assertEqual(diagnostics["av_server_start_count"], 1)
            self.assertEqual(diagnostics["synthesis_count"], 2)
        finally:
            await renderer.close()


if __name__ == "__main__":
    unittest.main()
