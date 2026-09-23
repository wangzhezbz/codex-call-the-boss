"""No-dial integration of real CallerTurns commits with a live Stop mailbox.

Not registered as a production transport. In particular, an offer/result from
this adapter must NOT be fed into the existing desktop-accepted speech branch.
Actual source receipt and execution confirmation are still separate gates.
"""
from dataclasses import asdict
import hashlib

from phone_stop_mailbox import MailboxError, _optional, _digest
from phone_stop_receipt import observe_receipt
from phone_source_view import prepare_source_view


class StopDispatch:
    def __init__(self, mailbox, scope):
        self.mailbox, self.scope = mailbox, scope

    def require_job(self, job):
        if (not isinstance(job, dict) or job.get('thread_id') != self.scope.source_thread_id
                or job.get('session_id', self.scope.source_thread_id) != self.scope.source_thread_id
                or job.get('source_root_turn_id') != self.scope.root_turn_id
                or job.get('job_id') != self.scope.call_id
                or job.get('cwd') != self.scope.cwd
                or job.get('stop_wait_scope') != asdict(self.scope)):
            raise MailboxError('call_binding_mismatch')

    def require_ready(self, job):
        self.require_job(job)
        if not self.mailbox.is_waiting(self.scope):
            raise MailboxError('source_not_waiting')

    async def prepare_context(self, job, server, local_context):
        self.require_ready(job)
        view = await prepare_source_view(server, self.scope.source_thread_id, local_context)
        if view.cwd != self.scope.cwd:
            raise MailboxError('source_directory_changed')
        self.require_ready(job)  # The original hook can time out during startup.
        view.bind_job(job)
        job['command_transport'] = 'synchronous_stop_candidate'
        return view.context_id

    def offer_committed(self, job, turns, identity, decision, *, classified_text, phone_history=None):
        """Accept the real gate's committed original utterance, not guessed text.

The caller must already have run the ordinary ASR completion, classifier and
CallerTurns.commit path. It must not use a warmup result or mutate the ledger
to manufacture a commit. This additional check never commits on its behalf.
        """
        self.require_ready(job)
        if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
            raise MailboxError('invalid_input_identity')
        row = turns.turns.get(identity)
        if (not isinstance(row, dict) or row.get('status') != 'committed'
                or row.get('complete') is not True or row.get('kind') != 'action'
                or turns.speaking or not isinstance(row.get('text'), str)
                or row['text'] != classified_text
                or not isinstance(decision, dict) or decision.get('kind') != 'action'):
            raise MailboxError('input_not_committed')
        # A later unclassified utterance may be a cancellation. Retain the
        # original producer fence even if it arrived after CallerTurns.commit.
        if any(other.get('order', -1) > row['order']
               and (not other.get('complete') or not other.get('kind'))
               for other in turns.turns.values()):
            raise MailboxError('newer_input_unresolved')
        original_text = row['text']
        command_id = hashlib.sha256((self.scope.call_id + ':' + identity).encode()).hexdigest()
        input_id = hashlib.sha256(identity.encode()).hexdigest()
        status = self.mailbox.offer(self.scope, command_id=command_id, input_id=input_id,
            text=original_text, classified_text=original_text, decision=decision, input_finalized=True,
            phone_history=phone_history)
        result = {'status': status, 'command_id': command_id,
                  'source_thread_id': self.scope.source_thread_id,
                  'delivered_to_model': False, 'execution_confirmed': False}
        if not any(item.get('command_id') == command_id for item in job.get('stop_offers', [])):
            job.setdefault('stop_offers', []).append(dict(result))
        return result

    def inspect_delivery(self, job, command_id, *, rollout_path, hook_config):
        """A reserved output is pending until actual typed source input is seen."""
        self.require_job(job)
        pending = {'status':'pending_stop_delivery', 'delivered_to_model':False,
                   'execution_confirmed':False}
        if not any(row.get('command_id') == command_id for row in job.get('stop_offers', [])):
            raise MailboxError('unknown_command')
        with self.mailbox.transaction():
            path, _ = self.mailbox._binding(self.scope)
            emission = _optional(path / 'emitted.json')
            if emission is None:
                return pending
            claim = _optional(self.mailbox.store / ('command-' + _digest([
                self.scope.source_thread_id, command_id]) + '.json'))
            if emission.get('command_id') != command_id or claim != emission:
                raise MailboxError('emission_binding_mismatch')
        observed = observe_receipt(rollout_path, self.scope, emission, hook_config=hook_config)
        if not observed['received_by_codex']:
            return pending
        started = observed['processing_verified']
        return {'status':'active_target_steered' if started else 'delivered_execution_unconfirmed',
                'delivered_to_model':True, 'execution_confirmed':started,
                'turn_id':self.scope.root_turn_id, 'evidence':observed}
