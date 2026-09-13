"""Joined synthetic signatures and actual temporary Git/archive; no claims."""
from dataclasses import replace, FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch

from researchops_completion_timing import campaign_admission as admission, local_claim
from tests.timed_campaign_fixture import TimedCampaignFixture

ROOT = Path(__file__).resolve().parents[1]


class CampaignAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TimedCampaignFixture()
        cls.addClassCleanup(cls.fixture.close)

    def arguments(self):
        fixture = self.fixture
        return dict(external_observation_bundle=fixture.campaign_observation, timing_plan_bytes=fixture.campaign_timing_plan,
                    admission_inputs=fixture.campaign_admission_inputs, verification_time_utc=fixture.campaign_as_of)

    def test_full_signed_campaign_and_actual_admission_join_without_claim(self):
        with patch.object(local_claim, '_reserve_with_ownership', side_effect=AssertionError('no claim')), \
             patch.object(local_claim, 'local_claim_store_status', side_effect=AssertionError('no store')):
            proof = admission.verify_campaign_prerequisites(self.fixture.repository.root, self.fixture.campaign_documents, **self.arguments())
        summary = proof.summary()
        self.assertTrue(summary['same_trust_and_candidate_freeze_verified'])
        self.assertEqual(summary['execution_commit'], self.fixture.campaign_commit)
        for name in ('runtime_authority_granted', 'winning_local_claim_verified', 'task_release_authorized', 'provider_key_load_authorized'):
            self.assertFalse(summary[name])
        with self.assertRaises(FrozenInstanceError): proof.expires_at_utc = 'changed'
        with self.assertRaises(TypeError): admission.VerifiedCampaignPrerequisites()

    def test_unrelated_review_trust_or_freeze_is_rejected_before_git_admission(self):
        for field, value in (('expected_trust_manifest_sha256', 'f' * 64),
                             ('expected_candidate_freeze_receipt_sha256', 'f' * 64),
                             ('expected_candidate_frozen_at_utc', '2026-09-06T01:00:59Z')):
            arguments = self.arguments(); arguments['admission_inputs'] = replace(arguments['admission_inputs'], **{field: value})
            with self.subTest(field=field), patch.object(admission, 'verify_timed_admission_links') as lower:
                with self.assertRaisesRegex(ValueError, 'cross_graph_mismatch'):
                    admission.verify_campaign_prerequisites(self.fixture.repository.root, self.fixture.campaign_documents, **arguments)
                lower.assert_not_called()

    def test_individually_valid_admission_cannot_supply_a_different_freeze(self):
        original = self.fixture.inputs()
        independently_valid = admission.verify_timed_admission_links(self.fixture.repository.root,
            execution_binding=self.fixture.binding, inputs=original)
        self.assertTrue(independently_valid.review_signature_and_external_bindings_verified)
        arguments = self.arguments(); arguments['admission_inputs'] = original
        # The campaign's signed scope is valid too (verified before this cross
        # check). Reject the join, not the signature of either independent graph.
        with patch.object(admission, 'verify_timed_admission_links') as lower:
            with self.assertRaisesRegex(ValueError, 'cross_graph_mismatch'):
                admission.verify_campaign_prerequisites(self.fixture.repository.root, self.fixture.campaign_documents, **arguments)
            lower.assert_not_called()


if __name__ == '__main__': unittest.main()
