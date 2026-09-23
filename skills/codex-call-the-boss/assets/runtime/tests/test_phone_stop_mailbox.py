"""Local-only tests; retained temporary fixtures never touch phone state."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import phone_stop_mailbox as m


class MailboxTests(unittest.TestCase):
    def setUp(self):
        # Intentionally retained: repository policy prohibits bulk removal.
        self.store = Path(tempfile.mkdtemp(prefix="phone-stop-mailbox-test-"))
        self.wall = 1000.0
        self.tick = 10.0
        self.box = m.StopMailbox(self.store, wall_clock=lambda: self.wall,
                                 monotonic=lambda: self.tick)
        self.scope = m.Scope("source-0001", "root-000001", "cycle-00001", "call-000001", "/test/project")
        self.event = {"hook_event_name": "Stop", "session_id": self.scope.source_thread_id,
                      "turn_id": self.scope.root_turn_id, "cwd": self.scope.cwd,
                      "stop_hook_active": False}

    def owner(self, scope=None, **kwargs):
        owner = self.box.open_window(scope or self.scope, **kwargs)
        self.addCleanup(owner.close)
        return owner

    def offer(self, scope=None, **kwargs):
        values = dict(command_id="command-0001", input_id="input-00001",
                      text="放两次礼花庆祝一下", classified_text="放两次礼花庆祝一下",
                      decision={"kind": "action", "clarification": ""}, input_finalized=True)
        values.update(kwargs)
        return self.box.offer(scope or self.scope, **values)

    def record(self, name):
        return m._read(self.box.path(self.scope) / name)

    def test_offer_is_not_delivery(self):
        self.owner()
        self.assertEqual(self.offer(), "offered_not_delivered")
        self.assertFalse((self.box.path(self.scope) / "emitted.json").exists())
        self.assertTrue(self.box.is_waiting(self.scope))

    def test_once_and_original_words(self):
        owner = self.owner()
        self.offer()
        self.event["last_assistant_message"] = "irrelevant historical command"
        result = owner.reserve(self.event)
        self.assertEqual(result["decision"], "block")
        payload = json.loads(result["reason"].splitlines()[-1])
        self.assertEqual(payload["original_caller_words"], "放两次礼花庆祝一下")
        self.assertNotIn("irrelevant", result["reason"])
        self.assertEqual(owner.reserve(self.event), {})
        self.assertFalse(self.box.is_waiting(self.scope))
        record = self.record("emitted.json")
        self.assertIs(record["received_by_codex"], False)
        self.assertIs(record["execution_confirmed"], False)

    def test_uncommitted_inputs_rejected(self):
        self.owner()
        for changes in ({"input_finalized": False}, {"input_finalized": 1},
                        {"classified_text": "听错的字"}, {"text": " "},
                        {"decision": {"kind": "question", "clarification": ""}},
                        {"decision": {"kind": "action", "clarification": "", "extra": 1}},
                        {"command_id": "../../outside"}, {"input_id": "x"},
                        {"text": "a" * 6001, "classified_text": "a" * 6001}):
            with self.subTest(changes=changes), self.assertRaisesRegex(m.MailboxError, "uncommitted_action"):
                self.offer(**changes)

    def test_duplicate_offer_idempotent_but_different_offer_rejected(self):
        self.owner()
        self.offer()
        self.assertEqual(self.offer(), "already_offered")
        with self.assertRaisesRegex(m.MailboxError, "different_command_pending"):
            self.offer(command_id="command-0002")

    def test_wrong_stop_identity_does_not_emit(self):
        owner = self.owner()
        self.offer()
        for change in ({"session_id": "other-source"}, {"thread_id": "other-source"},
                       {"turn_id": "other-root"}, {"cwd": "/wrong"},
                       {"agent_id": "child-agent"}, {"hook_event_name": "PreToolUse"},
                       {"stop_hook_active": 1}, {"stop_hook_active": None}):
            with self.subTest(change=change):
                self.assertEqual(owner.reserve(self.event | change), {})
        self.assertEqual(owner.reserve(None), {})
        self.assertFalse((self.box.path(self.scope) / "emitted.json").exists())

    def test_wrong_call_cycle_source_cannot_offer(self):
        self.owner()
        for field in ("source_thread_id", "root_turn_id", "cycle_id", "call_id", "cwd"):
            scope = replace(self.scope, **{field: "/wrong" if field == "cwd" else "other-0001"})
            with self.subTest(field=field), self.assertRaises(FileNotFoundError):
                self.offer(scope)

    def test_one_live_owner_per_source(self):
        self.owner()
        next_scope = replace(self.scope, cycle_id="cycle-00002")
        with self.assertRaisesRegex(m.MailboxError, "source_already_waiting"):
            self.box.open_window(next_scope)
        self.assertFalse(self.box.path(next_scope).exists())

    def test_independent_sources(self):
        self.owner()
        other = replace(self.scope, source_thread_id="source-0002")
        self.owner(other)
        self.assertTrue(self.box.is_waiting(other))

    def test_next_cycle_same_root_new_command(self):
        first = self.owner()
        self.offer()
        first_result = first.reserve(self.event)
        first.close()
        scope = replace(self.scope, cycle_id="cycle-00002", call_id="call-000002")
        second = self.owner(scope)
        self.offer(scope, command_id="command-0002")
        result = second.reserve(self.event | {"stop_hook_active": True})
        self.assertEqual(result["decision"], "block")
        self.assertNotEqual(first_result["reason"], result["reason"])

    def test_command_id_not_replayed_in_next_cycle(self):
        first = self.owner()
        self.offer()
        first.reserve(self.event)
        first.close()
        scope = replace(self.scope, cycle_id="cycle-00002", call_id="call-000002")
        self.owner(scope)
        with self.assertRaisesRegex(m.MailboxError, "command_already_reserved"):
            self.offer(scope)

    def test_cancellation_before_emission(self):
        owner = self.owner()
        self.offer()
        self.assertEqual(self.box.cancel(self.scope), "cancelled_before_emission")
        self.assertFalse(self.box.is_waiting(self.scope))
        self.assertEqual(owner.reserve(self.event), {})
        with self.assertRaisesRegex(m.MailboxError, "window_cancelled"):
            self.offer()

    def test_cancellation_without_offer(self):
        self.owner()
        self.box.cancel(self.scope)
        self.assertFalse(self.box.is_waiting(self.scope))

    def test_cancellation_after_reservation_is_not_undo(self):
        owner = self.owner()
        self.offer()
        owner.reserve(self.event)
        self.assertEqual(self.box.cancel(self.scope), "emission_uncertain_or_delivered_not_undone")
        self.assertFalse((self.box.path(self.scope) / "cancelled.json").exists())

    def test_closed_and_old_windows_cannot_reopen(self):
        owner = self.owner()
        owner.close("caller_hangup")
        self.assertEqual(self.record("closed.json")["status"], "caller_hangup")
        self.assertFalse(self.box.is_waiting(self.scope))
        with self.assertRaisesRegex(m.MailboxError, "window_closed"):
            self.offer()
        with self.assertRaises(FileExistsError):
            self.box.open_window(self.scope)

    def test_monotonic_timeout(self):
        owner = self.owner(timeout_seconds=1)
        self.offer()
        self.tick += 1
        self.assertEqual(owner.reserve(self.event), {})
        self.assertEqual(self.record("closed.json")["status"], "timeout")

    def test_wall_clock_expiry_or_rollback_not_ready(self):
        self.owner()
        for now in (999.0, 1045.0, float("nan"), float("inf")):
            self.wall = now
            self.assertFalse(self.box.is_waiting(self.scope))
            with self.assertRaisesRegex(m.MailboxError, "window_expired"):
                self.offer()

    def test_bad_timeout(self):
        for value in (0, -1, 601, True, "45", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaisesRegex(m.MailboxError, "invalid_timeout"):
                self.box.open_window(self.scope, timeout_seconds=value)

    def test_concurrent_reservation_at_most_once(self):
        owner = self.owner()
        self.offer()
        def reserve(_):
            try:
                return owner.reserve(self.event)
            except m.MailboxError as exc:
                self.assertEqual(str(exc), "transaction_busy")
                return {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(24)))
        self.assertEqual(sum(bool(result) for result in results), 1)

    def test_crash_after_claim_before_output_never_retries(self):
        owner = self.owner()
        self.offer()
        original = m._new
        def fail_emission(path, value):
            if path.name == "emitted.json":
                raise OSError("simulated disk failure")
            original(path, value)
        with patch.object(m, "_new", side_effect=fail_emission):
            with self.assertRaises(OSError):
                owner.reserve(self.event)
        self.assertEqual(owner.reserve(self.event), {})
        self.assertFalse(self.box.is_waiting(self.scope))
        self.assertEqual(self.box.cancel(self.scope), "emission_uncertain_or_delivered_not_undone")
        self.assertEqual(len(list(self.store.glob("command-*.json"))), 1)

    def test_owner_process_crash_loses_readiness(self):
        scope_json = json.dumps(m.asdict(self.scope))
        script = ("import json, os, sys; from pathlib import Path; "
                  "from phone_stop_mailbox import Scope, StopMailbox; "
                  "box=StopMailbox(Path(sys.argv[1]), wall_clock=lambda:1000.0, monotonic=lambda:10.0); "
                  "owner=box.open_window(Scope(**json.loads(sys.argv[2]))); os._exit(0)")
        subprocess.run([sys.executable, "-c", script, str(self.store), scope_json], check=True, timeout=5)
        self.assertFalse(self.box.is_waiting(self.scope))
        with self.assertRaisesRegex(m.MailboxError, "owner_not_waiting"):
            self.offer()

    def test_store_permissions_fail_closed(self):
        self.store.chmod(0o755)
        with self.assertRaisesRegex(m.MailboxError, "invalid_private_directory"):
            m.StopMailbox(self.store)

    def test_symlink_offer_rejected(self):
        owner = self.owner()
        target = self.store / "untrusted.json"
        m._new(target, {})
        (self.box.path(self.scope) / "offered.json").symlink_to(target)
        with self.assertRaises(OSError):
            owner.reserve(self.event)
        self.assertFalse(self.box.is_waiting(self.scope))

    def test_invalid_offer_cannot_cancel_or_emit(self):
        owner = self.owner()
        m._new(self.box.path(self.scope) / "offered.json", {})
        with self.assertRaisesRegex(m.MailboxError, "invalid_offer"):
            owner.reserve(self.event)
        with self.assertRaisesRegex(m.MailboxError, "invalid_offer"):
            self.box.cancel(self.scope)

    def test_record_size_includes_newline(self):
        # JSON {"x": ""} occupies nine bytes before its trailing newline.
        record = {"x": "a" * (m.LIMIT - 9)}
        self.assertEqual(len(json.dumps(record).encode()), m.LIMIT)
        with self.assertRaisesRegex(m.MailboxError, "record_too_large"):
            m._new(self.store / "oversized.json", record)
        self.assertFalse((self.store / "oversized.json").exists())

    def test_wait_receives_delayed_offer(self):
        owner = self.owner()
        async def run():
            pending = asyncio.create_task(owner.wait(self.event, poll_seconds=0.001))
            await asyncio.sleep(0.01)
            self.offer()
            return await asyncio.wait_for(pending, 1)
        self.assertEqual(asyncio.run(run())["decision"], "block")
        self.assertIsNone(owner.fd)

    def test_wait_invalid_event_returns_immediately(self):
        owner = self.owner()
        result = asyncio.run(asyncio.wait_for(owner.wait({}), 0.2))
        self.assertEqual(result, {})
        self.assertIsNone(owner.fd)

    def test_wait_wall_timeout_closes(self):
        owner = self.owner()
        self.wall += 50
        self.assertEqual(asyncio.run(owner.wait(self.event)), {})
        self.assertEqual(self.record("closed.json")["status"], "timeout")

    def test_wait_cancel_closes_as_interrupted(self):
        owner = self.owner()
        async def run():
            task = asyncio.create_task(owner.wait(self.event))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        self.assertIsNone(owner.fd)
        self.assertEqual(self.record("closed.json")["status"], "interrupted")

    def test_real_monotonic_wait_is_bounded(self):
        self.box = m.StopMailbox(self.store)
        owner = self.owner(timeout_seconds=0.025)
        async def run():
            return await asyncio.wait_for(owner.wait(self.event, poll_seconds=0.005), 0.5)
        self.assertEqual(asyncio.run(run()), {})
        self.assertIsNone(owner.fd)

    def test_transient_lock_contention_does_not_abort_wait(self):
        owner = self.owner()
        self.offer()
        async def run():
            with self.box.transaction():
                task = asyncio.create_task(owner.wait(self.event, poll_seconds=0.001))
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
            return await asyncio.wait_for(task, 0.5)
        self.assertEqual(asyncio.run(run())["decision"], "block")

    def test_invalid_poll_closes_owner(self):
        owner = self.owner()
        with self.assertRaisesRegex(m.MailboxError, "invalid_poll_interval"):
            asyncio.run(owner.wait(self.event, poll_seconds=0))
        self.assertIsNone(owner.fd)

    def test_cross_process_offer_reaches_waiting_owner(self):
        owner = self.owner()
        script = ("import json, sys; from pathlib import Path; "
                  "from phone_stop_mailbox import Scope, StopMailbox; "
                  "box=StopMailbox(Path(sys.argv[1]), wall_clock=lambda:1000.0); "
                  "scope=Scope(**json.loads(sys.argv[2])); assert box.is_waiting(scope); "
                  "box.offer(scope, command_id='command-0001', input_id='input-00001', "
                  "text='test words', classified_text='test words', "
                  "decision={'kind':'action','clarification':''}, input_finalized=True)")
        subprocess.run([sys.executable, "-c", script, str(self.store), json.dumps(m.asdict(self.scope))],
                       check=True, timeout=5)
        self.assertIn("test words", owner.reserve(self.event)["reason"])

    def test_corrupt_claim_never_replays(self):
        owner = self.owner()
        self.offer()
        claim = self.store / ("command-" + m._digest([self.scope.source_thread_id, "command-0001"]) + ".json")
        # Simulate interruption immediately after exclusive file creation.
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with self.assertRaises(ValueError):
            owner.reserve(self.event)
        self.assertFalse(self.box.is_waiting(self.scope))
        self.assertFalse((self.box.path(self.scope) / "emitted.json").exists())


if __name__ == "__main__":
    unittest.main()
