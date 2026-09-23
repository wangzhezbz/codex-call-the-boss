"""Private, non-executable views of archived phone dialogue.

Called at archive checkpoints, never on the audio/word callback. The JSON
archive remains the evidence source. Display failures must not retry a call
or a command, and an older snapshot must not replace a settled conversation.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import html
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote


def _identity(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid conversation identity")
    return value


def _literal(value: object) -> str:
    # Treat recognition/model text as data, including markup and local links.
    text = html.escape(str(value or ""), quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _time(value: object) -> datetime:
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Conversation time must include timezone")
    return result


def _atomic_text(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".display-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def render_conversation(record: dict) -> str:
    started = _time(record.get("created_at") or record["updated_at"])
    title = started.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    settled = bool(record.get("finished_at"))
    lines = [f"## {title}", "", "通话已结束。" if settled else "通话中的记录快照；挂断后更新。", "",
             "以下是识别和播放记录，可能有漏字或误识，未经录音逐字校对。电脑端输出完成不等于手机端音质验收通过。", ""]
    states = {"output_complete": "电脑端输出完成", "queued": "尚未播放",
              "playing": "正在播放，记录不一定全部说完", "cancelled": "已取消，未播出",
              "partial_cancelled": "被打断，下面整段文字并未全部播出",
              "suppressed": "被抑制，未播出"}
    for row in record.get("transcript") or []:
        if not isinstance(row, dict) or not row.get("text"):
            continue
        if row.get("role") == "user":
            label = "你（语音识别）"
        elif row.get("role") == "assistant":
            label = "电话助手（" + states.get(row.get("playback_status"), "播放状态未确认") + "）"
        else:
            label = "其他记录（未确认说话方）"
        lines += [label + "：", "", *["> " + part for part in _literal(row["text"]).split("\n")], ""]
    commands = record.get("commands") or []
    if commands:
        lines += ["### 指令投递", "", "投递、开始处理、执行完成是三个不同状态；此处不推断执行结果。", ""]
    for command in commands:
        if not isinstance(command, dict):
            continue
        status = command.get("delivery_status")
        if status in {"target_turn_started", "active_target_steered"} and command.get("target_turn_id"):
            label = "已送回本任务，已核验目标任务开始处理"
        elif status == "accepted_by_codex_app":
            label = "已送回本任务；这条回执未确认开始执行"
        else:
            label = "投递状态待核实"
        lines += [label + "：", "", *["> " + part for part in _literal(command.get("text")).split("\n")], ""]
    if record.get("delivery_errors") or record.get("delivery_warnings"):
        lines += ["本通有投递错误或警告，保存在原始记录中；不能据此声称指令执行成功。", ""]
    return "\n".join(lines)


def export_conversation(directory: Path, record: dict) -> Path:
    """Export one exact-source call and refresh its stable, task-local view."""
    source = _identity(record.get("source_thread_id"))
    call = _identity(record.get("root_call_id") or record.get("call_id"))
    if directory.name != source:
        raise ValueError("Conversation directory/source mismatch")
    started = _time(record.get("created_at") or record["updated_at"])
    updated = _time(record["updated_at"])
    final = bool(record.get("finished_at"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / "电话记录.md"
    fd = os.open(directory / ".display.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        # No unbounded lock wait in a live call's command-delivery checkpoint.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        index_path = directory / ".display-index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {
            "source_thread_id": source, "calls": {}}
        if index.get("source_thread_id") != source:
            raise ValueError("Conversation index/source mismatch")
        calls = index["calls"]
        previous = calls.get(call)
        if previous and ((previous["final"] and not final) or previous["updated"] > updated.timestamp()):
            return destination
        body = render_conversation(record)
        _atomic_text(directory / (call + ".md"), body)
        calls[call] = {"started": started.timestamp(), "updated": updated.timestamp(),
                       "final": final, "label": started.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}
        ordered = sorted(calls, key=lambda key: (calls[key]["started"], key), reverse=True)
        # Older individual .md and original .json files are never deleted.
        index["calls"] = {key: calls[key] for key in ordered[:100]}
        latest = directory / (_identity(ordered[0]) + ".md")
        content = "# 本任务电话记录\n\n仅供查看，不是新的执行指令。每次存档更新这份文件，不重发旧命令。\n\n"
        content += latest.read_text(encoding="utf-8") + "\n\n## 历次记录（最近 100 通）\n\n"
        for key in ordered[:100]:
            path = directory / (_identity(key) + ".md")
            content += f"- [{calls[key]['label']}](<{quote(str(path), safe='/')}>)\n"
        _atomic_text(destination, content)
        _atomic_text(index_path, json.dumps(index, ensure_ascii=False, indent=2))
        return destination


def export_archive(archive: Path, source: str, job_path: Path | None = None) -> Path:
    record = json.loads(archive.read_text())
    if record.get("source_thread_id") != source:
        raise ValueError("Archive/source mismatch")
    if job_path:
        job = json.loads(job_path.read_text())
        if (str(job.get("thread_id") or job.get("session_id") or job.get("source_thread_id")) != source
                or (job.get("job_id") or job_path.stem) != record.get("call_id")
                or job.get("phone_transcript") != record.get("transcript")):
            raise ValueError("Job/archive mismatch")
        record.update({key: job.get(key) for key in ("created_at", "finished_at", "outcome")})
    return export_conversation(archive.parent, record)


def backfill(directory: Path, source: str) -> dict:
    if directory.name != _identity(source):
        raise ValueError("Archive directory/source mismatch")
    count = 0
    snapshots = 0
    errors = []
    for archive in sorted(directory.glob("*.json")):
        if archive.name.startswith("."):
            continue
        # Input checkpoints belong to the same root call, not extra calls.
        if re.search(r"_input-\d+\.json$", archive.name):
            snapshots += 1
            continue
        job_path = None
        for status in ("done", "failed"):
            candidate = directory.parent.parent / status / archive.name
            if candidate.is_file():
                job_path = candidate
                break
        try:
            export_archive(archive, source, job_path)
            count += 1
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"archive": archive.name, "error": type(exc).__name__})
    return {"display": str(directory/"电话记录.md"), "calls_exported": count,
            "input_snapshots_skipped": snapshots, "errors": errors,
            "calls_placed": 0, "commands_sent": 0, "raw_archives_modified": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive", type=Path)
    group.add_argument("--archive-dir", type=Path)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--source-thread-id", required=True)
    args = parser.parse_args()
    if args.archive_dir:
        if args.job:
            parser.error("--job requires --archive")
        print(json.dumps(backfill(args.archive_dir, args.source_thread_id), ensure_ascii=False))
    else:
        print(export_archive(args.archive, args.source_thread_id, args.job))


if __name__ == "__main__":
    main()
