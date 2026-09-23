from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any


class MacTextToSpeechError(RuntimeError):
    pass


def installed_chinese_voices(
    *, timeout_seconds: float = 45
) -> list[dict[str, Any]]:
    """Return the Chinese voices that AVSpeechSynthesizer can actually open."""
    helper = Path(__file__).with_name("mac_tts_helper.swift")
    if not helper.is_file():
        raise MacTextToSpeechError("缺少 macOS 增强语音组件源码")
    try:
        result = subprocess.run(
            ["/usr/bin/swift", str(helper), "--list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(5.0, float(timeout_seconds)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MacTextToSpeechError(f"无法读取 macOS 中文声音：{exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise MacTextToSpeechError(
            "无法读取 macOS 中文声音："
            + (detail or f"退出码 {result.returncode}")
        )

    voices: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.rsplit("|", 2)
        if len(parts) != 3:
            continue
        name, identifier, raw_quality = (part.strip() for part in parts)
        if not name or not identifier:
            continue
        try:
            quality = int(raw_quality)
        except ValueError:
            continue
        voices.append(
            {"name": name, "identifier": identifier, "quality": quality}
        )
    if not voices:
        raise MacTextToSpeechError("没有找到已安装的 macOS 中文声音")
    return voices


class MacTextToSpeech:
    """Render text with macOS voices as mono PCM16 at the phone's 48 kHz rate."""

    sample_rate = 48_000

    def __init__(
        self,
        *,
        voice: str = "com.apple.siri.natural.Linfei",
        rate: int = 190,
        av_rate: float = 0.50,
        pitch: float = 1.04,
        timeout_seconds: float = 45,
        executable: str = "/usr/bin/say",
    ) -> None:
        self.voice = voice.strip() or "com.apple.siri.natural.Linfei"
        self.rate = max(80, min(360, int(rate)))
        self.av_rate = max(0.1, min(1.0, float(av_rate)))
        self.pitch = max(0.7, min(1.4, float(pitch)))
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.executable = executable
        self.renderer = (
            "macos-avspeech" if self.voice.startswith("com.apple.") else "macos-say"
        )
        self.synthesis_count = 0
        self.error_count = 0
        self.total_audio_frames = 0
        self.last_audio_ms = 0
        self.last_render_ms = 0
        self.last_text_chars = 0
        self._av_process: asyncio.subprocess.Process | None = None
        self._av_lock = asyncio.Lock()
        self._av_request_sequence = 0
        self.av_server_start_count = 0

    async def synthesize(self, text: str) -> bytes:
        text = self._normalize_text(text)
        if not text:
            return b""

        wav_handle, wav_name = tempfile.mkstemp(
            prefix="codex-phone-tts-", suffix=".wav"
        )
        os.close(wav_handle)
        wav_path = Path(wav_name)
        native_path: Path | None = None
        started_at = time.monotonic()
        try:
            if self.renderer == "macos-avspeech":
                native_handle, native_name = tempfile.mkstemp(
                    prefix="codex-phone-tts-", suffix=".caf"
                )
                os.close(native_handle)
                native_path = Path(native_name)
                await self._run_avspeech(native_path, text)
                await self._convert_to_phone_wav(native_path, wav_path)
            else:
                await self._run_say(wav_path, text)
            pcm = self._read_pcm48k(wav_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error_count += 1
            raise
        finally:
            if native_path is not None:
                native_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)

        self.synthesis_count += 1
        self.total_audio_frames += len(pcm) // 2
        self.last_audio_ms = round(len(pcm) * 1000 / (self.sample_rate * 2))
        self.last_render_ms = round((time.monotonic() - started_at) * 1000)
        self.last_text_chars = len(text)
        return pcm

    async def _run_say(self, destination: Path, text: str) -> None:
        await self._run_command(
            [
                self.executable,
                "-v",
                self.voice,
                "-r",
                str(self.rate),
                "-o",
                str(destination),
                "--file-format=WAVE",
                f"--data-format=LEI16@{self.sample_rate}",
                "--channels=1",
                "--",
                text,
            ],
            error_prefix="macOS 本地语音合成失败",
        )

    async def _run_avspeech(self, destination: Path, text: str) -> None:
        helper = Path(__file__).with_name("mac_tts_helper.swift")
        if not helper.is_file():
            raise MacTextToSpeechError("缺少 macOS 增强语音组件源码")
        async with self._av_lock:
            process = await self._ensure_avspeech_server(helper)
            assert process.stdin is not None and process.stdout is not None
            self._av_request_sequence += 1
            request_id = str(self._av_request_sequence)
            request = json.dumps(
                {
                    "id": request_id,
                    "output": str(destination),
                    "text": text,
                    "voice": self.voice,
                    "rate": self.av_rate,
                    "pitch": self.pitch,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                process.stdin.write(request + b"\n")
                await process.stdin.drain()
                raw = await asyncio.wait_for(
                    process.stdout.readline(), timeout=self.timeout_seconds
                )
            except (OSError, TimeoutError) as exc:
                await self._stop_avspeech_server()
                raise MacTextToSpeechError("macOS 增强语音合成超时") from exc
            except asyncio.CancelledError:
                await self._stop_avspeech_server()
                raise
            if not raw:
                detail = await self._avspeech_stderr(process)
                await self._stop_avspeech_server()
                raise MacTextToSpeechError(
                    "macOS 增强语音合成失败：" + (detail or "语音进程已退出")
                )
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:
                await self._stop_avspeech_server()
                raise MacTextToSpeechError("macOS 增强语音返回无效数据") from exc
            if response.get("id") != request_id or not response.get("ok"):
                detail = str(response.get("error") or "未知错误")
                raise MacTextToSpeechError(f"macOS 增强语音合成失败：{detail}")

    async def _ensure_avspeech_server(
        self, helper: Path
    ) -> asyncio.subprocess.Process:
        process = self._av_process
        if process is not None and process.returncode is None:
            return process
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/swift",
            str(helper),
            "--server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._av_process = process
        self.av_server_start_count += 1
        return process

    @staticmethod
    async def _avspeech_stderr(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        try:
            payload = await asyncio.wait_for(process.stderr.read(), timeout=1)
        except TimeoutError:
            return ""
        return payload.decode("utf-8", errors="replace").strip()

    async def _stop_avspeech_server(self) -> None:
        process = self._av_process
        self._av_process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        async with self._av_lock:
            await self._stop_avspeech_server()

    async def _convert_to_phone_wav(
        self, source: Path, destination: Path
    ) -> None:
        destination.unlink(missing_ok=True)
        await self._run_command(
            [
                "/usr/bin/afconvert",
                "-f",
                "WAVE",
                "-d",
                f"LEI16@{self.sample_rate}",
                "-c",
                "1",
                str(source),
                str(destination),
            ],
            error_prefix="macOS 增强语音格式转换失败",
        )

    async def _run_command(
        self,
        arguments: list[str],
        *,
        input_payload: bytes | None = None,
        error_prefix: str,
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=(
                asyncio.subprocess.PIPE if input_payload is not None else None
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(input_payload), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise MacTextToSpeechError("macOS 本地语音合成超时") from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise MacTextToSpeechError(
                error_prefix + "：" + (detail or f"退出码 {process.returncode}")
            )

    def _read_pcm48k(self, source: Path) -> bytes:
        try:
            with wave.open(str(source), "rb") as stream:
                metadata = (
                    stream.getnchannels(),
                    stream.getsampwidth(),
                    stream.getframerate(),
                    stream.getcomptype(),
                )
                expected = (1, 2, self.sample_rate, "NONE")
                if metadata != expected:
                    raise MacTextToSpeechError(
                        f"macOS 本地语音格式异常：{metadata!r}，期望 {expected!r}"
                    )
                payload = stream.readframes(stream.getnframes())
        except (OSError, EOFError, wave.Error) as exc:
            raise MacTextToSpeechError(f"无法读取 macOS 本地语音：{exc}") from exc
        if not payload or len(payload) % 2:
            raise MacTextToSpeechError("macOS 本地语音没有生成有效 PCM")
        return payload

    @staticmethod
    def _normalize_text(text: str) -> str:
        # `say` accepts embedded [[commands]].  Transcript text is model
        # output, so strip those controls and speak only the visible text.
        text = re.sub(r"\[\[[^\]]*\]\]", " ", str(text))
        return re.sub(r"\s+", " ", text).strip()[:1000]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "renderer": self.renderer,
            "voice": self.voice,
            "rate": self.rate,
            "av_rate": self.av_rate,
            "pitch": self.pitch,
            "sample_rate": self.sample_rate,
            "synthesis_count": self.synthesis_count,
            "error_count": self.error_count,
            "total_audio_ms": round(
                self.total_audio_frames * 1000 / self.sample_rate
            ),
            "last_audio_ms": self.last_audio_ms,
            "last_render_ms": self.last_render_ms,
            "last_text_chars": self.last_text_chars,
            "av_server_start_count": self.av_server_start_count,
            "av_server_running": bool(
                self._av_process is not None
                and self._av_process.returncode is None
            ),
        }
