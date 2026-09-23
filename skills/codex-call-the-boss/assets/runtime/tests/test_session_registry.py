from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from session_registry import (
    active_sessions,
    disable_session,
    enable_session,
    is_session_enabled,
    load_registry,
)


class SessionRegistryTests(unittest.TestCase):
    def test_only_explicitly_enabled_session_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions.json"
            enable_session("thread-12345678", cwd="/tmp/project", path=path)

            self.assertTrue(is_session_enabled("thread-12345678", path=path))
            self.assertFalse(is_session_enabled("thread-87654321", path=path))
            self.assertEqual(len(active_sessions(path)), 1)

    def test_disabling_keeps_audit_record_but_stops_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sessions.json"
            enable_session("thread-12345678", path=path)
            disable_session("thread-12345678", path=path)

            self.assertFalse(is_session_enabled("thread-12345678", path=path))
            record = load_registry(path)["sessions"]["thread-12345678"]
            self.assertIs(record["enabled"], False)
            self.assertIn("disabled_at", record)


if __name__ == "__main__":
    unittest.main()
