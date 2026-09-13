"""Temporary current/Git source identity; no runtime or online admission."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from researchops_external_closure import execution_components_v4 as source, execution_current_v4 as current
from researchops_external_closure import execution_binding_v4 as historical, git_objects, git_objects_v2
from tests import test_execution_profiles_v4 as prior
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree


def fixture_files():
    prior.ExecutionProfileV4Tests.setUpClass()
    files=dict(prior.ExecutionProfileV4Tests.files)
    for name in ('execution_current_v4.py','execution_binding_v4.py','git_objects_v2.py'):
        path='src/researchops_external_closure/'+name
        files[path]=(prior.ROOT/path).read_bytes()
    first=source.build_profile_documents(files,available_paths=tuple(sorted(files)),profile='first_live')
    return files,first


class CurrentV4ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'tree'; self.root.mkdir()
        self.files,self.first=fixture_files()
        self.write(self.files|dict(zip(source.PROFILE_PATHS['first_live'],(self.first.manifest,self.first.plan))))

    def write(self,files):
        for name,payload in files.items():
            path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(payload)

    def test_more_than_256_files_verify_readonly_without_git_or_provider(self):
        self.assertGreater(len(self.files),256)
        before={name:(self.root/name).read_bytes() for name in self.files}
        with patch('subprocess.Popen',side_effect=AssertionError('no Git fallback')),patch('socket.socket',side_effect=AssertionError('network forbidden')):
            result=current.verify_current_timed_profile(self.root)
        self.assertEqual(result.selected_file_count,len(self.files)); self.assertFalse(result.runtime_admission_verified)
        self.assertEqual(before,{name:(self.root/name).read_bytes() for name in before})

    def test_missing_profile_is_not_generated(self):
        path=self.root/source.PROFILE_PATHS['first_live'][1]; path.unlink()
        with self.assertRaisesRegex(ValueError,'current_file_unavailable'): current.verify_current_timed_profile(self.root)
        self.assertFalse(path.exists())

    def test_changed_source_and_new_json_directory_reject_old_profile(self):
        path=self.root/'src/researchops/__init__.py'; original=path.read_bytes(); path.write_bytes(original+b'\n# changed\n')
        with self.assertRaisesRegex(ValueError,'execution_v4_component_drift'): current.verify_current_timed_profile(self.root)
        path.write_bytes(original)
        self.write({'evals/provider_completion_future_test/new.json':b'{}'})
        with self.assertRaisesRegex(ValueError,'execution_v4_component_drift'): current.verify_current_timed_profile(self.root)

    def test_final_inventory_detects_added_contract_despite_restored_mtime(self):
        original=source.verify_profile_documents
        def changed(*args,**kwargs):
            result=original(*args,**kwargs); parent=self.root/'evals'; info=parent.stat()
            self.write({'evals/provider_completion_late_test/new.json':b'{}'})
            os.utime(parent,ns=(info.st_atime_ns,info.st_mtime_ns))
            return result
        with patch.object(source,'verify_profile_documents',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'execution_current_v4_tree_changed'): current.verify_current_timed_profile(self.root)

    def test_own_manifest_changed_after_validation_is_rejected(self):
        original=source.verify_profile_documents
        def changed(*args,**kwargs):
            result=original(*args,**kwargs); path=self.root/source.PROFILE_PATHS['first_live'][0]
            path.write_bytes(path.read_bytes()+b' '); return result
        with patch.object(source,'verify_profile_documents',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'tree_changed'): current.verify_current_timed_profile(self.root)

    def test_hardlinked_selected_file_rejects(self):
        path=self.root/'src/researchops/__init__.py'; os.link(path,Path(self.temp.name)/'alias.py')
        with self.assertRaises(ValueError): current.verify_current_timed_profile(self.root)

    def test_root_symlink_is_not_resolved_into_a_trusted_root(self):
        link=Path(self.temp.name)/'root-link'
        try: link.symlink_to(self.root,target_is_directory=True)
        except OSError as error:
            if getattr(error,'winerror',None)==1314: self.skipTest('Windows symlink privilege unavailable')
            raise
        with self.assertRaisesRegex(ValueError,'path_invalid'): current.verify_current_timed_profile(link)

    def test_directory_enumeration_stops_before_unbounded_materialization(self):
        original=current.os.scandir
        class Entry: name='unrelated'
        class Entries:
            count=0
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def __iter__(self): return self
            def __next__(self):
                self.count+=1
                if self.count>16385: raise AssertionError('unbounded enumeration')
                return Entry()
        entries=Entries()
        with patch.object(current.os,'scandir',side_effect=lambda path:entries if Path(path)==self.root/'evals' else original(path)):
            with self.assertRaisesRegex(ValueError,'execution_current_v4_inventory_limit'): current.verify_current_timed_profile(self.root)
        self.assertEqual(entries.count,16385)

    def test_campaign_profile_requires_matching_first_live_bytes(self):
        files=dict(self.files)|dict(zip(source.PROFILE_PATHS['first_live'],(self.first.manifest,self.first.plan)))
        files.update({'evals/provider_completion_runtime_registry_v3/runtime_registry_v3.json':b'{}',
                      'evals/provider_completion_runtime_registry_v3/registry_admission_bundle_v1.json':b'{}'})
        second=source.build_profile_documents(files,available_paths=tuple(sorted(files)),profile='campaign')
        self.write(files|dict(zip(source.PROFILE_PATHS['campaign'],(second.manifest,second.plan))))
        self.assertEqual(current.verify_current_timed_profile(self.root,profile='campaign').profile,'campaign')


class HistoricalV4ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.repository=Repository(Path(cls.temp.name)/'repo')
        cls.files,first=fixture_files()
        cls.tree=write_file_tree(cls.repository,cls.files|dict(zip(source.PROFILE_PATHS['first_live'],(first.manifest,first.plan))))
        cls.commit=cls.repository.commit(cls.tree)

    def test_real_git_profiles_over_256_paths_do_not_execute_or_read_current_source(self):
        (self.repository.root/'requirements.lock').write_bytes(b'conflicting current tree')
        result=historical.verify_historical_timed_profile(self.repository.root,commit=self.commit,tree=self.tree,profile='first_live')
        self.assertGreater(result.components.selected_file_count,256)
        self.assertFalse(result.runtime_authority_granted); self.assertFalse(result.execution_observed)

    def test_wrong_tree_and_missing_commit_still_reject(self):
        for commit,tree in ((self.commit,'f'*40),('1'*40,self.tree)):
            with self.subTest(commit=commit),self.assertRaises(ValueError):
                historical.verify_historical_timed_profile(self.repository.root,commit=commit,tree=tree,profile='first_live')

    def test_new_path_bound_does_not_change_the_old_reader(self):
        self.assertEqual(git_objects._MAX_PATHS,256)
        with self.assertRaisesRegex(ValueError,'paths_invalid'):
            git_objects.read_git_object_snapshot(self.repository.root,self.commit,tuple(sorted(self.files)))
        with self.assertRaisesRegex(ValueError,'paths_invalid'):
            git_objects_v2.read_git_object_snapshot(self.repository.root,self.commit,tuple('file'+str(i) for i in range(321)))

    def test_actual_320_path_snapshot_is_read_in_one_bounded_reader(self):
        blob=self.repository.object('blob',b'synthetic source identity only\n')
        paths=tuple(f'file{index:03d}' for index in range(320))
        tree=self.repository.tree(*(('100644',path,blob) for path in paths))
        commit=self.repository.commit(tree)
        with patch.object(git_objects,'_Reader',wraps=git_objects._Reader) as readers:
            result=git_objects_v2.read_git_object_snapshot(self.repository.root,commit,paths,expected_tree_oid=tree)
        self.assertEqual(len(result.blobs),320); self.assertEqual(readers.call_count,1)

    def test_repeated_blob_payloads_still_count_toward_selected_total(self):
        blob=self.repository.object('blob',b'x'*(8*1024*1024))
        paths=tuple(f'large{index}' for index in range(5))
        tree=self.repository.tree(*(('100644',path,blob) for path in paths))
        commit=self.repository.commit(tree)
        with self.assertRaisesRegex(ValueError,'selected_total_bytes_limit'):
            git_objects_v2.read_git_object_snapshot(self.repository.root,commit,paths,expected_tree_oid=tree)


if __name__=='__main__': unittest.main()
