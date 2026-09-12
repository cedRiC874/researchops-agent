"""Synthetic historical source and real Git objects, not runtime/live proof."""
import inspect
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_identity as legacy
from researchops_completion_timing import first_live_identity_v5 as identity
from researchops_external_closure import execution_components_v4 as source
from tests.test_execution_readers_v4 import fixture_files
from tests.test_external_closure_execution_binding import write_file_tree
from tests.test_external_closure_git_objects import Repository


ROOT = Path(__file__).resolve().parents[1]


def protocol_files():
    payload = (ROOT / identity.IMPLEMENTATION_PATH).read_bytes()
    protocol = json.loads(payload)
    return {identity.IMPLEMENTATION_PATH: payload} | {
        item['path']: (ROOT / item['path']).read_bytes() for item in protocol['fixed_bindings']}


class FirstLiveV5ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.files = protocol_files()

    def test_protocol_graph_is_pure_and_non_authorizing(self):
        with patch('builtins.open', side_effect=AssertionError('no file IO')), \
             patch('subprocess.Popen', side_effect=AssertionError('no process')), \
             patch('socket.socket', side_effect=AssertionError('no network')):
            checked = identity.verify_timed_implementation_files(self.files)
        self.assertEqual(checked['implementation_commitment_sha256'], identity.IMPLEMENTATION_COMMITMENT_SHA256)
        for name in ('runnable_implementation_verified', 'source_profile_verified',
                     'first_live_success_verified', 'runtime_authority_granted'):
            self.assertFalse(checked[name])

    def test_all_exact_byte_dependencies_are_enforced(self):
        for name in self.files:
            changed = self.files | {name: self.files[name] + b'\n'}
            expected = 'contract_invalid' if name == identity.IMPLEMENTATION_PATH else 'dependency_drift'
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, expected):
                identity.verify_timed_implementation_files(changed)

    def test_missing_and_extra_files_are_rejected(self):
        missing = dict(self.files)
        missing.pop(next(name for name in missing if name != identity.IMPLEMENTATION_PATH))
        for files in (missing, self.files | {'extra.json': b'{}'}):
            with self.assertRaisesRegex(ValueError, 'files_invalid'):
                identity.verify_timed_implementation_files(files)

    def test_old_and_new_protocols_cannot_substitute_for_one_another(self):
        with self.assertRaisesRegex(ValueError, 'contract_invalid'):
            legacy._document(self.files[identity.IMPLEMENTATION_PATH])
        with self.assertRaisesRegex(ValueError, 'contract_invalid'):
            identity._document(self.files[legacy.IMPLEMENTATION_PATH])

    def test_legacy_replay_is_explicit_archived_coverage_not_current_tree(self):
        from tests import execution_v3_fixture
        from researchops_external_closure import execution_components_v3
        with patch.object(execution_v3_fixture, 'first_live_files', side_effect=AssertionError('no current-tree substitution')):
            files, paths = execution_v3_fixture.archived_first_live_files()
        self.assertEqual(len(files), 252)
        self.assertNotIn(identity.IMPLEMENTATION_PATH, files)
        self.assertEqual(execution_components_v3.decode_recipe(files[execution_components_v3.RECIPE_PATH])['limits']['selected_files'], 256)
        self.assertIn(b'Synthetic archived source-profile fixture', files['src/researchops/__init__.py'])
        self.assertNotEqual(files['src/researchops/__init__.py'], (ROOT / 'src/researchops/__init__.py').read_bytes())

    def test_loader_never_generates_a_missing_source_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in self.files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            before = sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file())
            checked = identity.load_timed_implementation_contract(root)
            self.assertFalse(checked['source_profile_verified'])
            self.assertEqual(before, sorted(str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()))
            for name in source.PROFILE_PATHS['first_live']:
                self.assertFalse((root / name).exists())


class FirstLiveV5HistoricalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.repository = Repository(Path(cls.temporary.name) / 'repo')
        files, _ = fixture_files()
        files.update(protocol_files())
        name = 'src/researchops_completion_timing/first_live_identity_v5.py'
        files[name] = (ROOT / name).read_bytes()
        cls.documents = source.build_profile_documents(files, available_paths=tuple(sorted(files)), profile='first_live')
        files.update(dict(zip(source.PROFILE_PATHS['first_live'], (cls.documents.manifest, cls.documents.plan))))
        cls.tree = write_file_tree(cls.repository, files)
        cls.commit = cls.repository.commit(cls.tree)
        current = cls.repository.root / identity.IMPLEMENTATION_PATH
        current.parent.mkdir(parents=True)
        current.write_bytes(b'wrong checkout protocol; must not be read')
        with patch('socket.socket', side_effect=AssertionError('no network')):
            cls.result = identity.verify_historical_timed_implementation(
                cls.repository.root, commit=cls.commit, tree=cls.tree)

    def test_subject_is_derived_from_v11_and_v5_not_supplied(self):
        result = self.result
        self.assertEqual(result.source_recipe_version, 4)
        self.assertEqual(result.implementation_version, 5)
        self.assertEqual(result.control_protocol_commitment_sha256, identity.IMPLEMENTATION_COMMITMENT_SHA256)
        self.assertNotEqual(result.source_manifest_commitment_sha256, result.control_protocol_commitment_sha256)
        self.assertEqual(result.subject['first_live_execution_commit'], self.commit)
        self.assertEqual(result.subject['first_live_execution_tree'], self.tree)
        self.assertEqual(result.subject['first_live_source_integrity_commitment_sha256'],
                         json.loads(self.documents.plan)['plan_commitment_sha256'])
        self.assertEqual(json.loads(self.documents.plan)['plan_id'], 'phase6-deepseek-depth60-v11')
        self.assertNotIn('expected_subject', inspect.signature(identity.verify_historical_timed_implementation).parameters)
        for name in ('source_byte_inventory_sha256', 'dependency_lock_sha256', 'pyproject_sha256'):
            self.assertEqual(result.subject[name], result.historical.components.component_hashes[name])
        self.assertEqual(result.subject['selected_mapping_sha256'], result.historical.components.component_hashes['mapping_sha256'])

    def test_verified_identity_does_not_claim_artifacts_review_or_runtime(self):
        self.assertTrue(self.result.control_protocol_identity_verified)
        self.assertTrue(self.result.source_components_verified)
        for name in ('runnable_implementation_verified', 'artifact_binding_verified', 'review_verified',
                     'registry_admission_verified', 'runtime_authority_granted', 'first_live_success_verified'):
            self.assertFalse(getattr(self.result, name))
        with self.assertRaises(FrozenInstanceError):
            self.result.runtime_authority_granted = True
        with self.assertRaises(TypeError):
            self.result.subject['dependency_lock_sha256'] = 'f' * 64

    def test_incorrect_tree_is_rejected_without_checkout_fallback(self):
        with self.assertRaises(ValueError):
            identity.verify_historical_timed_implementation(self.repository.root, commit=self.commit, tree='f' * 40)


if __name__ == '__main__':
    unittest.main()
