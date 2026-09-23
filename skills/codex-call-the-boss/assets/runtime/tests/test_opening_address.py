from array import array
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import native_speech
from native_speech import NativeSpeechRenderer, NativeClipRejectedError
import opening_address as address
import phone_agent
from speech_quality import ALIGNMENT_REVISION


def fixture(directory, accepted=True):
    cache = Path(directory)
    key = 'a' * 64
    # Complete synthetic address, a long quiet boundary, then a report body.
    pcm = (bytes(1920) + array('h', [1000, -1000] * 9600).tobytes()
           + bytes(24960) + array('h', [2000, -2000] * 24000).tobytes())
    meta = {'text': '老板，任务已经完成。', 'model_transcript': '老板，任务已经完成。',
            'voice': 'cove', 'passed': True, 'script_similarity': 1.0, 'sha256': address.digest(pcm)}
    (cache/(key+'.pcm')).write_bytes(pcm)
    (cache/(key+'.wav')).write_bytes(address.wav_bytes(pcm))
    (cache/(key+'.json')).write_text(json.dumps(meta))
    excerpt, proof = address.read_source(cache, 'cove', key, address.digest(pcm), 28800)
    folder = address.asset_dir(cache, proof['pcm_sha256'])
    folder.mkdir(parents=True)
    asset = {**proof, 'quality': {'passed': True, 'script_similarity': 1.0}}
    raw = json.dumps(asset).encode()
    (folder/'asset.json').write_bytes(raw)
    (folder/'address.pcm').write_bytes(excerpt)
    (folder/'address.wav').write_bytes(address.wav_bytes(excerpt))
    if accepted:
        (folder/'acceptance.json').write_text(json.dumps({'kind': 'explicit_listener_acceptance',
            'scope': 'opening_address_only', 'pcm_sha256': proof['pcm_sha256'],
            'asset_sha256': address.digest(raw)}))
    return excerpt, proof, folder


def report_pcm(body):
    return b'\x00\x10'*19200 + bytes(19200) + body


