"""Phone worker's exact-call synchronous Stop transport.

Constructed explicitly by the synchronous-hook service, never inferred from a
job flag or used as an automatic fallback from a rejected desktop connection.
The service must keep a real Stop owner alive before a call can prepare/dial.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from phone_stop_dispatch import StopDispatch
from phone_stop_mailbox import MailboxError, Scope, _number, _optional, _digest


class StopBackend:
    def __init__(self, mailbox, *, rollout_path, hook_config, confirmation_seconds=6):
        if (not callable(rollout_path) or not Path(hook_config).is_absolute()
                or not _number(confirmation_seconds) or not 0 < confirmation_seconds <= 6):
            raise ValueError('invalid_stop_backend')
        self.mailbox = mailbox
        self.rollout_path = rollout_path
        self.hook_config = Path(hook_config)
        self.confirmation_seconds = confirmation_seconds

    def dispatch(self, job):
        try:
            scope = Scope(**job['stop_wait_scope'])
        except (KeyError, TypeError):
            raise MailboxError('missing_stop_scope') from None
        scope.validate()
        result = StopDispatch(self.mailbox, scope)
        result.require_job(job)
        return result

    def require_ready(self, job):
        self.dispatch(job).require_ready(job)

    def _check_health(self, job):
        """One coherent read; no nested lock or boolean error flattening."""
        dispatch = self.dispatch(job)
        with self.mailbox.transaction():
            path, _ = self.mailbox._binding(dispatch.scope)
            emitted = _optional(path / 'emitted.json')
            if emitted is not None:
                # A verified one-shot reservation legitimately releases its
                # owner. It is not permission to submit another command.
                claim = _optional(self.mailbox.store / ('command-' + _digest([
                    dispatch.scope.source_thread_id, emitted.get('command_id')]) + '.json'))
                if claim != emitted or emitted.get('scope') != job['stop_wait_scope']:
                    raise MailboxError('emission_binding_mismatch')
                ids = [row.get('command_id') for row in job.get('stop_offers', [])]
                if emitted.get('command_id') not in ids:
                    raise MailboxError('unknown_emission')
            else:
                self.mailbox._require_waiting(dispatch.scope)

    async def health_error(self, job):
        """Retry only a busy read, for at most 750 ms, without blocking audio.

        This never retries a send/dial, resets the owner deadline, or accepts
        cached readiness. Expiry, owner loss and invalid records fail directly.
        Cancellation from phone hangup remains cancellation, not a new notice.
        """
        loop = asyncio.get_running_loop()
        began = loop.time()
        deadline = began + .75
        trace = job.setdefault('stop_transport_health', {})
        trace['checks'] = trace.get('checks', 0) + 1
        try:
            while True:
                try:
                    self._check_health(job)
                    trace.update(last_result='ready', last_error_code='')
                    return None
                except MailboxError as exc:
                    if str(exc) != 'transaction_busy':
                        raise
                    trace['busy_retries'] = trace.get('busy_retries', 0) + 1
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise MailboxError('transaction_busy_timeout') from exc
                    await asyncio.sleep(min(.01, remaining))
                    if loop.time() >= deadline:
                        raise MailboxError('transaction_busy_timeout') from exc
        except (MailboxError, OSError, ValueError) as exc:
            allowed = {'window_closed', 'window_expired', 'owner_not_waiting',
                       'window_cancelled', 'window_emitted', 'binding_mismatch',
                       'call_binding_mismatch', 'missing_stop_scope', 'invalid_scope',
                       'invalid_cwd', 'invalid_private_record', 'invalid_private_directory',
                       'invalid_private_lock', 'invalid_offer', 'emission_binding_mismatch',
                       'unknown_emission', 'uncertain_command_reservation',
                       'transaction_busy_timeout'}
            code = (str(exc) if isinstance(exc, MailboxError) and str(exc) in allowed
                    else 'mailbox_io_error' if isinstance(exc, OSError)
                    else 'mailbox_validation_failed')
            trace.update(last_result='failed', last_error_code=code)
            if isinstance(exc, OSError):
                trace['last_os_errno'] = exc.errno
            if code == 'transaction_busy_timeout':
                return 'Codex synchronous task state check timed out'
            return 'Codex synchronous task waiting window unavailable: ' + code
        finally:
            trace['last_check_ms'] = round((loop.time() - began) * 1000)

    async def prepare_context(self, job, server, local_context):
        return await self.dispatch(job).prepare_context(job, server, local_context)

    def cancel_committed(self, job, turns, identity, decision, *, classified_text):
        dispatch = self.dispatch(job)
        row = turns.turns.get(identity)
        if (not isinstance(row, dict) or row.get('status') != 'committed'
                or row.get('kind') != 'cancel' or row.get('complete') is not True
                or row.get('text') != classified_text or turns.speaking
                or not isinstance(decision, dict) or set(decision) != {'kind', 'clarification'}
                or decision.get('kind') != 'cancel' or not isinstance(decision.get('clarification'), str)):
            raise MailboxError('cancellation_not_committed')
        status = self.mailbox.cancel(dispatch.scope)
        # The one-shot Stop window cannot steer an already resumed source.
        # Do not claim the cancellation was forwarded or the action undone.
        result = {'status': status, 'delivered_to_model': False,
                  'execution_confirmed': False, 'original_caller_words': classified_text}
        job.setdefault('stop_cancellations', []).append(result)
        return result

    async def relay_committed(self, job, turns, identity, decision, *, classified_text, phone_history=None):
        dispatch = self.dispatch(job)
        offered = dispatch.offer_committed(job, turns, identity, decision,
                                           classified_text=classified_text, phone_history=phone_history)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.confirmation_seconds
        best = {'status': 'pending_stop_delivery', 'delivered_to_model': False,
                'execution_confirmed': False, 'command_id': offered['command_id']}
        while loop.time() < deadline:
            path = self.rollout_path(dispatch.scope.source_thread_id)
            if path is not None:
                try:
                    # File I/O must not block the live audio event loop. It
                    # reads a bounded exact-source tail, not private app IPC.
                    async with asyncio.timeout(max(.001, deadline - loop.time())):
                        observed = await asyncio.to_thread(dispatch.inspect_delivery, job,
                            offered['command_id'], rollout_path=path, hook_config=self.hook_config)
                    best.pop('verification_warning', None)
                    if observed.get('delivered_to_model'):
                        best.update(observed)
                    if observed.get('execution_confirmed'):
                        break
                except MailboxError as exc:
                    if str(exc) == 'transaction_busy':
                        # Retry only observation, never the offer above. The
                        # owner/health reader can briefly hold this same lock.
                        # Keep prior delivery evidence and the original bound.
                        best['verification_busy_retries'] = best.get('verification_busy_retries', 0) + 1
                        best['verification_warning'] = 'transaction_busy_timeout'
                    else:
                        code = str(exc)
                        best['verification_warning'] = code if code in {
                            'call_binding_mismatch', 'unknown_command',
                            'emission_binding_mismatch', 'scope_mismatch',
                            'window_missing', 'invalid_window',
                        } else 'mailbox_validation_failed'
                        break
                except (TimeoutError, OSError, ValueError) as exc:
                    # An offered/reserved command is never retried on a read
                    # failure, and confirmed delivery never becomes unsent.
                    best['verification_warning'] = type(exc).__name__
                    break
            await asyncio.sleep(min(.05, max(0, deadline - loop.time())))
        if not best['execution_confirmed'] and 'verification_warning' not in best:
            best['verification_warning'] = ('processing_not_observed_before_deadline'
                if best['delivered_to_model'] else 'input_not_observed_before_deadline')
        row = {'command_id': offered['command_id'], 'prompt': classified_text,
               'verification': dict(best)}
        job.setdefault('stop_delivery_results', []).append(row)
        if best['delivered_to_model']:
            job.setdefault('relayed_phone_tasks', []).append(row)
        return best
