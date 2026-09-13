"""Real temporary v4 Git/source and synthetic claim store; no real credential use."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from researchops.audit import AuditLedger
from researchops_completion_timing import first_live_start as start, first_live, first_live_control as control, local_claim
from researchops_completion_timing import first_live_runtime
from researchops_completion_timing.contract import digest
from researchops_external_closure import execution_components_v4 as source, execution_local_v4
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.test_execution_readers_v4 import fixture_files
from tests.test_completion_first_live_identity_v5 import protocol_files
from tests import test_completion_first_live_control as controls
from tests.test_external_closure_git_objects import Repository
from tests.test_external_closure_execution_binding import write_file_tree

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'designated Windows environment')
class FirstLiveV5StartFullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name); cls.repository = Repository(cls.directory / 'repo')
        files, _ = fixture_files(); files.update(protocol_files())
        for path in ('src/researchops_completion_timing/first_live_start.py',
                     'src/researchops_completion_timing/first_live_identity_v5.py',
                     'src/researchops_external_closure/execution_local_v4.py'):
            files[path] = (ROOT / path).read_bytes()
        cls.source_documents = source.build_profile_documents(files, available_paths=tuple(sorted(files)), profile='first_live')
        manifest, plan = source.PROFILE_PATHS['first_live']
        files.update({manifest: cls.source_documents.manifest, plan: cls.source_documents.plan})
        for name, payload in files.items():
            path = cls.repository.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
        cls.tree = write_file_tree(cls.repository, files); cls.commit = cls.repository.commit(cls.tree)
        cls.repository.git('update-ref', 'HEAD', cls.commit)
        cls.known = cls.directory / 'test-known-folder'; cls.known.mkdir()
        cls.locator = patch.object(local_claim, '_windows_local_app_data', return_value=cls.known)
        cls.locator.start(); cls.addClassCleanup(cls.locator.stop)
        cls.environment_id = local_claim.provision_local_claim_store()['execution_environment_id']

    def documents(self):
        plan, auth = controls.FirstLiveControlTests().fixture(environment_id=self.environment_id,
            now=datetime.now(timezone.utc) - timedelta(seconds=1))
        identifier = digest(('synthetic-v5-' + self._testMethodName).encode())
        plan['binding'].update(execution_commit=self.commit, execution_tree=self.tree, authorization_id_sha256=identifier,
            source_integrity_commitment_sha256=json.loads(self.source_documents.plan)['plan_commitment_sha256'])
        plan['binding']['validation_run_id'] = first_live.validation_run_id(plan['binding'])
        plan['plan_commitment_sha256'] = first_live.commitment('plan', plan)
        auth.update(authorization_id_sha256=identifier, timing_plan_commitment_sha256=plan['plan_commitment_sha256'])
        auth['authorization_binding_sha256'] = control.authorization_binding(auth)
        return plan, auth

    def prepare(self, plan, auth):
        with patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            return start._prepare_claimed_first_live_start_v5(self.repository.root, plan_bytes=raw(plan), authorization_bytes=raw(auth),
                expected_authorization_binding_sha256=auth['authorization_binding_sha256'])

    def marker(self, auth):
        return self.known / 'ResearchOpsAgent/completion-claims-v1/claims' / (auth['authorization_id_sha256'] + '.json')

    def test_v5_source_and_synthetic_claim_prepare_distinct_non_executing_owner(self):
        plan, auth = self.documents(); prepared = self.prepare(plan, auth); self.addCleanup(prepared.abort)
        self.assertIs(type(prepared), start._ClaimedFirstLiveStartV5)
        self.assertTrue(self.marker(auth).is_file())
        summary = prepared.summary()
        self.assertEqual((summary['implementation_version'], summary['source_recipe_version']), (5, 4))
        for name in ('task_released', 'key_loaded', 'runtime_authority_granted', 'first_live_success_verified', 'retry_authorized'):
            self.assertFalse(summary[name])
        with self.assertRaises(TypeError): copy.copy(prepared)
        prepared._environment = {'synthetic': True}
        with self.assertRaisesRegex(start.FirstLiveStartError, 'preparation_required'):
            first_live_runtime._FirstLiveModelFactory(prepared, AuditLedger(self.directory / 'unused.sqlite3'))
        self.assertFalse(prepared._taken)
        self.assertIs(prepared._take_for_runtime(), prepared)
        with self.assertRaisesRegex(start.FirstLiveStartError, 'already_taken'): prepared._take_for_runtime()

    def test_expired_authorization_stops_before_source_or_claim(self):
        plan, auth = self.documents()
        with patch.object(start, '_utc_now', return_value=auth['expires_at_utc']), \
             patch.object(execution_local_v4, 'verify_local_timed_execution_identity', side_effect=AssertionError('no source')) as source_check:
            with self.assertRaises(start.FirstLiveStartError) as caught: self.prepare(plan, auth)
            self.assertFalse(caught.exception.claim_consumed)
            self.assertEqual(source_check.call_count, 0)
            self.assertFalse(self.marker(auth).exists())

    def test_source_drift_during_claim_consumes_and_never_prepares(self):
        plan, auth = self.documents()
        path = self.repository.root / 'src/researchops/__init__.py'; before = path.read_bytes()
        actual = local_claim._reserve_with_ownership
        def changed(request):
            winner = actual(request); path.write_bytes(before + b'\n# synthetic post-claim drift\n'); return winner
        try:
            with patch.object(local_claim, '_reserve_with_ownership', side_effect=changed):
                with self.assertRaises(start.FirstLiveStartError) as caught: self.prepare(plan, auth)
            self.assertTrue(caught.exception.claim_consumed)
            self.assertFalse(caught.exception.retry_authorized)
            self.assertTrue(self.marker(auth).exists())
        finally:
            path.write_bytes(before)  # Restore only this test-owned synthetic file.


if __name__ == '__main__': unittest.main()
