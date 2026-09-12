"""Real v5 source/review joined to real synthetic campaign signatures; no claim."""
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch

from researchops_completion_timing import campaign_admission as admission, local_claim
from tests.timed_campaign_fixture import TimedCampaignFixture


class CampaignPrerequisitesV5FullTests(unittest.TestCase):
    def test_real_join_returns_distinct_immutable_non_authorizing_v5_type(self):
        fixture = TimedCampaignFixture(implementation_version=5); self.addCleanup(fixture.close)
        with patch.object(local_claim, '_reserve_with_ownership', side_effect=AssertionError('no claim')) as reserve, \
             patch.object(local_claim, 'local_claim_store_status', side_effect=AssertionError('no store')) as store, \
             patch('socket.socket', side_effect=AssertionError('no Provider/network')):
            proof = admission.verify_campaign_prerequisites_v5(fixture.repository.root, fixture.campaign_documents,
                external_observation_bundle=fixture.campaign_observation, timing_plan_bytes=fixture.campaign_timing_plan,
                admission_inputs=fixture.campaign_admission_inputs, verification_time_utc=fixture.campaign_as_of)
        self.assertIs(type(proof), admission.VerifiedCampaignPrerequisitesV5)
        self.assertIsNot(type(proof), admission.VerifiedCampaignPrerequisites)
        self.assertEqual((reserve.call_count, store.call_count), (0, 0))
        summary = proof.summary()
        self.assertEqual((summary['first_live_implementation_version'], summary['source_recipe_version']), (5, 4))
        self.assertEqual(summary['execution_commit'], fixture.campaign_commit)
        for name in ('winning_local_claim_verified', 'task_release_authorized', 'provider_key_load_authorized',
                     'current_source_verified', 'installed_environment_verified', 'runtime_authority_granted', 'closure_claim_allowed'):
            self.assertFalse(summary[name])
        with self.assertRaises(FrozenInstanceError): proof.expires_at_utc = 'changed'
        with self.assertRaises(TypeError):
            admission.VerifiedCampaignPrerequisites._create(admission._TOKEN, proof.scope, proof.admission, proof.expires_at_utc)


if __name__ == '__main__': unittest.main()