class AddressAssetTests(unittest.TestCase):
    def test_body_boundary_is_one_quiet_gap_not_a_word_timestamp_guess(self):
        self.assertEqual(address.report_body_start(report_pcm(b'\x00\x20'*48000)), 24000)
        with self.assertRaises(address.OpeningBoundaryUnavailable):
            address.report_body_start(b'\x00\x20'*96000)
    def test_exact_accepted_clip_roundtrips_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            pcm, proof, _ = fixture(directory)
            source = Path(directory)/(proof['source_key']+'.pcm')
            before = source.read_bytes()
            actual, identity = address.load(directory, 'cove', proof['pcm_sha256'])
            self.assertEqual(actual, pcm)
            self.assertEqual(identity['pcm_sha256'], address.digest(pcm))
            self.assertEqual(source.read_bytes(), before)

    def test_unaccepted_candidate_never_becomes_a_production_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            pcm, proof, _ = fixture(directory, accepted=False)
            self.assertEqual(address.load(directory, 'cove', proof['pcm_sha256'], require_acceptance=False)[0], pcm)
            with self.assertRaises(FileNotFoundError):
                address.load(directory, 'cove', proof['pcm_sha256'])

    def test_changed_source_asset_or_acceptance_fails_closed(self):
        for mutation in ('source_pcm', 'source_metadata', 'address_pcm', 'asset_quality', 'acceptance_scope', 'acceptance_hash'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                _, proof, folder = fixture(directory)
                source = Path(directory)/(proof['source_key']+'.json')
                if mutation == 'source_pcm':
                    source.with_suffix('.pcm').write_bytes(b'bad')
                elif mutation == 'source_metadata':
                    source.write_bytes(source.read_bytes()+b' ')
                elif mutation == 'address_pcm':
                    (folder/'address.pcm').write_bytes(b'bad')
                elif mutation == 'asset_quality':
                    meta = json.loads((folder/'asset.json').read_bytes())
                    meta['quality']['passed'] = False
                    (folder/'asset.json').write_text(json.dumps(meta))
                else:
                    meta = json.loads((folder/'acceptance.json').read_bytes())
                    meta['scope' if mutation == 'acceptance_scope' else 'pcm_sha256'] = 'wrong'
                    (folder/'acceptance.json').write_text(json.dumps(meta))
                with self.assertRaises((ValueError, OSError)):
                    address.load(directory, 'cove', proof['pcm_sha256'])

    def test_voice_mismatch_or_either_listener_veto_blocks_reuse(self):
        for kind in ('voice', 'source', 'address'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                _, proof, _ = fixture(directory)
                if kind != 'voice':
                    sha = proof['source_pcm_sha256' if kind == 'source' else 'pcm_sha256']
                    marker = Path(directory)/'listener-rejections'/sha/'rejection.json'
                    marker.parent.mkdir(parents=True)
                    marker.write_text('{}')
                with self.assertRaises(ValueError):
                    address.load(directory, 'arbor' if kind == 'voice' else 'cove', proof['pcm_sha256'])

    def test_rejected_or_diagnostic_source_and_bad_boundary_are_not_eligible(self):
        for kind in ('failed', 'diagnostic', 'voiced_boundary', 'invalid_frame', 'path_escape'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                _, proof, _ = fixture(directory)
                path = Path(directory)/(proof['source_key']+'.json')
                meta = json.loads(path.read_bytes())
                if kind == 'failed':
                    meta['passed'] = False
                elif kind == 'diagnostic':
                    meta['diagnostic_only'] = True
                path.write_text(json.dumps(meta))
                with self.assertRaises(ValueError):
                    address.read_source(directory, 'cove', '../outside' if kind == 'path_escape' else proof['source_key'],
                        proof['source_pcm_sha256'], True if kind == 'invalid_frame' else 19200 if kind == 'voiced_boundary' else 28800)


class PinnedOpeningTests(unittest.IsolatedAsyncioTestCase):
    def renderer(self, directory, digest):
        quality = AsyncMock(return_value={'passed': True, 'script_similarity': 1.0,
                                          'alignment_revision': ALIGNMENT_REVISION})
        renderer = NativeSpeechRenderer(voice='cove', cache_dir=directory, trim=lambda pcm: pcm, validate=quality)
        renderer.opening_address_sha256 = digest
        return renderer, quality

    def source_renderer(self, directory):
        renderer, quality = self.renderer(directory, '')
        renderer.opening_join_pause = True
        return renderer, quality

    async def test_two_dynamic_reports_have_the_same_exact_address_and_unmodified_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, _ = fixture(directory)
            renderer, validate = self.renderer(directory, proof['pcm_sha256'])
            bodies = {'第一项已经完成。': b'\x00\x10'*48000,
                      '第二项仍需处理。': b'\x00\x20'*48000}
            generated = []
            async def generate(child, text):
                generated.append(text)
                return report_pcm(bodies[text[3:]]), text
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                for body_text, body in bodies.items():
                    full = '老板，'+body_text
                    self.assertEqual(await renderer.synthesize(full), prefix+bytes(9600)+body)
                    self.assertEqual(renderer.cached(full), prefix+bytes(9600)+body)
                    meta = json.loads(renderer.paths(full)[1].read_bytes())
                    self.assertEqual(meta['opening_composition']['body_text'], body_text)
                    self.assertEqual(meta['opening_composition']['address']['pcm_sha256'], proof['pcm_sha256'])
            self.assertEqual(generated, ['老板，'+body for body in bodies])
            self.assertEqual(validate.await_count, 6)  # Original full, suffix, assembled full.
            self.assertEqual([c.args[1] for c in validate.await_args_list],
                             ['老板，第一项已经完成。', '第一项已经完成。', '老板，第一项已经完成。',
                              '老板，第二项仍需处理。', '第二项仍需处理。', '老板，第二项仍需处理。'])

    async def test_cached_full_report_reuses_only_the_exact_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, _ = fixture(directory)
            renderer, _ = self.renderer(directory, proof['pcm_sha256'])
            text, pcm = '老板，任务完成。', b'\x00\x10'*48000
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(return_value=(report_pcm(pcm), text))) as generate:
                await renderer.synthesize(text)
                renderer.cache_only = True
                self.assertEqual(await renderer.synthesize(text), prefix+bytes(9600)+pcm)
                generate.assert_awaited_once()
            original, _ = self.source_renderer(directory)
            body_meta = original.paths(text)[1]
            body_meta.write_bytes(body_meta.read_bytes()+b' ')
            self.assertIsNone(renderer.cached(text))
            with self.assertRaisesRegex(RuntimeError, 'not prepared'):
                await renderer.synthesize(text)

    async def test_full_composed_script_gate_cannot_be_bypassed_by_passing_components(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            quality.side_effect = [{'passed': True, 'script_similarity': 1.0},
                                   {'passed': True, 'script_similarity': 1.0},
                                   {'passed': False, 'script_similarity': 1.0}]
            text, pcm = '老板，任务完成。', b'\x00\x10'*48000
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(return_value=(report_pcm(pcm), text))):
                with self.assertRaises(NativeClipRejectedError):
                    await renderer.synthesize(text)
            self.assertFalse(renderer.paths(text)[0].exists())
            self.assertIsNone(renderer.cached(text))
            original, _ = self.source_renderer(directory)
            self.assertEqual(original.cached(text), report_pcm(pcm))
            self.assertEqual(len(renderer.rejected_evidence), 1)
            calls = quality.await_count
            with self.assertRaisesRegex(RuntimeError, 'already rejected'):
                await renderer.synthesize(text)
            self.assertEqual(quality.await_count, calls)

    async def test_missing_acceptance_stops_before_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory, accepted=False)
            renderer, _ = self.renderer(directory, proof['pcm_sha256'])
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock()) as generate:
                with self.assertRaises(FileNotFoundError):
                    await renderer.synthesize('老板，任务完成。')
                generate.assert_not_awaited()

    async def test_prepare_retry_renders_fresh_body_without_overwriting_first_source(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, folder = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            ok = {'passed': True, 'script_similarity': 1.0}
            quality.side_effect = [ok, ok, {'passed': False, 'script_similarity': 1.0}, ok, ok, ok]
            text = '老板，任务完成。'
            first, second = (report_pcm(b'\x00\x10'*48000), report_pcm(b'\x00\x20'*48000))
            generate = AsyncMock(side_effect=[(first, text), (second, text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(quality.await_count, 6)
            self.assertFalse(renderer._fresh_opening_source)
            self.assertEqual(renderer.cached(text), prefix+second[48000:])
            original, _ = self.source_renderer(directory)
            self.assertEqual(original.cached(text), first)
            metadata = json.loads(renderer.paths(text)[1].read_bytes())
            composition = metadata['opening_composition']
            self.assertEqual(composition['kind'], 'fixed_address_dynamic_body_v2')
            source = renderer._opening_source_cache(composition['source_cache_id'])
            retry, _ = self.source_renderer(source)
            self.assertEqual(retry.cached(text), second)
            self.assertTrue((Path(directory)/composition['body_review_file']).is_file())
            self.assertEqual(len(list((folder/'rejected-compositions').glob('*.json'))), 1)
            self.assertTrue(all((Path(directory)/p).is_file() for p in renderer.rejected_evidence))
            self.assertEqual((folder/'address.pcm').read_bytes(), prefix)
            # Restarted readers use the exact new source, not the old key.
            restarted, _ = self.renderer(directory, proof['pcm_sha256'])
            self.assertEqual(restarted.cached(text), renderer.cached(text))
            retry_meta = retry.paths(text)[1]
            retry_meta.write_bytes(retry_meta.read_bytes()+b' ')
            self.assertIsNone(restarted.cached(text))

    async def test_prepare_two_failed_compositions_never_try_a_third_source(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, folder = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            ok, bad = {'passed': True, 'script_similarity': 1.0}, {'passed': False, 'script_similarity': 1.0}
            quality.side_effect = [ok, ok, bad, ok, ok, bad]
            text = '老板，任务完成。'
            generate = AsyncMock(side_effect=[(report_pcm(b'\x00\x10'*48000), text),
                                               (report_pcm(b'\x00\x20'*48000), text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                with self.assertRaises(NativeClipRejectedError):
                    await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(len(list((folder/'rejected-compositions').glob('*.json'))), 2)
            self.assertIsNone(renderer.cached(text))
            self.assertFalse(renderer._fresh_opening_source)

    async def test_missing_boundary_uses_one_fresh_source_and_preserves_original_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, folder = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            text = '老板，任务完成。'
            first, second = b'\x00\x20'*96000, report_pcm(b'\x00\x10'*48000)
            generate = AsyncMock(side_effect=[(first, text), (second, text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual([call.args[1] for call in quality.await_args_list],
                             [text, text, '任务完成。', text])
            original, _ = self.source_renderer(directory)
            self.assertEqual(original.cached(text), first)
            self.assertEqual(renderer.cached(text), prefix+second[48000:])
            self.assertEqual((folder/'address.pcm').read_bytes(), prefix)
            vetoes = list((folder/'rejected-compositions').glob('*.json'))
            self.assertEqual(len(vetoes), 1)
            veto = json.loads(vetoes[0].read_bytes())
            self.assertEqual(veto['kind'], 'opening_boundary_rejection_v1')
            self.assertEqual(veto['source_pcm_sha256'], address.digest(first))
            metadata = Path(directory)/veto['source_metadata']
            self.assertEqual(veto['source_metadata_sha256'], address.digest(metadata.read_bytes()))
            self.assertTrue(veto['waveform_unchanged'])
            self.assertEqual(renderer.rejected_evidence, [str(vetoes[0].relative_to(directory))])
            composition = json.loads(renderer.paths(text)[1].read_bytes())['opening_composition']
            retry, _ = self.source_renderer(renderer._opening_source_cache(composition['source_cache_id']))
            self.assertEqual(retry.cached(text), second)
            restarted, _ = self.renderer(directory, proof['pcm_sha256'])
            self.assertEqual(restarted.cached(text), prefix+second[48000:])

    async def test_two_missing_boundaries_reject_without_cutting_or_a_third_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, folder = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            text = '老板，任务完成。'
            generate = AsyncMock(side_effect=[(b'\x00\x20'*96000, text),
                                              (b'\x00\x10'*96000, text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                with self.assertRaisesRegex(NativeClipRejectedError, 'no safe address/body boundary'):
                    await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(quality.await_count, 2)  # Only the two original full sources.
            self.assertEqual(len(renderer.rejected_evidence), 2)
            self.assertTrue(all((Path(directory)/name).is_file() for name in renderer.rejected_evidence))
            self.assertEqual(len(list((folder/'rejected-compositions').glob('*.json'))), 2)
            self.assertFalse(renderer.paths(text)[0].exists())
            self.assertIsNone(renderer.cached(text))
            self.assertFalse(renderer._fresh_opening_source)
            self.assertFalse(renderer.cache_only)
            self.assertEqual((folder/'address.pcm').read_bytes(), prefix)

    async def test_boundary_veto_blocks_same_source_without_regeneration_or_evidence_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, folder = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            text = '老板，任务完成。'
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    return_value=(b'\x00\x20'*96000, text))) as generate:
                with self.assertRaisesRegex(NativeClipRejectedError, 'no safe address/body boundary'):
                    await renderer.synthesize(text)
                veto = next((folder/'rejected-compositions').glob('*.json'))
                original_evidence = veto.read_bytes()
                with self.assertRaisesRegex(NativeClipRejectedError, 'already rejected'):
                    await renderer.synthesize(text)
            self.assertEqual(generate.await_count, 1)
            self.assertEqual(quality.await_count, 1)
            self.assertEqual(veto.read_bytes(), original_evidence)
            self.assertIsNone(renderer.cached(text))

    async def test_unrelated_boundary_error_does_not_trigger_fresh_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, folder = fixture(directory)
            renderer, _ = self.renderer(directory, proof['pcm_sha256'])
            text = '老板，任务完成。'
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    return_value=(report_pcm(b'\x00\x20'*48000), text))) as generate, \
                    patch.object(address, 'report_body_start', side_effect=ValueError('invalid configuration')):
                with self.assertRaisesRegex(ValueError, 'invalid configuration'):
                    await renderer.prepare([text])
            self.assertEqual(generate.await_count, 1)
            self.assertFalse((folder/'rejected-compositions').exists())
            self.assertFalse(renderer._fresh_opening_source)

    async def test_cached_composition_veto_allows_only_the_existing_one_fresh_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            ok = {'passed': True, 'script_similarity': 1.0}
            quality.side_effect = [ok, ok, {'passed': False, 'script_similarity': 1.0}, ok, ok, ok]
            text = '老板，任务完成。'
            generate = AsyncMock(side_effect=[(report_pcm(b'\x00\x10'*48000), text),
                                               (report_pcm(b'\x00\x20'*48000), text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                with self.assertRaises(NativeClipRejectedError):
                    await renderer.synthesize(text)
                rejected = [(p, (Path(directory)/p).read_bytes()) for p in renderer.rejected_evidence]
                await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertTrue(all((Path(directory)/p).read_bytes() == content for p, content in rejected))
            self.assertIsNotNone(renderer.cached(text))

    async def test_new_source_cannot_bypass_global_listener_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, quality = self.renderer(directory, proof['pcm_sha256'])
            ok = {'passed': True, 'script_similarity': 1.0}
            quality.side_effect = [ok, ok, {'passed': False, 'script_similarity': 1.0}, ok]
            text, rejected_source = '老板，任务完成。', report_pcm(b'\x00\x20'*48000)
            veto = renderer._listener_rejection_path(address.digest(rejected_source))
            veto.parent.mkdir(parents=True)
            veto.write_text('{}')
            generate = AsyncMock(side_effect=[(report_pcm(b'\x00\x10'*48000), text), (rejected_source, text)])
            with patch.object(NativeSpeechRenderer, '_generate', new=generate):
                with self.assertRaisesRegex(NativeClipRejectedError, 'rejected by its listener'):
                    await renderer.prepare([text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(quality.await_count, 4)
            self.assertIsNone(renderer.cached(text))

    def test_retry_source_namespace_rejects_escaping_or_linked_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer, _ = self.renderer(directory, '')
            for source_id in ('', '../outside', 'f'*64, True, '/tmp/elsewhere'):
                self.assertIsNone(renderer._opening_source_cache(source_id))
            parent = Path(directory)/'opening-source-attempts'
            parent.symlink_to(Path(directory), target_is_directory=True)
            self.assertIsNone(renderer._opening_source_cache('a'*32))

    def test_notices_and_unconfigured_voices_keep_existing_cache_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            pinned, _ = self.renderer(directory, proof['pcm_sha256'])
            plain, _ = self.renderer(directory, '')
            for text in native_speech.NOTICE_TEXTS:
                self.assertEqual(pinned.paths(text), plain.paths(text))
            self.assertNotEqual(pinned.paths('老板，任务完成。'), plain.paths('老板，任务完成。'))

    def test_join_pause_versions_only_opening_sources_and_their_compositions(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            pinned, _ = self.renderer(directory, proof['pcm_sha256'])
            plain, _ = self.renderer(directory, '')
            source, _ = self.source_renderer(directory)
            text = '老板，任务完成。'
            self.assertEqual(len({pinned.paths(text), plain.paths(text), source.paths(text)}), 3)
            self.assertEqual(pinned._opening_proof(text)['body_generation'], source._opening_proof(text))
            self.assertEqual(source._opening_proof(text)['revision'],
                             native_speech.OPENING_PRONUNCIATION_REVISION+'+join-pause-v1')
            for notice in native_speech.NOTICE_TEXTS:
                self.assertEqual(source.paths(notice), plain.paths(notice))
                self.assertEqual(source._opening_proof(notice), {})

    async def test_empty_or_duplicate_address_body_never_generates(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, _ = self.renderer(directory, proof['pcm_sha256'])
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock()) as generate:
                for text in ('老板，', '老板，老板，任务完成。'):
                    with self.assertRaises(ValueError):
                        await renderer.synthesize(text)
                generate.assert_not_awaited()

    async def test_selection_requires_explicit_confirmation_before_any_read(self):
        with patch.object(phone_agent, 'load_config') as config:
            with self.assertRaises(ValueError):
                await phone_agent.set_opening_address('x', 'y', 28800, 'z')
            config.assert_not_called()

    async def test_selected_hash_must_match_the_reviewed_excerpt_before_config_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            cache = state/'native-speech'
            cache.mkdir()
            _, proof, _ = fixture(cache, accepted=False)
            with patch.object(phone_agent, 'STATE_DIR', state), \
                    patch.object(phone_agent, 'load_config', return_value={'voice': 'cove', 'phone_voice_renderer': 'realtime-unified'}), \
                    patch.object(phone_agent, '_atomic_write_json') as write:
                with self.assertRaises(ValueError):
                    await phone_agent.set_opening_address(proof['source_key'], proof['source_pcm_sha256'],
                                                         28800, 'b'*64, confirmed=True)
                write.assert_not_called()


class IndependentBodyTests(unittest.IsolatedAsyncioTestCase):
    renderer = PinnedOpeningTests.renderer
    def independent_renderer(self, directory, digest):
        renderer, quality = self.renderer(directory, digest)
        renderer.opening_mode = 'fixed-body'
        return renderer, quality

    async def test_independent_body_never_generates_or_cuts_another_address(self):
        with tempfile.TemporaryDirectory() as directory:
            address_pcm, proof, _ = fixture(directory)
            renderer, quality = self.independent_renderer(directory, proof['pcm_sha256'])
            body_text = '开场播报已经调整。'
            text, body = '老板，'+body_text, b'\x00\x20'*96000
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    return_value=(body, body_text))) as generate, \
                    patch.object(address, 'report_body_start', side_effect=AssertionError('must not cut')):
                self.assertEqual(await renderer.synthesize(text), address_pcm+body)
                self.assertEqual(renderer.cached(text), address_pcm+body)
            generate.assert_awaited_once_with(body_text)
            self.assertEqual([call.args[1] for call in quality.await_args_list], [body_text, text])
            meta = json.loads(renderer.paths(text)[1].read_bytes())
            self.assertEqual(meta['model_transcript'], text)
            self.assertEqual(meta['opening_composition']['kind'], 'fixed_address_independent_body_v1')
            self.assertEqual(meta['opening_composition']['body_start_frame'], 0)

    async def test_independent_cache_revalidates_complete_source_and_listener_veto(self):
        for mutation in ('metadata', 'body', 'address', 'listener', 'diagnostic', 'source_link', 'mode'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                address_pcm, proof, folder = fixture(directory)
                renderer, _ = self.independent_renderer(directory, proof['pcm_sha256'])
                text, body_text, body = '老板，任务已完成。', '任务已完成。', b'\x00\x20'*96000
                with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(return_value=(body, body_text))):
                    await renderer.synthesize(text)
                meta_path = renderer.paths(text)[1]
                composition = json.loads(meta_path.read_bytes())['opening_composition']
                source_dir = renderer._opening_source_cache(composition['source_cache_id'])
                original, _ = self.renderer(source_dir, '')
                pcm_path, source_meta = original.paths(body_text)
                if mutation == 'metadata':
                    source_meta.write_bytes(source_meta.read_bytes()+b' ')
                elif mutation == 'body':
                    pcm_path.write_bytes(b'changed')
                elif mutation == 'address':
                    (folder/'address.pcm').write_bytes(b'changed')
                elif mutation == 'listener':
                    veto = renderer._listener_rejection_path(address.digest(body))
                    veto.parent.mkdir(parents=True)
                    veto.write_text('{}')
                elif mutation == 'diagnostic':
                    data = json.loads(source_meta.read_bytes())
                    data['diagnostic_only'] = True
                    source_meta.write_text(json.dumps(data))
                    # Even internally consistent metadata is not promotion.
                    meta = json.loads(meta_path.read_bytes())
                    meta['opening_composition']['source_metadata_sha256'] = address.digest(source_meta.read_bytes())
                    meta_path.write_text(json.dumps(meta))
                elif mutation == 'source_link':
                    moved = source_dir.with_name(source_dir.name+'-preserved')
                    source_dir.rename(moved)
                    source_dir.symlink_to(moved, target_is_directory=True)
                else:
                    renderer.opening_mode = 'full-source'
                try:
                    value = renderer.cached(text)
                except ValueError:
                    value = None
                self.assertIsNone(value)

    async def test_independent_failed_body_never_assembles_or_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, quality = self.independent_renderer(directory, proof['pcm_sha256'])
            quality.return_value = {'passed': False, 'script_similarity': 1.0}
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    return_value=(b'\x00\x20'*96000, '任务已经完成。'))) as generate:
                with self.assertRaises(NativeClipRejectedError):
                    await renderer.prepare(['老板，任务已经完成。'])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(quality.await_count, 2)
            self.assertIsNone(renderer.cached('老板，任务已经完成。'))

    async def test_independent_failed_whole_gets_only_one_fresh_body(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix, proof, _ = fixture(directory)
            renderer, quality = self.independent_renderer(directory, proof['pcm_sha256'])
            ok = {'passed': True, 'script_similarity': 1.0, 'alignment_revision': ALIGNMENT_REVISION}
            quality.side_effect = [ok, {'passed': False}, ok, ok]
            body_text, first, second = '任务已经完成。', b'\x00\x20'*96000, b'\x00\x30'*96000
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    side_effect=[(first, body_text), (second, body_text)])) as generate:
                await renderer.prepare(['老板，'+body_text])
            self.assertEqual(generate.await_count, 2)
            self.assertEqual(renderer.cached('老板，'+body_text), prefix+second)
            self.assertEqual(len(list((Path(directory)/'opening-source-attempts').iterdir())), 2)
            self.assertTrue(all((Path(directory)/p).is_file() for p in renderer.rejected_evidence))

    async def test_independent_two_failed_wholes_remain_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            renderer, quality = self.independent_renderer(directory, proof['pcm_sha256'])
            quality.side_effect = [{'passed': True, 'script_similarity': 1.0}, {'passed': False}]*2
            with patch.object(NativeSpeechRenderer, '_generate', new=AsyncMock(
                    return_value=(b'\x00\x20'*96000, '任务已经完成。'))) as generate:
                with self.assertRaises(NativeClipRejectedError):
                    await renderer.prepare(['老板，任务已经完成。'])
            self.assertEqual(generate.await_count, 2)
            self.assertIsNone(renderer.cached('老板，任务已经完成。'))

    def test_independent_mode_separates_reports_but_never_invalidates_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            _, proof, _ = fixture(directory)
            new, _ = self.independent_renderer(directory, proof['pcm_sha256'])
            old, _ = self.renderer(directory, proof['pcm_sha256'])
            self.assertNotEqual(new.paths('老板，任务完成。'), old.paths('老板，任务完成。'))
            for text in native_speech.NOTICE_TEXTS:
                self.assertEqual(new.paths(text), old.paths(text))

    def test_factory_defaults_to_old_mode_and_refuses_invalid_or_missing_address(self):
        with patch.object(phone_agent, 'phone_media_proxy', return_value=None):
            cfg = {'voice': 'cove', 'phone_opening_address_sha256': 'a'*64}
            self.assertEqual(phone_agent.native_speech_renderer(cfg).opening_mode, 'full-source')
            cfg['phone_opening_mode'] = 'fixed-body'
            self.assertEqual(phone_agent.native_speech_renderer(cfg).opening_mode, 'fixed-body')
            for mode, digest in (('unknown', 'a'*64), ('fixed-body', '')):
                with self.assertRaises(ValueError):
                    phone_agent.native_speech_renderer({**cfg, 'phone_opening_mode': mode,
                                                       'phone_opening_address_sha256': digest})

    def test_mode_switch_requires_confirmation_before_reading_or_writing(self):
        with patch.object(phone_agent, 'load_config') as load, \
                patch.object(phone_agent, '_atomic_write_json') as write:
            with self.assertRaises(ValueError):
                phone_agent.set_opening_mode('fixed-body')
            with self.assertRaises(ValueError):
                phone_agent.set_opening_mode('unknown', confirmed=True)
            load.assert_not_called()
            write.assert_not_called()

    def test_mode_switch_preserves_voice_assets_and_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            cache = state/'native-speech'
            cache.mkdir()
            _, proof, _ = fixture(cache)
            cfg = {'voice': 'cove', 'phone_voice_renderer': 'realtime-unified',
                   'phone_opening_address_sha256': proof['pcm_sha256'], 'enabled': True,
                   'phone_media_route': 'system-socks'}
            for mode in ('fixed-body', 'full-source'):
                with patch.object(phone_agent, 'STATE_DIR', state), \
                        patch.object(phone_agent, 'load_config', return_value=dict(cfg)), \
                        patch.object(phone_agent, '_atomic_write_json') as write, patch('builtins.print'):
                    self.assertEqual(phone_agent.set_opening_mode(mode, confirmed=True), 0)
                    self.assertEqual(write.call_args.args[1], {**cfg, 'phone_opening_mode': mode})

    def test_mode_switch_refuses_pending_or_unconfirmed_lines(self):
        for relative in ('active-call.json', 'phone-line-unconfirmed.json', 'queue/report.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                marker = state/relative
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text('{}')
                with patch.object(phone_agent, 'STATE_DIR', state), \
                        patch.object(phone_agent, '_atomic_write_json') as write:
                    with self.assertRaises(RuntimeError):
                        phone_agent.set_opening_mode('fixed-body', confirmed=True)
                    write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
