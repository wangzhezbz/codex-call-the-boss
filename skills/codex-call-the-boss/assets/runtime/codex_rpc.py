from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import tomllib
from typing import Any


JsonObject = dict[str, Any]
NotificationHandler = Callable[[JsonObject], Any]


class CodexRpcError(RuntimeError):
    pass


def phone_control_system_proxy(path=None):
    """Read only this bridge's opt-in; never change Codex/global networking."""
    if path is None:
        state = Path(os.environ.get('CODEX_PHONE_STATE_DIR') or Path.home()/'.codex-phone')
        path = state/'config.json'
    if not path.exists():
        return False
    try:
        mode = json.loads(path.read_text()).get('phone_control_route', 'unchanged')
    except (OSError, ValueError, AttributeError) as exc:
        raise CodexRpcError('Phone control route configuration is unavailable') from exc
    if mode == 'unchanged':
        return False
    if mode != 'system-proxy':
        raise CodexRpcError('Unknown phone control route')
    from socks_media import system_socks_proxy
    if system_socks_proxy() is None:
        raise CodexRpcError('Selected existing loopback proxy is unavailable')
    return True


def readiness_error_code(error: Exception) -> str:
    """Allowlisted diagnostic only; never expose raw RPC data or guess logout."""
    if isinstance(error, TimeoutError):
        return 'codex_readiness_timeout'
    if isinstance(error, CodexRpcError):
        try:
            detail = json.loads(str(error))
        except (TypeError, ValueError):
            detail = None
        if (isinstance(detail, dict) and detail.get('code') == -32603
                and detail.get('message') == 'workspace routing discovery timed out'):
            return 'workspace_routing_timeout'
    return 'codex_readiness_unavailable'


def _classifier_catalog_candidate(cwd, *, now=None):
    """Reuse only a fresh standard Codex catalog, never a custom provider/catalog.

    Configuration files and the cache are read-only. Unknown/malformed settings
    leave normal Codex discovery in place; no new model is selected here.
    """
    codex_path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    if not codex_path.is_absolute() or cwd is None:
        return None, 'unresolved_config'
    project = Path(cwd)
    if not project.is_absolute():
        return None, 'unresolved_config'
    try:
        cache = codex_path / 'models_cache.json'
        if cache.stat().st_size > 4 * 1024 * 1024:
            return None, 'invalid_cache'
        data = json.loads(cache.read_text())
        if not isinstance(data, dict):
            return None, 'invalid_cache'
        fetched = datetime.fromisoformat(data['fetched_at'])
        age = ((now or datetime.now(timezone.utc)) - fetched).total_seconds()
        if not 0 <= age <= 300:
            return None, 'stale_cache'
        models = data['models']
        version = data['client_version']
        if (not isinstance(version, str) or not 1 <= len(version) <= 50
                or not isinstance(models, list) or not 1 <= len(models) <= 512
                or any(not isinstance(row, dict) or not isinstance(row.get('slug'), str)
                       or not row['slug'] or not isinstance(row.get('display_name'), str)
                       or not isinstance(row.get('supported_reasoning_levels'), list)
                       or not isinstance(row.get('model_messages'), dict)
                       or not isinstance(row['model_messages'].get('instructions_template'), str)
                       or not row['model_messages']['instructions_template'] for row in models)):
            return None, 'invalid_cache'
        slugs = {row['slug'] for row in models}
        if len(slugs) != len(models):
            return None, 'invalid_cache'

        def custom(settings):
            # Be conservative about inactive profiles as well: an explicit
            # alternate catalog/provider is not ours to replace for speed.
            if isinstance(settings, dict):
                if settings.get('model_catalog_json') is not None:
                    return True
                if settings.get('model_provider', 'openai') != 'openai':
                    return True
                if 'model' in settings and settings['model'] not in slugs:
                    return True
                return any(custom(value) for value in settings.values())
            return isinstance(settings, list) and any(custom(value) for value in settings)

        configs = {codex_path / 'config.toml', Path('/etc/codex/config.toml')}
        configs.update(parent / '.codex/config.toml' for parent in (project, *project.parents))
        for path in configs:
            if path.exists():
                if path.stat().st_size > 1024 * 1024 or custom(tomllib.loads(path.read_text())):
                    return None, 'custom_config'
        return (cache, version, round(age)), 'candidate'
    except (OSError, ValueError, TypeError, KeyError):
        return None, 'unavailable_cache'


def _catalog_version_matches(cli_output, cache_version):
    """Codex's catalog uses the release core, even in desktop alpha builds.

    Accept an exact full version or its exact major.minor.patch cache key.
    Never treat a different patch/minor or two prerelease versions as equal.
    Unknown CLI output retains normal discovery rather than guessing.
    """
    match = re.fullmatch(
        r'codex-cli ((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
        r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
        r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)', cli_output)
    if not match:
        return False
    prerelease = match.group(2)
    if prerelease and any(part.isdigit() and len(part) > 1 and part[0] == '0'
                          for part in prerelease.split('.')):
        return False
    full = match.group(1)
    core = full.split('+', 1)[0].split('-', 1)[0]
    return cache_version in (full, core)


