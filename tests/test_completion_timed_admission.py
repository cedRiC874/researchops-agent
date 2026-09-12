"""Composed timed prerequisites; no stubs stand in for successful verification."""
from __future__ import annotations

import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

from researchops_completion_timing import admission
from researchops_completion_timing.contract import TimingContractError
from researchops_external_closure.primitives import canonical_json_bytes as raw
from tests.timed_admission_fixture import TimedAdmissionFixture


class TimedAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedAdmissionFixture(); cls.addClassCleanup(cls.fixture.close)
        cls.checked = admission.verify_timed_admission_links(cls.fixture.repository.root, execution_binding=cls.fixture.binding, inputs=cls.fixture.inputs())

    def test_real_git_archive_signature_and_byte_equality_are_not_runtime_permission(self):
        result = self.checked
        self.assertTrue(result.artifact_source_binding_verified)
        self.assertTrue(result.review_signature_and_external_bindings_verified)
        self.assertTrue(result.source_byte_equality_verified)
        self.assertTrue(result.first_live_snapshot_link_verified)
        self.assertTrue(result.registry_links_verified)
        self.assertEqual(result.candidate_commit, self.fixture.campaign_commit)
        self.assertEqual(result.first_live_execution_commit, self.fixture.source_commit)
        for name in ("outer_preregistration_and_campaign_authorization_verified", "current_source_and_fresh_time_verified",
                     "winning_local_claim_verified", "installed_environment_verified", "actual_network_execution_independently_proved",
                     "runtime_authority_granted", "closure_claim_allowed"):
            self.assertIs(getattr(result, name), False, name)

    def test_wrong_independent_review_digest_rejects_before_git(self):
        inputs = replace(self.fixture.inputs(), expected_review_document_sha256="f" * 64)
        with patch.object(admission, "verify_timed_first_live_evidence", side_effect=AssertionError("must not read Git")):
            with self.assertRaisesRegex(TimingContractError, "timed_admission_review_digest_mismatch"):
                admission.verify_timed_admission_links(self.fixture.repository.root, execution_binding=self.fixture.binding, inputs=inputs)

    def test_freeze_projection_mismatch_rejects_before_evidence(self):
        inputs = replace(self.fixture.inputs(), expected_candidate_freeze_receipt_sha256="f" * 64)
        with patch.object(admission, "verify_timed_first_live_evidence", side_effect=AssertionError("must not read evidence")):
            with self.assertRaisesRegex(TimingContractError, "timed_admission_candidate_freeze_mismatch"):
                admission.verify_timed_admission_links(self.fixture.repository.root, execution_binding=self.fixture.binding, inputs=inputs)

    def test_resigned_rejected_review_cannot_be_overridden_by_registry(self):
        before_review, before_observation = copy.deepcopy(self.fixture.review), copy.deepcopy(self.fixture.observation)
        try:
            self.fixture.review.update(decision="rejected", reason_code="evidence_invalid")
            self.fixture.sign_review()
            with patch.object(admission, "verify_historical_timed_profile", side_effect=AssertionError("must not read candidate")):
                with self.assertRaisesRegex(TimingContractError, "timed_admission_review_not_approved"):
                    admission.verify_timed_admission_links(self.fixture.repository.root, execution_binding=self.fixture.binding, inputs=self.fixture.inputs())
        finally:
            self.fixture.review, self.fixture.observation = before_review, before_observation

    def test_final_archive_is_rechecked_after_real_registry_verification(self):
        path = self.fixture.archive.directory / "authorization.json"
        before = path.read_bytes()
        original = admission.validate_registry_carrier
        def verify(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(before + b" ")
            return result
        try:
            with patch.object(admission, "validate_registry_carrier", side_effect=verify):
                with self.assertRaisesRegex(TimingContractError, "first_live_bundle_file_mismatch"):
                    admission.verify_timed_admission_links(self.fixture.repository.root, execution_binding=self.fixture.binding, inputs=self.fixture.inputs())
        finally:
            path.write_bytes(before)  # Only this test-owned archive is restored.


if __name__ == "__main__":
    unittest.main()
