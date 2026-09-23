"""Transactional local installer. Failed candidates and old versions are kept.

The virtualenv is created at its permanent versioned path, never relocated.
Only the stable runtime link changes after all checks, while the daemon is off.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path


def select_python() -> str:
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3", sys.executable):
        candidate = shutil.which(name)
        if not candidate:
            continue
        try:
            result = subprocess.run([candidate, "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"],
                                    check=False, timeout=5, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate
    raise RuntimeError("Python 3.11 or newer is required; existing runtime was not changed")


def _idle(state: Path) -> None:
    if any((state / name).exists() for name in ("active-call.json", "phone-line-unconfirmed.json")):
        raise RuntimeError("Phone line is active or unconfirmed; update refused")
    if any(list((state / name).glob("*.json")) for name in ("queue", "calling")):
        raise RuntimeError("Pending phone jobs exist; update refused")


def install_runtime(home: Path, bundle: Path, files: tuple[str, ...], tests: tuple[str, ...], *, state: Path) -> dict:
    # Validate everything possible before creating files or touching the old version.
    home, bundle, state = home.resolve(), bundle.resolve(), state.resolve()
    python = select_python()
    names = [*files, *("tests/" + name for name in tests)]
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not (bundle / relative).is_file():
            raise RuntimeError("Incomplete or invalid bundled runtime manifest")
    _idle(state)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as locks:
        # Nonblocking also avoids the Stop-hook/start-daemon lock inversion.
        for path in (home / "install.lock", state / "daemon-start.lock", state / "daemon-instance.lock",
                     state / "completion-queue.lock"):
            handle = locks.enter_context(path.open("a+b"))
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Phone runtime is in use; stop the idle service before updating") from exc
        _idle(state)
        candidate = home / "versions" / uuid.uuid4().hex
        candidate.mkdir(parents=True, mode=0o700)
        try:
            for name in names:
                target = candidate / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(bundle / name, target)
            subprocess.run([python, "-m", "venv", str(candidate / ".venv")], check=True, timeout=120)
            runner = candidate / ".venv/bin/python"
            subprocess.run([str(runner), "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"], check=True, timeout=5)
            subprocess.run([str(runner), "-m", "pip", "install", "--disable-pip-version-check",
                            "-r", str(candidate / "requirements.txt")], check=True, timeout=600)
            subprocess.run([str(runner), "-m", "pip", "check"], check=True, timeout=30)
            test_args = [str(runner), "-m", "unittest", "discover", "-s", "tests", "-q"]
            if os.environ.get('CODEX_PHONE_RETAIN_TEST_FIXTURES') == '1':
                # Owner policy may forbid recursive fixture cleanup. This
                # applies only to this installer child, never to normal apps.
                test_args = [str(runner), '-c', (
                    "import sys,tempfile,unittest\n"
                    "class Retained(tempfile.TemporaryDirectory):\n"
                    " def cleanup(self): self._finalizer.detach()\n"
                    " @classmethod\n"
                    " def _cleanup(cls,*args,**kwargs): pass\n"
                    "tempfile.TemporaryDirectory=Retained\n"
                    "sys.path.insert(0,'tests')\n"
                    "r=unittest.TextTestRunner(verbosity=0).run(unittest.defaultTestLoader.discover('tests'))\n"
                    "raise SystemExit(not r.wasSuccessful())\n")]
            subprocess.run(test_args,
                           cwd=candidate, check=True, timeout=180)
            _idle(state)
            previous = _activate(home, candidate)
        except BaseException as exc:
            # No raw exception text, credentials, transcripts, or recursive cleanup.
            record = candidate / "installation-failed.json"
            try:
                record.write_text(json.dumps({"error_type": type(exc).__name__, "active_runtime_unchanged": True}))
                record.chmod(0o600)
            except OSError:
                pass  # A full disk may prevent evidence, but not restore activation.
            raise
    return {"installed": True, "runtime": str(home / "runtime"), "version_path": str(candidate),
            "previous_runtime": str(previous) if previous else None, "tests": "passed", "rollback_available": bool(previous)}


def _activate(home: Path, candidate: Path) -> Path | None:
    stable = home / "runtime"
    previous = stable.resolve() if stable.is_symlink() else None
    next_link = home / (".runtime-next-" + uuid.uuid4().hex)
    next_link.symlink_to(candidate, target_is_directory=True)
    moved = False
    try:
        if stable.exists() and not stable.is_symlink():
            previous = home / ("runtime-previous-" + uuid.uuid4().hex)
            os.replace(stable, previous)
            moved = True
        os.replace(next_link, stable)
    except BaseException:
        if moved:
            os.replace(previous, stable)
        raise
    return previous
