from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STATE_DIR = Path(
    os.environ.get("CODEX_PHONE_STATE_DIR") or Path.home() / ".codex-phone"
).expanduser()
REGISTRY_PATH = STATE_DIR / "sessions.json"
LOCK_PATH = STATE_DIR / "sessions.lock"
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def normalize_thread_id(value: str) -> str:
    thread_id = str(value or "").strip()
    if not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ValueError("无效的 Codex 会话 ID")
    return thread_id


def current_thread_id(explicit: str | None = None) -> str:
    value = (
        str(explicit or "").strip()
        or str(os.environ.get("CODEX_THREAD_ID") or "").strip()
        or str(os.environ.get("CODEX_SESSION_ID") or "").strip()
    )
    if not value:
        raise ValueError("找不到当前 Codex 会话 ID")
    return normalize_thread_id(value)


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "sessions": {}}


def load_registry(path: Path | None = None) -> dict[str, Any]:
    target = path or REGISTRY_PATH
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    if not isinstance(value, dict):
        return _empty_registry()
    sessions = value.get("sessions")
    if not isinstance(sessions, dict):
        value["sessions"] = {}
    value["version"] = 1
    return value


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextmanager
def _locked_registry(path: Path | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    target = path or REGISTRY_PATH
    lock_path = target.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield target, load_registry(target)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def enable_session(
    thread_id: str,
    *,
    cwd: str = "",
    path: Path | None = None,
) -> dict[str, Any]:
    thread_id = normalize_thread_id(thread_id)
    now = datetime.now(timezone.utc).isoformat()
    with _locked_registry(path) as (target, registry):
        sessions = registry.setdefault("sessions", {})
        previous = sessions.get(thread_id)
        activated_at = (
            str(previous.get("activated_at") or now)
            if isinstance(previous, dict)
            else now
        )
        record = {
            "enabled": True,
            "thread_id": thread_id,
            "cwd": str(cwd or ""),
            "activated_at": activated_at,
            "updated_at": now,
        }
        sessions[thread_id] = record
        _atomic_write(target, registry)
    return record


def disable_session(thread_id: str, *, path: Path | None = None) -> dict[str, Any]:
    thread_id = normalize_thread_id(thread_id)
    now = datetime.now(timezone.utc).isoformat()
    with _locked_registry(path) as (target, registry):
        sessions = registry.setdefault("sessions", {})
        previous = sessions.get(thread_id)
        record = dict(previous) if isinstance(previous, dict) else {}
        record.update(
            {
                "enabled": False,
                "thread_id": thread_id,
                "updated_at": now,
                "disabled_at": now,
            }
        )
        sessions[thread_id] = record
        _atomic_write(target, registry)
    return record


def is_session_enabled(*thread_ids: str, path: Path | None = None) -> bool:
    sessions = load_registry(path).get("sessions") or {}
    for raw in thread_ids:
        thread_id = str(raw or "").strip()
        record = sessions.get(thread_id)
        if isinstance(record, dict) and record.get("enabled") is True:
            return True
    return False


def active_sessions(path: Path | None = None) -> list[dict[str, Any]]:
    sessions = load_registry(path).get("sessions") or {}
    return [
        dict(record)
        for record in sessions.values()
        if isinstance(record, dict) and record.get("enabled") is True
    ]


def output_probe_seconds(thread_id: str, *, path: Path | None = None) -> int:
    record = (load_registry(path).get('sessions') or {}).get(thread_id)
    if not isinstance(record, dict) or record.get('enabled') is not True:
        return 0
    seconds = record.get('output_probe_seconds', 0)
    return seconds if type(seconds) is int and 1 <= seconds <= 120 else 0


def set_output_probe(thread_id: str, seconds: int, *, path: Path | None = None):
    thread_id = normalize_thread_id(thread_id)
    if type(seconds) is not int or not 0 <= seconds <= 120:
        raise ValueError('Output probe must be disabled or bounded to 1-120 seconds')
    with _locked_registry(path) as (target, registry):
        record = registry['sessions'].get(thread_id)
        if not isinstance(record, dict) or record.get('enabled') is not True:
            raise ValueError('Output probe requires this exact enabled phone session')
        record['output_probe_seconds'] = seconds
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        _atomic_write(target, registry)
    return {'enabled': seconds > 0, 'seconds': seconds, 'session_scoped': True}
