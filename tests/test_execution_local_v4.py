"""Real temporary fixed Git/current source, not an installed runtime or claim."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import execution_local_v4 as local,execution_components_v4 as source
from tests import test_execution_readers_v4 as fixtures
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


class LocalV4IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.repository=Repository(Path(cls.temp.name)/'repo')
        cls.files,_=fixtures.fixture_files()
        name='src/researchops_external_closure/execution_local_v4.py'
        cls.files[name]=(fixtures.prior.ROOT/name).read_bytes()
        cls.paths=tuple(sorted(cls.files))
        cls.documents=source.build_profile_documents(cls.files,available_paths=cls.paths,profile='first_live')
        cls.manifest_path,cls.plan_path=source.PROFILE_PATHS['first_live']
        cls.all_files=cls.files|{cls.manifest_path:cls.documents.manifest,cls.plan_path:cls.documents.plan}
        for path,payload in cls.all_files.items():
            target=cls.repository.root/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(payload)
        cls.tree=write_file_tree(cls.repository,cls.all_files); cls.commit=cls.repository.commit(cls.tree)
        cls.repository.git('update-ref','HEAD',cls.commit)
        (cls.repository.root/'unrelated-note.txt').write_bytes(b'keep test note')
        cls.expected=dict(profile='first_live',expected_commit=cls.commit,expected_tree=cls.tree,
            expected_source_integrity_commitment_sha256=json.loads(cls.documents.plan)['plan_commitment_sha256'],
            expected_source_manifest_commitment_sha256=json.loads(cls.documents.manifest)['commitment_sha256'])
        cls.checked=local.verify_local_timed_execution_identity(cls.repository.root,**cls.expected)

    def test_actual_identity_is_v4_not_global_clean_or_runtime_permission(self):
        self.assertEqual(self.checked.source_recipe_version,4)
        self.assertTrue(self.checked.current_and_historical_source_matched)
        self.assertEqual(self.checked.execution_commit,self.commit)
        for name in ('repository_globally_clean_verified','installed_environment_verified','fresh_authorization_verified',
                     'winning_claim_verified','runtime_authority_granted','closure_claim_allowed'):
            self.assertFalse(getattr(self.checked,name))
        self.assertEqual((self.repository.root/'unrelated-note.txt').read_bytes(),b'keep test note')

    def test_invalid_expected_oid_rejects_before_git(self):
        with patch.object(local,'_head',side_effect=AssertionError('must not query HEAD')) as head:
            with self.assertRaisesRegex(ValueError,'expectation_invalid'):
                local.verify_local_timed_execution_identity(self.repository.root,**(self.expected|{'expected_commit':'HEAD'}))
        head.assert_not_called()

    def test_wrong_head_rejects_before_historical_read(self):
        with patch.object(local,'verify_historical_timed_profile',side_effect=AssertionError('must not read history')) as reader:
            with self.assertRaisesRegex(ValueError,'head_mismatch'):
                local.verify_local_timed_execution_identity(self.repository.root,**(self.expected|{'expected_commit':'f'*40}))
        reader.assert_not_called()

    def test_same_tree_head_movement_after_current_check_is_rejected(self):
        other=self.repository.commit(self.tree,self.commit)
        original=local.verify_current_timed_profile
        def changed(*args,**kwargs):
            result=original(*args,**kwargs); self.repository.git('update-ref','HEAD',other); return result
        try:
            with patch.object(local,'verify_current_timed_profile',side_effect=changed):
                with self.assertRaisesRegex(ValueError,'execution_local_v4_head_changed'):
                    local.verify_local_timed_execution_identity(self.repository.root,**self.expected)
        finally: self.repository.git('update-ref','HEAD',self.commit)

    def test_self_consistent_different_local_profile_cannot_replace_fixed_git(self):
        files=dict(self.files); changed='src/researchops/__init__.py'; files[changed]+=b'\n# synthetic change\n'
        documents=source.build_profile_documents(files,available_paths=self.paths,profile='first_live')
        updates={changed:files[changed],self.manifest_path:documents.manifest,self.plan_path:documents.plan}
        before={path:(self.repository.root/path).read_bytes() for path in updates}
        try:
            for path,payload in updates.items(): (self.repository.root/path).write_bytes(payload)
            with self.assertRaisesRegex(ValueError,'execution_local_v4_commitment_mismatch'):
                local.verify_local_timed_execution_identity(self.repository.root,**self.expected)
        finally:
            for path,payload in before.items(): (self.repository.root/path).write_bytes(payload)


if __name__=='__main__': unittest.main()
