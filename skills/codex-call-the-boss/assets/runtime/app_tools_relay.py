from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any


class AppToolsRelayError(RuntimeError):
    pass


class AppToolsRelayClient:
    def __init__(self, socket_path: Path, *, timeout: float = 15.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path), limit=8 * 2**20),
                timeout=self.timeout,
            )
        except (OSError, TimeoutError) as exc:
            raise AppToolsRelayError("当前 Codex 窗口消息通道未运行") from exc
        request_id = secrets.token_hex(8)
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            writer.write(payload + b"\n")
            await writer.drain()
            # A tool may still be delivering after fifteen seconds. Closing
            # early makes an accepted command look failed and invites a
            # duplicate. Match the desktop relay's bounded tool deadline.
            response_timeout = max(self.timeout, 130) if method == "send_message_to_thread" else self.timeout
            raw = await asyncio.wait_for(reader.readline(), timeout=response_timeout)
        except (OSError, TimeoutError, ValueError) as exc:
            raise AppToolsRelayError("当前 Codex 窗口消息通道无响应") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if not raw:
            raise AppToolsRelayError("当前 Codex 窗口消息通道已断开")
        try:
            response = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppToolsRelayError("当前 Codex 窗口消息通道返回无效数据") from exc
        if not isinstance(response, dict):
            raise AppToolsRelayError("当前 Codex 窗口消息通道返回无效数据")
        if response.get("id") != request_id:
            raise AppToolsRelayError("当前 Codex 窗口消息通道响应不匹配")
        if response.get("error"):
            raise AppToolsRelayError(str(response["error"]))
        return response.get("result")

    async def health(self) -> bool:
        result = await self.request("health")
        return bool((result or {}).get("ready"))

    async def read_thread(self, thread_id: str) -> Any:
        return await self.request("read_thread", {"threadId": thread_id})

    async def read_context(self, thread_id: str) -> Any:
        return await self.request("read_context", {"threadId": thread_id})

    async def send_message_to_thread(
        self, caller_thread_id: str, target_thread_id: str, prompt: str
    ) -> Any:
        return await self.request(
            "send_message_to_thread",
            {
                "callerThreadId": caller_thread_id,
                "threadId": target_thread_id,
                "prompt": prompt,
            },
        )
