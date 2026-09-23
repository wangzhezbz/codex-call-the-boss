"""Durable one-call-per-logical-completion claims within one Stop root.

The initial root and each verified phone continuation have separate immutable
cycle IDs. A failed/uncertain cycle is never reopened. Neither a newly written
mailbox offer nor an old root task_started can authorize another callback.
"""
from dataclasses import asdict

from phone_stop_mailbox import Scope, MailboxError, _digest, _new, _optional
from phone_stop_receipt import observe_receipt


class StopCycles:
    def __init__(self, mailbox, *, rollout_path, hook_config, is_enabled):
        self.mailbox = mailbox
        self.rollout_path = rollout_path
        self.hook_config = hook_config
        self.is_enabled = is_enabled

    def claim(self, event):
        """Called only with the current synchronous hook's stdin event.

        Returns one newly reserved Scope or None for a consumed completion.
        The caller must not queue anything on None or on an exception. Claim
        precedes queueing: a crash between them is terminal, never a redial.
        """
        if (not isinstance(event, dict) or event.get('hook_event_name') != 'Stop'
                or event.get('agent_id') is not None
                or type(event.get('stop_hook_active')) is not bool
                or event.get('thread_id', event.get('session_id')) != event.get('session_id')
                or not isinstance(event.get('last_assistant_message'), str)
                or not event['last_assistant_message'].strip()):
            raise MailboxError('invalid_stop_event')
        source, root, cwd = event.get('session_id'), event.get('turn_id'), event.get('cwd')
        initial = _digest(['initial', source, root])
        scope = Scope(source, root, initial, _digest(['call', initial]), cwd)
        scope.validate()
        if not self.is_enabled(source):
            raise MailboxError('source_not_enabled')
        with self.mailbox.transaction():
            for _ in range(64):
                claim_path = self.mailbox.store / ('cycle-' + scope.cycle_id + '.json')
                existing = _optional(claim_path)
                if existing is None:
                    if scope.cycle_id == initial and event['stop_hook_active']:
                        raise MailboxError('unbound_continuation')
                    _new(claim_path, {'scope': asdict(scope), 'status': 'claimed_before_queue'})
                    return scope
                if existing.get('scope') != asdict(scope):
                    raise MailboxError('cycle_binding_mismatch')
                if not event['stop_hook_active']:
                    return None  # Duplicate initial Stop / legacy observer.
                path = self.mailbox.path(scope)
                emission = _optional(path / 'emitted.json')
                if emission is None:
                    return None  # Failed, skipped, queued or uncertain attempt.
                if emission.get('scope') != asdict(scope):
                    raise MailboxError('emission_binding_mismatch')
                claim = _optional(self.mailbox.store / ('command-' + _digest([
                    source, emission.get('command_id')]) + '.json'))
                if claim != emission:
                    raise MailboxError('uncertain_command_reservation')
                cycle = _digest(['after-command', source, root, emission['command_id']])
                child = Scope(source, root, cycle, _digest(['call', cycle]), cwd)
                if _optional(self.mailbox.store / ('cycle-' + cycle + '.json')) is not None:
                    # An already claimed successor was checked on creation.
                    # Its new HookPrompt legitimately supersedes the ancestor.
                    scope = child
                    continue
                rollout = self.rollout_path(source)
                observed = (observe_receipt(rollout, scope, emission, hook_config=self.hook_config,
                    expected_final=event['last_assistant_message']) if rollout else {})
                if not (observed.get('received_by_codex') and observed.get('processing_verified')
                        and observed.get('completion_report_matches')):
                    return None
                scope = child
            raise MailboxError('cycle_chain_limit')
