"""Exact typed Stop-input evidence; no writes, phone calls, or task control.

The observed host emits item_completed/HookPrompt/fragments, not UserMessage.
Ordinary user messages, response_item copies, quoted history and tool outputs
are deliberately ineligible. This reader is version-specific local evidence,
not a stable public transcript API or proof of a caller's completed task.
"""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re


def _new_processing(event, stamp, input_timestamp, earlier_ids):
    item = event.get('item') or {}
    if (not isinstance(item, dict)
            or item.get('type') not in {'AgentMessage', 'CommandExecution', 'FileChange'}
            or not isinstance(item.get('id'), str) or not item['id']
            or item['id'] in earlier_ids or stamp.tzinfo is None
            or input_timestamp is None or stamp.timestamp() < input_timestamp):
        return False
    if event.get('type') == 'item_started':
        return True
    if event.get('type') != 'item_completed':
        return False
    # Current hosts persist only completion events, with lifecycle times on
    # the event envelope. A late completion alone cannot certify new work.
    start, end = event.get('started_at_ms'), event.get('completed_at_ms')
    return (type(start) in (int, float) and type(end) in (int, float)
            and math.isfinite(start) and math.isfinite(end)
            and input_timestamp * 1000 <= start <= end <= stamp.timestamp() * 1000 + 1)


def observe_receipt(path, scope, emission, *, hook_config, max_bytes=8 * 1024 * 1024,
                    expected_final=None):
    unknown = {'received_by_codex': False, 'processing_verified': False}
    try:
        scope.validate()
        if (not isinstance(emission, dict) or emission.get('scope') != asdict(scope)
                or not re.fullmatch(r'codex-phone-wait-command:[0-9a-f]{64}', emission.get('marker', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', emission.get('reason_sha256', ''))
                or type(emission.get('reserved_at')) not in (int, float)
                or not math.isfinite(emission['reserved_at'])
                or not Path(hook_config).is_absolute()
                or type(max_bytes) is not int or not 1024 <= max_bytes <= 16 * 1024 * 1024):
            return unknown
        with Path(path).open('rb') as stream:
            header = json.loads(stream.readline(65536))
            if header.get('type') != 'session_meta' or header.get('payload', {}).get('id') != scope.source_thread_id:
                return unknown
            end = stream.seek(0, 2)
            offset = max(0, end - max_bytes)
            stream.seek(offset)
            data = stream.read(end - offset)
            if len(data) != end - offset or not data.endswith(b'\n'):
                return unknown
            lines = data.splitlines()[1:] if offset else data.splitlines()
        state, matches, input_while_running = '', [], False
        processing_after_input, superseded, input_timestamp = False, False, None
        earlier_ids = set()
        latest_final = None
        for raw in lines:
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get('type') != 'event_msg':
                continue
            event = record.get('payload') or {}
            if (not isinstance(event, dict) or event.get('turn_id') != scope.root_turn_id
                    or event.get('thread_id') not in (None, scope.source_thread_id)):
                continue
            kind = event.get('type')
            if kind in {'task_started', 'task_complete', 'turn_aborted'}:
                state = {'task_started':'inProgress', 'task_complete':'completed', 'turn_aborted':'aborted'}[kind]
                continue
            item = event.get('item') or {}
            if not matches and isinstance(item, dict) and isinstance(item.get('id'), str):
                earlier_ids.add(item['id'])
            if matches and isinstance(item, dict) and event.get('thread_id') == scope.source_thread_id:
                if kind == 'item_completed' and item.get('type') in {'UserMessage', 'userMessage', 'HookPrompt'}:
                    superseded = True
                # An earlier task_started only proves the original root was
                # running, possibly still waiting in its Stop hook. Require
                # new model activity after this exact input, not an old tool
                # completion or another user's steering message.
                if (not superseded and kind in {'item_started', 'item_completed'}
                        and item.get('type') in {'AgentMessage', 'CommandExecution', 'FileChange'}):
                    stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
                    if _new_processing(event, stamp, input_timestamp, earlier_ids):
                        processing_after_input = True
                if (not superseded and processing_after_input and kind == 'item_completed'
                        and item.get('type') in {'AgentMessage', 'agentMessage'}
                        and item.get('phase') in {'final', 'final_answer'}):
                    stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
                    if stamp.tzinfo is not None and stamp.timestamp() >= input_timestamp:
                        content = item.get('content')
                        if (isinstance(content, list) and content
                                and all(isinstance(part, dict) and isinstance(part.get('text'), str)
                                        for part in content)):
                            latest_final = ''.join(part['text'] for part in content).strip()
            if (kind != 'item_completed' or event.get('thread_id') != scope.source_thread_id
                    or not isinstance(item, dict) or item.get('type') != 'HookPrompt'
                    or not isinstance(item.get('id'), str) or not item['id']):
                continue
            fragments = item.get('fragments')
            if not isinstance(fragments, list) or not 1 <= len(fragments) <= 16:
                continue
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                text, run = fragment.get('text'), fragment.get('hookRunId')
                if (not isinstance(text, str) or len(text.encode()) > 16384
                        or not isinstance(run, str)
                        or not re.fullmatch(r'stop:\d+:' + re.escape(str(hook_config)), run)
                        or emission['marker'] not in text.splitlines()
                        or hashlib.sha256(text.encode()).hexdigest() != emission['reason_sha256']):
                    continue
                stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
                if stamp.tzinfo is None or stamp.timestamp() < emission['reserved_at']:
                    continue
                matches.append(item['id'])
                input_timestamp = stamp.timestamp()
                input_while_running = state == 'inProgress'
        if len(matches) != 1 or state == 'aborted':
            return unknown
        return {'received_by_codex': True,
                'processing_verified': (input_while_running and processing_after_input
                                        and not superseded and state in {'inProgress', 'completed'}),
                'source_thread_id': scope.source_thread_id, 'root_turn_id': scope.root_turn_id,
                'hook_item_id': matches[0], 'turn_status': state,
                'completion_report_matches': (isinstance(expected_final, str) and bool(expected_final.strip())
                    and latest_final == expected_final and not superseded and state != 'aborted'),
                'task_completed_successfully': False}
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return unknown
