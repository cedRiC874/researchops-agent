"""Campaign clock/ownership primitives; the process test covers actual admission."""
import copy
import pickle
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from researchops_completion_timing import campaign_start as start
from researchops_completion_timing.clock import _TimingClock
from researchops_completion_timing.contract import digest
from researchops_external_closure.primitives import canonical_json_bytes as raw


class CampaignValidityTests(unittest.TestCase):
    def test_expiry_counts_elapsed_preparation_even_if_utc_stalls(self):
        with patch.object(start.time, 'monotonic_ns', side_effect=[0, 110_000_000_000]), \
             patch.object(start, '_utc_now', return_value='2026-09-06T01:00:00Z'):
            validity = start._CampaignValidity(); validity.expires = '2026-09-06T01:01:00Z'
            with self.assertRaisesRegex(ValueError, 'authorization_expired'):
                validity.checkpoint()

    def test_campaign_does_not_inherit_first_live_330_second_budget(self):
        with patch.object(start.time, 'monotonic_ns', side_effect=[0, 400_000_000_000]), \
             patch.object(start, '_utc_now', side_effect=['2026-09-06T01:00:00Z', '2026-09-06T01:06:40Z']):
            validity = start._CampaignValidity(); validity.expires = '2026-09-06T02:00:00Z'
            _stamp, deadline = validity.checkpoint(reserve_ns=30_000_000_000)
            self.assertEqual(deadline, 3_570_000_000_000)

    def test_reversal_and_insufficient_sealing_reserve_reject(self):
        for times, stamps, reserve in (([5, 4], ['2026-09-06T01:00:00Z'] * 2, 0),
                ([0, 1], ['2026-09-06T01:00:01Z', '2026-09-06T01:00:00Z'], 0),
                ([0, 1], ['2026-09-06T01:00:00Z'] * 2, 60_000_000_000)):
            with self.subTest(times=times, reserve=reserve), patch.object(start.time, 'monotonic_ns', side_effect=times), \
                 patch.object(start, '_utc_now', side_effect=stamps):
                validity = start._CampaignValidity(); validity.expires = '2026-09-06T01:01:00Z'
                with self.assertRaises(ValueError): validity.checkpoint(reserve_ns=reserve)


class CampaignOwnershipTests(unittest.TestCase):
    def holder(self, *, envelope_bytes=b'{}'):
        validity = start._CampaignValidity(); validity.expires = '2099-01-01T00:00:00Z'
        return start._ClaimedCampaignStart(start._TOKEN,
            clock=_TimingClock(request_timeout_ns=120_000_000_000, phase_timeout_ns=600_000_000_000),
            validity=validity, proof=None, identity=None, environment=None, domain='fixture', request=b'{}', receipt=b'{}',
            plan_bytes=b'{}', envelope_bytes=envelope_bytes, sealing=30_000_000_000)

    def test_other_root_rejects_before_claim_or_material_checks(self):
        with patch.object(start, 'verify_campaign_prerequisites') as verifier, patch.object(start.local_claim, '_reserve_with_ownership') as claim:
            with self.assertRaisesRegex(start.CampaignStartError, 'process_root_mismatch') as caught:
                start._prepare_process_bound_campaign_start(start.ROOT.parent, object(), external_observation_bundle=b'{}',
                    timing_plan_bytes=b'{}', admission_inputs=object())
        verifier.assert_not_called(); claim.assert_not_called(); self.assertFalse(caught.exception.claim_consumed)

    def test_handoff_is_single_use_and_copy_pickle_are_forbidden(self):
        holder = self.holder()
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.assertRaises(TypeError): operation(holder)
        holder._take_for_runtime()
        with self.assertRaisesRegex(start.CampaignStartError, 'already_taken'): holder._take_for_runtime()
        self.assertTrue(holder.clock.snapshot()['halted'])

    def test_wrong_process_and_aborted_holder_cannot_transfer(self):
        holder = self.holder()
        with patch.object(start.os, 'getpid', return_value=-1):
            with self.assertRaisesRegex(start.CampaignStartError, 'wrong_process'): holder._take_for_runtime()
        holder.abort()
        with self.assertRaisesRegex(start.CampaignStartError, 'already_taken'): holder._take_for_runtime()

    def test_replacing_envelope_bytes_is_rejected_before_handoff(self):
        holder = self.holder(envelope_bytes=b'{"original":true}')
        self.assertEqual(holder._verified_envelope(), {'original': True})
        holder.envelope_bytes = b'{"replacement":true}'
        with self.assertRaisesRegex(start.CampaignStartError, 'envelope_changed'): holder._take_for_runtime()
        self.assertTrue(holder.clock.snapshot()['halted'])


@unittest.skipUnless(os.name == 'nt', 'temporary Windows claim store')
class CampaignPostClaimFailureTests(unittest.TestCase):
    def test_post_claim_source_failure_keeps_the_real_marker_consumed(self):
        # Isolate this failure branch only: expensive graph/source/environment
        # checks are stubs here, NOT evidence of admission. The separate child
        # test executes those checks for real. Claim IO remains real and local.
        from tests.test_completion_execution_scope import scoped_fixture, verify
        from tests.test_completion_timing_pre_execution import documents
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); known = root / 'known'; known.mkdir()
            with patch.object(start.local_claim, '_windows_local_app_data', return_value=known):
                environment = start.local_claim.provision_local_claim_store()['execution_environment_id']
                fixture, plan = scoped_fixture(environment); scope = verify(fixture, plan)
                proof = SimpleNamespace(scope=scope, expires_at_utc='2099-01-01T00:00:00Z')
                identity = SimpleNamespace()
                source_plan = raw({'component_hashes': {'dependency_lock_sha256': 'a' * 64, 'pyproject_sha256': 'b' * 64}})
                original = start.local_claim._reserve_with_ownership; captured = {}
                def reserve(request):
                    result = original(request)
                    captured.update(request=request, receipt=result._receipt)
                    return result
                with patch.object(start, 'ROOT', root), patch.object(start, '_current_file', return_value=source_plan), \
                     patch.object(start, 'verify_process_environment', return_value={}), \
                     patch.object(start, 'verify_campaign_prerequisites', return_value=proof), \
                     patch.object(start, 'verify_local_timed_execution_identity', return_value=identity), \
                     patch.object(start.local_claim, '_reserve_with_ownership', side_effect=reserve), \
                     patch.object(start, 'verify_current_timed_profile', side_effect=ValueError('campaign_start_post_claim_source_changed')):
                    with self.assertRaisesRegex(start.CampaignStartError, 'post_claim_source_changed') as caught:
                        start._prepare_process_bound_campaign_start(root, documents(fixture), external_observation_bundle=raw(fixture.observation),
                            timing_plan_bytes=raw(plan), admission_inputs=object())
                self.assertTrue(caught.exception.claim_consumed)
                self.assertEqual(start.local_claim.read_reserved_local_claim(captured['request'],
                    expected_receipt_sha256=digest(captured['receipt'])), captured['receipt'])
                with self.assertRaisesRegex(start.local_claim.LocalClaimError, 'already_exists'):
                    original(captured['request'])


if __name__ == '__main__': unittest.main()
