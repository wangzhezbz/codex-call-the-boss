#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
BUNDLED_RUNTIME = SKILL_DIR / "assets" / "runtime"
DEFAULT_HOME = Path(
    os.environ.get("CODEX_CALL_THE_BOSS_HOME")
    or Path.home() / ".local" / "share" / "codex-call-the-boss"
).expanduser()

RUNTIME_FILES = (
    "app_tools_relay.mjs",
    "app_tools_relay.py",
    "audio_codec.py",
    "audio_jitter.py",
    "audio_turn_fence.py",
    "playback_ledger.py",
    "phone_output_probe.py",
    "phone_intent.py",
    "phone_turns.py",
    "phone_source_view.py",
    "phone_stop_backend.py",
    "phone_stop_completion.py",
    "phone_stop_cycles.py",
    "phone_stop_dispatch.py",
    "phone_stop_mailbox.py",
    "phone_stop_queue.py",
    "phone_stop_receipt.py",
    "phone_stop_service.py",
    "phone_transcript.py",
    "socks_media.py",
    "codex_rpc.py",
    "hook_stop.py",
    "iphone_audio.py",
    "mac_tts.py",
    "mac_tts_helper.swift",
    "native_speech.py",
    "doubao_tts.py",
    "doubao_protocols.py",
    "doubao_speech.py",
    "opening_address.py",
    "opus_recovery.py",
    "speech_quality.py",
    "speech_pronunciation.swift",
    "phone_agent.py",
    "phone_reports.py",
    "phone_ax_helper.swift",
    "requirements.txt",
    "runtime_install.py",
    "service_state.py",
    "runtime_health.py",
    "session_registry.py",
    "webrtc_bridge.py",
)
TEST_FILES = (
    "test_audio_codec.py",
    "test_audio_jitter.py",
    "test_codex_rpc.py",
    "test_app_tools_relay.py",
    "test_hook_stop.py",
    "test_iphone_audio.py",
    "test_output_idle.py",
    "test_phone_output_probe.py",
    "test_phone_transcript.py",
    "test_phone_intent.py",
    "test_mac_tts.py",
    "test_doubao_tts.py",
    "test_doubao_speech.py",
    "test_native_speech.py",
    "test_native_speech_control.py",
    "test_opening_address.py",
    "test_opus_recovery.py",
    "test_session_registry.py",
    "test_thread_binding.py",
    "test_phone_source_view.py",
    "test_stop_backend.py",
    "test_sync_install.py",
    "test_phone_stop_dispatch.py",
    "test_phone_stop_receipt.py",
    "test_phone_stop_mailbox.py",
    "test_voice_bridge.py",
    "test_phone_reliability.py",
    "test_dial_action_safety.py",
    "test_webrtc_bridge.py",
    "test_realtime_startup_lifecycle.py",
    "test_startup_readiness.py",
    "test_conversation_contract.py",
    "test_phone_flow_regressions.py",
    "test_socks_media.py",
    "test_runtime_install.py",
    "test_service_state.py",
    "test_runtime_health.py",
)


def _runtime_dir(home: Path) -> Path:
    return home / "runtime"


def _runtime_python(home: Path) -> Path:
    return _runtime_dir(home) / ".venv" / "bin" / "python"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _current_thread_id() -> str:
    return str(
        os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_SESSION_ID")
        or ""
    ).strip()


def _session_enabled(thread_id: str) -> bool:
    if not thread_id:
        return False
    registry = _read_json(Path.home() / ".codex-phone" / "sessions.json")
    sessions = registry.get("sessions") or {}
    record = sessions.get(thread_id) if isinstance(sessions, dict) else None
    return isinstance(record, dict) and record.get("enabled") is True


def _app_message_relay_ready() -> bool:
    socket_path = Path.home() / ".codex-phone" / "app-tools-relay.sock"
    if not socket_path.exists():
        return False
    request_id = os.urandom(8).hex()
    request = json.dumps(
        {"id": request_id, "method": "health", "params": {}},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(socket_path))
            client.sendall(request)
            response = b""
            while b"\n" not in response and len(response) < 1_048_576:
                chunk = client.recv(65_536)
                if not chunk:
                    break
                response += chunk
        payload = json.loads(response.split(b"\n", 1)[0])
    except (OSError, TimeoutError, json.JSONDecodeError):
        return False
    return payload.get("id") == request_id and bool(
        (payload.get("result") or {}).get("ready")
    )


def _synchronous_stop_selected() -> bool:
    state = Path(os.environ.get('CODEX_PHONE_STATE_DIR') or Path.home() / '.codex-phone')
    return _read_json(state / 'config.json').get('phone_command_transport') == 'synchronous_stop'


