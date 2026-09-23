from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codex_rpc import CodexAppServer, CodexRpcError, _classifier_catalog_candidate, _catalog_version_matches, readiness_error_code, phone_control_system_proxy


class PhoneControlRouteTests(unittest.TestCase):
    def test_only_explicit_phone_route_uses_existing_proxy_and_changes_no_files(self):
        root = Path(tempfile.mkdtemp(prefix='phone-control-test-'))
        path = root/'config.json'
        self.assertFalse(phone_control_system_proxy(path))
        for mode in ('unchanged', 'system-proxy'):
            path.write_text(json.dumps({'phone_control_route': mode, 'untouched': True}))
            before = path.read_bytes()
            with patch('socks_media.system_socks_proxy', return_value=('127.0.0.1', 10809)):
                self.assertEqual(phone_control_system_proxy(path), mode == 'system-proxy')
            self.assertEqual(path.read_bytes(), before)
        with patch('socks_media.system_socks_proxy', return_value=None):
            with self.assertRaises(CodexRpcError):
                phone_control_system_proxy(path)
        path.write_text('{"phone_control_route":"unknown"}')
        with self.assertRaises(CodexRpcError):
            phone_control_system_proxy(path)


class ReadinessErrorTests(unittest.TestCase):
    def test_only_exact_routing_error_gets_routing_label(self):
        message = 'workspace routing discovery timed out'
        self.assertEqual(readiness_error_code(CodexRpcError(json.dumps({
            'code': -32603, 'message': message}))), 'workspace_routing_timeout')
        for detail in (message, 'PRIVATE', '[]', json.dumps({'code':401,'message':message}),
                       json.dumps({'code':-32603,'message':'PRIVATE'})):
            self.assertEqual(readiness_error_code(CodexRpcError(detail)), 'codex_readiness_unavailable')
        self.assertEqual(readiness_error_code(TimeoutError('PRIVATE')), 'codex_readiness_timeout')
        self.assertEqual(readiness_error_code(OSError('PRIVATE')), 'codex_readiness_unavailable')


