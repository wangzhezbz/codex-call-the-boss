"""Explicit synchronous hook/worker entry wiring; installation is separate.

Nothing here changes hooks.json, subscribes a source, starts a daemon, or
falls back to the rejected desktop IPC. The existing runtime stays unchanged
until this candidate is packaged and its synchronous entry is selected.
"""
import asyncio
from datetime import datetime, timezone
import sys

from phone_stop_backend import StopBackend
from phone_stop_completion import handle_completion
from phone_stop_cycles import StopCycles
from phone_stop_mailbox import StopMailbox, _new
from phone_stop_queue import StopQueue


def create_backend():
    import phone_agent as agent
    directory = agent.STATE_DIR / 'stop-transport'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    mailbox = StopMailbox(directory)  # Refuses an unsafe existing directory.
    config = agent.load_config()
    reader = agent.PhoneDaemon(config)
    return StopBackend(mailbox, rollout_path=reader._find_rollout_path,
                       hook_config=agent.GLOBAL_HOOK_PATH,
                       confirmation_seconds=config.get('phone_stop_confirmation_seconds', 30))


async def process_hook(event):
    import hook_stop
    import phone_agent as agent
    from session_registry import is_session_enabled
    from service_state import record_state

    if not isinstance(event, dict):
        return {}
    source = event.get('session_id')
    config = agent.load_config()
    if (config.get('enabled') is not True or config.get('provider') != 'iphone'
            or not isinstance(source, str) or not is_session_enabled(source, source)):
        return {}
    # An already recorded manual call or failed preparation remains terminal.
    root = event.get('turn_id')
    if isinstance(root, str) and hook_stop._completion_already_recorded(root):
        return {}
    backend = create_backend()
    cycles = StopCycles(backend.mailbox, rollout_path=backend.rollout_path,
                        hook_config=backend.hook_config, is_enabled=is_session_enabled)

    def ready():
        pid = agent.background_daemon_pid()
        if pid is None:
            return False, ''
        ok, _ = agent._daemon_status_ready(pid)
        status = agent._load_json(agent.DAEMON_STATUS_PATH)
        return ok, status.get('command_transport', '')

    queue = StopQueue(agent.STATE_DIR, backend, daemon_ready=ready, is_enabled=is_session_enabled)

    def failed(job, error_type):
        # This separate evidence never reopens a job or clears a physical
        # line. If enqueue may have succeeded, the worker retains ownership.
        evidence = {'job_id': job['job_id'], 'error_type': error_type,
                    'source_thread_id': source, 'root_turn_id': job['source_root_turn_id']}
        try:
            _new(backend.mailbox.store / ('hook-failure-' + job['job_id'] + '.json'), evidence)
            if any((agent.STATE_DIR / folder / (job['job_id'] + '.json')).exists()
                   for folder in ('queue', 'calling', 'done', 'failed')):
                return
            record = {**job, 'outcome': 'failed: synchronous_completion_setup',
                      'finished_at': datetime.now(timezone.utc).isoformat(),
                      'phone_startup_failure': {'stage': 'command_transport', 'dial_attempted': False}}
            agent.FAILED_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
            _new(agent.FAILED_DIR / (job['job_id'] + '.json'), record)
            record_state(agent.STATE_DIR, record, 'failed', reason='command_transport', notify=False)
        except (OSError, ValueError):
            pass  # Keep the original exception and immutable cycle claim.

    async def enqueue(job):
        directive = hook_stop._consume_staged_directive(source, source, turn_id=job['source_root_turn_id'])
        if directive.get('skip_call') is True:
            job.update(outcome=('skipped_after_manual_call_guard' if directive.get('manual_call')
                                else 'skipped_by_user_for_this_completion'),
                       finished_at=datetime.now(timezone.utc).isoformat())
            agent.DONE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            _new(agent.DONE_DIR / (job['job_id'] + '.json'), job)
            record_state(agent.STATE_DIR, job, 'skipped', notify=False)
            return
        spoken = str(directive.get('spoken_report') or '')
        job.update(spoken_report=spoken, spoken_report_needs_generation=not bool(spoken))
        await queue.queue(job)
        try:
            record_state(agent.STATE_DIR, job, 'queued')
        except OSError:
            pass  # Never repeat a successful enqueue to repair status.

    return await handle_completion(event, cycles=cycles, queue_job=enqueue,
                                   call_settled=queue.settled, on_failure=failed)


def run_hook(event):
    try:
        return asyncio.run(process_hook(event))
    except Exception as exc:
        # Keep stdout exclusively for the host's hook protocol. A failed
        # attempt remains claimed; no old IPC fallback and no redial.
        print('Synchronous phone hook stopped: ' + type(exc).__name__, file=sys.stderr)
        return {}
