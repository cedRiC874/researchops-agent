"""V5 routing/freshness boundaries; real signed scope, isolated admission calls."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from researchops_completion_timing import campaign_admission as admission
from researchops_completion_timing import admission as evidence_admission
from tests import test_completion_execution_scope as scope_fixture
from tests import test_completion_timing_pre_execution as pre_fixture
from tests import test_completion_campaign_run as shape_fixture
from researchops_external_closure.primitives import canonical_json_bytes as raw


class CampaignPrerequisitesV5Tests(unittest.TestCase):
    def test_wrong_cross_graph_values_stop_before_v5_admission(self):
        fixture, plan = scope_fixture.scoped_fixture()
        docs = fixture.documents
        inputs = replace(shape_fixture.valid_shape()['admission_inputs'],
            expected_trust_manifest_sha256=docs['trust_manifest']['document_sha256'],
            expected_candidate_freeze_receipt_sha256=docs['candidate_freeze_receipt']['document_sha256'],
            expected_candidate_frozen_at_utc=docs['candidate_freeze_receipt']['frozen_at_utc'])
        for field, value in (('expected_trust_manifest_sha256', 'f' * 64),
                             ('expected_candidate_freeze_receipt_sha256', 'f' * 64),
                             ('expected_candidate_frozen_at_utc', '2026-09-04T00:00:00Z')):
            with self.subTest(field=field), patch.object(evidence_admission, 'verify_timed_admission_links_v5',
                    side_effect=AssertionError('no artifact/Git admission')) as called:
                with self.assertRaisesRegex(ValueError, 'cross_graph_mismatch'):
                    admission.verify_campaign_prerequisites_v5(scope_fixture.ROOT, pre_fixture.documents(fixture),
                        external_observation_bundle=raw(fixture.observation), timing_plan_bytes=raw(plan),
                        admission_inputs=replace(inputs, **{field: value}), verification_time_utc='2026-09-04T00:00:17Z')
                self.assertEqual(called.call_count, 0)

    def test_invalid_version_rejects_before_signed_scope_or_clock(self):
        with patch.object(admission, 'verify_single_host_pre_execution', side_effect=AssertionError('no graph')) as graph, \
             patch.object(admission, '_current_utc', side_effect=AssertionError('no clock')) as clock:
            for version in (True, 4.0, '5', None, 6):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'version_invalid'):
                    admission._verify_current_campaign_prerequisites(None, None, external_observation_bundle=None,
                        timing_plan_bytes=None, admission_inputs=None, implementation_version=version)
            self.assertEqual((graph.call_count, clock.call_count), (0, 0))

    def test_current_v5_entry_checks_both_clock_samples_and_uses_v5_only(self):
        # Clock-only unit: this stub is not presented as valid source/admission.
        proof = SimpleNamespace(expires_at_utc='2026-09-06T02:00:00Z')
        for ended, error in (('2026-09-06T01:00:01Z', None),
                             ('2026-09-06T00:59:59Z', 'clock_reversed'),
                             ('2026-09-06T02:00:00Z', 'expired_during_verification')):
            with self.subTest(ended=ended), patch.object(admission, '_current_utc', side_effect=['2026-09-06T01:00:00Z', ended]), \
                 patch.object(admission, 'verify_campaign_prerequisites_v5', return_value=proof) as v5, \
                 patch.object(admission, 'verify_campaign_prerequisites', side_effect=AssertionError('no v4 fallback')) as old:
                arguments = dict(external_observation_bundle=b'', timing_plan_bytes=b'', admission_inputs=None)
                if error is None:
                    self.assertIs(admission.verify_current_campaign_prerequisites_v5(None, None, **arguments), proof)
                else:
                    with self.assertRaisesRegex(ValueError, error):
                        admission.verify_current_campaign_prerequisites_v5(None, None, **arguments)
                self.assertEqual(v5.call_count, 1); self.assertEqual(old.call_count, 0)
                self.assertEqual(v5.call_args.kwargs['verification_time_utc'], '2026-09-06T01:00:00Z')

    def test_new_proof_cannot_be_directly_constructed(self):
        with self.assertRaises(TypeError): admission.VerifiedCampaignPrerequisitesV5()


if __name__ == '__main__': unittest.main()
