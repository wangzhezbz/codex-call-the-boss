"""Ordered caller intents with an explicit, atomic dispatch boundary.

No model calls or task writes. New speech holds unsent actions until its
meaning is known; a late cancel can never be acknowledged as an undo of an
already committed desktop request.
"""
from __future__ import annotations

import asyncio


class CallerTurns:
    def __init__(self):
        self.turns = {}
        self.speaking = False
        self.changed = asyncio.Event()

    def _wake(self):
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    def observe(self, identity, text='', *, complete=False):
        if not identity:
            return
        turn = self.turns.setdefault(identity, {
            'order': len(self.turns), 'text': '', 'complete': False,
            'kind': '', 'status': 'pending',
        })
        if text:
            turn['text'] = text
        turn['complete'] = turn['complete'] or complete
        self._wake()

    def voice(self, active):
        if self.speaking != active:
            self.speaking = active
            self._wake()

    def decide(self, identity, kind):
        self.observe(identity, complete=True)
        current = self.turns[identity]
        current['kind'] = kind
        cancelled, committed = [], []
        if kind == 'cancel':
            for key, turn in self.turns.items():
                if turn['order'] >= current['order']:
                    continue
                if turn['status'] == 'committed':
                    committed.append(key)
                elif turn['status'] == 'pending' and turn['kind'] in {'', 'action', 'cancel'}:
                    turn['status'] = 'cancelled'
                    cancelled.append(key)
        self._wake()
        return {'cancelled': cancelled, 'already_committed': committed}

    def abandon(self, identity):
        self.observe(identity)
        self.turns[identity]['status'] = 'incomplete'
        self._wake()

    def supersede(self, old, new, text):
        if old == new or old not in self.turns:
            self.observe(new, text, complete=True)
            return
        previous = self.turns[old]
        previous.update(complete=True, kind='alias', status='superseded')
        self.observe(new, text, complete=True)
        self.turns[new]['order'] = previous['order']
        self._wake()

    async def commit(self, identity, *, timeout=12, allowed_kinds=('action',)):
        """Return True exactly once, immediately before the external send.

        Only later unresolved utterances hold an earlier action. Earlier
        actions preserve send order. Waiting here never owns the classifier
        lock, so the caller's cancel can finish classification.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            turn = self.turns.get(identity)
            if not turn or turn['status'] != 'pending' or turn['kind'] not in allowed_kinds:
                return False
            event = self.changed
            unresolved_later = any(
                row['order'] > turn['order'] and (not row['complete'] or not row['kind'])
                for row in self.turns.values())
            earlier_action = any(
                row['order'] < turn['order'] and row['status'] == 'pending'
                and (not row['kind'] or row['kind'] == 'action')
                for row in self.turns.values())
            if turn['complete'] and not self.speaking and not unresolved_later and not earlier_action:
                turn['status'] = 'committed'
                self._wake()
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                turn['status'] = 'deferred'
                self._wake()
                return False
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                continue

    def snapshot(self):
        return [{'id': identity, **row} for identity, row in self.turns.items()]
