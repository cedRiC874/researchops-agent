"""V5/v11/v12 prerequisites with real synthetic signatures, Git and archives."""
import copy
import inspect
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing import admission
from tests.timed_admission_fixture import TimedAdmissionFixture


class TimedAdmissionV5DispatchTests(unittest.TestCase):
    def test_invalid_version_rejects_before_contract_or_artifact_access(self):
        with patch.object(admission, '_binding', side_effect=AssertionError('no contract read')):
            for version in (True, 4.0, '5', None, 6):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'timed_admission_version_invalid'):
                    admission._verify_timed_admission_links(Path('.'), execution_binding={}, inputs=None,
                        sensitive_canaries=(), implementation_version=version)

    def test_public_entries_select_fixed_versions_not_caller_callbacks(self):
        for function, version in ((admission.verify_timed_admission_links, 4),
                                  (admission.verify_timed_admission_links_v5, 5)):
            with patch.object(admission, '_verify_timed_admission_links', return_value=None) as called:
                function(Path('.'), execution_binding={}, inputs=None)
            self.assertEqual(called.call_count, 1)
            self.assertEqual(called.call_args.kwargs['implementation_version'], version)
            for name in ('verifier', 'expected_subject', 'implementation_version', 'identity'):
                self.assertNotIn(name, inspect.signature(function).parameters)


class TimedAdmissionV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        cls.network.start(); cls.addClassCleanup(cls.network.stop)
        cls.fixture = TimedAdmissionFixture(implementation_version=5)
        cls.addClassCleanup(cls.fixture.close)
        cls.checked = admission.verify_timed_admission_links_v5(cls.fixture.repository.root,
            execution_binding=cls.fixture.binding, inputs=cls.fixture.inputs())

    def test_real_source_archive_review_registry_composition_is_not_runtime_permission(self):
        result = self.checked
        self.assertIs(type(result), admission.VerifiedTimedAdmissionLinksV5)
        self.assertIsNot(type(result), admission.VerifiedTimedAdmissionLinks)
        self.assertEqual(result.source_recipe_version, 4)
        self.assertEqual(result.first_live_implementation_version, 5)
        self.assertEqual(result.candidate_commit, self.fixture.campaign_commit)
        self.assertEqual(result.first_live_execution_commit, self.fixture.source_commit)
        for name in ('artifact_source_binding_verified', 'review_signature_and_external_bindings_verified',
                     'source_byte_equality_verified', 'first_live_snapshot_link_verified', 'registry_links_verified'):
            self.assertTrue(getattr(result, name))
        for name in ('outer_preregistration_and_campaign_authorization_verified', 'current_source_and_fresh_time_verified',
                     'winning_local_claim_verified', 'installed_environment_verified',
                     'actual_network_execution_independently_proved', 'runtime_authority_granted', 'closure_claim_allowed'):
            self.assertFalse(getattr(result, name))

    def test_wrong_independent_digest_stops_before_evidence(self):
        inputs = replace(self.fixture.inputs(), expected_review_document_sha256='f' * 64)
        with patch.object(admission, 'verify_timed_first_live_evidence_v5', side_effect=AssertionError('no artifact access')):
            with self.assertRaisesRegex(ValueError, 'timed_admission_review_digest_mismatch'):
                admission.verify_timed_admission_links_v5(self.fixture.repository.root,
                    execution_binding=self.fixture.binding, inputs=inputs)

    def test_wrong_candidate_freeze_stops_before_evidence(self):
        inputs = replace(self.fixture.inputs(), expected_candidate_freeze_receipt_sha256='f' * 64)
        with patch.object(admission, 'verify_timed_first_live_evidence_v5', side_effect=AssertionError('no artifact access')):
            with self.assertRaisesRegex(ValueError, 'timed_admission_candidate_freeze_mismatch'):
                admission.verify_timed_admission_links_v5(self.fixture.repository.root,
                    execution_binding=self.fixture.binding, inputs=inputs)

    def test_resigned_rejected_review_cannot_be_overridden_by_v12_registry(self):
        before = copy.deepcopy(self.fixture.review), copy.deepcopy(self.fixture.observation)
        try:
            self.fixture.review.update(decision='rejected', reason_code='evidence_invalid')
            self.fixture.sign_review()
            with patch.object(admission.execution_binding_v4, 'verify_historical_timed_profile',
                              side_effect=AssertionError('no candidate verification')):
                with self.assertRaisesRegex(ValueError, 'timed_admission_review_not_approved'):
                    admission.verify_timed_admission_links_v5(self.fixture.repository.root,
                        execution_binding=self.fixture.binding, inputs=self.fixture.inputs())
        finally:
            self.fixture.review, self.fixture.observation = before


if __name__ == '__main__':
    unittest.main()
