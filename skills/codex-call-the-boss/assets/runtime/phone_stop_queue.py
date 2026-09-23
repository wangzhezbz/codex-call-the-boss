"""Persistent queue adapter for an already-running synchronous phone worker."""
import asyncio
import fcntl
import json
import os
from pathlib import Path

from phone_stop_mailbox import MailboxError, _lock_file, _new


class StopQueue:
    def __init__(self, state, backend, *, daemon_ready, is_enabled):
        self.state, self.backend = Path(state), backend
        self.daemon_ready, self.is_enabled = daemon_ready, is_enabled

    def _queue(self, job):
        self.backend.require_ready(job)
        source = job['thread_id']
        if not self.is_enabled(source):
            raise MailboxError('source_not_enabled')
        ready, mode = self.daemon_ready()
        if ready is not True or mode != 'synchronous_stop':
            raise MailboxError('synchronous_worker_not_ready')
        fd = _lock_file(self.state / 'completion-queue.lock')
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MailboxError('completion_queue_busy') from None
            # Same queue and same single worker/line guard as all iPhone
            # calls. Never directly invoke a dialer or release an older call.
            name = job['job_id'] + '.json'
            if any((self.state / directory / name).exists()
                   for directory in ('queue', 'calling', 'done', 'failed')):
                raise MailboxError('call_already_recorded')
            directory = self.state / 'queue'
            directory.mkdir(mode=0o700, exist_ok=True)
            self.backend.require_ready(job)
            if not self.is_enabled(source):
                raise MailboxError('source_not_enabled')
            _new(directory / name, job)
        finally:
            os.close(fd)

    async def queue(self, job):
        # Bounded local writes only. Do not put this critical enqueue in an
        # uncancellable background thread that could enqueue after timeout.
        self._queue(job)

    def _settled(self, job):
        self.backend.dispatch(job)  # Exact source/root/call binding.
        for directory in ('done', 'failed'):
            path = self.state / directory / (job['job_id'] + '.json')
            try:
                with path.open('rb') as stream:
                    raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise MailboxError('oversized_call_record')
                record = json.loads(raw)
            except FileNotFoundError:
                continue
            if (not isinstance(record, dict) or record.get('job_id') != job['job_id']
                    or record.get('thread_id') != job['thread_id']
                    or record.get('source_root_turn_id') != job['source_root_turn_id']
                    or record.get('stop_wait_scope') != job['stop_wait_scope']
                    or not record.get('outcome') or not record.get('finished_at')):
                raise MailboxError('call_settlement_mismatch')
            return True
        return False

    async def settled(self, job):
        return await asyncio.to_thread(self._settled, job)
