from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from session_registry import is_session_enabled
from service_state import record_state


STATE_DIR = Path(
    os.environ.get("CODEX_PHONE_STATE_DIR") or Path.home() / ".codex-phone"
).expanduser()
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = STATE_DIR / "config.json"
QUEUE_DIR = STATE_DIR / "queue"
CALLING_DIR = STATE_DIR / "calling"
DONE_DIR = STATE_DIR / "done"
FAILED_DIR = STATE_DIR / "failed"
ACTIVE_CALL_PATH = STATE_DIR / "active-call.json"
STAGED_REPORT_DIR = STATE_DIR / "staged-reports"
STAGED_REPORT_MAX_AGE_SECONDS = 15 * 60
PHONE_REPORT_PATTERN = re.compile(
    r"<!--\s*codex-phone-report\s*:\s*(.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
DEFAULT_SPOKEN_REPORT = "这次任务的处理已结束，具体结果我来向你说明。"


def active_runtime_dir(project: Path | None = None) -> Path:
    """Resolve a versioned entry through its installation's activation link.

    A desktop backend can retain an old Stop command until it reloads. That
    command must not start its retired daemon or win completion deduplication
    with a false failure. Standalone source checkouts keep their own runtime.
    """
    project = (project or PROJECT_DIR).resolve()
    if project.parent.name != 'versions':
        return project
    stable = project.parent.parent / 'runtime'
    if not stable.is_symlink():
        raise RuntimeError('Phone runtime activation link is missing')
    active = stable.resolve(strict=True)
    if (active.parent != project.parent or not active.is_dir()
            or not (active / 'hook_stop.py').is_file()
            or not (active / 'phone_agent.py').is_file()
            or (active / 'installation-failed.json').exists()):
        raise RuntimeError('Phone runtime activation target is invalid')
    return active


def forward_to_active_runtime() -> None:
    active = active_runtime_dir()
    if active != PROJECT_DIR:
        # Replace this process before reading stdin, consuming staged copy,
        # acquiring queue locks, starting a daemon or recording a completion.
        os.execv(sys.executable, [sys.executable, str(active / 'hook_stop.py'), *sys.argv[1:]])


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:120] or "unknown-turn"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _active_call_running(path: Path = ACTIVE_CALL_PATH) -> bool:
    state = _load_json(path)
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _useful_report(report: str) -> bool:
    report = report.strip()
    if not report:
        return False
    if report[0] in "[{":
        try:
            value = json.loads(report)
        except json.JSONDecodeError:
            return True
        if isinstance(value, (dict, list)):
            return False
    return True


