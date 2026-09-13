"""Explicit v5 startup routing boundaries, with no real store or Provider calls."""
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from researchops_completion_timing import first_live_start as start
from researchops_completion_timing import first_live_identity_v5
from researchops_external_closure import execution_components_v4, execution_local_v4, execution_current_v4


class FirstLiveV5RoutingTests(unittest.TestCase):
    def test_static_routing_keeps_old_defaults_and_selects_all_v5_source_functions(self):
        old = start._source_functions(4); new = start._source_functions(5)
        self.assertIs(old[0], start.PROFILE_PATHS)
        self.assertIs(old[1], start.load_timed_implementation_contract)
        self.assertIs(new[0], execution_components_v4.PROFILE_PATHS)
        self.assertIs(new[1], first_live_identity_v5.load_timed_implementation_contract)
        self.assertIs(new[2], execution_local_v4.verify_local_timed_execution_identity)
        self.assertIs(new[3], execution_current_v4.verify_current_timed_profile)
        self.assertIn('plan_v11.json', new[0]['first_live'][1])

    def test_unknown_version_rejects_before_clock_or_store(self):
        with patch.object(start, '_StartBudget', side_effect=AssertionError('no clock')) as clock, \
             patch.object(start.local_claim, 'local_claim_store_status', side_effect=AssertionError('no store')) as store:
            for version in (True, 4.0, '5', None, 6):
                with self.subTest(version=version), self.assertRaisesRegex(start.FirstLiveStartError, 'version_invalid') as caught:
                    start._prepare_claimed_first_live_start_impl(None, plan_bytes=b'', authorization_bytes=b'',
                        expected_authorization_binding_sha256='f' * 64, _budget=None, _implementation_version=version)
                self.assertFalse(caught.exception.claim_consumed)
            self.assertEqual((clock.call_count, store.call_count), (0, 0))

    def test_v5_process_root_rejects_before_claim_preparation(self):
        with patch.object(start, '_prepare_claimed_first_live_start_impl', side_effect=AssertionError('no preparation')) as prepare:
            with self.assertRaisesRegex(start.FirstLiveStartError, 'process_root_mismatch') as caught:
                start._prepare_process_bound_first_live_start_v5(Path(__file__).resolve().parent,
                    plan_bytes=b'{}', authorization_bytes=b'{}', expected_authorization_binding_sha256='f' * 64)
            self.assertFalse(caught.exception.claim_consumed)
            self.assertEqual(prepare.call_count, 0)

    def test_v5_wrapper_selects_fixed_version_without_callback_or_source_fallback(self):
        with patch.object(start, '_prepare_claimed_first_live_start_impl', return_value=None) as prepare:
            start._prepare_claimed_first_live_start_v5(None, plan_bytes=b'', authorization_bytes=b'', expected_authorization_binding_sha256='f' * 64)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(prepare.call_args.kwargs['_implementation_version'], 5)
        self.assertIsNone(prepare.call_args.kwargs['_budget'])

    def test_v5_plan_change_after_claim_invalidates_owner_before_handoff(self):
        clock = SimpleNamespace(abort_new_work=unittest.mock.Mock())
        prepared = start._ClaimedFirstLiveStartV5(start._TOKEN, clock=clock, clock_domain_id='PCECLOCK-' + 'A' * 32,
            receipt_bytes=b'{}', intent_bytes=b'{}', source_identity=None, plan_bytes=b'original synthetic plan',
            budget=None, expires='2026-09-06T02:00:00Z')
        prepared.plan_bytes = b'changed synthetic plan'
        with self.assertRaisesRegex(start.FirstLiveStartError, 'plan_changed') as caught:
            prepared.summary()
        self.assertTrue(caught.exception.claim_consumed)
        self.assertTrue(prepared._taken)
        self.assertEqual(clock.abort_new_work.call_count, 1)
        prepared.abort()  # Cleanup must still work on the invalidated owner.
        prepared.plan_bytes = b'original synthetic plan'
        with self.assertRaisesRegex(start.FirstLiveStartError, 'already_taken'):
            prepared._take_for_runtime()


if __name__ == '__main__': unittest.main()
