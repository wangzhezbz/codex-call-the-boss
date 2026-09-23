"""Read-only version checks and allowlisted, content-free support exports."""
from __future__ import annotations

import hashlib
from datetime import datetime
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import subprocess

from service_state import PHASES, REASONS, recent_status

REVIEWED_CODEX_VERSION = '0.153.4'


def dependency_compatibility():
    dependencies = {}
    for line in (Path(__file__).parent/'requirements.txt').read_text().splitlines():
        if '==' not in line: continue
        name, expected = line.strip().split('==', 1)
        try: actual = metadata.version(name)
        except metadata.PackageNotFoundError: actual = None
        dependencies[name] = {'expected':expected, 'actual':actual, 'matches':actual == expected}
    return dependencies


def compatibility():
    dependencies = dependency_compatibility()
    try:
        version = subprocess.run(['codex','--version'], capture_output=True, text=True, check=True, timeout=3).stdout.strip()
        actual = version.split()[-1]
        # Export version characters only, never arbitrary command stdout.
        import re
        if not re.fullmatch(r'[0-9A-Za-z.+-]{1,50}', actual): actual = None
    except (OSError, subprocess.SubprocessError, IndexError):
        actual = None
    return {'python':platform.python_version(), 'python_supported':tuple(map(int, platform.python_version_tuple()[:2])) >= (3,11),
            'codex_version':actual, 'reviewed_codex_version':REVIEWED_CODEX_VERSION,
            'codex_version_reviewed':actual == REVIEWED_CODEX_VERSION, 'dependencies':dependencies,
            'audio_quality_verified':False, 'real_call_verified':False}


def export_diagnostics(state: Path, output: Path, source=''):
    calls = []
    for row in recent_status(state, source, limit=20):
        identity = row.get('job_id')
        if not isinstance(identity, str) or not identity: continue
        try:
            stamp = datetime.fromisoformat(row.get('updated_at')).isoformat()
        except (ValueError, TypeError):
            stamp = None
        notification = row.get('notification')
        notice_state = notification.get('state') if isinstance(notification, dict) else None
        if notice_state not in {'attempted', 'submitted_to_os', 'submission_failed'}: notice_state = None
        phase, reason = row.get('phase'), row.get('reason')
        calls.append({'job_key':hashlib.sha256(identity.encode()).hexdigest()[:12],
                      'phase':phase if isinstance(phase, str) and phase in PHASES else None,
                      'reason':reason if isinstance(reason, str) and reason in REASONS else None,
                      'updated_at':stamp, 'notification_state':notice_state})
    result = {'format':1, 'compatibility':compatibility(), 'calls':calls,
              'contains_audio':False, 'contains_transcripts':False, 'contains_numbers_or_credentials':False}
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    return result