def _normalize_spoken_report(text: str, limit: int = 40) -> str:
    """Keep purpose-written phone copy short without cutting it mid-sentence."""
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[`*_#>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n\"'“”‘’")
    text = re.sub(r"^老板[\s，,：:]*", "", text)
    if not text:
        return DEFAULT_SPOKEN_REPORT
    if len(text) > limit:
        complete_sentences = re.findall(r"[^。！？!?]+[。！？!?]", text)
        text = next(
            (sentence.strip() for sentence in complete_sentences if len(sentence.strip()) <= limit),
            DEFAULT_SPOKEN_REPORT,
        )
    if text[-1] not in "。！？!?":
        text += "。"
    return text


def _extract_phone_copy(message: str) -> tuple[str, str]:
    """Read legacy inline phone copy while keeping old jobs compatible."""
    matches = PHONE_REPORT_PATTERN.findall(message)
    visible = PHONE_REPORT_PATTERN.sub("", message).strip()
    spoken = _normalize_spoken_report(matches[-1] if matches else "")
    return visible, spoken


def _staged_report_path(session_id: str, directory: Path | None = None, turn_id: str = "") -> Path:
    directory = directory or STAGED_REPORT_DIR
    suffix = f"--{_safe_id(turn_id)}" if turn_id else ""
    return directory / f"{_safe_id(session_id)}{suffix}.json"


def current_root_turn_id(session_id: str, *, sessions_dir: Path | None = None) -> str:
    """Read only the source task's latest lifecycle metadata, never its text.

    A manual test can precede a long repair turn: a fifteen-minute session
    sidecar is not a one-turn guard. Bind it to the active persisted root ID.
    """
    explicit = str(os.environ.get('CODEX_TURN_ID') or '')
    current = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    if explicit and current == session_id: return explicit
    directory = sessions_dir or Path(os.environ.get('CODEX_SESSIONS_DIR') or Path.home()/'.codex/sessions')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',session_id):
        raise ValueError('无效的来源任务 ID')
    paths = list(directory.rglob(f'*{session_id}*.jsonl'))
    if not paths: raise ValueError('找不到当前来源任务的轮次记录')
    path = max(paths, key=lambda p:p.stat().st_mtime_ns)
    with path.open('rb') as stream:
        stream.seek(0,2)
        position, head, scanned = stream.tell(), b'', 0
        while position > 0 and scanned < 16*1024*1024:
            size = min(position,65536)
            position -= size
            scanned += size
            stream.seek(position)
            lines = (stream.read(size)+head).split(b'\n')
            head = lines.pop(0) if position else b''
            for line in reversed(lines):
                try: record = json.loads(line)
                except (ValueError,UnicodeDecodeError): continue
                if not isinstance(record,dict): continue
                payload = record.get('payload') or {}
                if not isinstance(payload,dict): continue
                turn = str(payload.get('turn_id') or '')
                if record.get('type') == 'event_msg' and payload.get('type') in {'task_complete','turn_aborted'}:
                    raise ValueError('来源任务没有可绑定的进行中轮次')
                if turn and (record.get('type') == 'turn_context' or
                    record.get('type') == 'event_msg' and payload.get('type') == 'task_started'):
                    return turn
    raise ValueError('无法确认当前来源任务的轮次，拒绝使用跨轮次电话标记')


def stage_phone_report(
    session_id: str,
    text: str,
    *,
    thread_id: str | None = None,
    directory: Path | None = None,
    turn_id: str = "",
) -> str:
    """Stage one invisible, purpose-written report for the next Stop event."""
    session_id = str(session_id or "").strip()
    thread_id = str(thread_id or session_id).strip()
    if not session_id or not thread_id:
        raise ValueError("找不到当前 Codex 会话 ID")
    if not is_session_enabled(session_id, thread_id):
        raise ValueError("当前 Codex 会话尚未启用电话汇报")
    if turn_id and _completion_already_recorded(turn_id):
        raise ValueError("本轮电话尝试已有记录，拒绝再次暂存汇报")
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("电话汇报不能为空")
    spoken = _normalize_spoken_report(raw)
    directory = directory or STAGED_REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    destination = _staged_report_path(session_id, directory, turn_id)
    payload = {
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "spoken_report": spoken,
        "staged_at_epoch": time.time(),
    }
    previous = _load_json(destination)
    if previous.get("manual_call") is True and previous.get("skip_call") is True:
        # Updating the final report after an explicit test must not erase the
        # one-call guard and cause a second automatic call for the same turn.
        payload.update(skip_call=True, manual_call=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return spoken


def stage_skip_call(
    session_id: str,
    *,
    thread_id: str | None = None,
    directory: Path | None = None,
    manual_call: bool = False,
    turn_id: str = "",
) -> None:
    """Skip only the next completion call without disabling the subscription."""
    session_id = str(session_id or "").strip()
    thread_id = str(thread_id or session_id).strip()
    if not session_id or not thread_id:
        raise ValueError("找不到当前 Codex 会话 ID")
    if not is_session_enabled(session_id, thread_id):
        raise ValueError("当前 Codex 会话尚未启用电话汇报")
    directory = directory or STAGED_REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    destination = _staged_report_path(session_id, directory, turn_id)
    payload = {
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "skip_call": True,
        "manual_call": manual_call,
        "staged_at_epoch": time.time(),
    }
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _consume_staged_directive(
    session_id: str,
    thread_id: str,
    *,
    directory: Path | None = None,
    now: float | None = None,
    turn_id: str = "",
) -> dict[str, Any]:
    """Atomically consume one staged report or one-turn skip directive."""
    directory = directory or STAGED_REPORT_DIR
    source = _staged_report_path(session_id, directory, turn_id)
    if turn_id and not source.exists():
        source = _staged_report_path(session_id, directory)  # legacy sidecars
    claimed = directory / f".{source.name}.{os.getpid()}.{time.time_ns()}.claimed"
    try:
        os.replace(source, claimed)
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        payload = _load_json(claimed)
    finally:
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass
    if str(payload.get("session_id") or "") != session_id:
        return {}
    if str(payload.get("thread_id") or "") not in {session_id, thread_id}:
        return {}
    bound_turn = str(payload.get('turn_id') or '')
    if bound_turn and bound_turn != turn_id:
        return {}
    try:
        age = (time.time() if now is None else now) - float(
            payload.get("staged_at_epoch") or 0
        )
    except (TypeError, ValueError):
        return {}
    if age < -30 or not bound_turn and age > STAGED_REPORT_MAX_AGE_SECONDS:
        return {}
    if payload.get("skip_call") is True:
        return {"skip_call": True, "manual_call": payload.get("manual_call") is True}
    return {
        "spoken_report": _normalize_spoken_report(
            str(payload.get("spoken_report") or "")
        )
    }


def _consume_staged_report(
    session_id: str,
    thread_id: str,
    *,
    directory: Path | None = None,
    now: float | None = None,
) -> str:
    directive = _consume_staged_directive(
        session_id, thread_id, directory=directory, now=now
    )
    return str(directive.get("spoken_report") or "")


def _ensure_background_daemon() -> tuple[bool, str]:
    try:
        project = active_runtime_dir()
        python = project / ".venv" / "bin" / "python"
        result = subprocess.run(
            [str(python), str(project / "phone_agent.py"), "ensure-daemon"],
            cwd=str(project),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, detail


def _completion_already_recorded(turn_id: str) -> bool:
    name = f"{_safe_id(turn_id)}.json"
    return any(
        (directory / name).exists()
        for directory in (QUEUE_DIR, CALLING_DIR, DONE_DIR, FAILED_DIR)
    )


def _publish_skipped_completion(session_id: str, thread_id: str, turn_id: str) -> None:
    """Publish only an exact-source durable skip; never retry a phone action.

    The second completion observer may repair a failed status write. Existing
    call results remain authoritative, and repeated observations do not move
    an unchanged status to the top of the recent-call list.
    """
    name = f"{_safe_id(turn_id)}.json"
    skipped = _load_json(DONE_DIR / name)
    reasons = {
        "skipped_by_user_for_this_completion": "skipped_by_user_for_this_completion",
        "skipped_after_manual_call_guard": "manual_call_guard",
    }
    reason = reasons.get(skipped.get("outcome"))
    if (not reason or skipped.get("session_id") != session_id
            or skipped.get("thread_id") != thread_id or skipped.get("turn_id") != turn_id
            or skipped.get("session_subscription") is not True):
        return
    state = DONE_DIR.parent
    previous = _load_json(state / "service-status" / name)
    if previous:
        return  # Do not reinterpret any existing result, including a failure.
    try:
        record_state(state, skipped, "skipped", reason=reason, notify=False)
    except OSError:
        pass  # The durable journal still blocks both observers from dialing.


def record_preparation_failure(session_id: str, *, turn_id: str, reason: str = 'native_audio_preparation_failed') -> bool:
    """Terminalize a failed pre-dial clip gate for this root turn only.

    This is a real failed preparation, not a user-requested call exemption.
    Both completion observers already deduplicate against FAILED_DIR.
    """
    if not session_id or not turn_id or not is_session_enabled(session_id, session_id):
        raise ValueError("无法绑定已启用会话的本轮准备失败")
    lock_path = QUEUE_DIR.parent / 'completion-queue.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open('a+b') as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _completion_already_recorded(turn_id):
            return False
        directive = _load_json(_staged_report_path(session_id, turn_id=turn_id))
        if directive.get('skip_call') is True:
            return False  # Preserve a prior manual call / explicit exemption.
        FAILED_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = FAILED_DIR / f'{_safe_id(turn_id)}.json'
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {'session_id':session_id, 'thread_id':session_id, 'turn_id':turn_id,
            'session_subscription':True, 'outcome':'failed: native_audio_preparation',
            'phone_startup_failure':{'stage':'report_preparation',
                'reason':reason, 'dial_attempted':False},
            'created_at':timestamp, 'finished_at':timestamp}
        evidence_path = FAILED_DIR.parent/'preparations'/f'{_safe_id(turn_id)}.json'
        evidence = _load_json(evidence_path)
        if (evidence.get('source_thread_id') == session_id and evidence.get('turn_id') == turn_id
                and evidence.get('passed') is False):
            record['preparation_evidence'] = str(evidence_path)
            if evidence.get('failure_code') in {'realtime_quota_exhausted', 'native_notice_library_incomplete'}:
                reason = evidence['failure_code']
                record['phone_startup_failure']['reason'] = reason
                if reason == 'native_notice_library_incomplete':
                    record['phone_startup_failure']['stage'] = 'notice_library'
        handle, temporary_name = tempfile.mkstemp(prefix=f'.{destination.stem}-',
                                                 suffix='.tmp', dir=FAILED_DIR)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        try:
            record_state(FAILED_DIR.parent, record, 'failed',
                reason=reason if reason in {'realtime_quota_exhausted', 'native_notice_library_incomplete'} else 'report_preparation',
                notify=_load_json(CONFIG_PATH).get('phone_failure_notifications', False))
        except OSError:
            pass  # The primary failure journal still prevents redial.
    return True


def queue_completion_event(
    event: dict[str, Any],
    *,
    daemon_status: tuple[bool, str] | None = None,
) -> bool:
    """Queue one completion from either the Stop hook or rollout fallback."""
    config = _load_json(CONFIG_PATH)
    if not config.get("enabled"):
        return False
    session_id = str(event.get("session_id") or "")
    thread_id = str(event.get("thread_id") or session_id)
    turn_id = str(event.get("turn_id") or "")
    raw_report = str(event.get("last_assistant_message") or "").strip()
    if not session_id or not turn_id or not _useful_report(raw_report):
        return False
    if not is_session_enabled(session_id, thread_id):
        return False

    if config.get('phone_command_transport') == 'synchronous_stop':
        # A cached legacy async hook/watcher cannot own a real waiting
        # continuation. It must not queue a second or undeliverable call.
        return False

    # The Stop hook and rollout watcher can observe the same completion at
    # almost the same time. Serialize the persistent dedupe check and staged
    # report consumption so only one of them can create a job or consume copy.
    lock_path = QUEUE_DIR.parent / "completion-queue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _completion_already_recorded(turn_id):
            _publish_skipped_completion(session_id, thread_id, turn_id)
            return False

        report, legacy_spoken_report = _extract_phone_copy(raw_report)
        directive = _consume_staged_directive(session_id, thread_id, turn_id=turn_id)
        if directive.get("skip_call") is True:
            DONE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = DONE_DIR / f"{_safe_id(turn_id)}.json"
            skipped = {
                "session_id": session_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "cwd": str(event.get("cwd") or os.getcwd()),
                "outcome": ("skipped_after_manual_call_guard" if directive.get("manual_call")
                            else "skipped_by_user_for_this_completion"),
                "session_subscription": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.stem}-", suffix=".tmp", dir=DONE_DIR
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(skipped, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.chmod(temporary_name, 0o600)
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
            _publish_skipped_completion(session_id, thread_id, turn_id)
            return False
        spoken_report = str(directive.get("spoken_report") or "")
        if not spoken_report:
            spoken_report = legacy_spoken_report
        if not report:
            report = "任务已完成。"

        daemon_ready, daemon_detail = daemon_status or _ensure_background_daemon()
        destination_dir = QUEUE_DIR if daemon_ready else FAILED_DIR
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{_safe_id(turn_id)}.json"
        job = {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "cwd": str(event.get("cwd") or os.getcwd()),
            "report": report[:4000],
            # This is generated specifically for speech by the completing
            # Codex turn.  Never derive it by slicing the visible final reply.
            "spoken_report": spoken_report,
            "spoken_report_needs_generation": spoken_report == DEFAULT_SPOKEN_REPORT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_subscription": True,
            # A subscribed source task may finish while its previous callback
            # call is still open. Keep that completion queued; the single
            # daemon worker will place it only after the current call ends.
            "queued_during_call": _active_call_running(),
            "command_transport": "codex_app_send_message",
            "daemon_ready_at_queue_time": daemon_ready,
        }
        if not daemon_ready:
            job["outcome"] = "failed: background_daemon_unavailable"
            job["daemon_detail"] = daemon_detail[-1000:]
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}-", suffix=".tmp", dir=destination_dir
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(job, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        try:
            record_state(destination_dir.parent, job, 'queued' if daemon_ready else 'failed',
                reason='' if daemon_ready else 'background_daemon_unavailable',
                notify=config.get('phone_failure_notifications', False))
        except OSError:
            pass
    return True


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        print("{}")
        return 0

    queue_completion_event(event)

    print("{}")
    return 0


def cli() -> int:
    if len(sys.argv) == 1:
        return main()
    parser = argparse.ArgumentParser(description="Codex 电话汇报 Hook")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser('synchronous-stop', help='候选同步等待入口；必须单独完成安装与启用')
    commands.add_parser('inspect-runtime', help='只读查看本次 Hook 使用的运行目录，不启动服务或拨号')
    stage = commands.add_parser("stage-report")
    stage.add_argument("--thread-id", required=True)
    stage.add_argument("--text", required=True)
    skip = commands.add_parser("skip-call")
    skip.add_argument("--thread-id", required=True)
    failed = commands.add_parser("preparation-failed")
    failed.add_argument("--thread-id", required=True)
    failed.add_argument('--reason', choices=['native_audio_preparation_failed','realtime_quota_exhausted'],
                        default='native_audio_preparation_failed')
    args = parser.parse_args()
    if args.command == 'synchronous-stop':
        # Selected explicitly by a synchronous host hook, never as an async
        # hook's automatic fallback. Use the candidate runtime's Python 3.11+.
        if sys.version_info < (3, 11):
            raise RuntimeError('Synchronous phone hooks require Python 3.11+')
        from phone_stop_service import run_hook
        try:
            event = json.load(sys.stdin)
        except (ValueError, OSError):
            event = None
        print(json.dumps(run_hook(event), ensure_ascii=False))
        return 0
    if args.command == 'inspect-runtime':
        print(json.dumps({'runtime': str(PROJECT_DIR), 'active_runtime': str(active_runtime_dir()),
                          'dial_attempted': False}))
        return 0
    if args.command == "stage-report":
        spoken = stage_phone_report(args.thread_id, args.text, turn_id=current_root_turn_id(args.thread_id))
        print(json.dumps({"staged": True, "characters": len(spoken)}, ensure_ascii=False))
        return 0
    if args.command == "skip-call":
        stage_skip_call(args.thread_id, turn_id=current_root_turn_id(args.thread_id))
        print(json.dumps({"staged": True, "skip_call": True}, ensure_ascii=False))
        return 0
    if args.command == "preparation-failed":
        recorded = record_preparation_failure(args.thread_id,
            turn_id=current_root_turn_id(args.thread_id), reason=args.reason)
        print(json.dumps({'recorded':recorded, 'dial_attempted':False}))
        return 0
    raise SystemExit(2)


if __name__ == "__main__":
    forward_to_active_runtime()
    raise SystemExit(cli())
