"""Temporary source-only generation, exclusive writes and retained failures."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import execution_components_v4 as source, execution_current_v4 as current
from tests.test_execution_readers_v4 import fixture_files

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_generator_v4', ROOT / 'scripts/build_execution_profile_snapshot_v4.py')
generator = importlib.util.module_from_spec(spec); spec.loader.exec_module(generator)


class SourceSnapshotV4GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'; self.root.mkdir()
        self.files, self.expected = fixture_files()
        for name, payload in self.files.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)

    def invoke(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            code = generator.main(['--project-root', str(self.root), *args])
        return code, json.loads(stream.getvalue())

    def test_preview_creates_nothing_and_returns_source_only_identity(self):
        code, result = self.invoke()
        self.assertEqual(code, 0); self.assertEqual(result['status'], 'preview_source_integrity_only')
        self.assertEqual(result['created_paths'], [])
        self.assertEqual(result['manifest_sha256'], source._sha(self.expected.manifest))
        for name in source.PROFILE_PATHS['first_live']: self.assertFalse((self.root / name).exists())
        self.assertFalse(result['online_execution_authorized']); self.assertFalse(result['runtime_admission_verified'])

    def test_write_verify_and_existing_file_refusal(self):
        self.assertEqual(self.invoke('--write')[0], 0)
        self.assertEqual(self.invoke('--verify')[0], 0)
        before = {name: (self.root / name).read_bytes() for name in source.PROFILE_PATHS['first_live']}
        with patch.object(generator, 'build_current_timed_profile_documents', side_effect=AssertionError('must refuse before building')) as build:
            code, result = self.invoke('--write')
        self.assertEqual(code, 2); self.assertEqual(result['error_code'], 'source_artifact_already_exists')
        self.assertEqual(result['created_paths'], []); self.assertEqual(build.call_count, 0)
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})

    def test_partial_write_is_retained_and_named_without_exception_body(self):
        actual = generator.os.write; calls = []
        def failed(descriptor, payload):
            if not calls:
                calls.append(True); return actual(descriptor, payload[:11])
            raise OSError('synthetic private exception body must not be returned')
        with patch.object(generator.os, 'write', side_effect=failed):
            code, result = self.invoke('--write')
        manifest, plan = source.PROFILE_PATHS['first_live']
        self.assertEqual(code, 2); self.assertEqual(result['created_paths'], [manifest])
        self.assertEqual((self.root / manifest).read_bytes(), self.expected.manifest[:11])
        self.assertFalse((self.root / plan).exists())
        self.assertNotIn('private exception', json.dumps(result))

    def test_source_drift_during_write_keeps_both_outputs_but_never_reports_success(self):
        actual = generator._write_new
        def changed(root, relative, payload, created):
            actual(root, relative, payload, created)
            if relative == source.PROFILE_PATHS['first_live'][0]:
                path = root / 'src/researchops/__init__.py'; path.write_bytes(path.read_bytes() + b'\n# synthetic drift\n')
        with patch.object(generator, '_write_new', side_effect=changed):
            code, result = self.invoke('--write')
        self.assertEqual(code, 2); self.assertEqual(result['created_paths'], list(source.PROFILE_PATHS['first_live']))
        self.assertEqual(result['error_code'], 'execution_v4_component_drift')
        self.assertTrue(all((self.root / name).exists() for name in result['created_paths']))

    def test_preview_detects_source_change_after_computation(self):
        actual = source.build_profile_documents
        def changed(*args, **kwargs):
            result = actual(*args, **kwargs)
            path = self.root / 'src/researchops/__init__.py'; path.write_bytes(path.read_bytes() + b'\n# changed\n')
            return result
        with patch.object(source, 'build_profile_documents', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'tree_changed'):
                current.build_current_timed_profile_documents(self.root)

    def test_campaign_requires_late_files_without_inventing_registration(self):
        code, result = self.invoke('--profile', 'campaign')
        self.assertEqual(code, 2); self.assertEqual(result['created_paths'], [])
        self.assertFalse((self.root / source.PROFILE_PATHS['campaign'][1]).exists())
        self.assertEqual(self.invoke('--write')[0], 0)
        for name in ('runtime_registry_v3.json', 'registry_admission_bundle_v1.json'):
            path = self.root / 'evals/provider_completion_runtime_registry_v3' / name
            path.write_bytes(b'{}')  # Source bytes only, deliberately not valid admission.
        code, result = self.invoke('--profile', 'campaign', '--write')
        self.assertEqual(code, 0); self.assertFalse(result['runtime_admission_verified'])
        self.assertFalse(result['online_execution_authorized'])
        self.assertEqual(self.invoke('--profile', 'campaign', '--verify')[0], 0)


if __name__ == '__main__': unittest.main()