class CodexAppServer:
    """Small JSONL client for the local `codex app-server` process."""

    def __init__(self, *, capability_profile: str = "conversation", respect_system_proxy=None) -> None:
        if capability_profile not in {"conversation", "classifier"}:
            raise ValueError("Unknown Codex process capability profile")
        self.capability_profile = capability_profile
        self.respect_system_proxy = respect_system_proxy
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._handlers: list[NotificationHandler] = []
        self.stderr_tail: deque[str] = deque(maxlen=30)
        self.closed = asyncio.Event()
        self.last_error = ""
        self.classifier_cwd = None
        self.classifier_catalog_diagnostics = {}

    async def _classifier_catalog_options(self):
        candidate, reason = _classifier_catalog_candidate(self.classifier_cwd)
        self.classifier_catalog_diagnostics = {'mode': 'normal_discovery', 'reason': reason}
        if candidate is None:
            return []
        path, version, age = candidate
        process = None
        try:
            # This is version inspection, not a second app-server or model
            # request. It shares classify's original startup deadline.
            async with asyncio.timeout(.75):
                process = await asyncio.create_subprocess_exec('codex', '--version',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                output, _ = await process.communicate()
            cli_version = output.decode().strip()
            if process.returncode != 0 or not _catalog_version_matches(cli_version, version):
                self.classifier_catalog_diagnostics['reason'] = 'version_mismatch'
                return []
        except (OSError, ValueError, TimeoutError):
            self.classifier_catalog_diagnostics['reason'] = 'version_unavailable'
            return []
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        self.classifier_catalog_diagnostics = {'mode': 'recent_codex_cache', 'age_seconds': age,
                                               'client_version': version,
                                               'cli_version': cli_version.removeprefix('codex-cli ')}
        return ['-c', 'model_catalog_json=' + json.dumps(str(path))]

    @property
    def running(self) -> bool:
        return (
            self.process is not None
            and self.process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def start(self) -> None:
        if self.process is not None:
            return
        self.closed.clear()
        self.last_error = ""
        options = []
        control_proxy = (phone_control_system_proxy() if self.respect_system_proxy is None
                         else self.respect_system_proxy)
        if control_proxy:
            options.extend(('--enable', 'respect_system_proxy'))
        if self.capability_profile == "classifier":
            options.extend(await self._classifier_catalog_options())
            # Some catalog initialization is process-scoped, before a
            # thread/start override can take effect. Isolate only the pure
            # classifier; the source and live Q&A keep all their capabilities.
            for option in ("features.apps=false", "features.plugins=false",
                           "features.remote_plugin=false", "skills.max_context_tokens=1",
                           "features.shell_snapshot=false"):
                options.extend(("-c", option))
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--stdio",
            "--enable",
            "realtime_conversation",
            *options,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Real thread responses can exceed asyncio's default 64 KiB
            # line limit. A crashed JSON reader otherwise looks like a
            # permanently silent conversation.
            limit=8 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_phone",
                    "title": "Codex Phone",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (self._reader_task, self._stderr_task)
                               if task is not None), return_exceptions=True)
        self.closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexRpcError("Codex app-server closed"))
        self._pending.clear()

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._handlers.append(handler)

    def remove_notification_handler(self, handler: NotificationHandler) -> None:
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    async def request(
        self, method: str, params: JsonObject | None = None, timeout: float = 30
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            # The deadline includes a blocked stdin drain. Always clean up
            # even if the pipe closes before sending, not only while waiting.
            async with asyncio.timeout(timeout):
                await self._send({"id": request_id, "method": method, "params": params or {}})
                return await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _send(self, payload: JsonObject) -> None:
        if self.closed.is_set():
            raise CodexRpcError(self.last_error or "Codex app-server closed")
        if self.process is None or self.process.stdin is None:
            raise CodexRpcError("Codex app-server is not running")
        if self.process.returncode is not None:
            detail = "\n".join(self.stderr_tail)
            raise CodexRpcError(f"Codex app-server exited: {detail}")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.process.stdin.write(data.encode("utf-8") + b"\n")
        await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            await self._read_messages(self.process.stdout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A malformed/oversized line or failed server-request reply must
            # fail every waiter immediately, not turn into minutes of silence.
            self.last_error = f"Codex app-server reader failed: {type(exc).__name__}: {exc}"
        finally:
            self.last_error = self.last_error or "Codex app-server output closed"
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRpcError(self.last_error))

    async def _read_messages(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                raise CodexRpcError("invalid non-object protocol response")
            request_id = message.get("id")
            if request_id is not None and "method" not in message:
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(CodexRpcError(json.dumps(message["error"])))
                else:
                    future.set_result(message.get("result"))
                continue
            if request_id is not None and "method" in message:
                await self._handle_server_request(message)
                continue
            if "method" in message:
                for handler in tuple(self._handlers):
                    try:
                        result = handler(message)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        # One observer must not break the app-server event loop.
                        continue

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    async def _handle_server_request(self, message: JsonObject) -> None:
        method = str(message.get("method", ""))
        request_id = message["id"]
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
            "item/permissions/requestApproval",
        }:
            await self._send({"id": request_id, "result": {"decision": "decline"}})
            return
        if method == "item/tool/requestUserInput":
            await self._send({"id": request_id, "result": {"answers": {}}})
            return
        await self._send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unsupported request: {method}"},
            }
        )


async def read_subscription_status() -> dict[str, Any]:
    server = CodexAppServer()
    try:
        await server.start()
        account_result = await server.request("account/read", {"refreshToken": False})
        voice_result = await server.request("thread/realtime/listVoices", {})
        account = (account_result or {}).get("account") or {}
        voices = (voice_result or {}).get("voices", {})
        return {
            "accountType": account.get("type"),
            "planType": account.get("planType"),
            # The phone path uses Realtime v3, whose live compatibility set is
            # currently the catalog exposed as v1.
            "voiceCount": len(voices.get("v1", [])),
            "v2VoiceCount": len(voices.get("v2", [])),
            "defaultVoice": voices.get("defaultV1"),
        }
    finally:
        await server.close()
