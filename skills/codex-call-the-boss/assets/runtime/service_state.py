"""Private service states and one-shot desktop failure notices.

Never writes to a Codex task, invokes a model, or dials. OS submission is not
proof a notification was shown; the durable status remains inspectable.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PHASES = {"queued", "preparing", "dialing", "connected", "delivered", "processing", "completed", "failed", "needs_review", "skipped"}
REASONS = {
    "report_preparation": "电话语音准备失败，本轮未拨号；具体阶段请查看准备记录。",
    "native_notice_library_incomplete": "固定回执或提示音尚未准备齐全，本轮未拨号。",
    "realtime_preflight": "语音连接准备失败，本轮未拨号。",
    "intent_warmup": "指令通道准备失败，本轮未拨号。",
    "local_address_unavailable": "电脑无法建立新的网络连接，本轮未拨号；请检查本机代理和连接状态。",
    "workspace_routing_timeout": "Codex 工作区路由发现超时，本轮未拨号；不是手机设置或退出登录的证明。",
    "account_read_timeout": "Codex 账号状态未在启动总时限内返回，本轮未拨号。",
    "background_daemon_unavailable": "电话后台不可用，本轮未拨号。",
    "call_failed": "本轮电话失败，请查看服务状态了解所处阶段。",
    "local_call_disconnect_request": "本机通话进程请求断开，本轮未接通；具体触发原因未确认。",
    "iphone_relay_setup": "Mac 与 iPhone 的电话接力未建立，本轮未接通。",
    "iphone_relay_network_unavailable": "系统报告电话接力远端网络不可用，本轮未接通。",
    "worker_error": "电话后台已停止，请检查服务状态后恢复。",
    "queue_age": "有积压汇报等待确认，已保留，未自动补拨。",
    "line_unconfirmed": "上一通电话的结束状态尚未确认，待拨汇报已保留。",
    "realtime_quota_exhausted": "语音服务额度不可用，本轮未拨号。",
    "runtime_compatibility": "运行依赖版本不匹配，本轮未拨号。",
    "invalid_job": "待拨记录不完整，本轮未拨号。",
    "automatic_callbacks_paused": "该任务的电话汇报已暂停，本轮未拨号。",
    "delivered_in_active_call": "该项汇报已在现有通话中处理，没有另拨电话。",
    "skipped_by_user_for_this_completion": "按您的要求，仅跳过本轮自动电话汇报；后续任务仍会通知。",
    "manual_call_guard": "本轮已安排手动呼叫，不再额外自动拨号；接通结果另行记录。",
}


def _read(path):
    try:
        if path.stat().st_size > 256 * 1024:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def desktop_notice(message):
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Codex 电话服务"\nend run'
    subprocess.run(["/usr/bin/osascript", "-e", script, message], check=True,
                   timeout=3, capture_output=True)


def record_state(state: Path, job: dict, phase: str, *, reason="", notify=False, notifier=None):
    if phase not in PHASES:
        raise ValueError("unknown service phase")
    identity = str(job.get("turn_id") or job.get("job_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        return None
    folder = Path(state) / "service-status"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (identity + ".json")
    notice = None
    # Independent of the completion queue lock; hooks can safely call us.
    with (folder / "status.lock").open("a+b") as lock:
        os.chmod(folder / "status.lock", 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = _read(target)
        if previous.get("phase") in {"completed", "failed", "skipped"} and phase != previous.get("phase"):
            return previous  # Late async work must not resurrect a terminal call.
        reason = reason if reason in REASONS else ("call_failed" if phase == "failed" else "")
        source = str(job.get("thread_id") or job.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source): source = ""
        stamp = datetime.now(timezone.utc).isoformat()
        event = {"phase": phase, "reason": reason, "at": stamp}
        history = previous.get("history", [])
        history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        if not history or (history[-1].get("phase"), history[-1].get("reason")) != (phase, reason):
            history = [*history[-31:], event]
        record = {"job_id": identity, "source_thread_id": source, "phase": phase,
                  "completion_scope": "phone_call_only",
                  "reason": reason, "message": REASONS.get(reason, ""), "updated_at": stamp,
                  "created_at": previous.get("created_at", stamp), "history": history,
                  "notification": previous.get("notification", {}) if isinstance(previous.get("notification"), dict) else {}}
        if notify and phase in {"failed", "needs_review"} and not record["notification"]:
            # Persist before external submission. A crash cannot cause a repeat.
            record["notification"] = {"state": "attempted", "visible_verified": False}
            notice = record["message"] or REASONS["call_failed"]
        _write(target, record)
    if notice is not None:
        notification = {"state":"submitted_to_os", "visible_verified":False}
        try:
            (notifier or desktop_notice)(notice)
        except Exception as exc:
            notification.update(state="submission_failed", error_type=type(exc).__name__)
        # Do not hold the shared state lock during OS I/O or overwrite a new
        # call phase that arrived while notification submission was pending.
        with (folder / "status.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            record = _read(target)
            record["notification"] = notification
            _write(target, record)
    return record


def recent_status(state: Path, source: str = "", limit: int = 10):
    paths = sorted((Path(state) / "service-status").glob("*.json"),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    result = []
    for path in paths:
        row = _read(path)
        if row and (not source or not row.get("source_thread_id") or row.get("source_thread_id") == source):
            result.append(row)
            if len(result) >= limit: break
    return result


def queue_needs_review(job: dict, *, max_age_seconds=900, now=None) -> bool:
    if job.get("phone_queue_review_required") is True:
        return True
    value = job.get("phone_queue_confirmed_at") or job.get("created_at")
    if not value:
        return True
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None: return True
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        return age > max_age_seconds or age < -60
    except (TypeError, ValueError):
        return True
