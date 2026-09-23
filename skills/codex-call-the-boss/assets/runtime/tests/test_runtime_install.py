import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import runtime_install as installer


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        # Retain fixture dirs; no batch deletion of the user's filesystem.
        self.base = Path(tempfile.mkdtemp(prefix="phone-install-test-"))
        self.home, self.state, self.bundle = (self.base / name for name in ("home", "state", "bundle"))
        self.bundle.mkdir()
        (self.bundle / "requirements.txt").write_text("")
        self.old = self.home / "runtime"
        self.old.mkdir(parents=True)
        (self.old / "old.txt").write_text("keep")

    def install(self):
        return installer.install_runtime(self.home, self.bundle, ("requirements.txt",), (), state=self.state)

    def test_python_310_is_not_accepted(self):
        with patch.object(installer.shutil, "which", return_value="/fake/python"), \
                patch.object(installer.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaisesRegex(RuntimeError, "3.11"):
                installer.select_python()
            self.assertIn("(3, 11)", run.call_args.args[0][-1])

    def test_missing_python_does_not_copy_or_change_runtime(self):
        with patch.object(installer, "select_python", side_effect=RuntimeError("missing")):
            with self.assertRaises(RuntimeError): self.install()
        self.assertFalse((self.home / "versions").exists())
        self.assertEqual((self.old / "old.txt").read_text(), "keep")

    def test_failed_pip_keeps_original_directory_and_candidate_evidence(self):
        def run(args, **kwargs):
            if "pip" in args: raise subprocess.CalledProcessError(1, args)
        with patch.object(installer, "select_python", return_value="/fake/python"), \
                patch.object(installer.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError): self.install()
        self.assertFalse(self.old.is_symlink())
        self.assertEqual((self.old / "old.txt").read_text(), "keep")
        self.assertEqual(len(list((self.home / "versions").glob("*/installation-failed.json"))), 1)

    def test_copy_failure_preserves_old_runtime_and_records_failed_candidate(self):
        with patch.object(installer, 'select_python', return_value='/fake/python'), \
                patch.object(installer.shutil, 'copy2', side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError): self.install()
        self.assertEqual((self.old/'old.txt').read_text(), 'keep')
        self.assertEqual(len(list((self.home/'versions').glob('*/installation-failed.json'))), 1)

    def test_dependency_timeout_preserves_old_runtime(self):
        with patch.object(installer, 'select_python', return_value='/fake/python'), \
                patch.object(installer.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fake', 120)) as run:
            with self.assertRaises(subprocess.TimeoutExpired): self.install()
        self.assertEqual(run.call_args.kwargs['timeout'], 120)
        self.assertEqual((self.old/'old.txt').read_text(), 'keep')

    def test_success_activates_permanent_venv_path_and_keeps_old_version(self):
        with patch.object(installer, "select_python", return_value="/fake/python"), \
                patch.object(installer.subprocess, "run") as run:
            result = self.install()
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.old.resolve(), Path(result["version_path"]))
        self.assertEqual((Path(result["previous_runtime"]) / "old.txt").read_text(), "keep")
        self.assertEqual(Path(run.call_args_list[0].args[0][-1]).parent, self.old.resolve())

    def test_failed_activation_restores_original_directory(self):
        original_replace = installer.os.replace
        def replace(source, target):
            if Path(source).name.startswith(".runtime-next-"): raise OSError("simulated switch failure")
            return original_replace(source, target)
        with patch.object(installer, "select_python", return_value="/fake/python"), \
                patch.object(installer.subprocess, "run"), patch.object(installer.os, "replace", side_effect=replace):
            with self.assertRaises(OSError): self.install()
        self.assertFalse(self.old.is_symlink())
        self.assertEqual((self.old / "old.txt").read_text(), "keep")

    def test_active_line_and_instance_lock_both_block_update(self):
        self.state.mkdir()
        marker = self.state / "phone-line-unconfirmed.json"
        marker.write_text("{}")
        with patch.object(installer, "select_python", return_value="/fake/python"):
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"): self.install()
        marker.unlink()  # One exact test fixture, not recursive deletion.
        with (self.state / "daemon-instance.lock").open("a+b") as lock, \
                patch.object(installer, "select_python", return_value="/fake/python"):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "in use"): self.install()
        self.assertFalse((self.home / "versions").exists())

    def test_failed_second_install_preserves_existing_symlink(self):
        with patch.object(installer, "select_python", return_value="/fake/python"), patch.object(installer.subprocess, "run"):
            first = self.install()
        with patch.object(installer, "select_python", return_value="/fake/python"), \
                patch.object(installer.subprocess, "run", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError): self.install()
        self.assertEqual(self.old.resolve(), Path(first["version_path"]))