class CatalogCandidateTests(unittest.TestCase):
    def test_desktop_prerelease_matches_only_its_exact_release_cache_key(self):
        for binary, cached, expected in (
            ('0.155.0-alpha.9.2', '0.155.0', True),
            ('0.155.0-alpha.9.2', '0.155.0-alpha.9.2', True),
            ('0.155.0-alpha.9.2', '0.155.0-alpha.9.1', False),
            ('0.155.0-alpha.9.2', '0.155.1', False),
            ('0.155.0-alpha.9.2', '0.154.0', False),
            ('0.155.0', '0.155.0-alpha.9.2', False),
            ('0.155.0+build.1', '0.155.0', True),
            ('0.155.0-alpha.09', '0.155.0', False),
            ('0.155.0-', '0.155.0', False),
            ('0.155.0alpha.9', '0.155.0', False),
            ('00.155.0', '00.155.0', False),
            ('0.155.0 extra', '0.155.0', False),
        ):
            with self.subTest(binary=binary, cached=cached):
                self.assertEqual(_catalog_version_matches('codex-cli ' + binary, cached), expected)
        self.assertFalse(_catalog_version_matches('unknown', 'unknown'))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / 'config.toml'
        self.cache = self.root / 'models_cache.json'
        self.now = datetime.now(timezone.utc)
        self.data = {'fetched_at': self.now.isoformat(), 'client_version': '0.153.4',
                     'models': [{'slug': 'configured-model', 'display_name': 'test',
                                 'supported_reasoning_levels': [],
                                 'model_messages': {'instructions_template': 'synthetic test instructions'},
                                 'test': 'preserve-me'}]}
        self.environment = patch.dict('codex_rpc.os.environ', {'CODEX_HOME': str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.write_cache()

    def write_cache(self):
        self.cache.write_text(json.dumps(self.data))

    def test_recent_catalog_is_read_only_and_never_selects_a_model(self):
        self.config.write_text('model = "configured-model"\n')
        before = self.cache.read_bytes(), self.config.read_bytes()
        result, reason = _classifier_catalog_candidate(self.root, now=self.now)
        self.assertEqual(result, (self.cache, '0.153.4', 0))
        self.assertEqual(reason, 'candidate')
        self.assertEqual(before, (self.cache.read_bytes(), self.config.read_bytes()))

    def test_stale_future_naive_and_invalid_catalogs_use_normal_discovery(self):
        for stamp in (self.now-timedelta(seconds=301), self.now+timedelta(seconds=1),
                      self.now.replace(tzinfo=None)):
            self.data['fetched_at'] = stamp.isoformat()
            self.write_cache()
            self.assertIsNone(_classifier_catalog_candidate(self.root, now=self.now)[0])
        for contents in ('[]', '{}', '{', '{"models": null}'):
            self.cache.write_text(contents)
            self.assertIsNone(_classifier_catalog_candidate(self.root, now=self.now)[0])

    def test_explicit_catalog_provider_unknown_model_or_invalid_config_is_not_overridden(self):
        for content in ('model_catalog_json = "/custom/catalog.json"',
                        'model_provider = "custom"', 'model = "not-in-cache"',
                        '[profiles.custom]\nmodel_catalog_json = "/custom/catalog.json"',
                        'invalid [ TOML'):
            self.config.write_text(content)
            self.assertIsNone(_classifier_catalog_candidate(self.root, now=self.now)[0])

    def test_project_override_is_not_replaced(self):
        project = self.root / 'project'
        (project / '.codex').mkdir(parents=True)
        (project / '.codex/config.toml').write_text('model_catalog_json = "/custom/catalog.json"')
        self.assertIsNone(_classifier_catalog_candidate(project, now=self.now)[0])

    def test_invalid_model_metadata_and_unresolved_scope_are_skipped(self):
        for models in ([], [None], [{}], [{'slug': 'x'}, {'slug': 'x'}]):
            self.data['models'] = models
            self.write_cache()
            self.assertIsNone(_classifier_catalog_candidate(self.root, now=self.now)[0])
        self.assertIsNone(_classifier_catalog_candidate(None, now=self.now)[0])
        self.assertIsNone(_classifier_catalog_candidate('.', now=self.now)[0])


class RpcFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        route = patch('codex_rpc.phone_control_system_proxy', return_value=False)
        route.start()
        self.addCleanup(route.stop)

    async def test_control_route_changes_only_owned_app_server_launch(self):
        process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())
        server = CodexAppServer(respect_system_proxy=True)
        server._read_stdout = AsyncMock()
        server._read_stderr = AsyncMock()
        server.request, server.notify = AsyncMock(), AsyncMock()
        with patch('codex_rpc.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as launch:
            await server.start()
            self.assertEqual(launch.await_args.args, ('codex', 'app-server', '--stdio', '--enable',
                'realtime_conversation', '--enable', 'respect_system_proxy'))
            self.assertNotIn('env', launch.await_args.kwargs)
            await server.close()

    async def test_catalog_requires_matching_running_binary_version(self):
        for output, expected in ((b'codex-cli 0.153.4\n', True),
                                 (b'codex-cli 0.153.4-alpha.9.2\n', True),
                                 (b'codex-cli 0.154.0\n', False), (b'unknown\n', False)):
            server = CodexAppServer(capability_profile='classifier')
            process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output, b'')))
            with patch('codex_rpc._classifier_catalog_candidate', return_value=((Path('/cache.json'), '0.153.4', 1), 'candidate')), \
                 patch('codex_rpc.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
                options = await server._classifier_catalog_options()
            launch.assert_awaited_once_with('codex', '--version', stdout=asyncio.subprocess.PIPE,
                                           stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(bool(options), expected)
            self.assertNotIn('model=', ' '.join(options))
            if expected:
                self.assertEqual(options, ['-c', 'model_catalog_json="/cache.json"'])
                self.assertEqual(server.classifier_catalog_diagnostics['mode'], 'recent_codex_cache')

    async def test_cancellation_reaps_owned_version_probe_and_never_launches_server(self):
        server = CodexAppServer(capability_profile='classifier')
        entered = asyncio.Event()
        async def wait_forever():
            entered.set()
            await asyncio.Event().wait()
        process = SimpleNamespace(returncode=None, communicate=wait_forever, kill=Mock(), wait=AsyncMock())
        with patch('codex_rpc._classifier_catalog_candidate', return_value=((Path('/cache.json'), '0.153.4', 1), 'candidate')), \
             patch('codex_rpc.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
            task = asyncio.create_task(server.start())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertEqual(launch.await_count, 1)
        self.assertIsNone(server.process)

    async def test_classifier_process_excludes_catalog_before_initialize_only(self):
        for profile in ('conversation', 'classifier'):
            process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())
            server = CodexAppServer(capability_profile=profile)
            server._read_stdout = AsyncMock()
            server._read_stderr = AsyncMock()
            server.request, server.notify = AsyncMock(), AsyncMock()
            with patch('codex_rpc.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as launch:
                await server.start()
                args = launch.await_args.args
                self.assertEqual(args[:5], ('codex', 'app-server', '--stdio', '--enable', 'realtime_conversation'))
                if profile == 'classifier':
                    overrides = args[5:]
                    self.assertEqual(overrides[::2], ('-c',) * 5)
                    self.assertEqual(set(overrides[1::2]), {
                        'features.apps=false', 'features.plugins=false', 'features.remote_plugin=false',
                        'skills.max_context_tokens=1', 'features.shell_snapshot=false'})
                else:
                    self.assertEqual(len(args), 5)
                self.assertNotIn('env', launch.await_args.kwargs)
                self.assertNotIn('--model', args)
                server.request.assert_awaited_once()
                self.assertEqual(server.request.await_args.args[0], 'initialize')
                await server.close()
                process.terminate.assert_called_once()

    async def test_unknown_process_profile_fails_before_launch(self):
        with self.assertRaises(ValueError):
            CodexAppServer(capability_profile='unvalidated')

    async def test_failed_send_does_not_leak_pending_request(self):
        server = CodexAppServer()
        server._send = AsyncMock(side_effect=BrokenPipeError())
        with self.assertRaises(BrokenPipeError):
            await server.request('test')
        self.assertEqual(server._pending, {})

    async def test_send_itself_has_a_deadline(self):
        server = CodexAppServer()
        async def hang(_): await asyncio.Event().wait()
        server._send = hang
        with self.assertRaises(TimeoutError):
            await server.request('test', timeout=.02)
        self.assertEqual(server._pending, {})

    async def test_reader_failure_wakes_all_waiters(self):
        for data in (b'[]\n', b'x'*4096+b'\n'):
            server = CodexAppServer()
            stream = asyncio.StreamReader(limit=2048)
            stream.feed_data(data)
            server.process = SimpleNamespace(stdout=stream)
            futures = [asyncio.get_running_loop().create_future() for _ in range(2)]
            server._pending = dict(enumerate(futures))
            await server._read_stdout()
            self.assertTrue(server.closed.is_set())
            for future in futures:
                with self.assertRaises(CodexRpcError): await future
            self.assertIn('reader failed', server.last_error)

    async def test_eof_wakes_waiter_and_fails_new_send(self):
        server = CodexAppServer()
        stream = asyncio.StreamReader()
        stream.feed_eof()
        server.process = SimpleNamespace(stdout=stream)
        await server._read_stdout()
        with self.assertRaises(CodexRpcError): await server.request('new')
        self.assertEqual(server._pending, {})


if __name__ == '__main__': unittest.main()
