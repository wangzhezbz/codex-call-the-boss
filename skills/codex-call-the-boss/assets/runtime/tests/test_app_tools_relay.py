from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app_tools_relay import AppToolsRelayClient, AppToolsRelayError


class RelayLaunchTests(unittest.TestCase):
    def test_launcher_preserves_managed_pid_by_exec_not_child_supervision(self):
        import phone_agent
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CODEX_MCP_NODE_PATH': '/usr/bin/true',
                                        'CODEX_APP_TOOLS_PIPE_PATH': 'fake-managed-pipe'}), \
                patch.object(phone_agent, 'STATE_DIR', Path(directory)), \
                patch.object(os, 'execv') as replace:
            self.assertEqual(phone_agent.run_app_tools_relay(), 0)
            replace.assert_called_once_with('/usr/bin/true', ['/usr/bin/true',
                str(phone_agent.PROJECT_DIR / 'app_tools_relay.mjs'), str(phone_agent.APP_TOOLS_RELAY_PATH)])


class RelayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.node = os.environ.get('CODEX_MCP_NODE_PATH') or shutil.which('node')
        if not self.node:
            self.skipTest('Node runtime unavailable')
        self.temporary = tempfile.TemporaryDirectory(prefix='phone-relay-', dir='/tmp')
        self.directory = Path(self.temporary.name)
        self.native_path = self.directory/'native.sock'
        self.relay_path = self.directory/'relay.sock'
        self.healthy = True
        self.available_tools = ['read_thread', 'send_message_to_thread']
        self.catalog_reads = 0
        self.malformed_once = False
        self.context_result = None
        self.reply_chars = 150000
        self.tool_calls = 0
        (self.directory/'config.json').write_text(json.dumps({'enabled':True, 'relay_caller_thread_id':'relay-12345678'}))
        self.registry = {'sessions':{'thread-12345678':{'thread_id':'thread-12345678', 'enabled':True}}}
        (self.directory/'sessions.json').write_text(json.dumps(self.registry))
        self.server = await asyncio.start_unix_server(self.serve_native, str(self.native_path))
        self.script = Path(__file__).resolve().parents[1]/'app_tools_relay.mjs'
        self.env = {**os.environ, 'CODEX_APP_TOOLS_PIPE_PATH': str(self.native_path),
                    'CODEX_PHONE_STATE_DIR': str(self.directory)}
        self.process = await self.launch()
        self.client = AppToolsRelayClient(self.relay_path, timeout=3)
        await asyncio.wait_for(self.process.stdout.readline(), 5)

    async def launch(self):
        return await asyncio.create_subprocess_exec(self.node, str(self.script), str(self.relay_path),
            env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def serve_native(self, reader, writer):
        try:
            while True:
                size = int.from_bytes(await reader.readexactly(4), 'little')
                request = json.loads(await reader.readexactly(size))
                if request['method']=='tools/list':
                    self.catalog_reads += 1
                    result = {'tools': [{'name': name, 'namespace': 'fake'} for name in
                        (self.available_tools if self.healthy else [])]}
                else:
                    self.tool_calls += 1
                    result = {'success': True, 'contentItems': [{'type': 'inputText', 'text': json.dumps(self.context_result) if self.context_result else 'x'*self.reply_chars}]}
                payload = json.dumps({'id':request['id'],'result':result}).encode()
                if self.malformed_once:
                    payload = b'{bad-json'
                    self.malformed_once = False
                writer.write(len(payload).to_bytes(4,'little')+payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def asyncTearDown(self):
        if self.process.returncode is None:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 5)
        self.server.close()
        await self.server.wait_closed()
        self.temporary.cleanup()

    async def test_health_is_fresh_not_a_cached_tool_name(self):
        self.assertTrue(await self.client.health())
        self.healthy = False
        with self.assertRaises(AppToolsRelayError):
            await self.client.health()
        self.assertGreaterEqual(self.catalog_reads, 2)

    async def test_fatal_exception_is_recorded_without_suppressing_exit_or_replaying(self):
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 5)
        script = ('process.argv[2]=' + json.dumps(str(self.relay_path)) + '; '
                  'await import(' + json.dumps(self.script.as_uri()) + '); '
                  'setTimeout(() => { throw new Error("private message must not enter lifecycle"); }, 50);')
        self.process = await asyncio.create_subprocess_exec(self.node, '--input-type=module', '-e', script,
            env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(self.process.communicate(), 5)
        log = (self.directory/'app-tools-relay-lifecycle.jsonl').read_text()
        events = [json.loads(line) for line in log.splitlines()]
        self.assertEqual(self.process.returncode, 1)
        self.assertTrue(any(e['event'] == 'uncaught_exception' for e in events))
        self.assertEqual(events[-1]['event'], 'exit')
        self.assertEqual(events[-1]['code'], 1)
        self.assertNotIn('private message', log)
        self.assertEqual(self.tool_calls, 0)

    async def test_ready_message_requires_native_health_before_listening(self):
        self.assertGreaterEqual(self.catalog_reads, 1)
        self.assertTrue(await self.client.health())

    async def test_health_requires_task_read_capability_as_well_as_send(self):
        self.available_tools = ['send_message_to_thread']
        with self.assertRaises(AppToolsRelayError):
            await self.client.health()
        self.assertEqual(self.tool_calls, 0)

    async def test_unhealthy_start_fails_without_advertising_ready(self):
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 5)
        self.healthy = False
        self.process = await self.launch()
        stdout, stderr = await asyncio.wait_for(self.process.communicate(), 5)
        self.assertNotEqual(self.process.returncode, 0)
        self.assertNotIn(b'relay ready', stdout)
        self.assertIn(b'health', stderr)
        self.assertFalse(self.relay_path.exists())

    async def test_missing_native_pipe_does_not_create_ready_relay(self):
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 5)
        self.env['CODEX_APP_TOOLS_PIPE_PATH'] = str(self.directory/'missing.sock')
        self.process = await self.launch()
        stdout, _ = await asyncio.wait_for(self.process.communicate(), 5)
        self.assertNotEqual(self.process.returncode, 0)
        self.assertNotIn(b'relay ready', stdout)
        self.assertFalse(self.relay_path.exists())

    async def test_duplicate_start_does_not_report_unhealthy_instance_as_ready(self):
        self.assertTrue(await self.client.health())
        inode = self.relay_path.stat().st_ino
        self.healthy = False
        second = await self.launch()
        stdout, _ = await asyncio.wait_for(second.communicate(), 7)
        self.assertNotEqual(second.returncode, 0)
        self.assertNotIn(b'already running', stdout)
        self.assertIsNone(self.process.returncode)
        self.assertEqual(self.relay_path.stat().st_ino, inode)
        self.healthy = True
        self.assertTrue(await self.client.health())

    async def test_duplicate_start_preserves_unrecognized_socket(self):
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 5)

        async def unrelated_socket(reader, writer):
            try:
                writer.write(b'not a phone relay\n')
                await writer.drain()
            except ConnectionError:
                pass
            finally:
                writer.close()

        unrelated = await asyncio.start_unix_server(unrelated_socket, str(self.relay_path))
        try:
            inode = self.relay_path.stat().st_ino
            second = await self.launch()
            stdout, _ = await asyncio.wait_for(second.communicate(), 7)
            self.assertNotEqual(second.returncode, 0)
            self.assertNotIn(b'already running', stdout)
            self.assertEqual(self.relay_path.stat().st_ino, inode)
        finally:
            unrelated.close()
            await unrelated.wait_closed()

    async def test_idle_client_cannot_prevent_relay_shutdown(self):
        reader, writer = await asyncio.open_unix_connection(str(self.relay_path))
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 4)
            self.assertEqual(self.process.returncode, 0)
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
            self.assertFalse(self.relay_path.exists())
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_large_thread_reply_does_not_break_64k_reader(self):
        reply = await self.client.read_thread('thread-12345678')
        self.assertEqual(len(reply['contentItems'][0]['text']), 150000)

    async def test_long_running_task_reply_can_exceed_one_megabyte(self):
        self.reply_chars = 1500000
        reply = await self.client.read_thread('thread-12345678')
        self.assertEqual(len(reply['contentItems'][0]['text']), self.reply_chars)

    async def test_starting_second_relay_preserves_first_socket(self):
        self.assertTrue(await self.client.health())
        second = await self.launch()
        stdout, stderr = await asyncio.wait_for(second.communicate(), 5)
        self.assertEqual(second.returncode, 0, stderr)
        self.assertIn(b'already running', stdout)
        self.assertTrue(await self.client.health())

    async def test_malformed_native_response_is_recoverable(self):
        self.malformed_once = True
        with self.assertRaises(AppToolsRelayError):
            await self.client.health()
        self.assertTrue(await self.client.health())

    async def test_context_includes_only_source_dialogue(self):
        self.context_result = {'turns':[{'items':[
            {'type':'reasoning','text':'private reasoning'},
            {'type':'commandExecution','command':'secret command'},
            {'type':'userMessage','content':[{'type':'text','text':'使用红色方案'}]},
            {'type':'agentMessage','phase':'final_answer','text':'红色方案已记录。'},
            {'type':'agentMessage','phase':'commentary','text':'intermediate'}]}]}
        context = await self.client.read_context('thread-12345678')
        self.assertEqual(context['sourceThreadId'], 'thread-12345678')
        self.assertEqual(context['text'], 'user: 使用红色方案\nassistant: 红色方案已记录。')

    async def test_unknown_target_and_wrong_relay_cannot_invoke_native_tools(self):
        for caller, target in [('relay-12345678','not-subscribed'), ('wrong-relay-id','thread-12345678')]:
            with self.assertRaisesRegex(AppToolsRelayError, 'not authorized'):
                await self.client.send_message_to_thread(caller, target, 'offline test')
        with self.assertRaisesRegex(AppToolsRelayError, 'not authorized'):
            await self.client.read_context('not-subscribed')
        self.assertEqual(self.tool_calls, 0)

    async def test_subscription_is_rechecked_after_revocation(self):
        await self.client.send_message_to_thread('relay-12345678','thread-12345678','offline test')
        self.assertEqual(self.tool_calls, 1)
        self.registry['sessions']['thread-12345678']['enabled'] = False
        (self.directory/'sessions.json').write_text(json.dumps(self.registry))
        with self.assertRaisesRegex(AppToolsRelayError, 'not authorized'):
            await self.client.send_message_to_thread('relay-12345678','thread-12345678','offline test')
        self.assertEqual(self.tool_calls, 1)

    async def test_missing_authorization_fails_closed_without_breaking_health(self):
        (self.directory/'sessions.json').write_text('{invalid')
        with self.assertRaisesRegex(AppToolsRelayError, 'authorization state unavailable'):
            await self.client.read_thread('thread-12345678')
        self.assertTrue(await self.client.health())
        self.assertEqual(self.tool_calls, 0)
