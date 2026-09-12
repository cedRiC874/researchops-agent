"""Local UTC wrapper branches only; the separate joined test proves the graph."""
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from researchops_completion_timing import campaign_admission as admission


class CampaignFreshnessTests(unittest.TestCase):
    def invoke(self):
        return admission.verify_current_campaign_prerequisites(Path('.'), object(), external_observation_bundle=b'{}',
            timing_plan_bytes=b'{}', admission_inputs=object())

    def test_current_entry_has_no_caller_clock_or_as_of_argument(self):
        self.assertEqual(list(inspect.signature(admission.verify_current_campaign_prerequisites).parameters),
            ['root', 'documents', 'external_observation_bundle', 'timing_plan_bytes', 'admission_inputs'])

    def test_time_is_sampled_around_verification_not_taken_from_historical_evidence(self):
        proof = SimpleNamespace(expires_at_utc='2026-09-06T02:00:00Z')
        with patch.object(admission, '_current_utc', side_effect=['2026-09-06T01:00:00Z', '2026-09-06T01:00:01Z']) as clock, \
             patch.object(admission, 'verify_campaign_prerequisites', return_value=proof) as lower:
            self.assertIs(self.invoke(), proof)
        self.assertEqual(clock.call_count, 2)
        self.assertEqual(lower.call_args.kwargs['verification_time_utc'], '2026-09-06T01:00:00Z')

    def test_expiry_during_verification_is_rejected(self):
        with patch.object(admission, '_current_utc', side_effect=['2026-09-06T01:00:00Z', '2026-09-06T02:00:00Z']), \
             patch.object(admission, 'verify_campaign_prerequisites', return_value=SimpleNamespace(expires_at_utc='2026-09-06T02:00:00Z')):
            with self.assertRaisesRegex(ValueError, 'expired_during_verification'): self.invoke()

    def test_clock_reversal_is_rejected(self):
        with patch.object(admission, '_current_utc', side_effect=['2026-09-06T01:00:01Z', '2026-09-06T01:00:00Z']), \
             patch.object(admission, 'verify_campaign_prerequisites', return_value=SimpleNamespace(expires_at_utc='2026-09-06T02:00:00Z')):
            with self.assertRaisesRegex(ValueError, 'clock_reversed'): self.invoke()

    def test_clock_failure_does_not_start_material_verification(self):
        with patch.object(admission, '_current_utc', side_effect=ValueError('clock unavailable')), \
             patch.object(admission, 'verify_campaign_prerequisites') as lower:
            with self.assertRaises(ValueError): self.invoke()
        lower.assert_not_called()


if __name__ == '__main__': unittest.main()