def plan(home: Path) -> int:
    config = _read_json(Path.home() / ".codex-phone" / "config.json")
    current = _current_thread_id()
    relay_identity = bool(str(config.get("relay_caller_thread_id") or "").strip())
    synchronous = _synchronous_stop_selected()
    relay_ready = False if synchronous else _app_message_relay_ready()
    result = {
        "platform": platform.system(),
        "phone_app": Path("/System/Applications/Phone.app").exists(),
        "relay_identity_configured": relay_identity,
        "app_message_relay_ready": relay_ready,
        "command_transport": (
            "synchronous_stop (per-call live owner required)" if synchronous else
            "codex_app_send_message" if relay_identity and relay_ready else "missing"
        ),
        "blackhole_2ch": Path(
            "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver"
        ).exists(),
        "blackhole_16ch": Path(
            "/Library/Audio/Plug-Ins/HAL/BlackHole16ch.driver"
        ).exists(),
        "runtime_installed": _runtime_python(home).is_file(),
        "phone_line_configured": bool(config.get("provider") and config.get("to_number")),
        "current_thread_available": bool(current),
        "current_session_enabled": _session_enabled(current),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _python_for_venv() -> str:
    return _installer_module().select_python()


def _installer_module():
    path = BUNDLED_RUNTIME / "runtime_install.py"
    spec = importlib.util.spec_from_file_location("phone_runtime_install", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_runtime(destination: Path) -> None:
    if not BUNDLED_RUNTIME.is_dir():
        raise RuntimeError(f"bundled runtime is missing: {BUNDLED_RUNTIME}")
    destination.mkdir(parents=True, exist_ok=True)
    tests_destination = destination / "tests"
    tests_destination.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        source = BUNDLED_RUNTIME / name
        if not source.is_file():
            raise RuntimeError(f"bundled runtime file is missing: {name}")
        shutil.copy2(source, destination / name)
    for name in TEST_FILES:
        source = BUNDLED_RUNTIME / "tests" / name
        if not source.is_file():
            raise RuntimeError(f"bundled test file is missing: {name}")
        shutil.copy2(source, tests_destination / name)


def install(home: Path, *, confirmed: bool) -> int:
    if not confirmed:
        raise SystemExit("install changes local files and downloads Python packages; pass --yes after user approval")
    if platform.system() != "Darwin":
        raise SystemExit("the bundled iPhone bridge currently supports macOS only")
    result = _installer_module().install_runtime(home, BUNDLED_RUNTIME, RUNTIME_FILES, TEST_FILES,
        state=Path(os.environ.get("CODEX_PHONE_STATE_DIR") or Path.home() / ".codex-phone"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _run_runtime(home: Path, arguments: list[str]) -> int:
    python = _runtime_python(home)
    agent = _runtime_dir(home) / "phone_agent.py"
    if not python.is_file() or not agent.is_file():
        raise SystemExit("runtime is not installed; run install --yes first")
    return subprocess.run([str(python), str(agent), *arguments], check=False).returncode


def _exec_runtime(home: Path, arguments: list[str]) -> int:
    python = _runtime_python(home)
    agent = _runtime_dir(home) / "phone_agent.py"
    if not python.is_file() or not agent.is_file():
        raise SystemExit("runtime is not installed; run install --yes first")
    os.execv(str(python), [str(python), str(agent), *arguments])
    return 0


def stage_report(home: Path, text: str) -> int:
    """Stage phone-only copy without placing metadata in the visible reply."""
    thread_id = _current_thread_id()
    if not thread_id:
        raise SystemExit("找不到当前 Codex 会话 ID")
    python = _runtime_python(home)
    hook = _runtime_dir(home) / "hook_stop.py"
    if not python.is_file() or not hook.is_file():
        raise SystemExit("runtime is not installed; run install --yes first")
    if _synchronous_stop_selected():
        if not _session_enabled(thread_id) or _run_runtime(home, ['check-completion-entry']) != 0:
            raise SystemExit('当前任务的同步电话入口未就绪，未暂存汇报')
    elif not _app_message_relay_ready():
        raise SystemExit("桌面消息通道已断开：先在当前 Codex 命令会话运行 start-relay，再重新 stage-report；不要假报电话已就绪")
    if _run_runtime(home, ["ensure-daemon"]) != 0:
        raise SystemExit("电话后台未通过真实就绪检查，未写入汇报；请检查 doctor")
    if _run_runtime(home, ["prepare-native-audio", "--report", text]) != 0:
        subprocess.run([str(python), str(hook), "preparation-failed", "--thread-id", thread_id],
                       check=True)
        raise SystemExit("本轮配音汇报未通过拨号前检查，已记录为本轮准备失败；不会拨号或由完成兜底重新尝试")
    return subprocess.run(
        [
            str(python),
            str(hook),
            "stage-report",
            "--thread-id",
            thread_id,
            "--text",
            text,
        ],
        check=False,
    ).returncode


def skip_call(home: Path) -> int:
    """Skip the next completion callback without disabling this session."""
    thread_id = _current_thread_id()
    if not thread_id:
        raise SystemExit("找不到当前 Codex 会话 ID")
    python = _runtime_python(home)
    hook = _runtime_dir(home) / "hook_stop.py"
    if not python.is_file() or not hook.is_file():
        raise SystemExit("runtime is not installed; run install --yes first")
    return subprocess.run(
        [
            str(python),
            str(hook),
            "skip-call",
            "--thread-id",
            thread_id,
        ],
        check=False,
    ).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and control the session-scoped Codex phone bridge"
    )
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--yes", action="store_true")
    commands.add_parser("configure-iphone")
    commands.add_parser("list-phone-voices")
    set_voice = commands.add_parser("set-phone-voice")
    set_voice.add_argument("--identifier", required=True)
    commands.add_parser("list-realtime-voices")
    set_realtime = commands.add_parser("set-realtime-voice")
    set_realtime.add_argument("--voice", required=True)
    media_route = commands.add_parser('set-media-route')
    media_route.add_argument('--route', choices=['direct','system-socks'], required=True)
    control_route = commands.add_parser('set-control-route')
    control_route.add_argument('--route', choices=['unchanged','system-proxy'], required=True)
    commands.add_parser("enable")
    commands.add_parser("disable")
    output_probe = commands.add_parser('output-probe')
    output_probe.add_argument('--seconds', type=int, default=60)
    output_probe.add_argument('--off', action='store_true')
    commands.add_parser("status")
    confirm = commands.add_parser('confirm-queued-report')
    confirm.add_argument('--job-id', required=True)
    confirm.add_argument('--confirm', action='store_true')
    commands.add_parser("doctor")
    export = commands.add_parser('export-diagnostics')
    export.add_argument('--output', type=Path, required=True)
    commands.add_parser("verify")
    commands.add_parser("start-relay")
    commands.add_parser('stop-for-update')
    commands.add_parser("prepare-voice")
    commands.add_parser('configure-doubao')
    commands.add_parser('prepare-doubao-voice')
    doubao = commands.add_parser('set-doubao-voice')
    doubao.add_argument('--confirm', action='store_true')
    excerpt = commands.add_parser('prepare-delivery-excerpt')
    excerpt.add_argument('--source-sha256', required=True)
    excerpt.add_argument('--start-frame', type=int, required=True)
    address = commands.add_parser('set-opening-address')
    address.add_argument('--source-key', required=True)
    address.add_argument('--source-sha256', required=True)
    address.add_argument('--end-frame', type=int, required=True)
    address.add_argument('--sha256', required=True)
    address.add_argument('--confirm', action='store_true')
    opening_mode = commands.add_parser('set-opening-mode')
    opening_mode.add_argument('--mode', choices=['full-source', 'fixed-body'], required=True)
    opening_mode.add_argument('--confirm', action='store_true')
    relay_identity = commands.add_parser("set-relay-identity")
    relay_identity.add_argument("--thread-id", required=True)
    stage = commands.add_parser("stage-report")
    stage.add_argument("--text", required=True)
    commands.add_parser("skip-call")
    self_test = commands.add_parser("voice-self-test")
    self_test.add_argument("--output", type=Path, default=None)
    self_test.add_argument("--voice", default=None)
    test_call = commands.add_parser("test-call")
    test_call.add_argument("--confirm", action="store_true")
    test_call.add_argument("--seconds", type=int, default=None)
    test_call.add_argument("--report", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    home = args.home.expanduser().resolve()
    if args.command == "plan":
        return plan(home)
    if args.command == "install":
        return install(home, confirmed=args.yes)
    if args.command == "configure-iphone":
        return _run_runtime(home, ["configure-iphone"])
    if args.command == "list-phone-voices":
        return _run_runtime(home, ["list-phone-voices"])
    if args.command == "set-phone-voice":
        return _run_runtime(
            home, ["set-phone-voice", "--identifier", args.identifier]
        )
    if args.command == "list-realtime-voices":
        return _run_runtime(home, ["list-realtime-voices"])
    if args.command == "set-realtime-voice":
        return _run_runtime(home, ["set-realtime-voice", "--voice", args.voice])
    if args.command == 'set-media-route':
        return _run_runtime(home, ['set-media-route','--route',args.route])
    if args.command == 'set-control-route':
        return _run_runtime(home, ['set-control-route','--route',args.route])
    if args.command == "enable":
        return _run_runtime(home, ["session-enable"])
    if args.command == "disable":
        return _run_runtime(home, ["session-disable"])
    if args.command == 'output-probe':
        return _run_runtime(home, ['session-output-probe', '--seconds', str(args.seconds)]
                            + (['--off'] if args.off else []))
    if args.command == "status":
        return _run_runtime(home, ["session-status"])
    if args.command == 'confirm-queued-report':
        if not args.confirm:
            raise SystemExit('explicit confirmation is required to release one held report')
        return _run_runtime(home, ['confirm-queued-report', '--job-id', args.job_id])
    if args.command == "doctor":
        return _run_runtime(home, ["doctor"])
    if args.command == 'export-diagnostics':
        return _run_runtime(home, ['export-diagnostics','--output',str(args.output.expanduser().resolve())])
    if args.command == "start-relay":
        return _exec_runtime(home, ["app-tools-relay"])
    if args.command == 'stop-for-update':
        return _run_runtime(home, ['stop-for-update'])
    if args.command == "prepare-voice":
        return _run_runtime(home, ["prepare-native-audio", "--initialize"])
    if args.command == 'configure-doubao':
        return subprocess.run([str(_runtime_python(home)), str(_runtime_dir(home)/'doubao_tts.py'),
                               'configure'], check=False).returncode
    if args.command == 'prepare-doubao-voice':
        return _run_runtime(home, ['prepare-doubao-voice'])
    if args.command == 'set-doubao-voice':
        if not args.confirm:
            raise SystemExit('先取得使用豆包配音服务的明确授权，再使用 --confirm；未改变配置')
        return _run_runtime(home, ['set-doubao-voice', '--confirm'])
    if args.command == 'prepare-delivery-excerpt':
        return _run_runtime(home, ['prepare-delivery-excerpt', '--source-sha256', args.source_sha256,
                                  '--start-frame', str(args.start_frame)])
    if args.command == 'set-opening-address':
        if not args.confirm:
            raise SystemExit('先由听众确认该段称呼，再使用 --confirm；未改变配置')
        return _run_runtime(home, ['set-opening-address', '--source-key', args.source_key,
                                  '--source-sha256', args.source_sha256, '--end-frame', str(args.end_frame),
                                  '--sha256', args.sha256, '--confirm'])
    if args.command == "set-relay-identity":
        return _run_runtime(
            home, ["set-relay-identity", "--thread-id", args.thread_id]
        )
    if args.command == 'set-opening-mode':
        if not args.confirm:
            raise SystemExit('先由用户选择开场组合方式，再使用 --confirm；未改变配置')
        return _run_runtime(home, ['set-opening-mode', '--mode', args.mode, '--confirm'])
    if args.command == "verify":
        python = _runtime_python(home)
        if not python.is_file():
            raise SystemExit("runtime is not installed")
        return subprocess.run(
            [str(python), "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=_runtime_dir(home),
            check=False,
        ).returncode
    if args.command == "stage-report":
        return stage_report(home, args.text)
    if args.command == "skip-call":
        return skip_call(home)
    if args.command == "voice-self-test":
        command = ["voice-self-test"]
        if args.output is not None:
            command.extend(["--output", str(args.output.expanduser().resolve())])
        if args.voice:
            command.extend(["--voice", args.voice])
        return _run_runtime(home, command)
    if args.command == "test-call":
        if not args.confirm:
            raise SystemExit("a real call requires an explicit request and --confirm")
        if not args.report.strip():
            raise SystemExit("请先写本次任务的真实简要结果；不能用默认或旧缓存台词代替开场")
        if _synchronous_stop_selected():
            if args.seconds is not None:
                raise SystemExit('同步模式暂不接受手动通话时长；使用默认有界等待，不会立即拨号')
            # Only the real host Stop can own this call. Stage once and end
            # this task promptly; never manufacture a waiting hook from a tool.
            result = stage_report(home, args.report)
            if result == 0:
                print(json.dumps({'staged_for_current_completion': True, 'dial_attempted': False}))
            return result
        if _run_runtime(home, ["prepare-native-audio", "--report", args.report or ""]) != 0:
            thread_id = _current_thread_id()
            if not thread_id:
                raise SystemExit('无法记录本轮准备失败：当前任务身份缺失；禁止再拨')
            subprocess.run([str(_runtime_python(home)), str(_runtime_dir(home) / 'hook_stop.py'),
                            'preparation-failed', '--thread-id', thread_id], check=True)
            raise SystemExit("原声检查未通过，本轮失败已记录；不拨号，也不由完成兜底重试")
        command = ["test-call"]
        if args.seconds is not None:
            command.extend(["--seconds", str(args.seconds)])
        if args.report:
            command.extend(["--report", args.report])
        return _run_runtime(home, command)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
