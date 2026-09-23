import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hook_stop
import phone_agent as agent


class SyncInstallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='phone-sync-install-test-'))

    def test_hook_uses_venv_and_one_synchronous_handler_preserving_unrelated_hook(self):
        hooks = self.base / 'hooks.json'
        original = {'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': '/usr/bin/true'},
            {'type': 'command', 'command': '/usr/bin/python3 /test/codex-phone/hook_stop.py', 'async': True}
        ]}]}}
        hooks.write_text(json.dumps(original))
        with (patch.object(agent, 'PROJECT_DIR', self.base),
              patch.object(agent, 'GLOBAL_HOOK_PATH', hooks),
              patch.object(agent, 'load_config', return_value={'phone_command_transport': 'synchronous_stop'})):
            agent.install_completion_hook()
            entries = [hook for group in json.loads(hooks.read_text())['hooks']['Stop'] for hook in group['hooks']]
            self.assertEqual(len(entries), 2)
            selected = entries[-1]
            self.assertFalse(selected['async'])
            self.assertEqual(selected['timeout'], 600)
            self.assertIn('.venv/bin/python', selected['command'])
            self.assertTrue(selected['command'].endswith(' synchronous-stop'))
            self.assertTrue(agent.completion_hook_installed())

    def test_wrong_async_mode_is_not_installed_readiness(self):
        hooks = self.base / 'hooks.json'
        with (patch.object(agent, 'PROJECT_DIR', self.base),
              patch.object(agent, 'GLOBAL_HOOK_PATH', hooks),
              patch.object(agent, 'load_config', return_value={'phone_command_transport': 'synchronous_stop'})):
            hooks.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
                {'command': agent._hook_command(), 'async': True, 'timeout': 600}]}]}}))
            self.assertFalse(agent.completion_hook_installed())

    def test_cached_async_entry_cannot_queue_in_synchronous_mode(self):
        with (patch.object(hook_stop, '_load_json', return_value={'enabled': True,
                   'phone_command_transport': 'synchronous_stop'}),
              patch.object(hook_stop, 'is_session_enabled', return_value=True),
              patch.object(hook_stop, '_ensure_background_daemon') as start):
            self.assertFalse(hook_stop.queue_completion_event({'session_id': 'source-0001',
                'turn_id': 'root-00001', 'last_assistant_message': '结果已完成'}))
            start.assert_not_called()

    def test_mode_change_preserves_config_and_does_not_subscribe_or_dial(self):
        config = self.base / 'config.json'
        old = {'provider': 'iphone', 'enabled': False, 'phone_voice_renderer': 'unchanged'}
        config.write_text(json.dumps(old))
        with (patch.object(agent, 'STATE_DIR', self.base), patch.object(agent, 'CONFIG_PATH', config),
              patch.object(agent, 'DAEMON_INSTANCE_LOCK_PATH', self.base / 'daemon-instance.lock'),
              patch.object(agent, 'load_config', return_value=old.copy()),
              patch.object(agent, 'current_thread_id', return_value='source-0001'),
              patch.object(agent, 'enable_session') as enable,
              patch.object(agent, 'start_background_daemon') as start):
            self.assertEqual(agent.set_command_transport('synchronous_stop', confirmed=True), 0)
            enable.assert_not_called()
            start.assert_not_called()
        updated = json.loads(config.read_text())
        self.assertEqual(updated['phone_voice_renderer'], 'unchanged')
        self.assertFalse(updated['enabled'])
        self.assertEqual(json.loads(next(self.base.glob('config-before-transport-*')).read_text()), old)

    def test_mode_change_refuses_pending_queue_without_mutation(self):
        (self.base / 'queue').mkdir()
        (self.base / 'queue' / 'pending.json').write_text('{}')
        with (patch.object(agent, 'STATE_DIR', self.base),
              patch.object(agent, 'current_thread_id', return_value='source-0001'),
              patch.object(agent, '_atomic_write_json') as write):
            with self.assertRaises(RuntimeError):
                agent.set_command_transport('synchronous_stop', confirmed=True)
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
