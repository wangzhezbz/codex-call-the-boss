import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service_state import record_state
from runtime_health import export_diagnostics


class DiagnosticExportTests(unittest.TestCase):
    def test_corrupted_metadata_cannot_smuggle_contents_into_export(self):
        root = Path(tempfile.mkdtemp(prefix='phone-export-corrupt-'))
        rows = [{'job_id':'opaque', 'phase':'SECRET', 'reason':'SECRET', 'updated_at':'SECRET',
                 'notification':{'state':'SECRET'}}, {'phase':['SECRET'], 'notification':'SECRET'}]
        with patch('runtime_health.recent_status', return_value=rows), \
                patch('runtime_health.compatibility', return_value={}):
            result = export_diagnostics(root, root/'export.json')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(result['calls']), 1)

    def test_only_allowlisted_metadata_is_exported_without_overwriting(self):
        root = Path(tempfile.mkdtemp(prefix='phone-export-test-'))
        job = {'turn_id':'private-turn-id','thread_id':'private-source-id','phone_transcript':'SECRET'}
        record_state(root, job, 'failed', reason='report_preparation')
        output = root/'export.json'
        with patch('runtime_health.compatibility', return_value={'audio_quality_verified':False}):
            export_diagnostics(root, output, 'private-source-id')
            with self.assertRaises(FileExistsError): export_diagnostics(root, output)
        text = output.read_text()
        for value in ('private-turn-id','private-source-id','SECRET'):
            self.assertNotIn(value, text)
        self.assertFalse(json.loads(text)['contains_audio'])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
