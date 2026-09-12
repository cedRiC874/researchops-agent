"""V5 source identity composed with actual synthetic archive/Git verification."""
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import first_live_evidence as evidence
from researchops_completion_timing import first_live_identity_v5 as identity
from tests.timed_first_live_evidence_fixture import TimedEvidenceFixture


class FirstLiveEvidenceDispatchTests(unittest.TestCase):
    def test_invalid_version_rejects_before_artifact_access(self):
        arguments = dict(artifact_directory=Path('.'), bundle_bytes=b'',
            expected_bundle_commitment_sha256='f' * 64,
            expected_authorization_binding_sha256='f' * 64,
            verification_time_utc='', reviewed_evidence_commit='', reviewed_evidence_tree='', sensitive_canaries=())
        with patch.object(evidence.archive, 'verify_first_live_artifact_bundle', side_effect=AssertionError('no artifact access')):
            for version in (True, 4.0, '5', None, 6):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'timed_first_live_evidence_version_invalid'):
                    evidence._verify_timed_first_live_evidence(Path('.'), implementation_version=version, **arguments)


class FirstLiveV5EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedEvidenceFixture(implementation_version=5)
        cls.addClassCleanup(cls.fixture.close)
        with patch('socket.socket', side_effect=AssertionError('no network')):
            cls.result = evidence.verify_timed_first_live_evidence_v5(
                cls.fixture.repository.root, **cls.fixture.arguments())

    def test_actual_v5_source_archive_and_reviewed_git_bytes_are_linked(self):
        result = self.result
        self.assertEqual(result.historical.implementation_version, 5)
        self.assertEqual(result.historical.source_recipe_version, 4)
        self.assertEqual(result.subject['first_live_implementation_commitment_sha256'],
                         identity.IMPLEMENTATION_COMMITMENT_SHA256)
        self.assertEqual(result.subject['first_live_execution_commit'], self.fixture.source_commit)
        self.assertEqual(result.reviewed_evidence_commit, self.fixture.reviewed_commit)
        for name in ('artifact_linkage_verified', 'reviewed_bundle_git_verified',
                     'execution_source_mapping_binding_verified', 'timing_gate_complete'):
            self.assertTrue(getattr(result, name))
        for name in ('source_equivalence_verified', 'review_signature_verified', 'registry_admission_verified',
                     'actual_live_execution_verified', 'runtime_authority_granted', 'first_live_success_verified'):
            self.assertFalse(getattr(result, name))
        parameters = inspect.signature(evidence.verify_timed_first_live_evidence_v5).parameters
        for name in ('expected_subject', 'verifier', 'identity', 'implementation_version'):
            self.assertNotIn(name, parameters)

    def test_wrong_independent_digest_stops_before_git_and_identity(self):
        arguments = self.fixture.arguments() | {'expected_bundle_commitment_sha256': 'f' * 64}
        with patch.object(evidence, 'read_git_object_snapshot', side_effect=AssertionError('no Git')), \
             patch.object(identity, 'verify_historical_timed_implementation', side_effect=AssertionError('no identity')):
            with self.assertRaisesRegex(ValueError, 'first_live_bundle_commitment_mismatch'):
                evidence.verify_timed_first_live_evidence_v5(self.fixture.repository.root, **arguments)

    def test_legacy_and_v5_entries_have_fixed_internal_dispatch(self):
        # This checks dispatch only; the positive above runs real verifiers.
        for function, version in ((evidence.verify_timed_first_live_evidence, 4),
                                  (evidence.verify_timed_first_live_evidence_v5, 5)):
            with patch.object(evidence, '_verify_timed_first_live_evidence', return_value=None) as called:
                function(self.fixture.repository.root, **self.fixture.arguments())
            self.assertEqual(called.call_count, 1)
            self.assertEqual(called.call_args.kwargs['implementation_version'], version)

    def test_rehashed_archive_cannot_forge_v11_source_commitment(self):
        state = dict(self.fixture.__dict__)
        archive_state = dict(self.fixture.archive.__dict__)
        helper_state = dict(self.fixture.archive.helper.__dict__)
        try:
            self.fixture.bind_archive(source_commitment='f' * 64)
            with patch('socket.socket', side_effect=AssertionError('no network')):
                with self.assertRaisesRegex(ValueError, 'timed_first_live_evidence_source_mismatch'):
                    evidence.verify_timed_first_live_evidence_v5(self.fixture.repository.root, **self.fixture.arguments())
        finally:
            self.fixture.__dict__.clear()
            self.fixture.__dict__.update(state)
            self.fixture.archive.__dict__.clear()
            self.fixture.archive.__dict__.update(archive_state)
            self.fixture.archive.helper.__dict__.clear()
            self.fixture.archive.helper.__dict__.update(helper_state)


if __name__ == '__main__':
    unittest.main()
