"""Synchronous completion lifecycle for the phone queue.

The host keeps this handler alive, while the independently instance-locked
phone worker uses its existing prepare/dial/line-guard path. Returning a block
releases the source immediately; it must not wait for that command's receipt.
No hook registration, subscription or daemon startup occurs in this module.
"""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone

from phone_stop_mailbox import MailboxError, _number


async def handle_completion(event, *, cycles, queue_job, call_settled,
                            spoken_report='', timeout_seconds=480, on_failure=None):
    """Queue once, then wait for a command, settled call, or the original bound.

    queue_job persists exactly this new job under the usual queue lock.
    call_settled must inspect its durable done/failed record, not a timeout,
    missing queue file, or unconfirmed physical hangup. Neither callback may
    call the source model or retry an uncertain queue/dial operation.
    """
    if not _number(timeout_seconds) or not 0 < timeout_seconds <= 540:
        raise MailboxError('invalid_hook_wait_timeout')
    if not isinstance(spoken_report, str):
        raise MailboxError('invalid_spoken_report')
    scope = cycles.claim(event)
    if scope is None:
        return {}
    owner = cycles.mailbox.open_window(scope, timeout_seconds=timeout_seconds)
    job = {'job_id': scope.call_id, 'thread_id': scope.source_thread_id,
           'session_id': scope.source_thread_id, 'source_root_turn_id': scope.root_turn_id,
           'turn_id': scope.cycle_id, 'stop_wait_scope': asdict(scope), 'cwd': scope.cwd,
           'report': event['last_assistant_message'][:4000], 'spoken_report': spoken_report,
           'spoken_report_needs_generation': not bool(spoken_report),
           'command_transport': 'synchronous_stop', 'session_subscription': True,
           'created_at': datetime.now(timezone.utc).isoformat()}
    wait_task = settled_task = None
    try:
        # Queueing is within the same deadline, never a fresh window after
        # report preparation, another call, or an uncertain enqueue failure.
        async with asyncio.timeout(timeout_seconds):
            await queue_job(job)
            wait_task = asyncio.create_task(owner.wait(event))

            async def observe_call():
                while not await call_settled(job):
                    await asyncio.sleep(.1)
                return {}

            settled_task = asyncio.create_task(observe_call())
            done, _ = await asyncio.wait({wait_task, settled_task}, return_when=asyncio.FIRST_COMPLETED)
            if wait_task in done:
                return wait_task.result()
            settled_task.result()  # Propagate observer failure; never retry.
            owner.close('caller_hangup')
            return {}
    except BaseException as exc:
        if on_failure is not None:
            on_failure(job, type(exc).__name__)
        raise
    finally:
        for task in (wait_task, settled_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (wait_task, settled_task) if task is not None),
                             return_exceptions=True)
        owner.close()
