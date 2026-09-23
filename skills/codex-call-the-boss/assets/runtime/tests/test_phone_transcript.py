from __future__ import annotations

from copy import deepcopy
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from phone_agent import PhoneDaemon
from phone_transcript import backfill, export_archive, export_conversation, render_conversation


class PhoneTranscriptTests(unittest.TestCase):
    def record(self, **updates):
        return {"source_thread_id": "source-task", "call_id": "call-1",
                "created_at": "2026-09-09T06:00:00+00:00",
                "updated_at": "2026-09-09T06:02:00+00:00",
                "finished_at": "2026-09-09T06:02:00+00:00",
                "transcript": [{"role": "user", "text": "刚才有颤音，请检查"},
                    {"role": "assistant", "text": "收到指令，已经发送。",
                     "playback_status": "output_complete"}],
                "commands": [], **updates}

    def test_export_contains_actual_words_and_separate_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "source-task"
            record = self.record()
            original = deepcopy(record)
            result = export_conversation(directory, record)
            text = result.read_text()
            self.assertIn("刚才有颤音，请检查", text)
            self.assertIn("你（语音识别）", text)
            self.assertIn("电话助手（电脑端输出完成）", text)
            self.assertIn("不等于手机端音质验收通过", text)
            self.assertEqual(record, original)
            for path in (result, directory/"call-1.md", directory/".display-index.json", directory/".display.lock"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_markup_in_transcript_cannot_create_links_images_or_html(self):
        text = '![x](https://example.invalid/track)\n</blockquote><script>run()</script>\n# Delete\n```sh'
        rendered = render_conversation(self.record(transcript=[{"role": "user", "text": text}]))
        for unsafe in ("![x](", "<script>", "\n# Delete", "\n```sh"):
            self.assertNotIn(unsafe, rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_partial_and_unplayed_text_never_claim_complete_playback(self):
        rows = [{"role": "assistant", "text": state, "playback_status": state}
                for state in ("queued", "playing", "partial_cancelled", "cancelled", "suppressed", "unknown")]
        text = render_conversation(self.record(transcript=rows))
        self.assertIn("整段文字并未全部播出", text)
        self.assertIn("尚未播放", text)
        self.assertIn("已取消，未播出", text)
        self.assertIn("播放状态未确认", text)
        self.assertNotIn("电话助手（电脑端输出完成）", text)

    def test_delivery_is_not_promoted_to_started_or_completed(self):
        text = render_conversation(self.record(commands=[{"text": "执行测试", "delivery_status": "accepted_by_codex_app"}]))
        self.assertIn("这条回执未确认开始执行", text)
        self.assertNotIn("已核验目标任务开始处理", text)

    def test_started_needs_correlated_turn_identity(self):
        command = {"text": "执行测试", "delivery_status": "target_turn_started"}
        self.assertNotIn("已核验目标任务开始处理", render_conversation(self.record(commands=[command])))
        command["target_turn_id"] = "target-turn"
        self.assertIn("已核验目标任务开始处理", render_conversation(self.record(commands=[command])))

    def test_snapshot_and_final_use_one_call_and_final_survives_late_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"source-task"
            snapshot = self.record(call_id="call-1_input-2", root_call_id="call-1", finished_at=None)
            export_conversation(directory, snapshot)
            self.assertIn("记录快照", (directory/"call-1.md").read_text())
            export_conversation(directory, self.record())
            final = (directory/"电话记录.md").read_bytes()
            snapshot["updated_at"] = "2026-09-09T07:00:00+00:00"
            export_conversation(directory, snapshot)
            self.assertEqual((directory/"电话记录.md").read_bytes(), final)
            self.assertFalse((directory/"call-1_input-2.md").exists())
            self.assertEqual(list(json.loads((directory/".display-index.json").read_text())["calls"]), ["call-1"])

    def test_older_backfill_cannot_replace_latest_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"source-task"
            export_conversation(directory, self.record(call_id="new-call"))
            older = self.record(call_id="old-call", created_at="2026-09-08T06:00:00+00:00",
                                transcript=[{"role": "user", "text": "旧的原话"}])
            export_conversation(directory, older)
            text = (directory/"电话记录.md").read_text()
            self.assertIn("刚才有颤音，请检查", text)
            self.assertNotIn("旧的原话", text)
            self.assertIn("old-call.md", text)
            self.assertIn("旧的原话", (directory/"old-call.md").read_text())

    def test_older_revision_of_same_call_cannot_regress_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"source-task"
            export_conversation(directory, self.record())
            original = (directory/"电话记录.md").read_bytes()
            export_conversation(directory, self.record(updated_at="2026-09-09T06:01:00+00:00", transcript=[]))
            self.assertEqual((directory/"电话记录.md").read_bytes(), original)

    def test_cross_source_directory_and_invalid_identity_refused_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                export_conversation(Path(temporary)/"another-task", self.record())
            with self.assertRaises(ValueError):
                export_conversation(Path(temporary)/"source-task", self.record(call_id="../../escape"))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_unknown_date_not_silently_reordered_by_file_modification_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            for date in ("not-a-date", "2026-09-09T06:00:00"):
                with self.assertRaises(ValueError):
                    export_conversation(Path(temporary)/"source-task", self.record(created_at=date))

    def test_archive_preserves_json_and_attaches_readable_path(self):
        with tempfile.TemporaryDirectory() as temporary, patch("phone_agent.STATE_DIR", Path(temporary)):
            job = {"thread_id": "source-task", "job_id": "call-1",
                   "phone_transcript": self.record()["transcript"],
                   "created_at": self.record()["created_at"], "finished_at": self.record()["finished_at"]}
            archive = PhoneDaemon.archive_phone_conversation("call-1", job)
            self.assertEqual(json.loads(Path(archive).read_text())["transcript"], job["phone_transcript"])
            self.assertTrue(Path(job["conversation_display"]).is_file())
            self.assertNotIn("conversation_display_error", job)

    def test_display_failure_does_not_fail_primary_archive_or_redeliver(self):
        with tempfile.TemporaryDirectory() as temporary, patch("phone_agent.STATE_DIR", Path(temporary)), \
                patch("phone_agent.export_conversation", side_effect=OSError("disk full")):
            job = {"thread_id": "source-task", "phone_transcript": self.record()["transcript"]}
            archive = PhoneDaemon.archive_phone_conversation("call-1", job)
            self.assertTrue(Path(archive).is_file())
            self.assertEqual(job["conversation_display_error"], "OSError")
            self.assertNotIn("relayed_phone_tasks", job)

    def test_backfill_preserves_raw_archives_and_does_not_duplicate_input_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"conversations"/"source-task"
            directory.mkdir(parents=True)
            record = self.record()
            archive = directory/"call-1.json"
            archive.write_text(json.dumps(record))
            snapshot = directory/"call-1_input-2.json"
            snapshot.write_text(json.dumps(self.record(call_id="call-1_input-2")))
            before = {path: path.read_bytes() for path in (archive, snapshot)}
            result = backfill(directory, "source-task")
            self.assertEqual(result["calls_exported"], 1)
            self.assertEqual(result["input_snapshots_skipped"], 1)
            self.assertEqual(result["errors"], [])
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertFalse((directory/"call-1_input-2.md").exists())

    def test_export_refuses_mismatched_job_before_any_derived_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"source-task"
            directory.mkdir()
            archive, job = directory/"call-1.json", Path(temporary)/"job.json"
            archive.write_text(json.dumps(self.record()))
            job.write_text(json.dumps({"thread_id": "other-task", "job_id": "call-1"}))
            with self.assertRaises(ValueError):
                export_archive(archive, "source-task", job)
            self.assertFalse((directory/"电话记录.md").exists())

    def test_legacy_job_without_job_id_requires_matching_filename_source_and_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/"source-task"
            directory.mkdir()
            archive, job = directory/"call-1.json", Path(temporary)/"call-1.json"
            record = self.record()
            archive.write_text(json.dumps(record))
            job.write_text(json.dumps({"thread_id": "source-task", "phone_transcript": record["transcript"],
                                       "created_at": record["created_at"], "finished_at": record["finished_at"]}))
            self.assertTrue(export_archive(archive, "source-task", job).is_file())


class AsyncArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_display_does_not_block_event_loop_and_uses_immutable_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        seen = {}
        def archive(call, snapshot):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test archive was not released")
            seen.update(snapshot)
            snapshot["conversation_display"] = "private-view.md"
            return "private-archive.json"
        job = {"thread_id": "source-task", "phone_transcript": [{"role": "user", "text": "原话"}]}
        with patch.object(PhoneDaemon, "archive_phone_conversation", side_effect=archive):
            task = asyncio.create_task(PhoneDaemon.archive_phone_conversation_async("call-1", job))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
                self.assertFalse(task.done())
                job["phone_transcript"][0]["text"] = "后续识别"
            finally:
                release.set()
            self.assertEqual(await task, "private-archive.json")
        self.assertEqual(seen["phone_transcript"][0]["text"], "原话")
        self.assertEqual(job["conversation_display"], "private-view.md")


if __name__ == "__main__":
    unittest.main()
